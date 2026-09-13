"""Configuration loading and validation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .settings import CONTENT_TYPES, load_overrides

CONFIG_FILE = "config.json"
DEFAULT_PORT = 8000
DEFAULT_SERVER_NAME = "LiteJelly"


@dataclass
class MediaDir:
    path: str
    content_type: str = "mixed"
    label: str = ""

    @property
    def display_name(self) -> str:
        return self.label or Path(self.path).name or self.path

    def to_dict(self) -> dict:
        return {"path": self.path, "content_type": self.content_type, "label": self.label}


@dataclass
class TranscodeSettings:
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    preset: str = "veryfast"
    crf: int = 22
    max_video_bitrate: str = "3000k"
    audio_bitrate: str = "160k"
    resolution: str = "1280x720"
    max_concurrent: int = 2

    @property
    def size(self) -> tuple[int, int]:
        try:
            w, h = self.resolution.lower().split("x")
            return int(w), int(h)
        except (ValueError, AttributeError):
            return 1280, 720


@dataclass
class Config:
    port: int = DEFAULT_PORT
    host: str = "0.0.0.0"
    server_name: str = DEFAULT_SERVER_NAME
    media_dirs: list[MediaDir] = field(default_factory=list)
    scan_interval: int = 60
    thumbnail_workers: int = 2
    allow_hevc_direct: bool = False
    stream_buffer_mb: int = 8
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    admin_token: str = ""
    log_verbosity: str = "info"
    log_to_file: bool = True
    log_max_mb: int = 2
    log_backups: int = 3
    transcode: TranscodeSettings = field(default_factory=TranscodeSettings)
    app_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)

    @property
    def cache_dir(self) -> Path:
        return self.app_dir / ".cache"

    @property
    def static_dir(self) -> Path:
        return self.app_dir / "static"

    @property
    def db_path(self) -> Path:
        return self.cache_dir / "litejelly.db"

    @property
    def stream_buffer_bytes(self) -> int:
        return max(1, self.stream_buffer_mb) * 1024 * 1024

    @property
    def media_paths(self) -> list[str]:
        return [entry.path for entry in self.media_dirs]

    def to_public_dict(self) -> dict:
        """Only fields that are safe to expose to browser clients.

        Deliberately excludes filesystem paths and binary locations: this is
        served unauthenticated to every device on the network.
        """
        return {
            "server_name": self.server_name,
            "version": __import__("litejelly").__version__,
        }

    def to_admin_dict(self) -> dict:
        """Full settings, for the authenticated admin page only."""
        return {
            "server_name": self.server_name,
            "port": self.port,
            "host": self.host,
            "media_dirs": [entry.to_dict() for entry in self.media_dirs],
            "scan_interval": self.scan_interval,
            "thumbnail_workers": self.thumbnail_workers,
            "allow_hevc_direct": self.allow_hevc_direct,
            "stream_buffer_mb": self.stream_buffer_mb,
            "ffmpeg_path": self.ffmpeg_path,
            "ffprobe_path": self.ffprobe_path,
            # Echoed so the page can show a ready-made remote URL. Safe here:
            # the caller already passed the admin guard to see this at all.
            "admin_token": self.admin_token,
            "log_verbosity": self.log_verbosity,
            "log_to_file": self.log_to_file,
            "log_max_mb": self.log_max_mb,
            "log_backups": self.log_backups,
            "transcode": asdict(self.transcode),
        }


def _coerce_int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def load_config(app_dir: Path, args=None) -> tuple[Config, list[str]]:
    """Load config.json, apply CLI overrides. Returns (config, warnings)."""
    warnings: list[str] = []
    raw: dict = {}
    config_path = app_dir / CONFIG_FILE

    if config_path.exists():
        try:
            # utf-8-sig also accepts a BOM, which Notepad and PowerShell add.
            raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                warnings.append(f"{CONFIG_FILE} must contain a JSON object; ignoring it.")
                raw = {}
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"Could not read {CONFIG_FILE}: {exc}")

    cfg = Config(app_dir=app_dir)

    # settings.json holds admin-page edits and wins over the hand-written file.
    overrides = load_overrides(app_dir)
    if overrides:
        transcode_override = overrides.pop("transcode", None)
        raw.update(overrides)
        if isinstance(transcode_override, dict):
            base = raw.get("transcode")
            merged = dict(base) if isinstance(base, dict) else {}
            merged.update(transcode_override)
            raw["transcode"] = merged

    cfg.port = _coerce_int(raw.get("port"), DEFAULT_PORT)
    cfg.host = str(raw.get("host") or "0.0.0.0")
    cfg.server_name = str(raw.get("server_name") or DEFAULT_SERVER_NAME)
    cfg.scan_interval = max(5, _coerce_int(raw.get("scan_interval"), 60))
    cfg.thumbnail_workers = max(1, _coerce_int(raw.get("thumbnail_workers"), 2))
    cfg.allow_hevc_direct = bool(raw.get("allow_hevc_direct", False))
    cfg.stream_buffer_mb = max(1, _coerce_int(raw.get("stream_buffer_mb"), 8))
    cfg.ffmpeg_path = str(raw.get("ffmpeg_path") or "")
    cfg.ffprobe_path = str(raw.get("ffprobe_path") or "")
    cfg.admin_token = str(raw.get("admin_token") or "")
    cfg.log_verbosity = str(raw.get("log_verbosity") or "info").lower()
    cfg.log_to_file = bool(raw.get("log_to_file", True))
    cfg.log_max_mb = max(1, _coerce_int(raw.get("log_max_mb"), 2))
    cfg.log_backups = max(0, _coerce_int(raw.get("log_backups"), 3))

    ts = raw.get("transcode")
    if isinstance(ts, dict):
        defaults = asdict(TranscodeSettings())
        merged = {k: ts.get(k, v) for k, v in defaults.items()}
        merged["crf"] = _coerce_int(merged["crf"], 22)
        merged["max_concurrent"] = max(1, _coerce_int(merged["max_concurrent"], 2))
        cfg.transcode = TranscodeSettings(**merged)
    elif ts is not None:
        warnings.append("'transcode' in config.json must be an object; using defaults.")

    raw_dirs = list(raw.get("media_dirs") or [])
    if args is not None:
        if getattr(args, "port", None):
            cfg.port = args.port
        if getattr(args, "host", None):
            cfg.host = args.host
        if getattr(args, "dir", None):
            raw_dirs = list(args.dir)

    resolved: list[MediaDir] = []
    seen: set[str] = set()
    for entry in raw_dirs:
        # Accept both the original list of paths and the tagged object form.
        if isinstance(entry, dict):
            raw_path = entry.get("path")
            content_type = str(entry.get("content_type") or "mixed").lower()
            label = str(entry.get("label") or "").strip()
        else:
            raw_path, content_type, label = entry, "mixed", ""

        cleaned = str(raw_path or "").strip().strip('"').strip("'").strip()
        if not cleaned:
            continue
        if content_type not in CONTENT_TYPES:
            warnings.append(f"Unknown content type '{content_type}' for {cleaned}; using mixed.")
            content_type = "mixed"

        path = Path(os.path.expandvars(os.path.expanduser(cleaned)))
        try:
            path = path.resolve(strict=True)
        except OSError:
            warnings.append(f"Directory not found or inaccessible: {cleaned}")
            continue
        if not path.is_dir():
            warnings.append(f"Not a directory: {cleaned}")
            continue

        as_str = str(path)
        if as_str.lower() in seen:
            continue
        seen.add(as_str.lower())
        resolved.append(MediaDir(path=as_str, content_type=content_type, label=label))

    cfg.media_dirs = resolved
    return cfg, warnings
