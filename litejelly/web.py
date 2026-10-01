"""HTTP layer: route table, static serving, streaming and the JSON API."""

from __future__ import annotations

import base64
import binascii
import gzip
import http.server
import json
import logging
import math
import mimetypes
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
from . import playback
from . import streaming
from .admin_routes import AdminRoutes, get_local_ip
from .chapters import read_chapters, skippable
from .enrich import Enricher
from .ffmpeg import FFmpegTools
from .library import (
    Library, build_continue_watching, episode_order, next_episode,
    previous_episode,
)
from .paths import is_within
from .playback import _audio_delay_ms, _audio_index, _audio_mode, _audio_tracks
from .providers import MetadataProviders, artwork_digest
from .store import ProgressStore, UnknownProfile
from .streaming import CHUNK_SIZE, ReadAhead, parse_range
from .subtitles import (SubtitleService, discover as discover_subtitles,
                        language_from_name, save_sidecar,
                        track_id_for)
from .opensubtitles import OpenSubtitles, OpenSubtitlesError, movie_hash
from .thumbnails import ThumbnailService
from .trickplay import TrickplayService

log = logging.getLogger("litejelly.web")

# Python's table predates web fonts, so without this they would be served as
# application/octet-stream and some browsers refuse to use them.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")

MAX_BODY_BYTES = 64 * 1024
# Only the subtitle upload may be larger, and only because base64 inflates a
# 2 MB file by a third. Raising the shared limit would loosen every other route.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
# A forced rescan re-walks every media folder; this is how often that is free.
RESCAN_COOLDOWN = 30.0
MAX_HTTP_CONNECTIONS = 64
# Below this, compressing costs more than it saves on a LAN.
GZIP_MIN_BYTES = 8 * 1024


def _accepts_gzip(header: str | None) -> bool:
    """True when Accept-Encoding lists gzip without refusing it via q=0."""
    for part in (header or "").split(","):
        name, *params = [piece.strip() for piece in part.split(";")]
        if name.lower() != "gzip":
            continue
        for param in params:
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    return float(value) > 0
                except ValueError:
                    return False
        return True
    return False


def _query_seconds(handler, query, name: str) -> float | None:
    """Validate bounded finite media time before probing or starting a process."""
    from .store import MAX_SECONDS

    try:
        value = float(query.get(name, ["0"])[0])
        if not math.isfinite(value) or value > MAX_SECONDS:
            raise ValueError("Time out of range")
    except (TypeError, ValueError, OverflowError):
        handler.send_api_error(HTTPStatus.BAD_REQUEST, f"{name} must be finite media seconds")
        return None
    return max(0.0, value)


def _refuse_constant(name: str):
    """json.loads accepts Infinity and NaN; nothing here should."""
    raise ValueError(f"{name} is not a number")
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


def _build_metadata(config):
    """Online lookups are opt-in: they send your titles to a third party."""
    if not getattr(config, "online_metadata", False):
        return None, None
    providers = MetadataProviders(config.cache_dir,
                                  tmdb_key=getattr(config, "tmdb_api_key", ""),
                                  omdb_key=getattr(config, "omdb_api_key", ""))
    return providers, Enricher(providers)


def _skip_segments(app, video, path, duration: float) -> tuple[list[dict], bool]:
    """Chapters first; an online answer only when the file has none.

    Chapters belong to this exact file. A shared database describes somebody
    else's copy, which may be cut differently.

    Returns the segments and whether a lookup was queued, so the client knows
    to ask again rather than concluding this episode simply has no intro.
    """
    tools = app.tools
    segments = skippable(read_chapters(tools.ffprobe, path, runner=tools.chapter_probe), duration)
    if segments or app.metadata is None:
        return segments, False

    # Cache only: a request must never wait on a third-party service.
    cached, pending = _cached_skip(app, video, duration)
    if pending and app.enricher is not None:
        _enqueue_skip(app, video, duration)
    return cached, pending


