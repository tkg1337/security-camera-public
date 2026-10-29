"""検出イベント → 通知用サムネイル保存 ＋ Web Push 通知。"""

from __future__ import annotations

import json
import threading
from datetime import datetime

import cv2

from .config import Config
from .constants import CATEGORIES
from .state import RuntimeSettings, in_roi

try:
    from pywebpush import WebPushException, webpush
except ImportError:  # 通知を使わないなら無くても起動できるようにする
    webpush = None

    class WebPushException(Exception):
        pass


class Notifier:
    def __init__(self, cfg: Config, state: RuntimeSettings):
        self.cfg = cfg
        self.state = state

    def handle_detection_event(self, detected_type, x, y, w, h, thumbnail_bytes=None):
        """検出イベントを処理する。戻り値は (レスポンス用dict, HTTPステータス)。"""
        if detected_type not in CATEGORIES:
            return {"status": "error", "message": f"不明な検出タイプ: {detected_type}"}, 400
        if any(v is None for v in (x, y, w, h)):
            print("検出イベント: 座標情報が不完全なのでスキップします。")
            return {"status": "error", "message": "座標情報不完全"}, 400
        if not in_roi(x, y, w, h, self.state.get_roi()):
            print(f"検出イベント: ({x},{y},{w},{h}) は検出範囲外なのでスキップします。")
            return {"status": "skipped", "message": "関心領域外"}, 200

        # サムネイル保存（ファイル名 notification_thumb_*.jpg は録画一覧には出ない）
        thumbnail_url = None
        if thumbnail_bytes:
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                save_dir = self.cfg.recordings_dir / detected_type
                save_dir.mkdir(parents=True, exist_ok=True)
                fname = f"notification_thumb_{ts}.jpg"
                (save_dir / fname).write_bytes(thumbnail_bytes)
                thumbnail_url = f"{self.cfg.server.public_base_url.rstrip('/')}/thumbnails/{detected_type}/{fname}"
                print(f"通知用サムネイルを保存しました: {fname}")
            except OSError as e:
                print(f"サムネイル保存エラー: {e}")

        if detected_type == "person":
            title, body = "人物検出", f"人を検出しました ({x},{y})"
        else:
            title, body = "車両検出", f"車両を検出しました ({x},{y})"
        payload = {"title": title, "body": body, "timestamp": datetime.now().strftime("%H:%M:%S")}
        if thumbnail_url:
            payload["image"] = thumbnail_url

        n = self.cfg.notifications
        if not n.enabled:
            return {"status": "skipped", "message": "通知は未設定（VAPID 鍵なし）", "thumbnail_url": thumbnail_url}, 200
        if not self.cfg.subscription_file.exists():
            print("通知の購読情報（subscription.json）が無いので通知は送りません。")
            return {"status": "error", "message": "通知購読情報なし", "thumbnail_url": thumbnail_url}, 404
        if webpush is None:
            print("pywebpush が入っていないので通知は送りません。")
            return {"status": "error", "message": "pywebpush未インストール", "thumbnail_url": thumbnail_url}, 500
        try:
            with open(self.cfg.subscription_file, "r", encoding="utf-8") as f:
                sub_info = json.load(f)
            # vapid_claims は pywebpush が exp を書き込むので毎回新しい dict を渡す（使い回すと12時間後に期限切れになる）
            webpush(subscription_info=sub_info, data=json.dumps(payload),
                    vapid_private_key=n.vapid_private_key, vapid_claims={"sub": n.vapid_subject})
            print("🔔 プッシュ通知送信成功")
            return {"status": "success", "message": "検出イベントを受信し、通知を送信しました",
                    "thumbnail_url": thumbnail_url}, 200
        except WebPushException as e:
            print(f"WebPush通知エラー: {e}")
            return {"status": "error", "message": f"WebPush失敗: {e}"}, 500
        except Exception as e:  # noqa: BLE001 - 通知の失敗で検出処理を止めない
            print(f"通知送信中の予期せぬエラー: {e}")
            return {"status": "error", "message": f"通知サーバーエラー: {e}"}, 500

    def notify_async(self, detected_type, x, y, w, h, frame):
        """通知用サムネイルを作って、別スレッドで通知処理（録画を止めないため）。"""
        n = self.cfg.notifications
        new_h = int(n.thumbnail_width * frame.shape[0] / frame.shape[1])
        small = cv2.resize(frame, (n.thumbnail_width, new_h), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), n.thumbnail_quality])
        thumb = buf.tobytes() if ok else None
        threading.Thread(target=self.handle_detection_event, args=(detected_type, x, y, w, h, thumb),
                         daemon=True).start()
