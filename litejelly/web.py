"""HTTP layer: route table, static serving, streaming and the JSON API."""

from __future__ import annotations

import collections
import http.server
import json
import logging
import mimetypes
import socket
import socketserver
import subprocess
import sys
import threading
import urllib.parse
from email.utils import formatdate
from http import HTTPStatus
from pathlib import Path

from .ffmpeg import QUALITY_LADDER, FFmpegTools, popen_quiet, resolve_quality
from .library import Library
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
    """Reorder compensation for copied video, plus any manual trim."""
    delay = info.reorder_delay * 1000.0 if plan.video_action == "copy" else 0.0
    try:
        delay += float(query.get("adelay", ["0"])[0])
    except (TypeError, ValueError):
        pass
    return max(-5000.0, min(5000.0, delay))


class Application:
    """Holds every service and maps request paths to handlers."""

    def __init__(self, config):
        self.config = config
        self.library = Library(config.media_dirs, config.scan_interval)
        self.tools = FFmpegTools(
            config.app_dir,
            transcode_slots=config.transcode.max_concurrent,
            thumbnail_slots=config.thumbnail_workers,
            ffmpeg_path=config.ffmpeg_path,
            ffprobe_path=config.ffprobe_path,
        )
        self.progress = ProgressStore(config.db_path)
        self.subtitles = SubtitleService(self.tools, config.cache_dir)
        self.thumbnails = ThumbnailService(self.tools, config.cache_dir)
        self.static_dir = config.static_dir.resolve()

        self.routes = {
            ("GET", "/"): Routes.index,
            ("GET", "/index.html"): Routes.index,
            ("GET", "/favicon.ico"): Routes.favicon,
            ("GET", "/api/config"): Routes.config,
            ("GET", "/api/library"): Routes.library,
            ("POST", "/api/rescan"): Routes.rescan,
            ("GET", "/api/playback"): Routes.playback,
            ("GET", "/api/seekpoint"): Routes.seekpoint,
            ("GET", "/api/thumbnail"): Routes.thumbnail,
            ("GET", "/api/subtitle"): Routes.subtitle,
            ("GET", "/api/progress"): Routes.progress_get,
            ("POST", "/api/progress"): Routes.progress_post,
            ("GET", "/media/stream"): Routes.stream,
            ("GET", "/media/transcode"): Routes.transcode,
        }

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

    def shutdown(self) -> None:
        self.library.stop()
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
        videos = [v.to_dict() for v in h.app.library.videos]
        h.send_json({
            "videos": videos,
            "progress": h.app.progress.all(),
            "status": h.app.library.status,
            "ffmpeg_available": h.app.tools.available,
        })

    @staticmethod
    def rescan(h, query):
        h.app.library.request_scan()
        h.send_json({"ok": True, "status": h.app.library.status})

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

        # Stream copies snap seeks to keyframes, so index them up front.
        if plan.video_action == "copy" and mode != "direct":
            app.tools.ensure_keyframes(path)

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
            start = app.tools.keyframe_before(path, target)
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
        thumb = h.app.thumbnails.get(path)
        if thumb is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "No thumbnail available")
            return
        h.serve_static_file(thumb, cache_control="public, max-age=604800", content_type="image/jpeg")

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
        if getattr(self, "path", "").startswith(("/static/", "/api/thumbnail")):
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

    def send_json(self, data, status=HTTPStatus.OK):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_bytes(payload, "application/json; charset=utf-8", status)

    def send_api_error(self, status, message: str):
        self.send_json({"error": message, "status": int(status)}, status=status)

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
            self.send_api_error(HTTPStatus.BAD_REQUEST, "Body must be valid JSON")
            return None
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
