"""Chapter markers, used to offer Skip intro.

Chapters come free with the file: no network, no third-party service and no
guessing. Anything that needs a lookup service can layer on top later, but a
file that already says where its intro ends should not need one.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("litejelly.chapters")

# Matched against the chapter title, case-insensitively.
INTRO_WORDS = re.compile(r"\b(intro|opening|op|theme|titles|recap|previously)\b",
                         re.IGNORECASE)
CREDIT_WORDS = re.compile(r"\b(credits|ending|ed|outro|preview|next episode)\b",
                          re.IGNORECASE)

# An "intro" spanning half the episode is a mislabelled chapter, not an intro.
MAX_INTRO_SECONDS = 300.0
MIN_SEGMENT_SECONDS = 5.0


@dataclass
class Chapter:
    start: float
    end: float
    title: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3),
                "title": self.title}


def parse_chapters(payload: str) -> list[Chapter]:
    """Read ffprobe's -show_chapters JSON."""
    try:
        data = json.loads(payload or "{}")
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []

    chapters: list[Chapter] = []
    for raw in data.get("chapters") or []:
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("start_time"))
            end = float(raw.get("end_time"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        title = ""
        tags = raw.get("tags")
        if isinstance(tags, dict):
            title = str(tags.get("title") or "").strip()[:120]
        chapters.append(Chapter(start, end, title))
    return chapters


def skippable(chapters: list[Chapter], duration: float = 0.0) -> list[dict]:
    """Segments worth offering a skip button for."""
    segments: list[dict] = []
    for chapter in chapters:
        if chapter.duration < MIN_SEGMENT_SECONDS:
            continue

        if INTRO_WORDS.search(chapter.title):
            # A whole-episode "intro" is a mislabel; skipping it would lose
            # the episode.
            if chapter.duration > MAX_INTRO_SECONDS:
                continue
            segments.append({"start": chapter.start, "end": chapter.end,
                             "label": "Skip intro", "kind": "intro"})
        elif CREDIT_WORDS.search(chapter.title):
            # Only the closing credits are worth a button; a mid-file chapter
            # called "ED" in an anime rip is usually the ending theme.
            if duration and chapter.start < duration * 0.5:
                continue
            segments.append({"start": chapter.start, "end": chapter.end,
                             "label": "Skip credits", "kind": "credits"})
    return segments


def read_chapters(ffprobe: str | None, path: Path,
                  runner=None) -> list[Chapter]:
    if not ffprobe:
        return []
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json",
           "-show_chapters", str(path)]
    try:
        run = runner or _default_runner
        result = run(cmd)
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("Chapter probe failed for %s: %s", path.name, exc)
        return []
    return parse_chapters(result)


def _default_runner(cmd) -> str:
    from .ffmpeg import run_quiet

    result = run_quiet(cmd, timeout=20)
    return result.stdout.decode("utf-8", "replace")
