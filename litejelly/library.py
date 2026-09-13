"""Media library: scanning, title cleanup and the in-memory index."""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import re
import threading
from dataclasses import dataclass, asdict
from pathlib import Path

log = logging.getLogger("litejelly.library")

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".webm", ".mov", ".m4v",
    ".wmv", ".flv", ".ts", ".m2ts", ".mpg", ".mpeg", ".ogv", ".3gp",
}

SKIP_DIRS = {".cache", ".thumbnails", "@eaDir", "$RECYCLE.BIN", "System Volume Information"}

# Release-scene noise to strip out of display titles.
_NOISE = re.compile(
    r"\b(?:1080p|2160p|1440p|720p|480p|4k|uhd|hdr10?|sdr|hevc|x264|x265|h ?26[45]|"
    r"av1|xvid|divx|aac\d?|ac3|eac3|dts(?:[- ]hd)?|truehd|atmos|ddp?5 1|5 1|7 1|"
    r"bluray|blu ray|bdrip|brrip|webrip|web ?dl|hdrip|dvdrip|hdtv|remux|proper|repack|"
    r"extended|uncut|remastered|imax|10bit|8bit|dual audio|multi|subbed|dubbed)\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_EPISODE = re.compile(r"\bS(\d{1,2})[ ._-]?E(\d{1,3})\b", re.IGNORECASE)
_BRACKETS = re.compile(r"[\[\(\{][^\]\)\}]*[\]\)\}]")
_ACRONYMS = {"tv", "us", "uk", "hd", "3d", "ii", "iii", "iv", "vi", "vii", "viii", "ix"}


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}".strip()
        num /= 1024.0
    return f"{num:.1f} PB"


def _smart_title(text: str) -> str:
    words = []
    for word in text.split():
        lowered = word.lower()
        if lowered in _ACRONYMS:
            words.append(word.upper())
        elif word.isupper() and len(word) <= 4:
            words.append(word)
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def parse_title(filename: str) -> dict:
    """Derive a display title (plus year/episode info) from a filename."""
    stem = os.path.splitext(filename)[0]
    working = _BRACKETS.sub(" ", stem)
    working = working.replace("_", " ").replace(".", " ").replace("-", " ")
    working = re.sub(r"\s+", " ", working).strip()

    episode = None
    match = _EPISODE.search(working)
    if match:
        episode = {"season": int(match.group(1)), "episode": int(match.group(2))}
        working = working[: match.start()].strip() or working

    year = None
    year_match = _YEAR.search(working)
    if year_match:
        year = int(year_match.group(1))
        if year_match.start() > 2:
            working = working[: year_match.start()].strip()

    working = _NOISE.sub(" ", working)
    working = re.sub(r"\s+", " ", working).strip(" -–—")

    if not working:
        working = re.sub(r"[._-]+", " ", stem).strip()

    return {"title": _smart_title(working), "year": year, "episode": episode}


def display_name(parsed: dict) -> str:
    """Every episode of a series parses to the same title, so re-attach SxxExx."""
    episode = parsed.get("episode")
    if not episode:
        return parsed["title"]
    return (f"{parsed['title']} \u00b7 "
            f"S{episode['season']:02d}E{episode['episode']:02d}")


@dataclass
class Video:
    id: str
    name: str
    filename: str
    path: str
    dir_index: int
    folder: str
    content_type: str
    size: int
    size_human: str
    modified: str
    modified_ts: float
    extension: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _make_id(dir_index: int, rel_path: str) -> str:
    digest = hashlib.sha1(f"{dir_index}\x00{rel_path}".encode("utf-8")).hexdigest()
    return digest[:16]