def _cached_skip(app, video, duration: float) -> tuple[list[dict], bool]:
    """What is already on disk, and whether anything is still worth asking.

    AniSkip is tried first for anime because it is scoped to exactly that;
    TheIntroDB covers everything else, and backs AniSkip up when it has
    nothing.
    """
    meta = app.metadata
    imdb_id = (video.meta or {}).get("imdb_id") or ""
    season, episode = _skip_episode(video)
    pending = False

    if video.mal_id and video.episode:
        cached = meta.cached_skip_times(video.mal_id, video.episode, duration)
        if cached:
            return cached, False
        pending = not meta.has_looked_up_skip(video.mal_id, video.episode)

    if imdb_id:
        cached = meta.cached_intro_times(imdb_id, season, episode, duration)
        if cached:
            return cached, False
        pending = pending or not meta.has_looked_up_intro(imdb_id, season,
                                                          episode)

    return [], pending


def _enqueue_skip(app, video, duration: float) -> None:
    if video.mal_id and video.episode:
        app.enricher.enqueue_skip(video.mal_id, video.episode)
    imdb_id = (video.meta or {}).get("imdb_id") or ""
    if imdb_id:
        season, episode = _skip_episode(video)
        app.enricher.enqueue_intro(imdb_id, season, episode, duration)


def _skip_payload(segments: list[dict], pending: bool) -> dict:
    return {"skip_segments": segments, "skip_pending": bool(pending)}


def _skip_episode(video):
    """A film is looked up by id alone; an episode needs its place in the run."""
    if not video.episode:
        return None, None
    return (video.season if video.season is not None else 1), video.episode


