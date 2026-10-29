"""高画質録画とループ録画。

メインストリームを再エンコードせず短いセグメント（.ts）に切って一時フォルダに溜めておき、
検出したら「検出の前後」に当たるセグメントをつなげて mp4 にする。
録画フォルダが上限を超えたら古いものから消す（ループ録画）。
"""

from __future__ import annotations

import atexit
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import CameraConfig, Config
from .constants import CATEGORIES, FINALIZE_WAIT, SEGMENT_MARGIN, SEGMENT_RE


# =====================================================================
# 子プロセス（ffmpeg）の管理 — 終了時に残らないようにする
# =====================================================================

class ProcessRegistry:
    def __init__(self):
        self._children: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        atexit.register(self.stop_all)

    def start(self, cmd, **kwargs) -> subprocess.Popen:
        proc = subprocess.Popen(cmd, **kwargs)
        with self._lock:
            self._children.add(proc)
        return proc

    def forget(self, proc):
        with self._lock:
            self._children.discard(proc)

    def stop(self, proc, timeout=5):
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        except Exception as e:  # noqa: BLE001
            print(f"ffmpeg 終了中のエラー: {e}")
        self.forget(proc)

    def stop_all(self):
        with self._lock:
            procs = list(self._children)
        for p in procs:
            self.stop(p, timeout=2)


# =====================================================================
# 録画
# =====================================================================

def _ffmpeg_path(p: Path) -> str:
    """ffmpeg の出力パターン用に区切り文字を / にそろえる（Windows 対策）。"""
    return str(p).replace("\\", "/")