class Library:
    """Holds an immutable snapshot of the scanned media, refreshed in the background."""

    def __init__(self, media_dirs, scan_interval: int = 60):
        self.media_dirs = list(media_dirs)
        self.scan_interval = scan_interval
        self._videos: list[Video] = []
        self._by_id: dict[str, Video] = {}
        self._signature: dict[str, tuple] = {}
        self._dirs_version = 0
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._scanning = False
        self._last_scan: float = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- public API -------------------------------------------------------
    @property
    def videos(self) -> list[Video]:
        with self._lock:
            return self._videos

    @property
    def status(self) -> dict:
        with self._lock:
            return {
                "count": len(self._videos),
                "scanning": self._scanning,
                "last_scan": self._last_scan,
            }

    def get(self, video_id: str) -> Video | None:
        with self._lock:
            return self._by_id.get(video_id)

    def set_media_dirs(self, media_dirs, scan_interval: int | None = None) -> None:
        """Swap the scanned directories and force a rescan.

        Bumping the version invalidates any scan already in flight: its results
        carry dir_index values that point into the old list.
        """
        with self._lock:
            self.media_dirs = list(media_dirs)
            if scan_interval is not None:
                self.scan_interval = scan_interval
            self._signature = {}
            self._dirs_version += 1
        self.request_scan()

    def absolute_path(self, video: Video) -> Path | None:
        from .paths import resolve_within

        with self._lock:
            dirs = self.media_dirs
        if not 0 <= video.dir_index < len(dirs):
            return None
        return resolve_within(Path(dirs[video.dir_index].path), video.path)

    def request_scan(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="library-scanner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- internals --------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.scan_interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.scan()
            except Exception:
                log.exception("Library scan failed")

    def _directory_signature(self, dirs) -> dict[str, tuple]:
        """Cheap fingerprint of each tree, used to skip unnecessary rescans."""
        signature: dict[str, tuple] = {}
        for entry in dirs:
            root_dir = entry.path
            latest = 0.0
            count = 0
            for root, dirs, _files in os.walk(root_dir):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                count += 1
                try:
                    latest = max(latest, os.path.getmtime(root))
                except OSError:
                    pass
            signature[root_dir] = (latest, count)
        return signature

    def scan(self, force: bool = False) -> list[Video]:
        with self._lock:
            if self._scanning:
                return self._videos
            self._scanning = True
            dirs = list(self.media_dirs)
            version = self._dirs_version

        try:
            signature = self._directory_signature(dirs)
            with self._lock:
                unchanged = signature == self._signature and self._videos
            if unchanged and not force:
                return self.videos

            log.info("Scanning %d media director%s...",
                     len(dirs), "y" if len(dirs) == 1 else "ies")
            videos = self._walk(dirs)
            videos.sort(key=lambda v: v.modified_ts, reverse=True)

            with self._lock:
                if version != self._dirs_version:
                    log.info("Media directories changed during the scan; discarding results")
                    return self._videos
                self._videos = videos
                self._by_id = {v.id: v for v in videos}
                self._signature = signature
                self._last_scan = datetime.datetime.now().timestamp()
            log.info("Indexed %d videos", len(videos))
            return videos
        finally:
            with self._lock:
                self._scanning = False

    def _walk(self, dirs) -> list[Video]:
        videos: list[Video] = []
        for index, entry in enumerate(dirs):
            root_dir = entry.path
            base = Path(root_dir)
            try:
                for root, dirs, files in os.walk(root_dir):
                    dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                    for filename in files:
                        if os.path.splitext(filename)[1].lower() not in VIDEO_EXTENSIONS:
                            continue
                        full = Path(root) / filename
                        try:
                            stat = full.stat()
                        except OSError as exc:
                            log.debug("Skipping %s: %s", full, exc)
                            continue
                        if stat.st_size == 0:
                            continue
                        rel = full.relative_to(base).as_posix()
                        parsed = parse_title(filename)
                        ep = parsed["episode"] or {}
                        modified = datetime.datetime.fromtimestamp(
                            stat.st_mtime, tz=datetime.timezone.utc)
                        videos.append(Video(
                            id=_make_id(index, rel),
                            name=display_name(parsed),
                            filename=filename,
                            path=rel,
                            dir_index=index,
                            folder=str(Path(rel).parent.as_posix()) if "/" in rel else "",
                            content_type=entry.content_type,
                            size=stat.st_size,
                            size_human=human_size(stat.st_size),
                            modified=modified.isoformat(),
                            modified_ts=stat.st_mtime,
                            extension=full.suffix.lstrip(".").lower(),
                            year=parsed["year"],
                            season=ep.get("season"),
                            episode=ep.get("episode"),
                        ))
            except OSError as exc:
                log.warning("Could not scan %s: %s", root_dir, exc)
        return videos
