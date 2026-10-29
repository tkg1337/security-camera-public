"""起動処理とコマンドライン。

    python run.py                    サーバーを起動
    python run.py --check-config     設定を読み込んで内容を表示（パスワードは伏せ字）
    python run.py --test-cameras     RTSP カメラに接続できるか確認
    python run.py -c 別の設定.yaml   設定ファイルを指定して起動
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import threading
import unicodedata

from . import __version__
from .config import Config, ConfigError, load_config


def _configure_console():
    # Windows のコンソールでも日本語・絵文字のログで落ちないようにする
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def guess_lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))   # 実際にはパケットは送られない
            return s.getsockname()[0]
    except OSError:
        return None


def print_config(cfg: Config):
    def onoff(v):
        return "オン" if v else "オフ"

    cams = cfg.cameras
    rows = [
        ("設定ファイル", cfg.source),
        ("メインカメラ", cams.main.masked_url() if cams.main.active else "（未設定）"),
        ("サブカメラ", cams.sub.masked_url() if cams.sub.active else "（使わない）"),
        ("待ち受け", f"{'https' if cfg.server.https else 'http'}://{cfg.server.host}:{cfg.server.port}/"),
        ("パスキー認証", onoff(cfg.auth.enabled)),
        ("LAN内のみ許可", onoff(cfg.server.local_network_only) + ("（認証オフのときだけ有効）" if not cfg.auth.enabled else "（認証オンなので無効）")),
        ("検知", f"{onoff(cfg.detection.enabled)}（モデル {cfg.detection.model}, {cfg.detection.device}, {cfg.detection.fps}fps）"),
        ("ライブ配信(HLS)", onoff(cfg.live.hls_enabled)),
        ("プッシュ通知", onoff(cfg.notifications.enabled)),
        ("録画の保存先", f"{cfg.recordings_dir}（上限 {cfg.recording.max_gb:g} GB）"),
        ("データ保存先", cfg.data_dir),
        ("一時セグメント", cfg.segments_root),
    ]
    def disp_width(s):  # 全角を2文字分として数える
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)

    width = max(disp_width(k) for k, _ in rows)
    print("=" * 70)
    for k, v in rows:
        print(f" {k}{' ' * (width - disp_width(k))} : {v}")
    for w in cfg.warnings():
        print(f" ⚠ {w}")
    print("=" * 70)


def test_cameras(cfg: Config) -> int:
    """各カメラから1コマ取得できるか ffmpeg で確認し、data/camera_test_*.jpg に保存する。"""
    ffmpeg = cfg.recording.ffmpeg_path
    if not shutil.which(ffmpeg):
        print(f"✗ ffmpeg が見つかりません（{ffmpeg}）。インストールして PATH を通すか、recording.ffmpeg_path を設定してください。")
        return 1
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name in ("main", "sub"):
        cam = getattr(cfg.cameras, name)
        if not cam.active:
            print(f"- {name}: 未設定のためスキップ")
            continue
        out = cfg.data_dir / f"camera_test_{name}.jpg"
        print(f"- {name}: {cam.masked_url()} に接続中...")
        try:
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-rtsp_transport", "tcp",
                 "-i", cam.full_url(), "-frames:v", "1", str(out)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20)
            ok = r.returncode == 0 and out.exists()
            err = r.stderr.decode("utf-8", "replace").strip()
        except subprocess.TimeoutExpired:
            ok, err = False, "20秒以内に応答がありませんでした（IPアドレス・ポート・同じネットワークにいるかを確認）"
        if ok:
            print(f"  ✓ 接続OK → {out}")
        else:
            failed += 1
            # エラー文にパスワード入りの URL が含まれることがあるので伏せる
            err = err.replace(cam.full_url(), cam.masked_url())
            if cam.password:
                err = err.replace(cam.password, "****")
            print(f"  ✗ 接続できませんでした: {err[-400:] or '原因不明'}")
    return 1 if failed else 0


def main(argv=None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(prog="camera_server", description="防犯カメラシステム")
    parser.add_argument("-c", "--config", help="設定ファイルのパス（既定: プロジェクト直下の config.yaml）")
    parser.add_argument("--check-config", action="store_true", help="設定を読み込んで内容を表示して終了")
    parser.add_argument("--test-cameras", action="store_true", help="RTSP カメラに接続できるか確認して終了")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"✗ 設定エラー: {e}")
        return 2

    if args.check_config:
        print_config(cfg)
        return 0
    if args.test_cameras:
        return test_cameras(cfg)
    return run_server(cfg)


def run_server(cfg: Config) -> int:
    # 重いライブラリ（cv2 など）はここで読み込む（--check-config を軽く保つため）
    from .detector import Detector
    from .notify import Notifier
    from .recorder import ProcessRegistry, Recorder
    from .state import RuntimeSettings
    from .web import create_app

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    state = RuntimeSettings(cfg)
    notifier = Notifier(cfg, state)
    app = create_app(cfg, state, notifier)

    if cfg.server.https and not (cfg.ssl_cert.exists() and cfg.ssl_key.exists()):
        print(f"✗ HTTPS 用の証明書が見つかりません: {cfg.ssl_cert} / {cfg.ssl_key}")
        return 1

    procs = ProcessRegistry()
    recorder = Recorder(cfg, procs)
    if cfg.cameras.main.active and (cfg.detection.enabled or cfg.live.hls_enabled):
        if not shutil.which(cfg.recording.ffmpeg_path):
            print(f"⚠ ffmpeg が見つかりません（{cfg.recording.ffmpeg_path}）。録画・ライブ配信は動きません。")
        recorder.prepare_dirs()
        recorder.clear_segments()
        detector = Detector(cfg, state, recorder, notifier, procs)
        threading.Thread(target=recorder.segment_cleanup_loop, name="segment-cleanup", daemon=True).start()
        threading.Thread(target=detector.run, name="pipeline-main", daemon=True).start()
        if cfg.cameras.sub.active:
            threading.Thread(target=recorder.sub_camera_loop, name="pipeline-sub", daemon=True).start()
        threading.Thread(target=recorder.loop_recording_cleanup_loop, name="loop-rec-cleanup", daemon=True).start()

    print_config(cfg)
    scheme = "https" if cfg.server.https else "http"
    print(f" このPCから   : {scheme}://localhost:{cfg.server.port}/")
    lan_ip = guess_lan_ip()
    if lan_ip:
        print(f" 同じWi-Fiから: {scheme}://{lan_ip}:{cfg.server.port}/")
    print(f" 初期ROI: {state.get_roi()}")
    print("=" * 70)

    ssl_context = (str(cfg.ssl_cert), str(cfg.ssl_key)) if cfg.server.https else None
    try:
        app.run(host=cfg.server.host, port=cfg.server.port, ssl_context=ssl_context,
                threaded=True, debug=False, use_reloader=False)
    finally:
        procs.stop_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
