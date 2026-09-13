"""Thumbnail generation with a bounded worker pool and an on-disk cache."""

from __future__ import annotations

import hashlib
import logging
import queue
import subprocess
import threading
from pathlib import Path

from .ffmpeg import run_quiet

log = logging.getLogger("litejelly.thumbnails")

THUMB_WIDTH = 480
THUMB_HEIGHT = 270


class ThumbnailService:
    """Generation happens on background workers, never inside a request.

    A TV browser opens only a handful of connections. Generating in the request
    held them all open behind a semaphore, so a slow machine starved the page of
    the connections it needed for everything else.
    """

    def __init__(self, tools, cache_dir: Path, workers: int = 2):
        self.tools = tools
        self.cache_dir = cache_dir / "thumbnails"
        self._queue: queue.Queue = queue.Queue()
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
        if not self.tools.available:
            return False
        if self.cached(video_path) is not None:
            return True
        cache_path = self._cache_path(video_path)
        if cache_path is None:
            return False

        key = cache_path.name
        with self._lock:
            # Give up on a file that has already failed repeatedly, or every
            # page load re-queues work that is never going to succeed.
            if self._failed.get(key, 0) >= 3:
                return False
            if key in self._pending:
                return True
            self._pending.add(key)

        self._ensure_workers()
        self._queue.put((key, video_path, cache_path, duration))
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
        self._stop.set()
        with self._lock:
            workers = list(self._workers)
            self._workers = []
        for _ in workers:
            self._queue.put(None)
        for thread in workers:
            thread.join(timeout=5)

    # -- internals --------------------------------------------------------
    def _ensure_workers(self) -> None:
        with self._lock:
            if self._workers:
                return
            for index in range(self._worker_count):
                thread = threading.Thread(target=self._loop, daemon=True,
                                          name=f"thumbnail-{index}")
                self._workers.append(thread)
                thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                return
            key, video_path, cache_path, duration = item
            try:
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
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Cannot create thumbnail cache: %s", exc)
            return

        # Prefer a frame a little way in; fall back progressively for short clips.
        offsets = [duration * 0.2] if duration > 30 else []
        offsets += [30.0, 5.0, 0.0]

        last_error = ""
        for offset in offsets:
            cmd = [
                self.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-ss", f"{max(0.0, offset):.2f}",
                "-i", str(video_path),
                "-map", "0:v:0",
                "-frames:v", "1",
                "-vf", f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=decrease",
                "-q:v", "4",
                "-f", "image2",
                "-y", str(cache_path),
            ]
            try:
                result = run_quiet(cmd, timeout=60)
            except subprocess.TimeoutExpired:
                last_error = "ffmpeg timed out"
                continue
            except OSError as exc:
                log.warning("Thumbnail failed for %s: %s", video_path.name, exc)
                return
            if result.returncode == 0 and cache_path.is_file() and cache_path.stat().st_size > 0:
                log.debug("Thumbnail ready for %s", video_path.name)
                return
            last_error = (result.stderr or b"").decode("utf-8", "replace").strip()

        # Surfaced at warning: a library with no thumbnails is a visible fault,
        # and the reason is otherwise invisible from the admin page.
        log.warning("Could not make a thumbnail for %s: %s",
                    video_path.name, last_error or "ffmpeg produced no frame")
