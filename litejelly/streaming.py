"""Per-request byte ranges, buffered process output and stream cleanup."""

from __future__ import annotations

import collections
import logging
import mimetypes
import subprocess
import threading
from email.utils import formatdate
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

from .ffmpeg import popen_quiet

if TYPE_CHECKING:
    from .web import RequestHandler

log = logging.getLogger("litejelly.streaming")
CHUNK_SIZE = 256 * 1024


def parse_range(header: str | None, file_size: int) -> tuple[int, int] | str | None:
    """Return a single byte range, None, or 'invalid' for an unsatisfiable range."""
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


class ReadAhead:
    """Drain one process pipe into a bounded queue independently of its socket."""

    def __init__(self, stream, capacity: int, chunk: int = CHUNK_SIZE):
        self._stream = stream
        self._capacity = max(chunk * 2, capacity)
        self._chunk = chunk
        self._queue: collections.deque[bytes] = collections.deque()
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


def serve_file_range(handler: RequestHandler, path: Path) -> None:
    """Serve an already-resolved media path using this request's private file handle."""
    try:
        stat = path.stat()
    except OSError:
        handler.send_api_error(HTTPStatus.NOT_FOUND, "Video not found")
        return
    file_size = stat.st_size
    content_type, _ = mimetypes.guess_type(str(path))
    if not content_type or not content_type.startswith("video/"):
        content_type = "video/mp4"
    parsed = parse_range(handler.headers.get("Range"), file_size)
    if parsed == "invalid":
        handler._begin(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, {
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
    handler._begin(status, headers)
    if getattr(handler, "_head_only", False):
        return
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                if not handler._write(chunk):
                    return
                remaining -= len(chunk)
    except OSError as exc:
        log.warning("Stream read error for %s: %s", path.name, exc)
        handler.close_connection = True


def pump_process(handler: RequestHandler, cmd: list[str], label: str) -> None:
    """Own one request's child and buffer, releasing its shared capacity on every exit."""
    if getattr(handler, "_head_only", False):
        handler.send_bytes(b"", "video/mp4")
        return
    sem = handler.app.tools.transcode_sem
    if not sem.acquire(timeout=20):
        handler.send_api_error(HTTPStatus.SERVICE_UNAVAILABLE,
                               "Server is busy transcoding. Try again shortly.",
                               extra={"Retry-After": "5"})
        return
    process = None
    try:
        try:
            process = popen_quiet(cmd)
        except OSError as exc:
            handler.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"ffmpeg failed: {exc}")
            return
        if not handler.app.track_stream(process):
            handler.send_api_error(HTTPStatus.SERVICE_UNAVAILABLE, "Server is shutting down")
            return
        log.info("Streaming %s", label)
        handler.close_connection = True
        handler._begin(HTTPStatus.OK, {
            "Content-Type": "video/mp4",
            "Cache-Control": "no-store",
            "Connection": "close",
            "Accept-Ranges": "none",
        })
        if getattr(handler, "_head_only", False):
            return
        reader = ReadAhead(process.stdout, handler.app.config.stream_buffer_bytes)
        try:
            while True:
                chunk = reader.read()
                if not chunk:
                    break
                if not handler._write(chunk):
                    break
                try:
                    handler.wfile.flush()
                except (OSError, ValueError):
                    break
        finally:
            reader.close()
    finally:
        if process is not None:
            handler.app.forget_stream(process)
            handler._terminate(process)
        sem.release()
        log.info("Stream ended: %s", label)


def terminate_process(process: subprocess.Popen) -> None:
    """Terminate and reap one child, escalating to kill after two seconds."""
    try:
        if process.poll() is None:
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