class Recorder:
    def __init__(self, cfg: Config, procs: ProcessRegistry):
        self.cfg = cfg
        self.procs = procs
        self.ffmpeg = cfg.recording.ffmpeg_path
        self.seg_dir_main = cfg.segments_dir_main
        self.seg_dir_sub = cfg.segments_dir_sub
        self._holds: dict[int, float] = {}       # 録画中のセグメント保護: id -> 保護開始時刻
        self._holds_lock = threading.Lock()
        self._hold_seq = 0

    @property
    def sub_enabled(self) -> bool:
        return self.cfg.cameras.sub.active

    def _segment_dirs(self) -> list[Path]:
        return [self.seg_dir_main] + ([self.seg_dir_sub] if self.sub_enabled else [])

    # ---------------- 準備 ----------------
    def prepare_dirs(self):
        for cat in CATEGORIES:
            (self.cfg.recordings_dir / cat).mkdir(parents=True, exist_ok=True)
        for d in self._segment_dirs():
            d.mkdir(parents=True, exist_ok=True)
        if self.cfg.live.hls_enabled:
            self.cfg.hls_dir.mkdir(parents=True, exist_ok=True)

    def clear_segments(self):
        for seg_dir in self._segment_dirs():
            seg_dir.mkdir(parents=True, exist_ok=True)
            for name in os.listdir(seg_dir):
                if SEGMENT_RE.fullmatch(name) or name.startswith("concat_"):
                    try:
                        os.remove(seg_dir / name)
                    except OSError:
                        pass

    # ---------------- ffmpeg コマンド ----------------
    def _input_args(self, camera: CameraConfig) -> list[str]:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error"]
        if self.cfg.recording.ffmpeg_hwaccel:
            cmd += ["-hwaccel", self.cfg.recording.ffmpeg_hwaccel]
        cmd += ["-rtsp_transport", "tcp", "-i", camera.full_url()]
        return cmd

    def _segment_args(self, seg_dir: Path) -> list[str]:
        pattern = _ffmpeg_path(seg_dir / "seg_%Y%m%d_%H%M%S.ts")
        return ["-map", "0:v:0", "-c:v", "copy", "-an",
                "-f", "segment", "-segment_time", str(self.cfg.recording.segment_seconds),
                "-segment_format", "mpegts", "-reset_timestamps", "1", "-strftime", "1", pattern]

    def build_main_camera_cmd(self, with_detection: bool) -> list[str]:
        """メインカメラ: セグメント録画 + (HLS ライブ配信) + (検知用の縮小映像を標準出力へ)"""
        d = self.cfg.detection
        cmd = self._input_args(self.cfg.cameras.main) + self._segment_args(self.seg_dir_main)
        if self.cfg.live.hls_enabled:
            cmd += ["-map", "0:v:0", "-c:v", "copy", "-an", "-f", "hls",
                    "-hls_time", "2", "-hls_list_size", "5", "-hls_flags", "delete_segments",
                    _ffmpeg_path(self.cfg.hls_dir / "live.m3u8")]
        if with_detection:
            cmd += ["-map", "0:v:0", "-an",
                    "-vf", f"fps={d.fps},scale={d.width}:{d.height}",
                    "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
        return cmd

    def build_sub_camera_cmd(self) -> list[str]:
        """サブカメラ: セグメント録画のみ"""
        return self._input_args(self.cfg.cameras.sub) + self._segment_args(self.seg_dir_sub)

    # ---------------- セグメント管理 ----------------
    @staticmethod
    def list_segments(seg_dir: Path) -> list[tuple[float, Path]]:
        """(開始時刻, パス) を古い順に返す。"""
        out = []
        try:
            names = os.listdir(seg_dir)
        except OSError:
            return out
        for name in names:
            m = SEGMENT_RE.fullmatch(name)
            if m:
                start = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").timestamp()
                out.append((start, seg_dir / name))
        out.sort()
        return out

    def add_hold(self, since: float) -> int:
        with self._holds_lock:
            self._hold_seq += 1
            self._holds[self._hold_seq] = since
            return self._hold_seq

    def release_hold(self, hold_id: int):
        with self._holds_lock:
            self._holds.pop(hold_id, None)

    def segment_cleanup_loop(self):
        """古いセグメントを消す。録画中のぶんは消さない。"""
        keep = self.cfg.recording.segment_keep_seconds
        while True:
            time.sleep(10)
            cutoff = time.time() - keep
            with self._holds_lock:
                if self._holds:
                    cutoff = min(cutoff, min(self._holds.values()))
            for seg_dir in self._segment_dirs():
                for start, path in self.list_segments(seg_dir):
                    if start < cutoff - 10:       # 10秒は「セグメントが想定より長い」ときの余裕
                        try:
                            os.remove(path)
                        except OSError:
                            pass

    # ---------------- mp4 の作成 ----------------
    def _mux_to_mp4(self, chosen, out_dir: Path, filename: str, seg_dir: Path, make_thumbnail: bool):
        """セグメントを mp4 にまとめる。make_thumbnail=True のとき .jpg も作る。"""
        final_path = out_dir / filename
        part_path = out_dir / (filename + ".part")
        list_path = seg_dir / f"concat_{filename}.txt"

        with open(list_path, "w", encoding="utf-8") as f:
            for _, path in chosen:
                f.write("file '{}'\n".format(_ffmpeg_path(path).replace("'", "'\\''")))

        r = subprocess.run(
            [self.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
             "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(part_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
        try:
            os.remove(list_path)
        except OSError:
            pass
        if r.returncode != 0 or not part_path.exists():
            print(f"⚠ mp4 の作成に失敗しました ({filename}):", r.stderr.decode("utf-8", "replace")[-300:])
            return
        os.replace(part_path, final_path)

        if make_thumbnail:
            subprocess.run(
                [self.ffmpeg, "-y", "-i", str(final_path), "-frames:v", "1",
                 "-vf", f"scale={self.cfg.recording.thumbnail_width}:-2", "-q:v", "3",
                 str(final_path.with_suffix(".jpg"))],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)

        size_mb = final_path.stat().st_size / 1024 / 1024
        print(f"💾 録画を保存しました: {final_path} ({size_mb:.1f} MB, セグメント {len(chosen)} 個)")

    @staticmethod
    def _choose(segments, lo, hi):
        """lo〜hi の時間帯にかかるセグメントを選ぶ（lo より前に始まった1個も含める）。"""
        before = [s for s in segments if s[0] <= lo]
        return ([before[-1]] if before else []) + [s for s in segments if lo < s[0] <= hi]

    def finalize_recording(self, hold_id, t_start, t_end, category, stamp):
        """検出の前後のセグメントを両カメラ分つなげて mp4 を作る（別スレッドで実行）。"""
        try:
            time.sleep(FINALIZE_WAIT)
            lo = t_start - self.cfg.recording.buffer_before - SEGMENT_MARGIN
            hi = t_end + SEGMENT_MARGIN

            out_dir = self.cfg.recordings_dir / category
            out_dir.mkdir(parents=True, exist_ok=True)

            chosen = self._choose(self.list_segments(self.seg_dir_main), lo, hi)
            if chosen:
                self._mux_to_mp4(chosen, out_dir, f"record_{stamp}.mp4", self.seg_dir_main, True)
            else:
                print(f"⚠ メインカメラ: 録画に使えるセグメントがありませんでした ({stamp})")

            if self.sub_enabled:
                chosen = self._choose(self.list_segments(self.seg_dir_sub), lo, hi)
                if chosen:
                    self._mux_to_mp4(chosen, out_dir, f"record_{stamp}_cam2.mp4", self.seg_dir_sub, False)
                else:
                    print(f"⚠ サブカメラ: 録画に使えるセグメントがありませんでした ({stamp})")
        except Exception as e:  # noqa: BLE001
            print(f"⚠ 録画の仕上げ中にエラー: {e}")
        finally:
            self.release_hold(hold_id)

    def finalize_async(self, *args):
        threading.Thread(target=self.finalize_recording, args=args, daemon=True).start()

    # ---------------- サブカメラ ----------------
    def sub_camera_loop(self):
        """サブカメラのセグメント録画を維持するスレッド（検知なし・録画専用）。"""
        self.seg_dir_sub.mkdir(parents=True, exist_ok=True)
        log_path = self.seg_dir_sub / "ffmpeg_sub.log"
        while True:
            print(f"📡 サブカメラ接続中... ({self.cfg.cameras.sub.masked_url()})")
            try:
                with open(log_path, "ab") as logf:
                    proc = self.procs.start(self.build_sub_camera_cmd(), stdout=subprocess.DEVNULL, stderr=logf)
            except FileNotFoundError:
                print(f"⚠ ffmpeg が見つかりません（{self.ffmpeg}）。サブカメラは停止します。")
                return
            proc.wait()
            self.procs.forget(proc)
            print(f"⚠ サブカメラが切断されました（ログ: {log_path}）。3秒後に再接続します...")
            time.sleep(3)

    # ---------------- ループ録画 ----------------
    def total_recording_size(self) -> int:
        """録画フォルダ以下の全ファイルのサイズ合計（バイト）。"""
        total = 0
        for cat in CATEGORIES:
            dir_path = self.cfg.recordings_dir / cat
            if not dir_path.is_dir():
                continue
            for entry in os.scandir(dir_path):
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
        return total

    def loop_recording_cleanup_loop(self):
        """録画フォルダが上限を超えたら古いファイルから削除する（上限の 90% まで）。"""
        limit = self.cfg.max_recording_bytes
        while True:
            time.sleep(60)
            total = self.total_recording_size()
            if total <= limit:
                continue

            records = []
            for cat in CATEGORIES:
                dir_path = self.cfg.recordings_dir / cat
                if not dir_path.is_dir():
                    continue
                for fname in os.listdir(dir_path):
                    if fname.endswith(".mp4") and fname.startswith("record_") and "_cam2" not in fname:
                        records.append((fname, cat))
            records.sort(key=lambda x: x[0])   # ファイル名＝日時なので古い順

            for fname, cat in records:
                if total <= limit * 0.9:
                    break
                base = fname[:-4]
                for suffix in (".mp4", ".jpg", "_cam2.mp4"):
                    fpath = self.cfg.recordings_dir / cat / (base + suffix)
                    if fpath.exists():
                        try:
                            size = fpath.stat().st_size
                            fpath.unlink()
                            total -= size
                            print(f"🗑 ループ録画削除: {base}{suffix}")
                        except OSError:
                            pass
