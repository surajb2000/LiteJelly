"""HTTP layer: route table, static serving, streaming and the JSON API."""

from __future__ import annotations

import collections
import hmac
import http.server
import json
import logging
import mimetypes
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from email.utils import formatdate
from http import HTTPStatus
from pathlib import Path

from . import admin as admin_auth
from . import auth as admin_accounts
from . import logs as log_setup
from . import settings as user_settings
from .chapters import read_chapters, skippable
from .config import load_config
from .enrich import Enricher
from .ffmpeg import QUALITY_LADDER, FFmpegTools, popen_quiet, resolve_quality
from .library import (
    Library, build_continue_watching, next_episode, previous_episode,
)
from .paths import is_within
from .providers import MetadataProviders
from .store import ProgressStore
from .subtitles import SubtitleService, discover as discover_subtitles
from .thumbnails import ThumbnailService

log = logging.getLogger("litejelly.web")

CHUNK_SIZE = 256 * 1024
MAX_BODY_BYTES = 64 * 1024
DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'"
    ),
}

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/vtt", ".vtt")
mimetypes.add_type("application/manifest+json", ".webmanifest")


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def parse_range(header: str | None, file_size: int):
    """Parse a single-range 'Range' header.

    Returns ``(start, end)``, ``None`` when no range was requested, or the
    string ``"invalid"`` when the range cannot be satisfied.
    """
    if not header:
        return None
    header = header.strip()
    if not header.lower().startswith("bytes="):
        return "invalid"
    spec = header[6:].split(",")[0].strip()
    if "-" not in spec:
        return "invalid"

    start_text, _, end_text = spec.partition("-")
    try:
        if not start_text:
            # Suffix range: last N bytes.
            length = int(end_text)
            if length <= 0:
                return "invalid"
            start = max(0, file_size - length)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
    except ValueError:
        return "invalid"

    if start < 0 or start >= file_size or end < start:
        return "invalid"
    return start, min(end, file_size - 1)


def _audio_delay_ms(info, plan, query) -> float:
    """Manual audio trim only.

    Compensating the B-frame reorder delay automatically was tried and removed:
    measuring the delivered audio against a beep reference showed ffmpeg
    already accounts for most of it, so adding the full reorder depth pushed
    the audio late instead of aligning it.
    """
    try:
        delay = float(query.get("adelay", ["0"])[0])
    except (TypeError, ValueError):
        delay = 0.0
    return max(-5000.0, min(5000.0, delay))


def _drive_roots() -> list[dict]:
    """Top level of the directory picker: drive letters on Windows, / elsewhere."""
    if os.name != "nt":
        return [{"name": "/", "path": "/"}]
    roots = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = f"{letter}:\\"
        if os.path.exists(candidate):
            roots.append({"name": candidate, "path": candidate})
    return roots


def _build_metadata(config):
    """Online lookups are opt-in: they send your titles to a third party."""
    if not getattr(config, "online_metadata", False):
        return None, None
    providers = MetadataProviders(config.cache_dir)
    return providers, Enricher(providers)


def _skip_segments(app, video, path, duration: float) -> list[dict]:
    """Chapters first; an online answer only when the file has none.

    Chapters belong to this exact file. A shared database describes somebody
    else's copy, which may be cut differently.
    """
    segments = skippable(read_chapters(app.tools.ffprobe, path), duration)
    if segments or app.metadata is None or not video.mal_id or not video.episode:
        return segments

    # Cache only: a request must never wait on a third-party service.
    cached = app.metadata.cached_skip_times(video.mal_id, video.episode, duration)
    if not cached and app.enricher is not None:
        app.enricher.enqueue_skip(video.mal_id, video.episode)
    return cached


