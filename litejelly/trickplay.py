"""Sprite sheets of a video, for the scrub bar.

One JPEG holds every preview frame for a file, so the client fetches a single
image and shows the right cell rather than asking the server per position.
Only keyframes are decoded: measured on a 10 minute 1080p clip, that is about
0.3 s of CPU per minute of video, and a 24 minute episode costs roughly 7 s
and 224 KB at the defaults.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import queue
import subprocess
import threading
from dataclasses import dataclass, asdict
from pathlib import Path

from .ffmpeg import run_quiet

log = logging.getLogger("litejelly.trickplay")

COLUMNS = 10
QUEUE_LIMIT = 200
MAX_FAILURES = 2
# One sheet has to stay a sane download; beyond this the interval is widened.
MAX_TILES = 900


@dataclass
class Sheet:
    key: str
    interval: float
    columns: int
    rows: int
    count: int
    tile_width: int
    tile_height: int
    duration: float

    def to_dict(self) -> dict:
        return asdict(self)


class TrickplayService:
    """Builds sheets on a background worker and serves them from disk."""

    def __init__(self, tools, cache_dir: Path, enabled: bool = True,
                 interval: float = 10.0, tile_width: int = 160):
        self.tools = tools
        self.cache_dir = cache_dir / "trickplay"
        self.enabled = bool(enabled)
        self.interval = max(1.0, float(interval))
        self.tile_width = max(80, int(tile_width))
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_LIMIT)
        self._pending: set[str] = set()
        self._failed: dict[str, int] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # -- public API -------------------------------------------------------
    def key_for(self, video_path: Path) -> str | None:
        try:
            stat = video_path.stat()
        except OSError:
            return None
        raw = (f"{video_path}|{stat.st_mtime_ns}|{stat.st_size}"
               f"|{self.interval}|{self.tile_width}")
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    def cached(self, video_path: Path) -> Sheet | None:
        """The finished sheet, or None. Never starts any work."""
        key = self.key_for(video_path)
        if key is None:
            return None
        try:
            payload = json.loads((self.cache_dir / f"{key}.json").read_text("utf-8"))
            if not (self.cache_dir / f"{key}.jpg").is_file():
                return None
            return Sheet(**payload)
        except (OSError, ValueError, TypeError):
            return None

    def sheet_path(self, key: str) -> Path | None:
        if not _is_key(key):
            return None
        target = self.cache_dir / f"{key}.jpg"
        try:
            return target if target.is_file() and target.stat().st_size else None
        except OSError:
            return None

    def request(self, video_path: Path, duration: float) -> bool:
        """Queue a sheet. False means it is not coming."""
        if not self.enabled or not self.tools.available or duration <= 0:
            return False
        key = self.key_for(video_path)
        if key is None:
            return False
        with self._lock:
            if self._failed.get(key, 0) >= MAX_FAILURES:
                return False
            if key in self._pending:
                return True
            self._pending.add(key)

        self._ensure_worker()
        try:
            self._queue.put_nowait((key, video_path, duration))
        except queue.Full:
            with self._lock:
                self._pending.discard(key)
            return False
        return True

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            worker = self._worker
            self._worker = None
        if worker is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            worker.join(timeout=5)

    # -- internals --------------------------------------------------------
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None or self._stop.is_set():
                return
            # One worker only: this is background work behind live playback.
            self._worker = threading.Thread(target=self._loop, daemon=True,
                                            name="trickplay")
            self._worker.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                return
            key, video_path, duration = item
            built = False
            try:
                built = self._generate(key, video_path, duration)
            except Exception:
                log.exception("Trickplay failed for %s", video_path.name)
            finally:
                with self._lock:
                    self._pending.discard(key)
                    if built:
                        self._failed.pop(key, None)
                    else:
                        self._failed[key] = self._failed.get(key, 0) + 1
                self._queue.task_done()

    def plan(self, duration: float) -> tuple[float, int, int]:
        """(interval, count, rows) for a duration, widened to stay downloadable."""
        interval = self.interval
        count = max(1, int(math.ceil(duration / interval)))
        while count > MAX_TILES:
            interval *= 2
            count = max(1, int(math.ceil(duration / interval)))
        rows = max(1, int(math.ceil(count / COLUMNS)))
        return interval, count, rows

    def _generate(self, key: str, video_path: Path, duration: float) -> bool:
        interval, count, rows = self.plan(duration)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Cannot create the trickplay cache: %s", exc)
            return False

        target = self.cache_dir / f"{key}.jpg"
        partial = self.cache_dir / f"{key}.part.jpg"
        columns = min(COLUMNS, count)
        graph = (f"fps=1/{interval:g},scale={self.tile_width}:-2,"
                 f"tile={columns}x{rows}")

        # Keyframes only is far cheaper, but on a clip shorter than one
        # interval it yields no frames at all, so fall back to a full decode.
        for fast in (True, False):
            cmd = [self.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
            if fast:
                cmd += ["-skip_frame", "nokey"]
            cmd += [
                "-i", str(video_path),
                "-vf", graph,
                "-frames:v", "1", "-an", "-sn", "-fps_mode", "passthrough",
                "-q:v", "6", "-y", str(partial),
            ]
            try:
                result = run_quiet(cmd, timeout=600)
            except (subprocess.SubprocessError, OSError) as exc:
                log.debug("Trickplay ffmpeg failed for %s: %s", video_path.name, exc)
                partial.unlink(missing_ok=True)
                return False
            if result.returncode == 0 and partial.is_file() and partial.stat().st_size:
                break
            partial.unlink(missing_ok=True)
        else:
            log.debug("Trickplay produced nothing for %s", video_path.name)
            return False

        size = _jpeg_size(partial)
        if size is None:
            partial.unlink(missing_ok=True)
            return False
        width, height = size
        sheet = Sheet(
            key=key, interval=interval, columns=columns, rows=rows, count=count,
            tile_width=width // columns, tile_height=height // rows,
            duration=duration,
        )
        try:
            partial.replace(target)
            (self.cache_dir / f"{key}.json").write_text(
                json.dumps(sheet.to_dict()), encoding="utf-8")
        except OSError as exc:
            log.warning("Cannot store the trickplay sheet: %s", exc)
            return False
        log.debug("Trickplay sheet for %s: %d tiles", video_path.name, count)
        return True


def _is_key(value: str) -> bool:
    return bool(value) and len(value) == 20 and all(c in "0123456789abcdef" for c in value)


def _jpeg_size(path: Path) -> tuple[int, int] | None:
    """Pixel size straight from the JPEG header, so no image library is needed."""
    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"\xff\xd8":
                return None
            while True:
                marker = handle.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                kind = marker[1]
                length = int.from_bytes(handle.read(2), "big")
                if length < 2:
                    return None
                # The frame headers carry the dimensions; everything else is skipped.
                if kind in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    body = handle.read(5)
                    if len(body) < 5:
                        return None
                    height = int.from_bytes(body[1:3], "big")
                    width = int.from_bytes(body[3:5], "big")
                    return (width, height) if width and height else None
                handle.seek(length - 2, 1)
    except OSError:
        return None
