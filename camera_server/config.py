"""設定ファイル（config.yaml）の読み込み。

- 項目と既定値は下の dataclass が唯一の定義元。config.yaml に書かなかった項目は既定値になる。
- 値の中に ${環境変数名} または ${環境変数名:-既定値} と書くと環境変数から読み込む。
- 相対パスは config.yaml が置いてあるフォルダを基準に解決する。
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_NAME = "config.yaml"
EXAMPLE_CONFIG_NAME = "config.example.yaml"


class ConfigError(Exception):
    """設定ファイルの内容に問題があるときのエラー。"""


# =====================================================================
# 設定項目の定義（既定値つき）
# =====================================================================

# RTSP URL のユーザー情報部分でそのまま使える記号（RFC 3986）。それ以外（@ : / ? # % など）はエンコードする
_USERINFO_SAFE = "!$&'()*+,;=-._~"


@dataclass
class CameraConfig:
    enabled: bool = True
    url: str = ""
    username: str = ""
    password: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.url.strip())

    def full_url(self) -> str:
        """username / password を埋め込んだ RTSP URL を返す（URL に既に含まれていればそのまま）。"""
        url = self.url.strip()
        if not self.username:
            return url
        parts = urlsplit(url)
        if "@" in parts.netloc:
            return url
        cred = quote(self.username, safe=_USERINFO_SAFE)
        if self.password:
            cred += ":" + quote(self.password, safe=_USERINFO_SAFE)
        return urlunsplit((parts.scheme, f"{cred}@{parts.netloc}", parts.path, parts.query, parts.fragment))

    def masked_url(self) -> str:
        """ログ表示用に、パスワードを伏せた URL を返す。"""
        url = self.full_url()
        parts = urlsplit(url)
        if "@" not in parts.netloc:
            return url
        userinfo, host = parts.netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        return urlunsplit((parts.scheme, f"{user}:****@{host}", parts.path, parts.query, parts.fragment))


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    https: bool = False
    ssl_cert: str = "data/certs/cert.pem"
    ssl_key: str = "data/certs/key.pem"
    local_network_only: bool = True
    secret_key: str = ""
    public_base_url: str = ""


@dataclass
class AuthConfig:
    enabled: bool = False
    invite_code: str = ""
    rp_id: str = "localhost"
    rp_origin: str = "https://localhost"
    rp_name: str = "Security Camera"


@dataclass
class PathsConfig:
    data_dir: str = "data"
    recordings_dir: str = "data/recordings"
    hls_dir: str = "data/hls"
    segments_dir: str = ""


@dataclass
class RecordingConfig:
    max_gb: float = 100.0
    segment_seconds: int = 2
    segment_keep_seconds: int = 45
    buffer_before: int = 10
    buffer_after: int = 7
    thumbnail_width: int = 960
    ffmpeg_path: str = "ffmpeg"
    ffmpeg_hwaccel: str = ""


@dataclass
class LiveConfig:
    hls_enabled: bool = False


@dataclass
class DetectionConfig:
    enabled: bool = True
    model: str = "yolov8s.pt"
    device: str = "auto"
    fps: int = 10
    width: int = 960
    height: int = 540
    imgsz: int = 960
    roi_width: int = 640
    roi_height: int = 360
    confidence: float = 0.35
    motion_threshold_person: int = 10
    motion_threshold_car: int = 30
    show_debug_window: bool = False


@dataclass
class NotificationConfig:
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:you@example.com"
    thumbnail_width: int = 320
    thumbnail_quality: int = 80

    @property
    def enabled(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key)


@dataclass
class CamerasConfig:
    main: CameraConfig = field(default_factory=CameraConfig)
    sub: CameraConfig = field(default_factory=lambda: CameraConfig(enabled=False))


# config.yaml の最上位のキー → dataclass
_SECTIONS = {
    "server": ServerConfig,
    "auth": AuthConfig,
    "paths": PathsConfig,
    "recording": RecordingConfig,
    "live": LiveConfig,
    "detection": DetectionConfig,
    "notifications": NotificationConfig,
}


@dataclass
class Config:
    base_dir: Path
    source: Path | None
    cameras: CamerasConfig
    server: ServerConfig
    auth: AuthConfig
    paths: PathsConfig
    recording: RecordingConfig
    live: LiveConfig
    detection: DetectionConfig
    notifications: NotificationConfig

    # ---- パス（相対パスは config.yaml の場所を基準に解決）----
    def resolve(self, p: str) -> Path:
        path = Path(os.path.expandvars(os.path.expanduser(p)))
        return path if path.is_absolute() else (self.base_dir / path)

    @property
    def data_dir(self) -> Path:
        return self.resolve(self.paths.data_dir)

    @property
    def recordings_dir(self) -> Path:
        return self.resolve(self.paths.recordings_dir)

    @property
    def hls_dir(self) -> Path:
        return self.resolve(self.paths.hls_dir)

    @property
    def segments_root(self) -> Path:
        if self.paths.segments_dir:
            return self.resolve(self.paths.segments_dir)
        return Path(tempfile.gettempdir()) / "camera_segments"

    @property
    def segments_dir_main(self) -> Path:
        return self.segments_root / "main"

    @property
    def segments_dir_sub(self) -> Path:
        return self.segments_root / "sub"

    @property
    def roi_file(self) -> Path:
        return self.data_dir / "roi_settings.json"

    @property
    def sensitivity_file(self) -> Path:
        return self.data_dir / "sensitivity.json"

    @property
    def subscription_file(self) -> Path:
        return self.data_dir / "subscription.json"

    @property
    def credentials_dir(self) -> Path:
        return self.data_dir / "credentials"

    @property
    def ssl_cert(self) -> Path:
        return self.resolve(self.server.ssl_cert)

    @property
    def ssl_key(self) -> Path:
        return self.resolve(self.server.ssl_key)

    @property
    def max_recording_bytes(self) -> int:
        return int(self.recording.max_gb * 1024 ** 3)

    def warnings(self) -> list[str]:
        """起動はできるが気をつけたほうがよい設定を返す。"""
        out = []
        if not self.cameras.main.active:
            out.append("cameras.main.url が空なので、録画・検知・ライブ配信は動きません。")
        if self.auth.enabled and not self.server.https:
            out.append("パスキー認証は HTTPS でないと動きません（server.https: true にしてください）。")
        if self.auth.enabled and not self.auth.invite_code:
            out.append("auth.invite_code が空なので、管理者の新規登録はできません。")
        if not self.auth.enabled and not self.server.local_network_only:
            out.append("認証オフかつ local_network_only: false です。外部から誰でも映像を見られる状態になります。")
        if not self.notifications.enabled:
            out.append("VAPID 鍵が未設定なので、プッシュ通知はオフです（tools/gen_vapid_keys.py で作成できます）。")
        return out


# =====================================================================
# 読み込み
# =====================================================================

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value, where):
    if isinstance(value, str):
        def repl(m):
            name, default = m.group(1), m.group(2)
            env = os.environ.get(name)
            if env is not None:
                return env
            if default is not None:
                return default
            raise ConfigError(f"{where}: 環境変数 {name} が設定されていません。")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0", ""}


def _coerce(value, default, where):
    """YAML の値を既定値と同じ型にそろえる（環境変数から来た文字列の "8080" なども数値にする）。"""
    if value is None:
        return default
    kind = type(default)
    try:
        if kind is bool:
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in _TRUE:
                return True
            if text in _FALSE:
                return False
            raise ValueError
        if kind is int:
            if isinstance(value, bool):
                raise ValueError
            return int(value)
        if kind is float:
            if isinstance(value, bool):
                raise ValueError
            return float(value)
        if kind is str:
            if isinstance(value, (dict, list)):
                raise ValueError
            return str(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where} の値 {value!r} は {kind.__name__} 型として読めません。") from None
    return value


def _build(cls, data, where):
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{where} は「キー: 値」の形で書いてください。")
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigError(f"{where} に知らない項目があります: {', '.join(unknown)}（綴りを確認してください。"
                          f"使える項目: {', '.join(known)}）")
    defaults = cls()
    kwargs = {name: _coerce(value, getattr(defaults, name), f"{where}.{name}") for name, value in data.items()}
    return cls(**kwargs)


def find_config_path(path: str | os.PathLike | None = None) -> Path:
    """使う設定ファイルを決める: 引数 > 環境変数 CAMERA_CONFIG > プロジェクト直下の config.yaml"""
    if path:
        return Path(path).expanduser().resolve()
    env = os.environ.get("CAMERA_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return PROJECT_ROOT / DEFAULT_CONFIG_NAME


def load_config(path: str | os.PathLike | None = None) -> Config:
    config_path = find_config_path(path)
    if not config_path.exists():
        raise ConfigError(
            f"設定ファイルが見つかりません: {config_path}\n"
            f"  {EXAMPLE_CONFIG_NAME} を {DEFAULT_CONFIG_NAME} という名前でコピーして、"
            f"カメラの RTSP アドレスなどを書き換えてください。")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{config_path.name} の書き方に誤りがあります:\n{e}") from None
    return config_from_dict(raw, base_dir=config_path.parent, source=config_path)


def config_from_dict(raw: dict, base_dir: Path | None = None, source: Path | None = None) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("設定ファイルの一番外側は「キー: 値」の形で書いてください。")
    raw = _expand_env(raw, "config")

    allowed = {"cameras", *_SECTIONS}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"知らない項目があります: {', '.join(unknown)}（使える項目: {', '.join(sorted(allowed))}）")

    cams_raw = raw.get("cameras") or {}
    if not isinstance(cams_raw, dict):
        raise ConfigError("cameras は main: / sub: の形で書いてください。")
    unknown = sorted(set(cams_raw) - {"main", "sub"})
    if unknown:
        raise ConfigError(f"cameras に知らない項目があります: {', '.join(unknown)}（main と sub だけ使えます）")
    cameras = CamerasConfig(
        main=_build(CameraConfig, cams_raw.get("main"), "cameras.main"),
        sub=_build(CameraConfig, cams_raw.get("sub"), "cameras.sub") if "sub" in cams_raw
        else CameraConfig(enabled=False),
    )

    sections = {name: _build(cls, raw.get(name), name) for name, cls in _SECTIONS.items()}
    cfg = Config(base_dir=(base_dir or PROJECT_ROOT).resolve(), source=source, cameras=cameras, **sections)
    _validate(cfg)
    return cfg


def _validate(cfg: Config):
    d, r = cfg.detection, cfg.recording
    checks = [
        (1 <= cfg.server.port <= 65535, "server.port は 1〜65535 にしてください。"),
        (d.fps > 0, "detection.fps は 1 以上にしてください。"),
        (d.width > 0 and d.height > 0, "detection.width / height は 1 以上にしてください。"),
        (d.roi_width > 0 and d.roi_height > 0, "detection.roi_width / roi_height は 1 以上にしてください。"),
        (0.0 < d.confidence < 1.0, "detection.confidence は 0〜1 の間にしてください。"),
        (d.device in ("auto", "cpu", "cuda") or d.device.startswith("cuda:"),
         "detection.device は auto / cpu / cuda / cuda:0 などにしてください。"),
        (r.segment_seconds > 0, "recording.segment_seconds は 1 以上にしてください。"),
        (r.max_gb > 0, "recording.max_gb は 0 より大きくしてください。"),
        (r.buffer_before >= 0 and r.buffer_after >= 0, "recording.buffer_before / buffer_after は 0 以上にしてください。"),
    ]
    for ok, message in checks:
        if not ok:
            raise ConfigError(message)
    for name in ("main", "sub"):
        cam = getattr(cfg.cameras, name)
        if cam.active and not urlsplit(cam.url.strip()).scheme:
            raise ConfigError(f"cameras.{name}.url は rtsp://〜 の形で書いてください（今: {cam.url!r}）。")