class Application:
    """Holds every service and maps request paths to handlers."""
    def __init__(self, config):
        self.config = config
        self._config_lock = threading.RLock()
        self.metadata, self.enricher = _build_metadata(config)
        self.library = Library(config.media_dirs, config.scan_interval,
                               metadata=self.metadata, enricher=self.enricher)
        self.tools = FFmpegTools(
            config.app_dir,
            transcode_slots=config.transcode.max_concurrent,
            thumbnail_slots=config.thumbnail_workers,
            ffmpeg_path=config.ffmpeg_path,
            ffprobe_path=config.ffprobe_path,
        )
        self.progress = ProgressStore(config.db_path)
        self.subtitles = SubtitleService(self.tools, config.cache_dir)
        self.thumbnails = ThumbnailService(self.tools, config.cache_dir,
                                           config.thumbnail_workers)
        self.static_dir = config.static_dir.resolve()

        self.sessions = admin_accounts.SessionStore()
        self.throttle = admin_accounts.LoginThrottle()
        self.credentials = admin_accounts.load_credentials(config.app_dir)
        if self.enricher is not None:
            self.enricher.on_updated = lambda: self.library.request_scan(force=True)

        self.routes = {
            ("GET", "/"): Routes.index,
            ("GET", "/index.html"): Routes.index,
            ("GET", "/favicon.ico"): Routes.favicon,
            ("GET", "/admin"): Routes.admin_page,
            ("GET", "/admin/"): Routes.admin_page,
            ("GET", "/api/config"): Routes.config,
            ("GET", "/api/library"): Routes.library,
            ("POST", "/api/rescan"): Routes.rescan,
            ("GET", "/api/playback"): Routes.playback,
            ("GET", "/api/seekpoint"): Routes.seekpoint,
            ("GET", "/api/thumbnail"): Routes.thumbnail,
            ("GET", "/api/artwork"): Routes.artwork,
            ("GET", "/api/details"): Routes.details,
            ("GET", "/api/subtitle"): Routes.subtitle,
            ("GET", "/api/progress"): Routes.progress_get,
            ("POST", "/api/progress"): Routes.progress_post,
            ("GET", "/api/admin/settings"): Routes.admin_settings_get,
            ("POST", "/api/admin/settings"): Routes.admin_settings_post,
            ("GET", "/api/admin/session"): Routes.admin_session,
            ("POST", "/api/admin/setup"): Routes.admin_setup,
            ("POST", "/api/admin/login"): Routes.admin_login,
            ("POST", "/api/admin/logout"): Routes.admin_logout,
            ("POST", "/api/admin/password"): Routes.admin_password,
            ("GET", "/api/admin/browse"): Routes.admin_browse,
            ("GET", "/api/admin/logs"): Routes.admin_logs,
            ("POST", "/api/admin/logs/clear"): Routes.admin_logs_clear,
            ("GET", "/media/stream"): Routes.stream,
            ("GET", "/media/transcode"): Routes.transcode,
        }

    def apply_config(self, new_config) -> None:
        """Swap in a reloaded config and rebuild everything derived from it.

        Rebuilding matters: the ffmpeg tools, subtitle and thumbnail services
        all captured values from the previous config, so replacing only
        ``self.config`` would leave them pointing at the old binaries and
        concurrency limits.
        """
        with self._config_lock:
            old_tools = self.tools
            tools = FFmpegTools(
                new_config.app_dir,
                transcode_slots=new_config.transcode.max_concurrent,
                thumbnail_slots=new_config.thumbnail_workers,
                ffmpeg_path=new_config.ffmpeg_path,
                ffprobe_path=new_config.ffprobe_path,
            )
            old_thumbnails = self.thumbnails
            old_enricher = self.enricher
            metadata, enricher = _build_metadata(new_config)
            self.config = new_config
            self.tools = tools
            self.metadata = metadata
            self.enricher = enricher
            self.subtitles = SubtitleService(tools, new_config.cache_dir)
            self.thumbnails = ThumbnailService(tools, new_config.cache_dir,
                                               new_config.thumbnail_workers)
            self.static_dir = new_config.static_dir.resolve()
            self.library.metadata = metadata
            self.library.enricher = enricher
            old_thumbnails.close()
            if old_enricher is not None:
                old_enricher.stop()
            del old_tools

        # Verbosity applies to the live handlers; the file settings only take
        # effect on restart because reopening the file would lose buffered lines.
        log_setup.set_verbosity(new_config.log_verbosity, new_config.log_to_console)
        self.library.set_media_dirs(new_config.media_dirs, new_config.scan_interval)

    def resolve_video(self, query: dict):
        """Look up a video by opaque id and return (video, absolute_path)."""
        video_id = query.get("id", [""])[0]
        if not video_id:
            return None, None
        video = self.library.get(video_id)
        if video is None:
            return None, None
        path = self.library.absolute_path(video)
        if path is None or not path.is_file():
            return video, None
        return video, path

    def media_root(self, video) -> Path | None:
        """The configured folder a video was found under."""
        dirs = self.library.media_dirs
        if not 0 <= video.dir_index < len(dirs):
            return None
        return Path(dirs[video.dir_index].path)

    def shutdown(self) -> None:
        self.library.stop()
        if self.enricher is not None:
            self.enricher.stop()
        self.thumbnails.close()
        self.progress.close()


