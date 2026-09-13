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

from .metadata import ArtworkIndex, read_nfo

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


def _strip_release_group(stem: str) -> str:
    """Drop the trailing -GROUP tag scene releases end with.

    Left in place it becomes the episode title, so every episode of a show
    ends up captioned with the encoder's name.
    """
    match = re.search(r"-([A-Za-z0-9]{2,})$", stem)
    if not match:
        return stem
    before = stem[: match.start()]
    # Not a group tag when it is half of a hyphenated format name (WEB-DL,
    # BLU-RAY, DTS-HD, H-264).
    if re.search(r"(?:web|blu|dts|h|x|true)$", before, re.IGNORECASE):
        return stem
    return before


def parse_title(filename: str) -> dict:
    """Derive a display title (plus year/episode info) from a filename."""
    stem = os.path.splitext(filename)[0]
    working = _BRACKETS.sub(" ", stem)
    working = _strip_release_group(working.strip())
    working = working.replace("_", " ").replace(".", " ").replace("-", " ")
    working = re.sub(r"\s+", " ", working).strip()

    episode = None
    episode_title = ""
    match = _EPISODE.search(working)
    if match:
        episode = {"season": int(match.group(1)), "episode": int(match.group(2))}
        # Whatever follows SxxExx is usually the episode name.
        episode_title = _clean_fragment(working[match.end():])
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

    return {
        "title": _smart_title(working),
        "year": year,
        "episode": episode,
        "episode_title": episode_title,
    }


def _clean_fragment(text: str) -> str:
    """Tidy the text trailing an SxxExx marker into an episode name."""
    cleaned = _NOISE.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—")
    if not cleaned or cleaned.isdigit():
        return ""
    return _smart_title(cleaned)


def resolve_category(content_type: str, has_episode: bool) -> str:
    """A tagged folder wins; an untagged one is judged by the filename."""
    if content_type in ("movies", "shows", "anime"):
        return content_type
    return "shows" if has_episode else "movies"


def series_key(category: str, title: str) -> str:
    """Stable id so episodes group together across folders and rescans.

    Separators are removed rather than collapsed so that "S.H.I.E.L.D." and
    "SHIELD" resolve to the same show.
    """
    normalised = re.sub(r"[^a-z0-9]+", "", title.lower())
    digest = hashlib.sha1(f"{category}\x00{normalised}".encode("utf-8")).hexdigest()
    return digest[:16]


def _merge_nfo(parsed: dict, nfo) -> dict:
    """Let a .nfo override what the filename guessed, field by field.

    Only fields the file actually supplies are replaced: a .nfo with just a
    plot should not wipe an episode number the filename got right.
    """
    merged = dict(parsed)
    episode = dict(merged.get("episode") or {})

    if nfo.season is not None:
        episode["season"] = nfo.season
    if nfo.episode is not None:
        episode["episode"] = nfo.episode
    if episode.get("season") is not None and episode.get("episode") is not None:
        merged["episode"] = episode

    if nfo.title:
        # For an episode the <title> is the episode name, not the series.
        if merged.get("episode"):
            merged["episode_title"] = nfo.title
        else:
            merged["title"] = nfo.title
    if nfo.year is not None:
        merged["year"] = nfo.year
    return merged


def display_name(parsed: dict) -> str:
    """Flat label for search results and the player, where there is no series
    heading to give an episode its context."""
    episode = parsed.get("episode")
    if not episode:
        return parsed["title"]
    code = f"S{episode['season']:02d}E{episode['episode']:02d}"
    if parsed.get("episode_title"):
        return f"{parsed['title']} \u00b7 {code} \u00b7 {parsed['episode_title']}"
    return f"{parsed['title']} \u00b7 {code}"


@dataclass
class Video:
    id: str
    name: str
    title: str
    filename: str
    path: str
    dir_index: int
    folder: str
    content_type: str
    category: str
    series_id: str
    size: int
    size_human: str
    modified: str
    modified_ts: float
    extension: str
    episode_title: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    rating: float | None = None
    has_poster: bool = False
    mal_id: int | None = None
    # Kept in memory for /api/details and artwork serving. Plots run to
    # thousands of characters, so they must not ride along in the listing.
    meta: dict | None = None
    poster_path: str = ""
    backdrop_path: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        for internal in ("meta", "poster_path", "backdrop_path", "mal_id"):
            data.pop(internal, None)
        return data


def _make_id(dir_index: int, rel_path: str) -> str:
    digest = hashlib.sha1(f"{dir_index}\x00{rel_path}".encode("utf-8")).hexdigest()
    return digest[:16]


def episode_order(video: Video) -> tuple:
    """Sort key inside a series. Filename breaks ties so order is stable."""
    return (video.season or 0, video.episode or 0, video.filename.lower())


def _sibling_episode(videos, current: Video | None, step: int) -> Video | None:
    """The episode ``step`` places from ``current`` inside its series.

    Lives on the server because the edge cases deserve tests: the first and
    last episodes of a show, numbering with gaps, and films, which have no
    neighbours at all.
    """
    if current is None or not current.series_id:
        return None
    siblings = sorted((v for v in videos if v.series_id == current.series_id),
                      key=episode_order)
    for index, video in enumerate(siblings):
        if video.id == current.id:
            target = index + step
            return siblings[target] if 0 <= target < len(siblings) else None
    return None


def next_episode(videos, current: Video) -> Video | None:
    return _sibling_episode(videos, current, 1)


def previous_episode(videos, current: Video) -> Video | None:
    return _sibling_episode(videos, current, -1)


