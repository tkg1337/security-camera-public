"""メインカメラの映像で YOLO を動かし、動いている人・車を見つけたら録画と通知を行う。"""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from datetime import datetime

import cv2
import numpy as np

from .config import Config
from .constants import CAR_CLASSES, CATEGORIES, PERSON_CLASSES, SEGMENT_MARGIN
from .notify import Notifier
from .recorder import ProcessRegistry, Recorder
from .state import RuntimeSettings, in_roi


class FrameGrabber(threading.Thread):
    """ffmpeg のパイプを読み続け、最新の1コマだけ持つ。
    YOLO が遅れても古いコマは捨てるので、パイプが詰まって録画やライブまで止まることがない。"""

    def __init__(self, proc, width, height):
        super().__init__(name="frame-grabber", daemon=True)
        self.proc = proc
        self.width, self.height = width, height
        self.cv = threading.Condition()
        self.frame = None
        self.seq = 0
        self.alive = True

    def run(self):
        size = self.width * self.height * 3
        try:
            while True:
                raw = self.proc.stdout.read(size)
                if raw is None or len(raw) != size:
                    break
                frame = np.frombuffer(raw, dtype=np.uint8).reshape((self.height, self.width, 3))
                with self.cv:
                    self.frame = frame
                    self.seq += 1
                    self.cv.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self.cv:
                self.alive = False
                self.cv.notify_all()

    def get(self, last_seq, timeout=20.0):
        """last_seq より新しいコマを待って (frame, seq) を返す。切断・タイムアウトなら None。"""
        end = time.time() + timeout
        with self.cv:
            while self.seq == last_seq:
                if not self.alive:
                    return None
                remaining = end - time.time()
                if remaining <= 0:
                    return None
                self.cv.wait(remaining)
            return self.frame, self.seq


def load_yolo_model(cfg: Config):
    import torch
    from ultralytics import YOLO

    device = cfg.detection.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    model_path = cfg.detection.model
    resolved = cfg.resolve(model_path)
    if resolved.exists():
        model_path = str(resolved)
    print(f"🧠 YOLO モデル読み込み中 ({model_path}, {device})")
    return YOLO(model_path).to(device)


