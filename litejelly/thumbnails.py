"""Thumbnail generation with a bounded worker pool and an on-disk cache."""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .ffmpeg import run_quiet

log = logging.getLogger("litejelly.thumbnails")

THUMB_WIDTH = 480
THUMB_HEIGHT = 270
QUEUE_LIMIT = 500


class ThumbnailService:
    """Generation happens on background workers, never inside a request.

    A TV browser opens only a handful of connections. Generating in the request
    held them all open behind a semaphore, so a slow machine starved the page of
    the connections it needed for everything else.
    """

    def __init__(self, tools, cache_dir: Path, workers: int = 2):
        self.tools = tools
        self.cache_dir = cache_dir / "thumbnails"
        # Bounded: the endpoint is unauthenticated, and a backlog no worker
        # will reach this decade is not worth the memory.
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_LIMIT)
        self._pending: set[str] = set()
        self._failed: dict[str, int] = {}
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._worker_count = max(1, workers)
        self._stop = threading.Event()

    # -- public API -------------------------------------------------------
    def cached(self, video_path: Path) -> Path | None:
        """The thumbnail if it already exists, without generating anything."""
        cache_path = self._cache_path(video_path)
        if cache_path is None:
            return None
        try:
            if cache_path.is_file() and cache_path.stat().st_size > 0:
                return cache_path
        except OSError:
            return None
        return None

    def request(self, video_path: Path, duration: float = 0.0) -> bool:
        """Queue generation. Returns False if it is not worth waiting for."""
        if self._stop.is_set() or not self.tools.available:
            return False
        if self.cached(video_path) is not None:
            return True
        cache_path = self._cache_path(video_path)
        if cache_path is None:
            return False

        key = cache_path.name
        with self._lock:
            if self._stop.is_set():
                return False
            # Give up on a file that has already failed repeatedly, or every
            # page load re-queues work that is never going to succeed.
            if self._failed.get(key, 0) >= 3:
                return False
            if key in self._pending:
                return True
            self._pending.add(key)

        self._ensure_workers()
        with self._lock:
            if self._stop.is_set():
                self._pending.discard(key)
                return False
            try:
                self._queue.put_nowait((key, video_path, cache_path, duration))
            except queue.Full:
                self._pending.discard(key)
                return False
        return True

    def get(self, video_path: Path, duration: float = 0.0,
            timeout: float = 0.0) -> Path | None:
        """Cached thumbnail, optionally waiting briefly for a fresh one."""
        existing = self.cached(video_path)
        if existing is not None:
            return existing
        if not self.request(video_path, duration):
            return None
        if timeout <= 0:
            return None

        deadline = threading.Event()
        deadline.wait(timeout)
        return self.cached(video_path)

    def close(self) -> None:
        """Reject new work, discard queued jobs, and wait at most five seconds."""
        with self._lock:
            self._stop.set()
            workers = list(self._workers)
            self._workers = []
            self._pending.clear()
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        deadline = time.monotonic() + 5
        for thread in workers:
            thread.join(timeout=max(0, deadline - time.monotonic()))

    # -- internals --------------------------------------------------------
    def _ensure_workers(self) -> None:
        with self._lock:
            if self._workers or self._stop.is_set():
                return
            for index in range(self._worker_count):
                thread = threading.Thread(target=self._loop, daemon=True,
                                          name=f"thumbnail-{index}")
                self._workers.append(thread)
                thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                return
            key, video_path, cache_path, duration = item
            try:
                if not self._stop.is_set():
                    self._generate(video_path, cache_path, duration)
            except Exception:
                log.exception("Thumbnail worker failed for %s", video_path.name)
            finally:
                with self._lock:
                    self._pending.discard(key)
                    if not (cache_path.is_file() and cache_path.stat().st_size > 0):
                        self._failed[key] = self._failed.get(key, 0) + 1
                    else:
                        self._failed.pop(key, None)
                self._queue.task_done()

    def _cache_path(self, video_path: Path) -> Path | None:
        try:
            stat = video_path.stat()
        except OSError:
            return None
        key = f"{video_path}|{stat.st_mtime_ns}|{stat.st_size}"
        return self.cache_dir / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()}.jpg"

    def _generate(self, video_path: Path, cache_path: Path, duration: float) -> None:
        """Publish a complete thumbnail only; cancellation removes the private temporary file."""
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(dir=cache_path.parent, prefix=cache_path.stem,
                                          suffix=".part.jpg")
            os.close(handle)
        except OSError as exc:
            log.warning("Cannot create thumbnail cache: %s", exc)
            return
        partial = Path(name)
        try:
            if self._generate_partial(video_path, partial, duration) and not self._stop.is_set():
                partial.replace(cache_path)
        finally:
            partial.unlink(missing_ok=True)

    def _generate_partial(self, video_path: Path, cache_path: Path, duration: float) -> bool:
        """Try fallback timestamps in a private output file and report completion."""
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Cannot create thumbnail cache: %s", exc)
            return False

        # Prefer a frame a little way in; fall back progressively for short clips.
        offsets = [duration * 0.2] if duration > 30 else []
        offsets += [30.0, 5.0, 0.0]

        last_error = ""
        for offset in offsets:
            if self._stop.is_set():
                return False
            cmd = [
                self.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-threads", "1", "-filter_threads", "1",
                "-ss", f"{max(0.0, offset):.2f}",
                "-i", str(video_path),
                "-threads", "1",
                "-map", "0:v:0",
                "-frames:v", "1",
                "-vf", f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=decrease",
                "-q:v", "4",
                "-f", "image2",
                "-y", str(cache_path),
            ]
            try:
                result = run_quiet(cmd, timeout=60, cancel_event=self._stop)
            except subprocess.TimeoutExpired:
                last_error = "ffmpeg timed out"
                continue
            except OSError as exc:
                log.warning("Thumbnail failed for %s: %s", video_path.name, exc)
                return False
            except subprocess.SubprocessError:
                return False
            if result.returncode == 0 and cache_path.is_file() and cache_path.stat().st_size > 0:
                log.debug("Thumbnail ready for %s", video_path.name)
                return True
            last_error = (result.stderr or b"").decode("utf-8", "replace").strip()

        # Surfaced at warning: a library with no thumbnails is a visible fault,
        # and the reason is otherwise invisible from the admin page.
        log.warning("Could not make a thumbnail for %s: %s",
                    video_path.name, last_error or "ffmpeg produced no frame")
        return False