class Application:
    """Holds every service and maps request paths to handlers."""
    def __init__(self, config):
        self.config = config
        self._config_lock = threading.RLock()
        self.progress = ProgressStore(config.db_path, config.legacy_db_path)
        self.metadata, self.enricher = _build_metadata(config)
        self.library = Library(config.media_dirs, config.scan_interval,
                       metadata=self.metadata, enricher=self.enricher,
                       identify=self.progress.identify_media)
        self.tools = FFmpegTools(
            config.app_dir,
            transcode_slots=config.transcode.max_concurrent,
            thumbnail_slots=config.thumbnail_workers,
            ffmpeg_path=config.ffmpeg_path,
            ffprobe_path=config.ffprobe_path,
        )
        self.subtitles = SubtitleService(self.tools, config.cache_dir)
        self.thumbnails = ThumbnailService(self.tools, config.cache_dir,
                                           config.thumbnail_workers)
        self.trickplay = TrickplayService(self.tools, config.cache_dir,
                                          config.trickplay, config.trickplay_interval)
        self.static_dir = config.static_dir.resolve()

        self.sessions = admin_accounts.SessionStore()
        self.throttle = admin_accounts.LoginThrottle()
        self.credentials = admin_accounts.load_credentials(config.app_dir)
        self.credentials_damaged = (self.credentials is None
                                    and admin_accounts.credentials_damaged(config.app_dir))
        if self.credentials_damaged:
            log.error("%s exists but cannot be read; admin setup stays closed until "
                      "'python server.py --reset-admin' repairs it",
                      admin_accounts.CREDENTIALS_FILE)
        self.opensubtitles = OpenSubtitles(config.app_dir)
        self._last_rescan = 0.0
        self._streams: set = set()
        self._streams_lock = threading.Lock()
        self._closing = False
        if self.enricher is not None:
            self.enricher.on_updated = lambda: self.library.request_scan(force=True)

        self.routes = {
            ("GET", "/"): Routes.index,
            ("GET", "/index.html"): Routes.index,
            ("GET", "/favicon.ico"): Routes.favicon,
            ("GET", "/admin"): AdminRoutes.admin_page,
            ("GET", "/admin/"): AdminRoutes.admin_page,
            ("GET", "/api/config"): Routes.config,
            ("GET", "/api/library"): Routes.library,
            ("POST", "/api/rescan"): Routes.rescan,
            ("GET", "/api/playback"): Routes.playback,
            ("GET", "/api/seekpoint"): Routes.seekpoint,
            ("GET", "/api/skip"): Routes.skip,
            ("GET", "/api/thumbnail"): Routes.thumbnail,
            ("GET", "/api/trickplay"): Routes.trickplay,
            ("GET", "/media/trickplay"): Routes.trickplay_sheet,
            ("GET", "/api/artwork"): Routes.artwork,
            ("GET", "/api/image"): Routes.image,
            ("GET", "/api/series"): Routes.series,
            ("GET", "/api/details"): Routes.details,
            ("GET", "/api/subtitle"): Routes.subtitle,
            ("GET", "/api/subtitles"): Routes.subtitle_list,
            ("POST", "/api/subtitles/upload"): Routes.subtitle_upload,
            ("GET", "/api/subtitles/search"): Routes.subtitle_search,
            ("POST", "/api/subtitles/fetch"): Routes.subtitle_fetch,
            ("GET", "/api/admin/opensubtitles"): AdminRoutes.admin_opensubtitles,
            ("POST", "/api/admin/opensubtitles"): AdminRoutes.admin_opensubtitles_save,
            ("POST", "/api/admin/opensubtitles/test"): AdminRoutes.admin_opensubtitles_test,
            ("GET", "/api/progress"): Routes.progress_get,
            ("POST", "/api/progress"): Routes.progress_post,
            ("GET", "/api/profiles"): Routes.profiles,
            ("POST", "/api/admin/profiles"): AdminRoutes.admin_profile_create,
            ("POST", "/api/admin/profiles/rename"): AdminRoutes.admin_profile_rename,
            ("POST", "/api/admin/profiles/delete"): AdminRoutes.admin_profile_delete,
            ("GET", "/api/admin/settings"): AdminRoutes.admin_settings_get,
            ("POST", "/api/admin/settings"): AdminRoutes.admin_settings_post,
            ("GET", "/api/admin/settings/export"): AdminRoutes.admin_settings_export,
            ("POST", "/api/admin/settings/import"): AdminRoutes.admin_settings_import,
            ("POST", "/api/admin/metadata/clear"): AdminRoutes.admin_metadata_clear,
            ("POST", "/api/admin/metadata/forget"): AdminRoutes.admin_metadata_forget,
            ("POST", "/api/admin/metadata/test"): AdminRoutes.admin_metadata_test,
            ("POST", "/api/admin/encoder/test"): AdminRoutes.admin_encoder_test,
            ("GET", "/api/admin/session"): AdminRoutes.admin_session,
            ("POST", "/api/admin/setup"): AdminRoutes.admin_setup,
            ("POST", "/api/admin/login"): AdminRoutes.admin_login,
            ("POST", "/api/admin/logout"): AdminRoutes.admin_logout,
            ("POST", "/api/admin/password"): AdminRoutes.admin_password,
            ("GET", "/api/admin/browse"): AdminRoutes.admin_browse,
            ("GET", "/api/admin/logs"): AdminRoutes.admin_logs,
            ("POST", "/api/admin/logs/clear"): AdminRoutes.admin_logs_clear,
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
            if self._closing:
                raise RuntimeError("Server is shutting down")
            # Build everything first, so a failure leaves the running services untouched.
            built = []
            try:
                tools = FFmpegTools(
                    new_config.app_dir,
                    transcode_slots=new_config.transcode.max_concurrent,
                    thumbnail_slots=new_config.thumbnail_workers,
                    ffmpeg_path=new_config.ffmpeg_path,
                    ffprobe_path=new_config.ffprobe_path,
                )
                built.append(tools.close)
                metadata, enricher = _build_metadata(new_config)
                if enricher is not None:
                    built.append(enricher.stop)
                    enricher.on_updated = lambda: self.library.request_scan(force=True)
                subtitles = SubtitleService(tools, new_config.cache_dir)
                thumbnails = ThumbnailService(tools, new_config.cache_dir,
                                              new_config.thumbnail_workers)
                built.append(thumbnails.close)
                trickplay = TrickplayService(tools, new_config.cache_dir,
                                             new_config.trickplay,
                                             new_config.trickplay_interval)
                built.append(trickplay.close)
                static_dir = new_config.static_dir.resolve()
            except BaseException:
                for close in reversed(built):
                    close()
                raise

            old_tools = self.tools
            old_thumbnails = self.thumbnails
            old_trickplay = self.trickplay
            old_enricher = self.enricher
            tools.transcode_sem = old_tools.transcode_sem
            tools.transcode_sem.resize(new_config.transcode.max_concurrent)
            self.config = new_config
            self.tools = tools
            self.metadata = metadata
            self.enricher = enricher
            self.subtitles = subtitles
            self.thumbnails = thumbnails
            self.trickplay = trickplay
            self.static_dir = static_dir
            self.library.metadata = metadata
            self.library.enricher = enricher
            old_thumbnails.close()
            old_trickplay.close()
            if old_enricher is not None:
                old_enricher.stop()
            old_tools.close()
            self.library.set_media_dirs(new_config.media_dirs, new_config.scan_interval)

        # Verbosity applies to the live handlers; the file settings only take
        # effect on restart because reopening the file would lose buffered lines.
        log_setup.set_verbosity(new_config.log_verbosity, new_config.log_to_console)

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
        return self.library.root_for(video)

    def rescan_cooldown_remaining(self) -> float:
        return max(0.0, RESCAN_COOLDOWN - (time.monotonic() - self._last_rescan))

    def note_rescan(self) -> None:
        self._last_rescan = time.monotonic()

    def track_stream(self, process) -> bool:
        """Register a spawned stream unless shutdown has already taken ownership."""
        with self._streams_lock:
            if self._closing:
                return False
            self._streams.add(process)
            return True

    def forget_stream(self, process) -> None:
        with self._streams_lock:
            self._streams.discard(process)

    def shutdown(self) -> None:
        """Reject new streams before cancelling background work and closing storage."""
        with self._config_lock, self._streams_lock:
            if self._closing:
                return
            self._closing = True
            live = list(self._streams)
            self._streams.clear()
        self.tools.transcode_sem.close()
        for process in live:
            streaming.terminate_process(process)
        self.tools.close()
        self.library.stop()
        if self.enricher is not None:
            self.enricher.stop()
        self.thumbnails.close()
        self.trickplay.close()
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
    def _profile(h, value) -> int | None:
        """The viewer a request is for; answers 404 itself when that profile is gone."""
        profile = h.app.progress.resolve_profile(value)
        if profile is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Unknown profile")
        return profile

    @staticmethod
    def profiles(h, query):
        store = h.app.progress
        h.send_json({"profiles": store.list_profiles(), "default": store.default_profile()})

    @staticmethod
    def library(h, query):
        profile = Routes._profile(h, query.get("profile", [""])[0])
        if profile is None:
            return
        entries = h.app.library.videos
        progress = h.app.progress.all(profile)
        h.send_json({
            "videos": h.app.library.listing(),
            "progress": progress,
            "continue_watching": build_continue_watching(entries, progress),
            "status": h.app.library.status,
            "ffmpeg_available": h.app.tools.available,
        })

    @staticmethod
    def rescan(h, query):
        if h.cross_site_write():
            return
        app = h.app
        # A forced rescan walks every media folder, so it cannot be free to
        # ask for: without this one page could keep the disk busy for ever.
        wait = app.rescan_cooldown_remaining()
        if wait > 0 and not h.is_admin():
            h.send_api_error(HTTPStatus.TOO_MANY_REQUESTS,
                             f"A rescan just ran. Try again in {wait:.0f}s.",
                             extra={"Retry-After": str(int(wait) + 1)})
            return
        # Forced: the button says rescan, so it should not quietly do nothing
        # when the folder fingerprint happens to be unchanged.
        app.note_rescan()
        app.library.request_scan(force=True)
        h.send_json({"ok": True, "status": app.library.status})

    @staticmethod
    def progress_get(h, query):
        profile = Routes._profile(h, query.get("profile", [""])[0])
        if profile is None:
            return
        video_id = query.get("id", [""])[0]
        if video_id:
            h.send_json(h.app.progress.get(video_id, profile) or {})
        else:
            h.send_json({"progress": h.app.progress.all(profile)})

    @staticmethod
    def progress_post(h, query):
        if h.cross_site_write():
            return
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
        if not (math.isfinite(position) and math.isfinite(duration)):
            h.send_api_error(HTTPStatus.BAD_REQUEST, "position/duration must be finite")
            return
        finished = body.get("finished")
        profile = Routes._profile(h, body.get("profile"))
        if profile is None:
            return
        saved = h.app.progress.save(
            video_id, position, duration,
            bool(finished) if finished is not None else None,
            profile=profile,
        )
        h.send_json({"ok": True, **saved})

    @staticmethod
    def playback(h, query):
        profile = Routes._profile(h, query.get("profile", [""])[0])
        if profile is None:
            return
        video, path = h.app.resolve_video(query)
        if video is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        if path is None:
            h.send_api_error(HTTPStatus.GONE, "File is no longer on disk")
            return

        app = h.app
        payload = playback.build_payload(app.tools, app.config, video, path, query)
        h.send_json({
            **payload,
            "resume": app.progress.get(video.id, profile) or {},
            "next_id": (following.id if (following := next_episode(app.library.videos, video))
                        else ""),
            "prev_id": (earlier.id if (earlier := previous_episode(app.library.videos, video))
                        else ""),
            **_skip_payload(*_skip_segments(app, video, path, payload["duration"])),
        })

    @staticmethod
    def skip(h, query):
        """Re-ask for skip times once a queued lookup has had time to land.

        Playback must not wait on a third party, so the first answer can be
        empty while the lookup is still in flight.
        """
        app = h.app
        video, path = app.resolve_video(query)
        if path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        duration = app.tools.probe(path).duration
        if app.metadata is None:
            h.send_json(_skip_payload([], False))
            return
        h.send_json(_skip_payload(*_cached_skip(app, video, duration)))

    @staticmethod
    def seekpoint(h, query):
        """Where a restarted stream will really begin for a given seek target."""
        app = h.app
        video, path = app.resolve_video(query)
        if path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        target = _query_seconds(h, query, "t")
        if target is None:
            return

        h.send_json(playback.seek_payload(app.tools, app.config, path, query, target))

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

        start = _query_seconds(h, query, "ss")
        if start is None:
            return

        cmd, label = playback.build_stream(app.tools, app.config, video, path, query, start)
        h.pump_process(cmd, label=label)

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
    def trickplay(h, query):
        """Where the scrub previews for a video are, building one if needed."""
        app = h.app
        video, path = app.resolve_video(query)
        if path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        if not app.trickplay.enabled:
            h.send_json({"status": "off"})
            return

        sheet = app.trickplay.cached(path)
        if sheet is not None:
            payload = sheet.to_dict()
            # The key is in the URL, so a rebuilt sheet is a different address
            # and the old one can be cached hard.
            payload["url"] = ("/media/trickplay?"
                              + urllib.parse.urlencode({"id": video.id, "k": sheet.key}))
            payload["status"] = "ready"
            h.send_json(payload)
            return

        duration = app.tools.probe(path).duration
        queued = app.trickplay.request(path, duration)
        h.send_json({"status": "building" if queued else "unavailable"},
                    status=HTTPStatus.ACCEPTED if queued else HTTPStatus.OK)

    @staticmethod
    def trickplay_sheet(h, query):
        app = h.app
        video, path = app.resolve_video(query)
        if path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        requested = query.get("k", [""])[0]
        # Recomputed rather than trusted: the key names a file in the cache.
        if requested != app.trickplay.key_for(path):
            h.send_api_error(HTTPStatus.NOT_FOUND, "No preview sheet")
            return
        target = app.trickplay.sheet_path(requested)
        if target is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "No preview sheet")
            return
        h.serve_static_file(target, cache_control="public, max-age=604800",
                            content_type="image/jpeg")

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
    def series(h, query):
        """Everything the series page needs, in one request.

        Fetching a plot per episode would mean one request per row, so the
        whole season's text, the cast and the artwork all come back together.
        """
        app = h.app
        profile = Routes._profile(h, query.get("profile", [""])[0])
        if profile is None:
            return
        series_id = query.get("id", [""])[0]
        episodes = [v for v in app.library.videos if v.series_id == series_id]
        if not episodes:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Series not found")
            return

        episodes.sort(key=episode_order)
        first = episodes[0]
        meta = first.meta or {}

        info = None
        if app.metadata is not None:
            info = app.metadata.cached_series(first.title,
                                              first.category == "anime")

        cast = []
        for member in (info.cast if info else []):
            digest = artwork_digest(member.get("image") or "")
            if member.get("image") and app.metadata.cached_image(digest) is None:
                digest = ""
            cast.append({"name": member.get("name", ""),
                         "character": member.get("character", ""),
                         "image": digest})

        progress = app.progress.all(profile)
        h.send_json({
            "id": series_id,
            "title": (info.title if info and info.title else first.title),
            "category": first.category,
            "plot": meta.get("series_plot") or meta.get("plot") or "",
            "genres": meta.get("genres") or (info.genres if info else []),
            "rating": first.rating,
            "imdb_rating": first.imdb_rating,
            # An episode carries no year of its own; the show does.
            "year": first.year or (info.year if info else None),
            "poster_id": first.id if first.has_poster else "",
            "backdrop_id": next((v.id for v in episodes if v.backdrop_path), ""),
            "cast": cast,
            "episodes": [Routes._episode_row(video, progress.get(video.id) or {})
                         for video in episodes],
        })

    @staticmethod
    def _episode_row(video, progress):
        meta = video.meta or {}
        return {
            "id": video.id,
            "season": video.season,
            "episode": video.episode,
            "title": video.episode_title or "",
            "plot": meta.get("plot") or "",
            "aired": meta.get("aired") or "",
            "runtime": meta.get("runtime"),
            "rating": video.rating,
            "position": progress.get("position") or 0,
            "duration": progress.get("duration") or 0,
            "finished": bool(progress.get("finished")),
        }

    @staticmethod
    def image(h, query):
        """Serve a downloaded image by digest, for cast portraits."""
        app = h.app
        if app.metadata is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "No artwork")
            return
        target = app.metadata.cached_image(query.get("h", [""])[0])
        if target is None:
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
        offset = _query_seconds(h, query, "offset")
        if offset is None:
            return

        vtt = h.app.subtitles.get_vtt(path, track_id, offset)
        if vtt is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Subtitle track unavailable")
            return
        h.send_bytes(
            vtt.encode("utf-8"),
            content_type="text/vtt; charset=utf-8",
            cache_control="no-cache",
        )

    @staticmethod
    def subtitle_list(h, query):
        """The tracks available now, so adding one need not restart the stream."""
        video, path = h.app.resolve_video(query)
        if path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        tracks = discover_subtitles(path, h.app.tools.probe(path))
        h.send_json({"id": video.id, "subtitles": [t.to_dict() for t in tracks]})

    @staticmethod
    def subtitle_upload(h, query):
        """Save a subtitle file sent from the player, beside its video.

        This writes into a media folder, so it takes an admin session rather
        than the lighter Origin check the progress route uses. The name is
        built from the video's own path and a language tag matched against a
        short pattern, so nothing the client sends reaches the filesystem.
        """
        if not h.require_admin(query, write=True):
            return
        body = h.read_json_body(limit=MAX_UPLOAD_BYTES)
        if body is None:
            return

        video, path = h.app.resolve_video({"id": [str(body.get("id") or "")]})
        if path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return

        try:
            data = base64.b64decode(str(body.get("data") or ""), validate=True)
        except (ValueError, binascii.Error):
            h.send_api_error(HTTPStatus.BAD_REQUEST, "That upload was not readable")
            return

        name = str(body.get("name") or "")
        language = str(body.get("language") or "") or language_from_name(name)
        try:
            saved = save_sidecar(path, data, language)
        except ValueError as exc:
            h.send_api_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except (OSError, FileExistsError) as exc:
            log.warning("Could not save a subtitle for %s: %s", video.id, exc)
            h.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR,
                             "Could not write to that folder")
            return

        log.info("Subtitle added for %s: %s", video.id, saved.name)
        tracks = discover_subtitles(path, h.app.tools.probe(path))
        h.send_json({
            "ok": True,
            "id": video.id,
            "track": track_id_for(path, saved),
            "subtitles": [t.to_dict() for t in tracks],
        })

    @staticmethod
    def subtitle_search(h, query):
        """Ask OpenSubtitles what it has for this file.

        Admin-only like the upload, because the result of picking one is a
        file written into a media folder, and because searching spends the
        account's own rate limit.
        """
        if not h.require_admin(query, write=False):
            return
        video, path = h.app.resolve_video(query)
        if video is None or path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return

        language = (query.get("language", ["en"])[0] or "en").strip().lower()[:8]
        text = (query.get("q", [""])[0] or "").strip()[:200]
        if not text:
            text = video.title or video.name

        try:
            found = h.app.opensubtitles.search(
                text, language, movie_hash(path),
                season=video.season, episode=video.episode)
        except OpenSubtitlesError as exc:
            h.send_json({"ok": False, "error": str(exc)},
                        status=HTTPStatus.BAD_GATEWAY)
            return
        h.send_json({"ok": True, "query": text, "language": language,
                     "results": [c.to_dict() for c in found]})

    @staticmethod
    def subtitle_fetch(h, query):
        """Download one of those results and keep it beside the video."""
        if not h.require_admin(query, write=True):
            return
        body = h.read_json_body()
        if body is None:
            return
        video, path = h.app.resolve_video({"id": [str(body.get("id") or "")]})
        if path is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
            return
        try:
            file_id = int(body.get("file_id"))
        except (TypeError, ValueError):
            h.send_api_error(HTTPStatus.BAD_REQUEST, "Which subtitle?")
            return

        try:
            data, remote_name = h.app.opensubtitles.download(file_id)
        except OpenSubtitlesError as exc:
            h.send_json({"ok": False, "error": str(exc)},
                        status=HTTPStatus.BAD_GATEWAY)
            return

        # Nothing downloaded is trusted any more than something uploaded: it
        # goes through the same check before it reaches the folder.
        language = str(body.get("language") or "") or language_from_name(remote_name)
        try:
            saved = save_sidecar(path, data, language)
        except ValueError as exc:
            h.send_api_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except (OSError, FileExistsError) as exc:
            log.warning("Could not save a subtitle for %s: %s", video.id, exc)
            h.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR,
                             "Could not write to that folder")
            return

        log.info("Subtitle fetched for %s: %s", video.id, saved.name)
        tracks = discover_subtitles(path, h.app.tools.probe(path))
        h.send_json({
            "ok": True,
            "id": video.id,
            "track": track_id_for(path, saved),
            "subtitles": [t.to_dict() for t in tracks],
        })


class RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15
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
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                self._body_read = True
                self.close_connection = True
                self.send_api_error(HTTPStatus.BAD_REQUEST, "Unsupported request framing")
                return

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
        except UnknownProfile:
            # Deleted between resolving the request and using it.
            self.send_api_error(HTTPStatus.NOT_FOUND, "Unknown profile")
        except Exception as exc:
            log.exception("Unhandled error for %s %s", method, self.path)
            try:
                self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal server error")
            except Exception:
                self.close_connection = True

    # -- response helpers -------------------------------------------------
    def begin_response(self, status, headers: dict):
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
        self.begin_response(status, headers)
        if not getattr(self, "_head_only", False):
            self.write_body(payload)

    def send_json(self, data, status=HTTPStatus.OK, extra_headers: dict | None = None):
        # allow_nan would emit bare Infinity, which no browser's JSON.parse
        # accepts, so one poisoned value would break the whole response.
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        extra = dict(extra_headers or {})
        # Measured: a 10,000-file library is 5.4 MB of JSON and 440 KB gzipped at level 1.
        if len(payload) >= GZIP_MIN_BYTES:
            extra["Vary"] = "Accept-Encoding"
            if _accepts_gzip(self.headers.get("Accept-Encoding")):
                payload = gzip.compress(payload, compresslevel=1)
                extra["Content-Encoding"] = "gzip"
        self.send_bytes(payload, "application/json; charset=utf-8", status, extra=extra)

    def send_api_error(self, status, message: str, extra: dict | None = None):
        self.send_json({"error": message, "status": int(status)}, status=status,
                       extra_headers=extra)

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

    # -- write guards -----------------------------------------------------
    def cross_site_write(self) -> bool:
        """Refuse a write a browser says came from another page.

        These routes take no account, so any site you happen to visit could
        otherwise post to the server on your network.
        """
        if admin_auth.foreign_origin(self.headers, self.headers.get("Host", "")):
            self.send_api_error(HTTPStatus.FORBIDDEN, "Cross-site request refused.")
            return True
        return False

    def is_admin(self) -> bool:
        return self.app.sessions.validate(admin_auth.session_token(self.headers)) is not None

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

    def read_json_body(self, limit: int = MAX_BODY_BYTES):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            self.send_api_error(HTTPStatus.BAD_REQUEST, "Missing or oversized body")
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"),
                              parse_constant=_refuse_constant)
        except (ValueError, UnicodeDecodeError):
            self._body_read = True
            self.send_api_error(HTTPStatus.BAD_REQUEST, "Body must be valid JSON")
            return None
        self._body_read = True
        if not isinstance(body, dict):
            self.send_api_error(HTTPStatus.BAD_REQUEST, "Body must be a JSON object")
            return None
        return body

    def write_body(self, data: bytes) -> bool:
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
        # A font is nearly half a megabyte and never changes; revalidating it
        # on every page load is pure cost. Markup and code stay uncached so an
        # edit shows up on the next reload.
        cache = ("public, max-age=31536000, immutable"
                 if target.suffix.lower() in (".woff2", ".woff") else "no-cache")
        self.serve_static_file(target, cache_control=cache)

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
            self.begin_response(HTTPStatus.NOT_MODIFIED, {"ETag": etag, "Cache-Control": cache_control})
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
        streaming.serve_file_range(self, path)

    # -- ffmpeg pipe ------------------------------------------------------
    def pump_process(self, cmd: list[str], label: str):
        """Stream an ffmpeg process' stdout to the client, bounded by a semaphore."""
        streaming.pump_process(self, cmd, label)


class LiteJellyHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Long media streams must not block new requests.
    request_queue_size = 64

    def __init__(self, address, handler_cls, app: Application):
        self.app = app
        self._request_slots = threading.BoundedSemaphore(MAX_HTTP_CONNECTIONS)
        super().__init__(address, handler_cls)

    def process_request(self, request, client_address):
        """Bound active connection threads and fail fast when every slot is occupied."""
        if not self._request_slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                body = b'{"error": "Server is busy", "status": 503}'
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                                b"Content-Type: application/json; charset=utf-8\r\n"
                                b"Content-Length: " + str(len(body)).encode("ascii")
                                + b"\r\nRetry-After: 1\r\n\r\n" + body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        """Release capacity even when request handling or connection cleanup fails."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, DISCONNECT_ERRORS):
            return
        log.debug("Connection error from %s: %s", client_address, exc)


def create_server(app: Application) -> LiteJellyHTTPServer:
    return LiteJellyHTTPServer((app.config.host, app.config.port), RequestHandler, app)