class Detector:
    def __init__(self, cfg: Config, state: RuntimeSettings, recorder: Recorder,
                 notifier: Notifier, procs: ProcessRegistry):
        self.cfg = cfg
        self.state = state
        self.recorder = recorder
        self.notifier = notifier
        self.procs = procs
        d = cfg.detection
        self.w, self.h = d.width, d.height
        # 動きの面積の閾値は 640 幅の映像で決めた値なので、検出映像のサイズに合わせて換算する
        area_scale = (self.w / 640) ** 2
        self.motion_min_contour = 100 * area_scale
        self.motion_min_area = 10 * area_scale

    def motion_in_box(self, gray, prev_gray, x1, y1, x2, y2, threshold):
        """前フレームとの差分で、ボックス内が動いているか。比較できなければ None。"""
        y1, y2 = max(0, y1), min(self.h, y2)
        x1, x2 = max(0, x1), min(self.w, x2)
        now, prev = gray[y1:y2, x1:x2], prev_gray[y1:y2, x1:x2]
        if now.shape != prev.shape or now.size == 0:
            return None
        diff = cv2.absdiff(now, prev)
        _, mask = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
        mask = cv2.medianBlur(mask, 5)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        area = sum(cv2.contourArea(c) for c in contours if cv2.contourArea(c) > self.motion_min_contour)
        return area > self.motion_min_area

    def run(self):
        """メインカメラの ffmpeg を維持し、検出用映像で YOLO を動かす（スレッドで実行）。"""
        cfg = self.cfg
        model = None
        if cfg.detection.enabled:
            try:
                model = load_yolo_model(cfg)
            except Exception as e:  # noqa: BLE001
                print(f"⚠ YOLO を読み込めなかったので検出・録画は止めて、ライブ配信だけ動かします: {e}")
        with_detection = model is not None

        for cat in CATEGORIES:
            (cfg.recordings_dir / cat).mkdir(parents=True, exist_ok=True)
        self.recorder.seg_dir_main.mkdir(parents=True, exist_ok=True)
        log_path = self.recorder.seg_dir_main / "ffmpeg.log"
        sx, sy = self.state.frame_w / self.w, self.state.frame_h / self.h     # 検出映像 → ROI座標系
        show = cfg.detection.show_debug_window

        while True:
            print(f"📡 メインカメラ接続開始... ({cfg.cameras.main.masked_url()})")
            try:
                with open(log_path, "ab") as logf:
                    proc = self.procs.start(
                        self.recorder.build_main_camera_cmd(with_detection),
                        stdout=subprocess.PIPE if with_detection else subprocess.DEVNULL,
                        stderr=logf, bufsize=self.w * self.h * 3 * 4)
            except FileNotFoundError:
                print(f"⚠ ffmpeg が見つからないので録画・ライブ配信は動きません（{cfg.recording.ffmpeg_path}）。")
                return

            if not with_detection:
                proc.wait()
                self.procs.forget(proc)
                print(f"⚠ ffmpeg が止まりました（ログ: {log_path}）。3秒後に再起動します...")
                time.sleep(3)
                continue

            grabber = FrameGrabber(proc, self.w, self.h)
            grabber.start()
            recording = False
            rec = None                   # 録画中の情報 dict(hold, t_start, category, stamp)
            last_detection_time = 0.0
            prev_gray = None
            seq = 0
            person_history, car_history = {}, {}

            try:
                while True:
                    got = grabber.get(seq, timeout=20)
                    if got is None:
                        print(f"⚠ 映像が止まりました。再接続します...（ffmpegログ: {log_path}）")
                        break
                    frame, seq = got

                    sens = self.state.get_sensitivity()
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    results = model(frame, imgsz=cfg.detection.imgsz, conf=sens["conf"], verbose=False)[0]
                    roi = self.state.get_roi()

                    person_box = car_box = None      # 録画のきっかけになったボックス（ROI座標系の x, y, w, h）
                    seen_person, seen_car = set(), set()

                    for box in results.boxes:
                        cls = int(box.cls[0])
                        x1, y1, x2, y2 = map(int, box.xyxy[0].int().tolist())
                        w, h = x2 - x1, y2 - y1
                        if w <= 0 or h <= 0 or x1 < 0 or y1 < 0 or x2 > self.w or y2 > self.h:
                            continue
                        rx, ry, rw, rh = int(x1 * sx), int(y1 * sy), int(w * sx), int(h * sy)
                        if not in_roi(rx, ry, rw, rh, roi):
                            continue

                        if cls in PERSON_CLASSES:
                            history, seen, threshold = person_history, seen_person, sens["person_thresh"]
                        elif cls in CAR_CLASSES:
                            history, seen, threshold = car_history, seen_car, sens["car_thresh"]
                        else:
                            continue

                        key = (x1, y1, x2, y2)
                        seen.add(key)
                        history.setdefault(key, deque(maxlen=3))
                        if prev_gray is None:
                            continue
                        moving = self.motion_in_box(gray, prev_gray, x1, y1, x2, y2, threshold)
                        if moving is None:
                            continue
                        history[key].append(moving)
                        if all(history[key]):        # 直近の履歴がずっと動いている
                            if cls in PERSON_CLASSES and person_box is None:
                                person_box = (rx, ry, rw, rh)
                            elif cls in CAR_CLASSES and car_box is None:
                                car_box = (rx, ry, rw, rh)

                    person_history = {k: v for k, v in person_history.items() if k in seen_person}
                    car_history = {k: v for k, v in car_history.items() if k in seen_car}
                    prev_gray = gray

                    if person_box or car_box:
                        if not recording:
                            detected_type = "person" if person_box else "car"
                            bx, by, bw, bh = person_box or car_box
                            print(f"🔴 録画開始 ({detected_type})")
                            self.notifier.notify_async(detected_type, bx, by, bw, bh, frame)

                            t0 = time.time()
                            rec = {
                                "t_start": t0,
                                "category": detected_type,
                                "stamp": datetime.fromtimestamp(t0).strftime("%Y%m%d_%H%M%S"),
                                # 検出の少し前のセグメントから消されないようにする
                                "hold": self.recorder.add_hold(t0 - cfg.recording.buffer_before - SEGMENT_MARGIN),
                            }
                            recording = True
                        last_detection_time = time.time()

                    elif recording:
                        if time.time() - last_detection_time >= cfg.recording.buffer_after:
                            print("🟢 録画終了")
                            self.recorder.finalize_async(rec["hold"], rec["t_start"], time.time(),
                                                         rec["category"], rec["stamp"])
                            recording, rec = False, None

                    if show:
                        cv2.imshow("Detection", frame)
                        cv2.waitKey(1)

            except Exception as e:  # noqa: BLE001
                print(f"🚨 検出ループでエラー: {e}")
            finally:
                self.procs.stop(proc)
                if recording and rec:        # 接続が切れても、ここまでの録画は残す
                    self.recorder.finalize_async(rec["hold"], rec["t_start"], time.time(),
                                                 rec["category"], rec["stamp"])
                if show:
                    cv2.destroyAllWindows()
                print("再起動中...")
                time.sleep(3)
