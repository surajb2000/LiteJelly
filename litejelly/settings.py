"""User settings that layer on top of config.json.

config.json stays as the hand-written baseline; anything changed through the
admin page is written to settings.json beside it. Keeping them apart means a
user can always delete settings.json to get their file back, and it keeps
editable state out of .cache, which is disposable.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .logs import VERBOSITY

log = logging.getLogger("litejelly.settings")

SETTINGS_FILE = "settings.json"

CONTENT_TYPES = ("mixed", "movies", "shows", "anime")

# Changing these needs a restart, so they are saved but not applied live.
RESTART_REQUIRED = ("port", "host")

PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)


def settings_path(app_dir: Path) -> Path:
    return app_dir / SETTINGS_FILE


def load_overrides(app_dir: Path) -> dict:
    path = settings_path(app_dir)
    if not path.is_file():
        return {}
    try:
        # utf-8-sig: Notepad and PowerShell write a BOM that plain utf-8 rejects.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Ignoring unreadable %s: %s", SETTINGS_FILE, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("%s must contain a JSON object; ignoring it.", SETTINGS_FILE)
        return {}
    return data


def save_overrides(app_dir: Path, data: dict) -> None:
    """Write atomically so an interrupted save cannot truncate the file."""
    path = settings_path(app_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise


def _as_int(value, minimum: int, maximum: int, field: str, errors: list[str]):
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be a whole number")
        return None
    if not minimum <= number <= maximum:
        errors.append(f"{field} must be between {minimum} and {maximum}")
        return None
    return number


def _clean_media_dirs(value, errors: list[str]):
    if not isinstance(value, list):
        errors.append("media_dirs must be a list")
        return None

    cleaned = []
    seen = set()
    for entry in value:
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict):
            errors.append("each media directory must be a path or an object")
            continue

        raw_path = str(entry.get("path") or "").strip().strip('"').strip("'")
        if not raw_path:
            continue

        expanded = Path(os.path.expandvars(os.path.expanduser(raw_path)))
        try:
            resolved = expanded.resolve(strict=True)
        except OSError:
            errors.append(f"Not found: {raw_path}")
            continue
        if not resolved.is_dir():
            errors.append(f"Not a directory: {raw_path}")
            continue

        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)

        content_type = str(entry.get("content_type") or "mixed").lower()
        if content_type not in CONTENT_TYPES:
            errors.append(f"Unknown content type '{content_type}' for {raw_path}")
            content_type = "mixed"

        cleaned.append({
            "path": str(resolved),
            "content_type": content_type,
            "label": str(entry.get("label") or "").strip(),
        })
    return cleaned


def _clean_transcode(value, errors: list[str]):
    if not isinstance(value, dict):
        errors.append("transcode must be an object")
        return None

    out = {}
    for key in ("video_codec", "audio_codec", "max_video_bitrate", "audio_bitrate"):
        if key in value:
            text = str(value[key]).strip()
            if not text:
                errors.append(f"transcode.{key} cannot be empty")
            else:
                out[key] = text

    if "preset" in value:
        preset = str(value["preset"]).strip().lower()
        if preset not in PRESETS:
            errors.append(f"transcode.preset must be one of: {', '.join(PRESETS)}")
        else:
            out["preset"] = preset

    if "crf" in value:
        number = _as_int(value["crf"], 0, 51, "transcode.crf", errors)
        if number is not None:
            out["crf"] = number

    if "max_concurrent" in value:
        number = _as_int(value["max_concurrent"], 1, 16, "transcode.max_concurrent", errors)
        if number is not None:
            out["max_concurrent"] = number

    if "resolution" in value:
        text = str(value["resolution"]).strip().lower()
        parts = text.split("x")
        if len(parts) != 2 or not all(p.isdigit() and int(p) > 0 for p in parts):
            errors.append("transcode.resolution must look like 1280x720")
        else:
            out["resolution"] = f"{int(parts[0])}x{int(parts[1])}"

    return out


def validate(payload: dict) -> tuple[dict, list[str]]:
    """Return (accepted settings, errors). Unknown keys are ignored."""
    errors: list[str] = []
    clean: dict = {}

    if not isinstance(payload, dict):
        return {}, ["settings must be a JSON object"]

    if "server_name" in payload:
        name = str(payload["server_name"]).strip()
        if not name:
            errors.append("server_name cannot be empty")
        elif len(name) > 64:
            errors.append("server_name must be 64 characters or fewer")
        else:
            clean["server_name"] = name

    if "media_dirs" in payload:
        dirs = _clean_media_dirs(payload["media_dirs"], errors)
        if dirs is not None:
            # An empty list is allowed: the server starts without media and
            # points the user at this page to add some.
            clean["media_dirs"] = dirs

    if "scan_interval" in payload:
        number = _as_int(payload["scan_interval"], 5, 86400, "scan_interval", errors)
        if number is not None:
            clean["scan_interval"] = number

    if "thumbnail_workers" in payload:
        number = _as_int(payload["thumbnail_workers"], 1, 16, "thumbnail_workers", errors)
        if number is not None:
            clean["thumbnail_workers"] = number

    if "stream_buffer_mb" in payload:
        number = _as_int(payload["stream_buffer_mb"], 1, 512, "stream_buffer_mb", errors)
        if number is not None:
            clean["stream_buffer_mb"] = number

    if "allow_hevc_direct" in payload:
        clean["allow_hevc_direct"] = bool(payload["allow_hevc_direct"])

    if "online_metadata" in payload:
        clean["online_metadata"] = bool(payload["online_metadata"])

    for key in ("tmdb_api_key", "omdb_api_key"):
        if key in payload:
            value = str(payload[key] or "").strip()
            if value and (len(value) > 128 or any(ch.isspace() for ch in value)):
                errors.append(f"{key} does not look like an API key")
            else:
                clean[key] = value

    for key in ("ffmpeg_path", "ffprobe_path"):
        if key in payload:
            text = str(payload[key] or "").strip()
            if text:
                candidate = Path(os.path.expandvars(os.path.expanduser(text)))
                if not candidate.exists():
                    errors.append(f"{key} does not exist: {text}")
                    continue
            clean[key] = text

    if "port" in payload:
        number = _as_int(payload["port"], 1, 65535, "port", errors)
        if number is not None:
            clean["port"] = number

    if "host" in payload:
        host = str(payload["host"]).strip()
        if not host:
            errors.append("host cannot be empty")
        else:
            clean["host"] = host

    if "log_verbosity" in payload:
        verbosity = str(payload["log_verbosity"] or "").strip().lower()
        if verbosity not in VERBOSITY:
            errors.append(f"log_verbosity must be one of: {', '.join(VERBOSITY)}")
        else:
            clean["log_verbosity"] = verbosity

    if "log_to_file" in payload:
        clean["log_to_file"] = bool(payload["log_to_file"])

    if "log_to_console" in payload:
        clean["log_to_console"] = bool(payload["log_to_console"])

    if "log_max_mb" in payload:
        number = _as_int(payload["log_max_mb"], 1, 512, "log_max_mb", errors)
        if number is not None:
            clean["log_max_mb"] = number

    if "log_backups" in payload:
        number = _as_int(payload["log_backups"], 0, 20, "log_backups", errors)
        if number is not None:
            clean["log_backups"] = number

    if "transcode" in payload:
        transcode = _clean_transcode(payload["transcode"], errors)
        if transcode:
            clean["transcode"] = transcode

    return clean, errors


def restart_required(previous: dict, incoming: dict) -> list[str]:
    """Saved keys that will not take effect until the server restarts."""
    return [key for key in RESTART_REQUIRED
            if key in incoming and incoming[key] != previous.get(key)]
