"""Local metadata: Kodi-style .nfo sidecars and artwork files.

Local first, on purpose. A .nfo file and a poster.jpg sitting next to the
media need no API key, no network and no third-party service, and they already
cover most libraries. Online lookups can layer on top later; they must never
be what the library depends on to be usable.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("litejelly.metadata")

# Kodi writes <movie>, <episodedetails> or <tvshow> at the root.
ROOT_TAGS = ("movie", "episodedetails", "tvshow", "musicvideo")

ARTWORK_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")

# Checked in order; the first that exists wins.
POSTER_NAMES = ("poster", "folder", "cover", "show", "season-all-poster")
BACKDROP_NAMES = ("fanart", "backdrop", "background")

MAX_NFO_BYTES = 512 * 1024
MAX_PLOT_CHARS = 2000


@dataclass
class Metadata:
    title: str = ""
    plot: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    rating: float | None = None
    runtime_minutes: int | None = None
    genres: list[str] = field(default_factory=list)
    studio: str = ""
    aired: str = ""

    def is_empty(self) -> bool:
        return not any([self.title, self.plot, self.year, self.rating,
                        self.genres, self.studio, self.aired])

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "plot": self.plot,
            "year": self.year,
            "season": self.season,
            "episode": self.episode,
            "rating": self.rating,
            "runtime_minutes": self.runtime_minutes,
            "genres": list(self.genres),
            "studio": self.studio,
            "aired": self.aired,
        }


def _text(element, *names: str) -> str:
    for name in names:
        found = element.find(name)
        if found is not None and found.text and found.text.strip():
            return found.text.strip()
    return ""


def _number(element, *names: str):
    raw = _text(element, *names)
    if not raw:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", raw)
    if not match:
        return None
    return match.group(0)


def parse_nfo(text: str) -> Metadata | None:
    """Read a Kodi .nfo. Returns None when it is not one."""
    if not text or not text.strip():
        return None
    try:
        root = ElementTree.fromstring(text.strip())
    except ElementTree.ParseError:
        # Some .nfo files are just a URL or a scraper stub; that is not an error.
        return None

    if root.tag.lower() not in ROOT_TAGS:
        return None

    meta = Metadata()
    meta.title = _text(root, "title", "originaltitle", "showtitle")[:300]
    meta.plot = _text(root, "plot", "outline", "summary")[:MAX_PLOT_CHARS]
    meta.studio = _text(root, "studio")[:120]
    meta.aired = _text(root, "aired", "premiered", "releasedate")[:40]

    year = _number(root, "year")
    if year:
        try:
            value = int(float(year))
            meta.year = value if 1800 <= value <= 2200 else None
        except ValueError:
            meta.year = None
    if meta.year is None and meta.aired[:4].isdigit():
        meta.year = int(meta.aired[:4])

    for attribute, names in (("season", ("season",)), ("episode", ("episode",))):
        raw = _number(root, *names)
        if raw is not None:
            try:
                setattr(meta, attribute, int(float(raw)))
            except ValueError:
                pass

    rating = _number(root, "rating", "userrating")
    if rating is not None:
        try:
            value = float(rating)
            meta.rating = round(value, 1) if 0 <= value <= 10 else None
        except ValueError:
            meta.rating = None

    runtime = _number(root, "runtime", "durationinseconds")
    if runtime is not None:
        try:
            minutes = int(float(runtime))
            # <durationinseconds> is exactly what it says.
            if root.find("durationinseconds") is not None:
                minutes = round(minutes / 60)
            meta.runtime_minutes = minutes if 0 < minutes < 6000 else None
        except ValueError:
            meta.runtime_minutes = None

    meta.genres = [g.text.strip() for g in root.findall("genre")
                   if g.text and g.text.strip()][:8]

    return None if meta.is_empty() else meta


def read_nfo(path: Path) -> Metadata | None:
    """Load the .nfo beside a video file, if there is one."""
    candidate = path.with_suffix(".nfo")
    if not candidate.is_file():
        return None
    try:
        if candidate.stat().st_size > MAX_NFO_BYTES:
            log.debug("Ignoring oversized nfo %s", candidate.name)
            return None
        text = candidate.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        log.debug("Could not read %s: %s", candidate.name, exc)
        return None
    return parse_nfo(text)


class ArtworkIndex:
    """Artwork lookup backed by one directory listing per folder.

    Probing every candidate name costs dozens of stat calls per video, which
    is felt on a phone with a large library. Listing the folder once and
    matching in memory is the same answer for a fraction of the I/O.
    """

    def __init__(self):
        self._folders: dict[str, dict[str, Path]] = {}

    def _listing(self, folder: Path) -> dict[str, Path]:
        key = str(folder)
        cached = self._folders.get(key)
        if cached is not None:
            return cached

        names: dict[str, Path] = {}
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    lowered = entry.name.lower()
                    if lowered.endswith(ARTWORK_SUFFIXES) and entry.is_file():
                        names[lowered] = Path(entry.path)
        except OSError:
            names = {}

        self._folders[key] = names
        return names

    def _match(self, folder: Path, stems) -> Path | None:
        listing = self._listing(folder)
        if not listing:
            return None
        for stem in stems:
            for suffix in ARTWORK_SUFFIXES:
                found = listing.get(f"{stem}{suffix}".lower())
                if found is not None:
                    return found
        return None

    def find(self, video_path: Path, kind: str = "poster") -> Path | None:
        """Artwork for a video: its own image first, then the folder's.

        Looks one level up as well, since a season folder usually leaves the
        poster with the show rather than with each episode.
        """
        names = POSTER_NAMES if kind == "poster" else BACKDROP_NAMES
        suffix = "-thumb" if kind == "poster" else "-fanart"
        folder = video_path.parent
        stem = video_path.stem

        own = self._match(folder, (f"{stem}{suffix}", stem))
        if own is not None:
            return own

        here = self._match(folder, names)
        if here is not None:
            return here

        parent = folder.parent
        return self._match(parent, names) if parent != folder else None


def find_artwork(video_path: Path, kind: str = "poster") -> Path | None:
    return ArtworkIndex().find(video_path, kind)
