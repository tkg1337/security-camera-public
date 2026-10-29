"""Web 画面から変更できる設定（検出範囲 ROI・検知感度）。

Web のリクエスト処理と検出スレッドの両方から使うので、ロックで守る。
変更した値は data フォルダの JSON に保存し、次回起動時にも使う。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .config import Config

ROI_KEYS = ("ROI_X_MIN", "ROI_Y_MIN", "ROI_X_MAX", "ROI_Y_MAX")


def in_roi(x, y, w, h, roi) -> bool:
    """バウンディングボックスの中心が ROI 内にあるか。"""
    cx, cy = x + w / 2, y + h / 2
    return roi["ROI_X_MIN"] <= cx <= roi["ROI_X_MAX"] and roi["ROI_Y_MIN"] <= cy <= roi["ROI_Y_MAX"]


def _read_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    tmp.replace(path)


class RuntimeSettings:
    def __init__(self, cfg: Config):
        self.frame_w = cfg.detection.roi_width
        self.frame_h = cfg.detection.roi_height
        self.roi_file = cfg.roi_file
        self.sensitivity_file = cfg.sensitivity_file
        self._lock = threading.Lock()
        self._roi = self._load_roi()
        self._sens = self._load_sensitivity(cfg)

    # ---------------- ROI ----------------
    def default_roi(self) -> dict:
        return {"ROI_X_MIN": 0, "ROI_Y_MIN": 0, "ROI_X_MAX": self.frame_w, "ROI_Y_MAX": self.frame_h}

    def _load_roi(self) -> dict:
        roi = self.default_roi()
        if not self.roi_file.exists():
            print(f"{self.roi_file.name} が無いので、検出範囲は画面全体を使います。")
            return roi
        try:
            saved = _read_json(self.roi_file)
            for key in ROI_KEYS:
                roi[key] = int(saved.get(key, roi[key]))
        except (ValueError, TypeError, OSError, AttributeError) as e:
            print(f"ROI設定の読み込みに失敗しました（{e}）。画面全体を使います。")
        return roi

    def get_roi(self) -> dict:
        with self._lock:
            return dict(self._roi)

    def update_roi(self, data: dict) -> dict:
        """値を範囲内に収めて保存する。不正な値なら ValueError / TypeError。"""
        with self._lock:
            cur = self._roi
            x_min = max(0, int(data.get("ROI_X_MIN", cur["ROI_X_MIN"])))
            y_min = max(0, int(data.get("ROI_Y_MIN", cur["ROI_Y_MIN"])))
            x_max = min(self.frame_w, max(x_min + 1, int(data.get("ROI_X_MAX", cur["ROI_X_MAX"]))))
            y_max = min(self.frame_h, max(y_min + 1, int(data.get("ROI_Y_MAX", cur["ROI_Y_MAX"]))))
            cur.update({"ROI_X_MIN": x_min, "ROI_Y_MIN": y_min, "ROI_X_MAX": x_max, "ROI_Y_MAX": y_max})
            snapshot = dict(cur)
        _write_json(self.roi_file, snapshot)
        print(f"ROI設定を保存しました: {self.roi_file}")
        return snapshot

    # ---------------- 検知感度 ----------------
    @staticmethod
    def _clamp_sensitivity(conf, person, car) -> dict:
        return {
            "conf": max(0.05, min(0.95, float(conf))),
            "person_thresh": max(1, min(100, int(person))),
            "car_thresh": max(1, min(100, int(car))),
        }

    def _load_sensitivity(self, cfg: Config) -> dict:
        d = cfg.detection
        sens = self._clamp_sensitivity(d.confidence, d.motion_threshold_person, d.motion_threshold_car)
        if self.sensitivity_file.exists():
            try:
                saved = _read_json(self.sensitivity_file)
                sens = self._clamp_sensitivity(saved.get("conf", sens["conf"]),
                                               saved.get("person_thresh", sens["person_thresh"]),
                                               saved.get("car_thresh", sens["car_thresh"]))
            except (ValueError, TypeError, OSError, AttributeError) as e:
                print(f"感度設定の読み込みに失敗しました（{e}）。config.yaml の値を使います。")
        return sens

    def get_sensitivity(self) -> dict:
        with self._lock:
            return dict(self._sens)

    def update_sensitivity(self, data: dict) -> dict:
        with self._lock:
            cur = self._sens
            self._sens = self._clamp_sensitivity(data.get("conf", cur["conf"]),
                                                 data.get("person_thresh", cur["person_thresh"]),
                                                 data.get("car_thresh", cur["car_thresh"]))
            snapshot = dict(self._sens)
        _write_json(self.sensitivity_file, snapshot)
        print(f"感度設定更新: conf={snapshot['conf']:.2f}  person={snapshot['person_thresh']}  "
              f"car={snapshot['car_thresh']}")
        return snapshot
