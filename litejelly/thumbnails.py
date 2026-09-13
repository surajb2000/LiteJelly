"""Thumbnail generation with a bounded worker pool and an on-disk cache."""

from __future__ import annotations

import hashlib
import logging
import subprocess
import threading
from pathlib import Path

from .ffmpeg import run_quiet

log = logging.getLogger("litejelly.thumbnails")

THUMB_WIDTH = 480
THUMB_HEIGHT = 270


class ThumbnailService:
    def __init__(self, tools, cache_dir: Path):
        self.tools = tools
        self.cache_dir = cache_dir / "thumbnails"
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _cache_path(self, video_path: Path) -> Path | None:
        try:
            stat = video_path.stat()
        except OSError:
            return None
        key = f"{video_path}|{stat.st_mtime_ns}|{stat.st_size}"
        return self.cache_dir / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()}.jpg"

    def get(self, video_path: Path, duration: float = 0.0) -> Path | None:
        """Return a cached thumbnail, generating it once if needed."""
        cache_path = self._cache_path(video_path)
        if cache_path is None:
            return None
        if cache_path.is_file() and cache_path.stat().st_size > 0:
            return cache_path
        if not self.tools.available:
            return None

        key = cache_path.name
        with self._lock:
            event = self._inflight.get(key)
            leader = event is None
            if leader:
                event = threading.Event()
                self._inflight[key] = event

        if not leader:
            # Another request is already generating this exact thumbnail.
            event.wait(timeout=45)
            return cache_path if cache_path.is_file() else None

        try:
            self._generate(video_path, cache_path, duration)
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            event.set()

        return cache_path if cache_path.is_file() and cache_path.stat().st_size > 0 else None

    def _generate(self, video_path: Path, cache_path: Path, duration: float) -> None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Cannot create thumbnail cache: %s", exc)
            return

        # Prefer a frame a little way in; fall back progressively for short clips.
        offsets = [duration * 0.2] if duration > 30 else []
        offsets += [30.0, 5.0, 0.0]

        acquired = self.tools.thumbnail_sem.acquire(timeout=30)
        if not acquired:
            log.debug("Thumbnail queue busy, skipping %s", video_path.name)
            return
        try:
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
                    result = run_quiet(cmd, timeout=30)
                except subprocess.TimeoutExpired:
                    log.debug("Thumbnail timed out for %s", video_path.name)
                    continue
                except OSError as exc:
                    log.warning("Thumbnail failed for %s: %s", video_path.name, exc)
                    return
                if result.returncode == 0 and cache_path.is_file() and cache_path.stat().st_size > 0:
                    return
        finally:
            self.tools.thumbnail_sem.release()
