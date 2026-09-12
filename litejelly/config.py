"""Configuration loading and validation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

CONFIG_FILE = "config.json"
DEFAULT_PORT = 8000
DEFAULT_SERVER_NAME = "LiteJelly"


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
    media_dirs: list[str] = field(default_factory=list)
    scan_interval: int = 60
    thumbnail_workers: int = 2
    allow_hevc_direct: bool = False
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

    def to_public_dict(self) -> dict:
        """Only fields that are safe to expose to browser clients."""
        return {
            "server_name": self.server_name,
            "version": __import__("litejelly").__version__,
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
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                warnings.append(f"{CONFIG_FILE} must contain a JSON object; ignoring it.")
                raw = {}
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"Could not read {CONFIG_FILE}: {exc}")

    cfg = Config(app_dir=app_dir)
    cfg.port = _coerce_int(raw.get("port"), DEFAULT_PORT)
    cfg.host = str(raw.get("host") or "0.0.0.0")
    cfg.server_name = str(raw.get("server_name") or DEFAULT_SERVER_NAME)
    cfg.scan_interval = max(5, _coerce_int(raw.get("scan_interval"), 60))
    cfg.thumbnail_workers = max(1, _coerce_int(raw.get("thumbnail_workers"), 2))
    cfg.allow_hevc_direct = bool(raw.get("allow_hevc_direct", False))

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
        if getattr(args, "name", None):
            cfg.server_name = args.name
        if getattr(args, "dir", None):
            raw_dirs = list(args.dir)

    if not raw_dirs:
        raw_dirs = [os.path.expanduser("~/Videos")]
        warnings.append("No media_dirs configured; defaulting to ~/Videos.")

    resolved: list[str] = []
    for entry in raw_dirs:
        cleaned = str(entry).strip().strip('"').strip("'").strip()
        if not cleaned:
            continue
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
        if as_str not in resolved:
            resolved.append(as_str)

    cfg.media_dirs = resolved
    return cfg, warnings
