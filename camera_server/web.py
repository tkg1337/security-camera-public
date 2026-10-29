"""Web 画面（Flask）。ホーム / 録画一覧 / 設定 と、その API・動画配信・PWA 用ファイル。"""

from __future__ import annotations

import base64
import io
import ipaddress
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from flask import (Flask, Response, abort, jsonify, redirect, render_template, request, send_file,
                   send_from_directory, session, url_for)

from .config import Config
from .constants import CATEGORIES
from .notify import Notifier
from .state import RuntimeSettings

PACKAGE_DIR = Path(__file__).resolve().parent


def create_app(cfg: Config, state: RuntimeSettings, notifier: Notifier) -> Flask:
    app = Flask(__name__,
                template_folder=str(PACKAGE_DIR / "templates"),
                static_folder=str(PACKAGE_DIR / "static"),
                static_url_path="/static")
    # secret_key が空なら起動ごとにランダム（再起動するとログインが切れる）
    app.secret_key = cfg.server.secret_key or os.urandom(32)

    auth_enabled = cfg.auth.enabled
    recordings_dir = cfg.recordings_dir
    hls_dir = cfg.hls_dir
    static_dir = PACKAGE_DIR / "static"

    def is_admin():
        """認証オフなら常に管理者扱い。認証オンならログイン済みかどうか。"""
        if not auth_enabled:
            return True
        return bool(session.get("is_admin"))

    @app.before_request
    def restrict_to_local_network():
        """認証オフの間はプライベートIP（LAN内・このPC）以外からのアクセスを拒否する。"""
        if auth_enabled or not cfg.server.local_network_only:
            return None
        try:
            ip = ipaddress.ip_address(request.remote_addr or "")
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
        except ValueError:
            abort(403)
        if ip.is_private or ip.is_loopback:
            return None
        return Response("認証オフのテストモード中は LAN 内からのみアクセスできます。", status=403,
                        mimetype="text/plain; charset=utf-8")

    @app.context_processor
    def inject_globals():
        return {
            "auth_enabled": auth_enabled,
            "is_admin": is_admin(),
            "hls_enabled": cfg.live.hls_enabled,
            "frame_w": state.frame_w,
            "frame_h": state.frame_h,
        }

    # =================================================================
    # ページ
    # =================================================================

    @app.route("/")
    def index():
        return render_template("index.html", vapid_public_key=cfg.notifications.vapid_public_key)

    @app.route("/records")
    def records():
        date_str = request.args.get("date")
        category = request.args.get("category", "all")
        try:
            date_key = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y%m%d")
        except (TypeError, ValueError):
            date_key = datetime.now().strftime("%Y%m%d")
            date_str = datetime.now().strftime("%Y-%m-%d")

        videos = []
        if is_admin():
            for cat in CATEGORIES:
                if category != "all" and cat != category:
                    continue
                dir_path = recordings_dir / cat
                if not dir_path.is_dir():
                    continue
                for fname in os.listdir(dir_path):
                    # 通知用サムネイル (notification_thumb_*.jpg) とサブカメラ録画 (_cam2) は一覧に出さない
                    if (fname.endswith(".mp4") and fname.startswith(f"record_{date_key}")
                            and "_cam2" not in fname):
                        videos.append({"filename": fname, "thumbnail": fname.replace(".mp4", ".jpg"),
                                       "category": cat, "type": "video"})
            videos.sort(key=lambda v: v["filename"], reverse=True)

        return render_template("record.html", videos=videos, date_str=date_str, category=category)

    @app.route("/settings")
    def settings():
        return render_template("settings.html")

    @app.route("/logout")
    def logout():
        session.pop("is_admin", None)
        session.pop("logged_in_user", None)
        return redirect(url_for("index"))

    # =================================================================
    # API
    # =================================================================

    @app.route("/api/sensitivity_settings", methods=["GET", "POST"])
    def sensitivity_settings():
        """検知感度設定の取得・更新。"""
        if request.method == "GET":
            return jsonify(state.get_sensitivity())
        if not is_admin():
            return jsonify({"error": "Unauthorized"}), 403
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "invalid json"}), 400
        try:
            state.update_sensitivity(data)
        except (ValueError, TypeError) as e:
            return jsonify({"error": f"invalid value: {e}"}), 400
        except OSError as e:
            return jsonify({"error": f"保存に失敗しました: {e}"}), 500
        return jsonify({"status": "ok"})

    @app.route("/api/roi_settings", methods=["GET", "POST"])
    def api_roi_settings():
        if not is_admin():
            return jsonify({"error": "Unauthorized"}), 403
        if request.method == "GET":
            return jsonify(state.get_roi())

        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"status": "error", "message": "Invalid JSON or missing Content-Type header"}), 400
        try:
            snapshot = state.update_roi(data)
            return jsonify({"status": "success", "message": "ROI settings saved successfully", "settings": snapshot})
        except (ValueError, TypeError) as e:
            return jsonify({"status": "error", "message": f"Invalid input for ROI coordinates: {e}"}), 400
        except OSError as e:
            return jsonify({"status": "error", "message": f"Failed to save ROI settings: {e}"}), 500

    @app.route("/delete_record/<category>/<filename>", methods=["POST"])
    def delete_record(category, filename):
        if not is_admin():
            return "Unauthorized", 403
        if category not in CATEGORIES or os.path.basename(filename) != filename:
            abort(404)

        target = recordings_dir / category / filename
        try:
            if filename.endswith(".mp4"):
                base = filename[:-4]
                for path in (target, target.with_suffix(".jpg"), recordings_dir / category / f"{base}_cam2.mp4"):
                    if path.exists():
                        path.unlink()
                        print(f"削除: {path}")
            elif filename.startswith("notification_thumb_") and target.exists():
                target.unlink()
                print(f"削除: {target}")
            return redirect(url_for("records"))
        except OSError as e:
            print(f"削除エラー: {e}")
            return f"削除エラー: {e}", 500

    @app.route("/subscribe", methods=["POST"])
    def subscribe():
        if not is_admin():
            return jsonify({"error": "Unauthorized"}), 403
        sub_info = request.get_json(silent=True)
        if not sub_info:
            return jsonify({"error": "No subscription info"}), 400
        try:
            cfg.subscription_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cfg.subscription_file, "w", encoding="utf-8") as f:
                json.dump(sub_info, f)
            print("通知購読情報を保存しました。")
            return ("", 204)
        except OSError as e:
            return jsonify({"error": f"Failed to save subscription: {e}"}), 500

    @app.route("/receive_detection_event", methods=["POST"])
    def receive_detection_event():
        """外部から検出イベントを受け取る口（内部の検出スレッドは Notifier を直接呼ぶ）。"""
        if not is_admin():
            return jsonify({"error": "Unauthorized"}), 403
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"status": "error", "message": "Invalid JSON or missing Content-Type header"}), 400
        thumb = None
        if data.get("thumbnail_base64"):
            try:
                thumb = base64.b64decode(data["thumbnail_base64"])
            except (ValueError, TypeError):
                thumb = None
        result, status = notifier.handle_detection_event(
            data.get("detected_type"), data.get("x"), data.get("y"),
            data.get("width"), data.get("height"), thumb)
        return jsonify(result), status

    @app.route("/api/live_snapshot")
    def live_snapshot():
        """最新の HLS セグメントから静止画を1枚取り出して返す。"""
        if not is_admin():
            return "Unauthorized", 403
        fw, fh = state.frame_w, state.frame_h
        expected = fw * fh * 3
        for attempt in range(1, 101):
            try:
                ts_files = [f for f in os.listdir(hls_dir) if f.endswith(".ts")]
            except OSError:
                ts_files = []
            ts_files.sort(key=lambda f: os.path.getmtime(hls_dir / f), reverse=True)
            if not ts_files:
                time.sleep(0.5)
                continue
            latest = hls_dir / ts_files[0]
            if latest.stat().st_size < 1024:   # 書き込み途中
                time.sleep(0.5)
                continue
            try:
                proc = subprocess.Popen(
                    [cfg.recording.ffmpeg_path, "-i", str(latest), "-vframes", "1", "-f", "image2pipe",
                     "-pix_fmt", "rgb24", "-s", f"{fw}x{fh}", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                raw = proc.stdout.read(expected)
                proc.wait()
                if proc.returncode == 0 and len(raw) == expected:
                    frame = np.frombuffer(raw, dtype=np.uint8).reshape((fh, fw, 3))
                    ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                    if not ok:
                        return "Failed to encode image", 500
                    return send_file(io.BytesIO(jpg.tobytes()), mimetype="image/jpeg",
                                     as_attachment=False, download_name="snapshot.jpg")
            except Exception as e:  # noqa: BLE001
                print(f"スナップショット取得エラー (試行 {attempt}): {e}")
            time.sleep(0.5)
        return "Failed to capture live snapshot after multiple attempts", 500

    # =================================================================
    # 動画・サムネイル・HLS 配信（フォルダ外のファイルは読めないよう send_from_directory を使用）
    # =================================================================

    @app.route("/videos/<category>/<filename>")
    def serve_video(category, filename):
        if not is_admin():
            return "Unauthorized", 403
        if category not in CATEGORIES:
            abort(404)
        return send_from_directory(recordings_dir / category, filename, mimetype="video/mp4")

    @app.route("/thumbnails/<category>/<filename>")
    def serve_thumbnail(category, filename):
        # 通知の画像はログインしていない端末にも表示されるので、サムネイルだけは認証なしで出す
        if category not in CATEGORIES or not filename.lower().endswith(".jpg"):
            abort(404)
        return send_from_directory(recordings_dir / category, filename, mimetype="image/jpeg")

    @app.route("/hls/<path:filename>")
    def serve_hls(filename):
        if not is_admin():
            return "Unauthorized", 403
        if filename.endswith(".m3u8"):
            return send_from_directory(hls_dir, filename, mimetype="application/vnd.apple.mpegurl", max_age=0)
        if filename.endswith(".ts"):
            return send_from_directory(hls_dir, filename, mimetype="video/mp2t")
        abort(404)

    # =================================================================
    # PWA 用ファイル（manifest.json / sw.js）
    # =================================================================

    @app.route("/manifest.json")
    def manifest():
        return jsonify({
            "name": "Camera Live Viewer",
            "short_name": "Camera",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#000000",
            "theme_color": "#000000",
            "scope": "/",
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        })

    @app.route("/sw.js")
    def service_worker():
        # Service Worker はサイト全体を対象にするため、/static/ ではなくルートから配信する
        return send_from_directory(static_dir, "sw.js", mimetype="application/javascript", max_age=0)

    if auth_enabled:
        from .auth import register_passkey_routes
        register_passkey_routes(app, cfg)

    return app