def build_continue_watching(videos, progress: dict, limit: int = 12) -> list[dict]:
    """One entry per series, newest first.

    A show with twenty part-watched episodes should occupy one row, not twenty.
    When the last thing watched in a series is finished, the row becomes the
    next episode instead, which is the thing the viewer actually wants next.
    """
    by_id = {video.id: video for video in videos}
    entries = sorted(
        (entry for entry in progress.values() if entry.get("video_id") in by_id),
        key=lambda entry: entry.get("updated_at") or 0,
        reverse=True,
    )

    seen: set[str] = set()
    out: list[dict] = []
    for entry in entries:
        video = by_id[entry["video_id"]]
        key = video.series_id or video.id
        if key in seen:
            continue

        if entry.get("finished"):
            following = next_episode(videos, video)
            if following is None:
                continue
            if (progress.get(following.id) or {}).get("finished"):
                continue
            seen.add(key)
            out.append({"id": following.id, "position": 0.0,
                        "duration": 0.0, "next_up": True})
        else:
            # A few seconds in is an accident, not something to resume.
            if (entry.get("position") or 0) < 15:
                continue
            seen.add(key)
            out.append({"id": video.id,
                        "position": entry.get("position") or 0.0,
                        "duration": entry.get("duration") or 0.0,
                        "next_up": False})

        if len(out) >= limit:
            break
    return out


class Library:
    """Holds an immutable snapshot of the scanned media, refreshed in the background."""

    def __init__(self, media_dirs, scan_interval: int = 60,
                 metadata=None, enricher=None):
        self.media_dirs = list(media_dirs)
        self.scan_interval = scan_interval
        # Both optional: without them the library works entirely from local files.
        self.metadata = metadata
        self.enricher = enricher
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

    def request_scan(self, force: bool = False) -> None:
        """Wake the scanner. ``force`` also re-reads unchanged folders.

        The fingerprint only notices file changes, so metadata arriving from a
        lookup needs the force flag or the scan would skip the re-read.
        """
        if force:
            with self._lock:
                self._signature = {}
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

    def _apply_online(self, video: Video) -> None:
        """Fill gaps from the cached online lookup, and queue a missing one.

        Anything found locally wins: a .nfo and a poster.jpg were put there
        deliberately, and a fuzzy title match should not override them.
        """
        if self.metadata is None or not video.series_id:
            return

        anime = video.category == "anime"
        info = self.metadata.cached_series(video.title, anime)
        if info is None:
            if self.enricher is not None and not self.metadata.has_looked_up(
                    video.title, anime):
                self.enricher.enqueue_series(video.title, anime)
            return

        episode = info.episodes.get(f"s{video.season or 0}e{video.episode or 0}") or {}

        if not video.episode_title and episode.get("title"):
            video.episode_title = episode["title"]
            video.name = display_name({
                "title": video.title,
                "episode": {"season": video.season, "episode": video.episode},
                "episode_title": video.episode_title,
            })
        if video.rating is None:
            video.rating = episode.get("rating") or info.rating

        if not video.poster_path and info.poster_url:
            downloaded = self.metadata.artwork_path(info.poster_url)
            if downloaded is not None:
                video.poster_path = str(downloaded)
                video.has_poster = True

        merged = dict(video.meta or {})
        merged.setdefault("plot", episode.get("summary") or info.summary)
        merged.setdefault("genres", info.genres)
        merged.setdefault("aired", episode.get("airdate") or "")
        merged["source"] = info.source
        merged["imdb_id"] = info.imdb_id
        merged["series_plot"] = info.summary
        video.meta = merged

        if anime and info.mal_id:
            video.mal_id = info.mal_id
            if self.enricher is not None and video.episode:
                self.enricher.enqueue_skip(info.mal_id, video.episode)

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
        artwork = ArtworkIndex()
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
                        # A hand-written .nfo beats anything guessed from a filename.
                        nfo = read_nfo(full)
                        if nfo is not None:
                            parsed = _merge_nfo(parsed, nfo)
                        ep = parsed["episode"] or {}
                        category = resolve_category(entry.content_type, bool(ep))
                        poster = artwork.find(full, "poster")
                        backdrop = artwork.find(full, "backdrop")
                        modified = datetime.datetime.fromtimestamp(
                            stat.st_mtime, tz=datetime.timezone.utc)
                        video = Video(
                            id=_make_id(index, rel),
                            name=display_name(parsed),
                            title=parsed["title"],
                            filename=filename,
                            path=rel,
                            dir_index=index,
                            folder=str(Path(rel).parent.as_posix()) if "/" in rel else "",
                            content_type=entry.content_type,
                            category=category,
                            # Only episodes form a series; a movie stands alone.
                            series_id=series_key(category, parsed["title"]) if ep else "",
                            size=stat.st_size,
                            size_human=human_size(stat.st_size),
                            modified=modified.isoformat(),
                            modified_ts=stat.st_mtime,
                            extension=full.suffix.lstrip(".").lower(),
                            episode_title=parsed["episode_title"],
                            year=parsed["year"],
                            rating=nfo.rating if nfo else None,
                            has_poster=poster is not None,
                            meta=nfo.to_dict() if nfo else None,
                            poster_path=str(poster) if poster else "",
                            backdrop_path=str(backdrop) if backdrop else "",
                            season=ep.get("season"),
                            episode=ep.get("episode"),
                        )
                        self._apply_online(video)
                        videos.append(video)
            except OSError as exc:
                log.warning("Could not scan %s: %s", root_dir, exc)
        return videos