class Routes:
    """Request handlers. Each receives the active RequestHandler and query dict."""

    @staticmethod
    def index(h, query):
        h.serve_static_file(h.app.static_dir / "index.html")

    @staticmethod
    def favicon(h, query):
        icon = h.app.static_dir / "favicon.svg"
        if icon.is_file():
            h.serve_static_file(icon)
        else:
            h.send_api_error(HTTPStatus.NOT_FOUND, "No favicon")

    @staticmethod
    def config(h, query):
        payload = h.app.config.to_public_dict()
        payload.update({
            "ffmpeg_available": h.app.tools.available,
            "ffprobe_available": h.app.tools.can_probe,
            "media_dir_count": len(h.app.config.media_dirs),
        })
        h.send_json(payload)

    @staticmethod
    def library(h, query):
        entries = h.app.library.videos
        progress = h.app.progress.all()
        h.send_json({
            "videos": [v.to_dict() for v in entries],
            "progress": progress,
            "continue_watching": build_continue_watching(entries, progress),
            "status": h.app.library.status,
            "ffmpeg_available": h.app.tools.available,
        })

    @staticmethod
    def rescan(h, query):
        # Forced: the button says rescan, so it should not quietly do nothing
        # when the folder fingerprint happens to be unchanged.
        h.app.library.request_scan(force=True)
        h.send_json({"ok": True, "status": h.app.library.status})

    # -- admin ------------------------------------------------------------
    @staticmethod
    def admin_page(h, query):
        # The page itself is public; it decides what to show from the session
        # state, and every endpoint behind it is guarded individually.
        h.serve_static_file(h.app.static_dir / "admin.html")

    @staticmethod
    def admin_session(h, query):
        """What the admin page should show: setup, login, or the settings."""
        app = h.app
        client = h.client_address[0] if h.client_address else ""
        if app.credentials is None:
            h.send_json({
                "state": "setup",
                "can_set_up_here": admin_auth.is_loopback(client),
                "min_password_length": admin_accounts.MIN_PASSWORD_LENGTH,
            })
            return

        username = app.sessions.validate(admin_auth.session_token(h.headers))
        if username is None:
            locked = app.throttle.locked_for(client)
            h.send_json({
                "state": "login",
                "locked_seconds": int(locked),
            })
            return
        h.send_json({"state": "ready", "username": username})

    @staticmethod
    def admin_setup(h, query):
        """Create the first admin account. Only from the machine itself.

        Allowing this over the network would be a land grab: whoever reached a
        freshly started server first would own it.
        """
        app = h.app
        client = h.client_address[0] if h.client_address else ""
        if app.credentials is not None:
            h.send_api_error(HTTPStatus.CONFLICT, "An admin account already exists.")
            return
        if not admin_auth.is_loopback(client):
            log.warning("Refused remote admin setup from %s", client)
            h.send_api_error(
                HTTPStatus.FORBIDDEN,
                "The first admin account must be created on the machine "
                "running LiteJelly.")
            return
        if not admin_auth.same_origin(h.headers, h.headers.get("Host", "")):
            h.send_api_error(HTTPStatus.FORBIDDEN, "Cross-site request refused.")
            return

        body = h.read_json_body()
        if body is None:
            return
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")

        errors = admin_accounts.check_username(username)
        errors += admin_accounts.check_password_strength(password)
        if errors:
            h.send_json({"ok": False, "errors": errors}, status=HTTPStatus.BAD_REQUEST)
            return

        credentials = admin_accounts.Credentials(
            username=username,
            password_hash=admin_accounts.hash_password(password),
            updated_at=time.time(),
        )
        try:
            admin_accounts.save_credentials(h.app.config.app_dir, credentials)
        except OSError as exc:
            h.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR,
                             f"Could not save the account: {exc}")
            return

        app.credentials = credentials
        log.info("Admin account created for %s", username)
        h.start_session(username)

    @staticmethod
    def admin_login(h, query):
        app = h.app
        client = h.client_address[0] if h.client_address else ""
        if app.credentials is None:
            h.send_api_error(HTTPStatus.CONFLICT, "No admin account exists yet.")
            return
        if not admin_auth.same_origin(h.headers, h.headers.get("Host", "")):
            h.send_api_error(HTTPStatus.FORBIDDEN, "Cross-site request refused.")
            return

        locked = app.throttle.locked_for(client)
        if locked > 0:
            h.send_json(
                {"ok": False, "errors": [f"Too many attempts. Try again in "
                                         f"{int(locked // 60) + 1} minutes."]},
                status=HTTPStatus.TOO_MANY_REQUESTS)
            return

        body = h.read_json_body()
        if body is None:
            return
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")

        # Compare both, and always run the hash, so a wrong username is not
        # measurably faster to reject than a wrong password.
        name_ok = hmac.compare_digest(username, app.credentials.username)
        password_ok = admin_accounts.verify_password(password,
                                                     app.credentials.password_hash)
        if not (name_ok and password_ok):
            app.throttle.record_failure(client)
            remaining = app.throttle.remaining_attempts(client)
            log.warning("Failed admin sign-in from %s (%d attempts left)",
                        client, remaining)
            h.send_json({"ok": False, "errors": ["Incorrect username or password."],
                         "remaining_attempts": remaining},
                        status=HTTPStatus.UNAUTHORIZED)
            return

        app.throttle.record_success(client)
        log.info("Admin signed in from %s", client)
        h.start_session(username)

    @staticmethod
    def admin_logout(h, query):
        token = admin_auth.session_token(h.headers)
        h.app.sessions.revoke(token)
        h.send_json({"ok": True}, extra_headers={"Set-Cookie": admin_auth.clear_cookie()})

    @staticmethod
    def admin_password(h, query):
        if not h.require_admin(query, write=True):
            return
        app = h.app
        body = h.read_json_body()
        if body is None:
            return

        current = str(body.get("current_password") or "")
        new_password = str(body.get("new_password") or "")
        username = str(body.get("username") or app.credentials.username).strip()

        if not admin_accounts.verify_password(current, app.credentials.password_hash):
            log.warning("Admin password change refused: current password wrong")
            h.send_json({"ok": False, "errors": ["Current password is incorrect."]},
                        status=HTTPStatus.UNAUTHORIZED)
            return

        errors = admin_accounts.check_username(username)
        errors += admin_accounts.check_password_strength(new_password)
        if errors:
            h.send_json({"ok": False, "errors": errors}, status=HTTPStatus.BAD_REQUEST)
            return

        credentials = admin_accounts.Credentials(
            username=username,
            password_hash=admin_accounts.hash_password(new_password),
            updated_at=time.time(),
        )
        try:
            admin_accounts.save_credentials(app.config.app_dir, credentials)
        except OSError as exc:
            h.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR,
                             f"Could not save the account: {exc}")
            return

        app.credentials = credentials
        # Every other session was authorised by the old password.
        app.sessions.revoke_all()
        log.info("Admin password changed; all sessions signed out")
        h.start_session(username)

    @staticmethod
    def admin_settings_get(h, query):
        if not h.require_admin(query):
            return
        config = h.app.config
        h.send_json({
            "settings": config.to_admin_dict(),
            "overrides": user_settings.load_overrides(config.app_dir),
            "content_types": list(user_settings.CONTENT_TYPES),
            "presets": list(user_settings.PRESETS),
            "restart_required_fields": list(user_settings.RESTART_REQUIRED),
            "settings_file": str(user_settings.settings_path(config.app_dir)),
            "library": h.app.library.status,
            "local_ip": get_local_ip(),
            "ffmpeg": {
                "available": h.app.tools.available,
                "can_probe": h.app.tools.can_probe,
                "ffmpeg_path": str(h.app.tools.ffmpeg or ""),
                "ffprobe_path": str(h.app.tools.ffprobe or ""),
            },
            "version": __import__("litejelly").__version__,
        })

    @staticmethod
    def admin_settings_post(h, query):
        if not h.require_admin(query, write=True):
            return
        body = h.read_json_body()
        if body is None:
            return

        clean, errors = user_settings.validate(body)
        if errors:
            h.send_json({"ok": False, "errors": errors}, status=HTTPStatus.BAD_REQUEST)
            return
        if not clean:
            h.send_json({"ok": False, "errors": ["Nothing to save"]},
                        status=HTTPStatus.BAD_REQUEST)
            return

        app_dir = h.app.config.app_dir
        existing = user_settings.load_overrides(app_dir)

        # The form posts every field, including ones the user never touched.
        # Compare the restart-required ones against what is actually running so
        # a no-op save neither persists a temporary CLI override nor claims a
        # restart is needed.
        running = {"port": h.app.config.port, "host": h.app.config.host}
        for key, current in running.items():
            if key in clean and clean[key] == current and key not in existing:
                del clean[key]
        needs_restart = user_settings.restart_required(running, clean)

        merged = dict(existing)
        for key, value in clean.items():
            if key == "transcode" and isinstance(existing.get("transcode"), dict):
                combined = dict(existing["transcode"])
                combined.update(value)
                merged["transcode"] = combined
            else:
                merged[key] = value

        try:
            user_settings.save_overrides(app_dir, merged)
        except OSError as exc:
            h.send_json({"ok": False, "errors": [f"Could not save settings: {exc}"]},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        new_config, warnings = load_config(app_dir)
        h.app.apply_config(new_config)

        h.send_json({
            "ok": True,
            "settings": h.app.config.to_admin_dict(),
            "warnings": warnings,
            "restart_required": needs_restart,
        })

    @staticmethod
    def admin_browse(h, query):
        """List subdirectories so the admin page can offer a picker."""
        if not h.require_admin(query):
            return
        raw = query.get("path", [""])[0].strip()
        if not raw:
            roots = _drive_roots()
            h.send_json({"path": "", "parent": None, "entries": roots})
            return

        target = Path(os.path.expandvars(os.path.expanduser(raw)))
        try:
            target = target.resolve(strict=True)
        except OSError:
            h.send_api_error(HTTPStatus.NOT_FOUND, "No such directory")
            return
        if not target.is_dir():
            h.send_api_error(HTTPStatus.BAD_REQUEST, "Not a directory")
            return

        entries = []
        try:
            for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                if child.name.startswith("."):
                    continue
                try:
                    if child.is_dir():
                        entries.append({"name": child.name, "path": str(child)})
                except OSError:
                    continue
        except OSError as exc:
            h.send_api_error(HTTPStatus.FORBIDDEN, f"Cannot list directory: {exc}")
            return

        parent = str(target.parent) if target.parent != target else ""
        h.send_json({"path": str(target), "parent": parent, "entries": entries})

    @staticmethod
    def admin_logs(h, query):
        if not h.require_admin(query):
            return
        config = h.app.config
        try:
            limit = int(query.get("lines", ["300"])[0])
        except (TypeError, ValueError):
            limit = 300
        limit = max(1, min(limit, 2000))
        level = query.get("level", [""])[0]

        path = log_setup.log_path(config.app_dir)
        h.send_json({
            "entries": log_setup.read_entries(config.app_dir, limit, level),
            # Name only. Whoever can reach this page knows where it installed
            # LiteJelly, so the absolute path just puts the host's layout on screen.
            "file": log_setup.LOG_FILE,
            "folder": log_setup.LOG_DIR,
            "exists": path.is_file(),
            "size": log_setup.file_size(),
            "to_file": config.log_to_file,
            "to_console": config.log_to_console,
            "verbosity": config.log_verbosity,
            "verbosity_options": list(log_setup.VERBOSITY),
            "view_levels": list(log_setup.VIEW_LEVELS),
        })

    @staticmethod
    def admin_logs_clear(h, query):
        if not h.require_admin(query, write=True):
            return
        path = log_setup.log_path(h.app.config.app_dir)
        try:
            if path.is_file():
                # Truncate rather than unlink: the handler holds it open.
                with open(path, "w", encoding="utf-8"):
                    pass
        except OSError as exc:
            h.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not clear: {exc}")
            return
        log.info("Log file cleared from the admin page")
        h.send_json({"ok": True})

    @staticmethod
    def progress_get(h, query):
        video_id = query.get("id", [""])[0]
        if video_id:
            h.send_json(h.app.progress.get(video_id) or {})
        else:
            h.send_json({"progress": h.app.progress.all()})

    @staticmethod
    def progress_post(h, query):
        body = h.read_json_body()
        if body is None:
            return
        video_id = str(body.get("id") or "")
        if not video_id or h.app.library.get(video_id) is None:
            h.send_api_error(HTTPStatus.BAD_REQUEST, "Unknown video id")
            return
        try:
            position = float(body.get("position", 0))
            duration = float(body.get("duration", 0))
        except (TypeError, ValueError):
            h.send_api_error(HTTPStatus.BAD_REQUEST, "position/duration must be numbers")
            return
        finished = body.get("finished")
        saved = h.app.progress.save(
            video_id, position, duration,
            bool(finished) if finished is not None else None,
        )
        h.send_json({"ok": True, **saved})

    @staticmethod
    def playback(h, query):
        video, path = h.app.resolve_video(query)
        if video is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        if path is None:
            h.send_api_error(HTTPStatus.GONE, "File is no longer on disk")
            return

        app = h.app
        info = app.tools.probe(path)
        quality = resolve_quality(query.get("quality", [""])[0])
        plan = app.tools.plan_playback(info, app.config.allow_hevc_direct, quality)
        tracks = discover_subtitles(path, info)

        requested_sub = query.get("sub", [""])[0]
        burn_track = next(
            (t for t in tracks if t.id == requested_sub and t.burn_in_only), None
        )

        params = {"id": video.id}
        if quality.id != "auto":
            params["quality"] = quality.id
        manual_offset = query.get("adelay", ["0"])[0]
        try:
            if float(manual_offset):
                params["adelay"] = manual_offset
        except (TypeError, ValueError):
            pass
        if burn_track is not None:
            mode, badge = "transcode", f"Burning in {burn_track.label}"
            params["sub"] = burn_track.id
            url = "/media/transcode?" + urllib.parse.urlencode(params)
        elif plan.mode == "direct":
            mode, badge = "direct", "Direct Play"
            url = "/media/stream?" + urllib.parse.urlencode(params)
        else:
            mode = plan.mode
            badge = "Remuxed" if plan.mode == "remux" else "Transcoded"
            url = "/media/transcode?" + urllib.parse.urlencode(params)

        out_width, out_height = app.tools.output_size(info, plan, app.config.transcode, quality)

        h.send_json({
            "id": video.id,
            "title": video.name,
            "filename": video.filename,
            "mode": mode,
            "badge": badge,
            "reason": plan.reason,
            "duration": info.duration,
            "width": info.width,
            "height": info.height,
            "output_width": out_width,
            "output_height": out_height,
            "video_codec": info.video_codec,
            "audio_codec": info.audio_codec,
            "video_action": plan.video_action,
            "audio_action": plan.audio_action,
            "audio_delay_ms": round(_audio_delay_ms(info, plan, query), 1),
            "reorder_delay_ms": round(info.reorder_delay * 1000.0, 1),
            "target_video_codec": app.config.transcode.video_codec,
            "target_audio_codec": app.config.transcode.audio_codec,
            "quality": quality.id,
            "qualities": [
                {"id": level.id, "label": level.label, "height": level.height}
                for level in QUALITY_LADDER
                if level.height == 0 or not info.height or level.height <= info.height
            ],
            # Direct play seeks via byte ranges; piped output needs a restart.
            "native_seek": mode == "direct",
            # A re-encode can start anywhere; a stream copy snaps to a keyframe.
            "exact_seek": plan.video_action == "encode",
            "url": url,
            "subtitles": [t.to_dict() for t in tracks],
            "resume": app.progress.get(video.id) or {},
            "next_id": (following.id if (following := next_episode(app.library.videos, video))
                        else ""),
            "prev_id": (earlier.id if (earlier := previous_episode(app.library.videos, video))
                        else ""),
            "skip_segments": _skip_segments(app, video, path, info.duration),
        })

    @staticmethod
    def seekpoint(h, query):
        """Where a restarted stream will really begin for a given seek target."""
        app = h.app
        video, path = app.resolve_video(query)
        if path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        try:
            target = max(0.0, float(query.get("t", ["0"])[0]))
        except (TypeError, ValueError):
            target = 0.0

        info = app.tools.probe(path)
        quality = resolve_quality(query.get("quality", [""])[0])
        plan = app.tools.plan_playback(info, app.config.allow_hevc_direct, quality)

        # Re-encoding can start anywhere; a stream copy snaps to a keyframe.
        start = target
        if plan.video_action == "copy" and target > 0:
            start = app.tools.seek_landing(path, target)
        h.send_json({"requested": target, "start": start, "exact": plan.video_action != "copy"})

    @staticmethod
    def stream(h, query):
        video, path = h.app.resolve_video(query)
        if path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        h.serve_file_range(path)

    @staticmethod
    def transcode(h, query):
        app = h.app
        if not app.tools.available:
            h.send_api_error(HTTPStatus.SERVICE_UNAVAILABLE, "ffmpeg is not available")
            return

        video, path = app.resolve_video(query)
        if path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return

        try:
            start = max(0.0, float(query.get("ss", ["0"])[0]))
        except (TypeError, ValueError):
            start = 0.0

        info = app.tools.probe(path)
        quality = resolve_quality(query.get("quality", [""])[0])
        plan = app.tools.plan_playback(info, app.config.allow_hevc_direct, quality)

        burn_index = None
        requested_sub = query.get("sub", [""])[0]
        if requested_sub.startswith("emb:"):
            tracks = discover_subtitles(path, info)
            track = next((t for t in tracks if t.id == requested_sub), None)
            if track is not None and track.burn_in_only:
                burn_index = int(requested_sub.split(":")[1])

        cmd = app.tools.build_stream_command(
            path, plan, app.config.transcode, start=start,
            burn_subtitle_index=burn_index, quality=quality,
            audio_delay_ms=_audio_delay_ms(info, plan, query),
        )
        h.pump_process(cmd, label=f"{video.name} @ {start:.0f}s ({plan.mode}/{quality.id})")

    @staticmethod
    def thumbnail(h, query):
        video, path = h.app.resolve_video(query)
        if path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return

        thumb = h.app.thumbnails.cached(path)
        if thumb is not None:
            h.serve_static_file(thumb, cache_control="public, max-age=604800",
                                content_type="image/jpeg")
            return

        # Not ready: queue it and answer at once. Blocking here would pin one of
        # the few connections a TV browser has while ffmpeg works.
        if h.app.thumbnails.request(path):
            h.send_json({"status": "generating"}, status=HTTPStatus.ACCEPTED)
        else:
            h.send_api_error(HTTPStatus.NOT_FOUND, "No thumbnail available")

    @staticmethod
    def artwork(h, query):
        """Serve a poster or backdrop found next to the media."""
        video, path = h.app.resolve_video(query)
        if video is None or path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return

        kind = query.get("kind", ["poster"])[0]
        source = video.backdrop_path if kind == "backdrop" else video.poster_path
        if not source:
            h.send_api_error(HTTPStatus.NOT_FOUND, "No artwork")
            return

        # The path came from a scan, but a symlinked image could still point
        # outside the library, so check containment rather than trusting it.
        # Downloaded artwork lives in the cache, which is equally trusted.
        target = Path(source)
        roots = [h.app.config.cache_dir]
        media_root = h.app.media_root(video)
        if media_root is not None:
            roots.append(media_root)
        if not any(is_within(root, target) for root in roots) or not target.is_file():
            log.warning("Refusing artwork outside the media and cache folders: %s",
                        source)
            h.send_api_error(HTTPStatus.NOT_FOUND, "No artwork")
            return

        h.serve_static_file(target, cache_control="public, max-age=604800")

    @staticmethod
    def details(h, query):
        """Everything about one item that is too bulky for the library list."""
        video, _path = h.app.resolve_video(query)
        if video is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        h.send_json({
            "id": video.id,
            "metadata": video.meta or {},
            "has_poster": bool(video.poster_path),
            "has_backdrop": bool(video.backdrop_path),
        })

    @staticmethod
    def subtitle(h, query):
        video, path = h.app.resolve_video(query)
        if path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        track_id = query.get("track", [""])[0]
        try:
            offset = max(0.0, float(query.get("offset", ["0"])[0]))
        except (TypeError, ValueError):
            offset = 0.0

        vtt = h.app.subtitles.get_vtt(path, track_id, offset)
        if vtt is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Subtitle track unavailable")
            return
        h.send_bytes(
            vtt.encode("utf-8"),
            content_type="text/vtt; charset=utf-8",
            cache_control="public, max-age=3600",
        )


class ReadAhead:
    """Drains an ffmpeg pipe in a thread so it can run ahead of the socket.

    Without this, ffmpeg blocks the moment the client stops pulling, so there
    is no reserve to cover a network dip or to refill quickly after a seek.
    """

    def __init__(self, stream, capacity: int, chunk: int = CHUNK_SIZE):
        self._stream = stream
        self._capacity = max(chunk * 2, capacity)
        self._chunk = chunk
        self._queue: collections.deque = collections.deque()
        self._size = 0
        self._eof = False
        self._stopped = False
        self._lock = threading.Lock()
        self._not_full = threading.Condition(self._lock)
        self._not_empty = threading.Condition(self._lock)
        self._thread = threading.Thread(target=self._fill, name="read-ahead", daemon=True)
        self._thread.start()

    @property
    def buffered(self) -> int:
        with self._lock:
            return self._size

    def _fill(self) -> None:
        try:
            while True:
                data = self._stream.read(self._chunk)
                if not data:
                    break
                with self._lock:
                    while (self._size + len(data) > self._capacity
                           and not self._stopped):
                        self._not_full.wait(0.5)
                    if self._stopped:
                        return
                    self._queue.append(data)
                    self._size += len(data)
                    self._not_empty.notify()
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                self._eof = True
                self._not_empty.notify_all()

    def read(self, timeout: float = 30.0) -> bytes:
        with self._lock:
            while not self._queue and not self._eof and not self._stopped:
                if not self._not_empty.wait(timeout):
                    return b""
            if not self._queue:
                return b""
            data = self._queue.popleft()
            self._size -= len(data)
            self._not_full.notify()
            return data

    def close(self) -> None:
        with self._lock:
            self._stopped = True
            self._queue.clear()
            self._size = 0
            self._not_full.notify_all()
            self._not_empty.notify_all()


class RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LiteJelly"
    sys_version = ""

    @property
    def app(self) -> Application:
        return self.server.app

    # -- logging ----------------------------------------------------------
    def log_message(self, fmt, *args):
        # The log viewer polls; logging its own requests would bury everything
        # else in the file it is displaying.
        if getattr(self, "path", "").startswith(
                ("/static/", "/api/thumbnail", "/api/admin/logs")):
            return
        log.info("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- dispatch ---------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query)
            self._head_only = method == "HEAD"
            self._body_read = method not in ("POST", "PUT", "PATCH")

            lookup = "GET" if method == "HEAD" else method
            handler = self.app.routes.get((lookup, path))
            if handler is not None:
                handler(self, query)
                return

            if lookup == "GET" and path.startswith("/static/"):
                self.serve_static_asset(path)
                return

            self.send_api_error(HTTPStatus.NOT_FOUND, "Not found")
        except DISCONNECT_ERRORS:
            self.close_connection = True
        except Exception as exc:
            log.exception("Unhandled error for %s %s", method, self.path)
            try:
                self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            except Exception:
                self.close_connection = True

    # -- response helpers -------------------------------------------------
    def _begin(self, status, headers: dict):
        # Any part of the body the handler did not read is still queued on the
        # socket; with keep-alive the next read would treat it as a request
        # line. Drain it before replying, whatever the outcome.
        self.discard_body()
        self.send_response(status)
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in headers.items():
            if value is not None:
                self.send_header(key, str(value))
        self.end_headers()

    def send_bytes(self, payload: bytes, content_type: str,
                   status=HTTPStatus.OK, cache_control: str = "no-store",
                   extra: dict | None = None):
        headers = {
            "Content-Type": content_type,
            "Content-Length": len(payload),
            "Cache-Control": cache_control,
        }
        headers.update(extra or {})
        self._begin(status, headers)
        if not getattr(self, "_head_only", False):
            self._write(payload)

    def send_json(self, data, status=HTTPStatus.OK, extra_headers: dict | None = None):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_bytes(payload, "application/json; charset=utf-8", status,
                        extra=extra_headers)

    def send_api_error(self, status, message: str):
        self.send_json({"error": message, "status": int(status)}, status=status)

    def discard_body(self) -> None:
        """Consume an unread request body so the connection stays in sync."""
        if getattr(self, "_body_read", True):
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            # Too big to drain cheaply, or unknown: drop the connection instead.
            self.close_connection = True
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, CHUNK_SIZE))
            if not chunk:
                self.close_connection = True
                return
            remaining -= len(chunk)

    # -- admin guard ------------------------------------------------------
    def require_admin(self, query, write: bool = False) -> bool:
        """Gate every admin route. Sends the error response when denied."""
        app = self.app
        if app.credentials is None:
            self.send_api_error(HTTPStatus.UNAUTHORIZED,
                                "Admin setup has not been completed.")
            return False

        username = app.sessions.validate(admin_auth.session_token(self.headers))
        if username is None:
            self.send_api_error(HTTPStatus.UNAUTHORIZED, "Sign in to continue.")
            return False
        if write and not admin_auth.same_origin(self.headers, self.headers.get("Host", "")):
            self.send_api_error(HTTPStatus.FORBIDDEN, "Cross-site admin request refused.")
            return False
        return True

    def start_session(self, username: str) -> None:
        token = self.app.sessions.create(username)
        cookie = admin_auth.build_cookie(token, int(self.app.sessions.lifetime))
        self.send_json({"ok": True, "username": username},
                       extra_headers={"Set-Cookie": cookie})

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_api_error(HTTPStatus.BAD_REQUEST, "Missing or oversized body")
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._body_read = True
            self.send_api_error(HTTPStatus.BAD_REQUEST, "Body must be valid JSON")
            return None
        self._body_read = True
        if not isinstance(body, dict):
            self.send_api_error(HTTPStatus.BAD_REQUEST, "Body must be a JSON object")
            return None
        return body

    def _write(self, data: bytes) -> bool:
        try:
            self.wfile.write(data)
            return True
        except DISCONNECT_ERRORS:
            self.close_connection = True
            return False
        except OSError:
            self.close_connection = True
            return False

    # -- static files -----------------------------------------------------
    def serve_static_asset(self, url_path: str):
        relative = url_path[len("/static/"):]
        from .paths import resolve_within

        target = resolve_within(self.app.static_dir, relative)
        if target is None or not target.is_file():
            self.send_api_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        self.serve_static_file(target, cache_control="no-cache")

    def serve_static_file(self, path: Path, cache_control: str = "no-cache",
                          content_type: str | None = None):
        try:
            stat = path.stat()
            payload = path.read_bytes()
        except OSError:
            self.send_api_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self._begin(HTTPStatus.NOT_MODIFIED, {"ETag": etag, "Cache-Control": cache_control})
            return

        if content_type is None:
            content_type, _ = mimetypes.guess_type(str(path))
            content_type = content_type or "application/octet-stream"
            if content_type.startswith("text/") or "javascript" in content_type or "json" in content_type:
                content_type += "; charset=utf-8"

        self.send_bytes(
            payload, content_type,
            cache_control=cache_control,
            extra={
                "ETag": etag,
                "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
            },
        )

    # -- byte-range streaming ---------------------------------------------
    def serve_file_range(self, path: Path):
        try:
            stat = path.stat()
        except OSError:
            self.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        file_size = stat.st_size

        content_type, _ = mimetypes.guess_type(str(path))
        if not content_type or not content_type.startswith("video/"):
            content_type = "video/mp4"

        parsed = parse_range(self.headers.get("Range"), file_size)
        if parsed == "invalid":
            self._begin(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, {
                "Content-Range": f"bytes */{file_size}",
                "Content-Length": 0,
            })
            return

        headers = {
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
        }
        if parsed is None:
            start, end, status = 0, file_size - 1, HTTPStatus.OK
        else:
            start, end = parsed
            status = HTTPStatus.PARTIAL_CONTENT
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

        remaining = end - start + 1
        headers["Content-Length"] = remaining
        self._begin(status, headers)

        if getattr(self, "_head_only", False):
            return

        try:
            with path.open("rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    chunk = handle.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    if not self._write(chunk):
                        return
                    remaining -= len(chunk)
        except OSError as exc:
            log.warning("Stream read error for %s: %s", path.name, exc)
            self.close_connection = True

    # -- ffmpeg pipe ------------------------------------------------------
    def pump_process(self, cmd: list[str], label: str):
        """Stream an ffmpeg process' stdout to the client, bounded by a semaphore."""
        sem = self.app.tools.transcode_sem
        if not sem.acquire(timeout=20):
            self.send_api_error(HTTPStatus.SERVICE_UNAVAILABLE,
                                "Server is busy transcoding. Try again shortly.")
            return

        process = None
        try:
            try:
                process = popen_quiet(cmd)
            except OSError as exc:
                self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"ffmpeg failed: {exc}")
                return

            log.info("Streaming %s", label)
            self.close_connection = True
            self._begin(HTTPStatus.OK, {
                "Content-Type": "video/mp4",
                "Cache-Control": "no-store",
                "Connection": "close",
                "Accept-Ranges": "none",
            })

            if getattr(self, "_head_only", False):
                return

            reader = ReadAhead(process.stdout, self.app.config.stream_buffer_bytes)
            try:
                while True:
                    chunk = reader.read()
                    if not chunk:
                        break
                    if not self._write(chunk):
                        break
                    try:
                        self.wfile.flush()
                    except (OSError, ValueError):
                        break
            finally:
                reader.close()
        finally:
            if process is not None:
                self._terminate(process)
            sem.release()
            log.info("Stream ended: %s", label)

    @staticmethod
    def _terminate(process: subprocess.Popen):
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=2)
            except OSError:
                pass
        except OSError:
            pass
        finally:
            if process.stdout:
                try:
                    process.stdout.close()
                except OSError:
                    pass


class LiteJellyHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Long media streams must not block new requests.
    request_queue_size = 64

    def __init__(self, address, handler_cls, app: Application):
        self.app = app
        super().__init__(address, handler_cls)

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, DISCONNECT_ERRORS):
            return
        log.debug("Connection error from %s: %s", client_address, exc)


def create_server(app: Application) -> LiteJellyHTTPServer:
    return LiteJellyHTTPServer((app.config.host, app.config.port), RequestHandler, app)
