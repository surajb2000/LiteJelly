"""Subtitle discovery and conversion to WebVTT.

Browsers only understand WebVTT, so everything found on disk or inside a
container is normalised to it. SubRip is converted in pure Python so subtitles
still work when ffmpeg is unavailable; other text formats go through ffmpeg.
Bitmap subtitles (PGS/VobSub) cannot be converted and are flagged for burn-in.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

from .ffmpeg import MediaInfo, run_quiet

log = logging.getLogger("litejelly.subtitles")

SUBTITLE_EXTENSIONS = {".srt", ".vtt", ".ass", ".ssa", ".sub", ".sbv", ".smi"}
# Converted by ffmpeg rather than in-process.
FFMPEG_ONLY_EXTENSIONS = {".ass", ".ssa", ".sub", ".sbv", ".smi"}
SUBTITLE_DIR_NAMES = {"subs", "subtitles", "sub"}

LANGUAGE_NAMES = {
    "en": "English", "eng": "English", "english": "English",
    "es": "Spanish", "spa": "Spanish", "spanish": "Spanish",
    "fr": "French", "fre": "French", "fra": "French", "french": "French",
    "de": "German", "ger": "German", "deu": "German", "german": "German",
    "it": "Italian", "ita": "Italian", "italian": "Italian",
    "pt": "Portuguese", "por": "Portuguese", "portuguese": "Portuguese",
    "nl": "Dutch", "dut": "Dutch", "nld": "Dutch", "dutch": "Dutch",
    "ru": "Russian", "rus": "Russian", "russian": "Russian",
    "ja": "Japanese", "jpn": "Japanese", "japanese": "Japanese",
    "ko": "Korean", "kor": "Korean", "korean": "Korean",
    "zh": "Chinese", "chi": "Chinese", "zho": "Chinese", "chinese": "Chinese",
    "hi": "Hindi", "hin": "Hindi", "hindi": "Hindi",
    "ta": "Tamil", "tam": "Tamil", "tamil": "Tamil",
    "te": "Telugu", "tel": "Telugu", "telugu": "Telugu",
    "bn": "Bengali", "ben": "Bengali", "bengali": "Bengali",
    "mr": "Marathi", "mar": "Marathi", "marathi": "Marathi",
    "ar": "Arabic", "ara": "Arabic", "arabic": "Arabic",
    "tr": "Turkish", "tur": "Turkish", "turkish": "Turkish",
    "pl": "Polish", "pol": "Polish", "polish": "Polish",
    "sv": "Swedish", "swe": "Swedish", "swedish": "Swedish",
    "da": "Danish", "dan": "Danish", "danish": "Danish",
    "no": "Norwegian", "nor": "Norwegian", "norwegian": "Norwegian",
    "fi": "Finnish", "fin": "Finnish", "finnish": "Finnish",
    "cs": "Czech", "cze": "Czech", "czech": "Czech",
    "el": "Greek", "gre": "Greek", "greek": "Greek",
    "he": "Hebrew", "heb": "Hebrew", "hebrew": "Hebrew",
    "th": "Thai", "tha": "Thai", "thai": "Thai",
    "vi": "Vietnamese", "vie": "Vietnamese", "vietnamese": "Vietnamese",
    "id": "Indonesian", "ind": "Indonesian", "indonesian": "Indonesian",
}
_TO_BCP47 = {v: k for k, v in LANGUAGE_NAMES.items() if len(k) == 2}

_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_VTT_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})\.(\d{1,3})")
_CUE_INDEX = re.compile(r"^\d+$")


@dataclass
class SubtitleTrack:
    id: str
    label: str
    language: str          # BCP-47-ish tag for the <track srclang> attribute
    kind: str              # "external" | "embedded"
    codec: str = ""
    forced: bool = False
    default: bool = False
    burn_in_only: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _language_from_token(token: str) -> tuple[str, str]:
    """Map a filename token or ffprobe tag to (srclang, display name)."""
    key = token.strip().lower().replace("_", "-")
    base = key.split("-")[0]
    name = LANGUAGE_NAMES.get(key) or LANGUAGE_NAMES.get(base)
    if not name:
        return "", ""
    return _TO_BCP47.get(name, base[:2]), name


def _sidecar_candidates(video_path: Path) -> list[Path]:
    stem = video_path.stem.lower()
    found: list[Path] = []
    seen: set[str] = set()

    def collect(directory: Path, require_stem: bool) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            if require_stem and not entry.stem.lower().startswith(stem):
                continue
            key = str(entry).lower()
            if key not in seen:
                seen.add(key)
                found.append(entry)

    parent = video_path.parent
    collect(parent, require_stem=True)

    # Rips often drop subtitles in a Subs/ folder, sometimes one per title.
    try:
        children = sorted(p for p in parent.iterdir() if p.is_dir())
    except OSError:
        children = []
    for child in children:
        if child.name.lower() not in SUBTITLE_DIR_NAMES:
            continue
        collect(child, require_stem=False)
        nested = child / video_path.stem
        if nested.is_dir():
            collect(nested, require_stem=False)
    return found


def _describe_sidecar(video_path: Path, sub_path: Path, position: int) -> SubtitleTrack:
    stem = video_path.stem.lower()
    name = sub_path.stem
    remainder = name[len(stem):] if name.lower().startswith(stem) else name
    tokens = [t for t in re.split(r"[.\-_ ]+", remainder) if t]

    forced = any(t.lower() == "forced" for t in tokens)
    sdh = any(t.lower() in {"sdh", "cc", "hi"} for t in tokens)
    srclang, language_name = "", ""
    for token in tokens:
        srclang, language_name = _language_from_token(token)
        if language_name:
            break

    label = language_name or (" ".join(tokens).strip() or sub_path.stem)
    if forced:
        label += " (Forced)"
    if sdh:
        label += " (SDH)"
    return SubtitleTrack(
        id=f"ext:{position}",
        label=label,
        language=srclang,
        kind="external",
        codec=sub_path.suffix.lstrip("."),
        forced=forced,
    )


def discover(video_path: Path, info: MediaInfo) -> list[SubtitleTrack]:
    """All subtitle tracks available for a video, external first."""
    tracks: list[SubtitleTrack] = []

    for position, sub_path in enumerate(_sidecar_candidates(video_path)):
        tracks.append(_describe_sidecar(video_path, sub_path, position))

    for stream in info.subtitles:
        srclang, language_name = _language_from_token(stream.language)
        label = stream.title or language_name or f"Track {stream.index + 1}"
        if stream.forced and "forced" not in label.lower():
            label += " (Forced)"
        if stream.is_bitmap:
            label += " (Image)"
        tracks.append(SubtitleTrack(
            id=f"emb:{stream.index}",
            label=label,
            language=srclang,
            kind="embedded",
            codec=stream.codec,
            forced=stream.forced,
            default=stream.default,
            burn_in_only=stream.is_bitmap,
        ))
    return tracks


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def srt_to_vtt(text: str) -> str:
    """Convert SubRip to WebVTT without touching ffmpeg."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines_out: list[str] = ["WEBVTT", ""]
    previous_blank = True

    for line in text.split("\n"):
        stripped = line.strip()
        if previous_blank and _CUE_INDEX.match(stripped):
            # Drop the numeric counter; WebVTT does not need it.
            previous_blank = False
            continue
        if "-->" in stripped:
            converted = _SRT_TIME.sub(
                lambda m: (
                    f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3)}.{m.group(4):0<3.3}"
                    f" --> "
                    f"{int(m.group(5)):02d}:{m.group(6)}:{m.group(7)}.{m.group(8):0<3.3}"
                ),
                stripped,
            )
            lines_out.append(converted)
        else:
            lines_out.append(line)
        previous_blank = not stripped
    return "\n".join(lines_out) + "\n"


def _ensure_vtt_header(text: str) -> str:
    text = text.lstrip("\ufeff")
    if not text.lstrip().upper().startswith("WEBVTT"):
        return "WEBVTT\n\n" + text
    return text


def _cue_seconds(match: re.Match) -> float:
    hours = int(match.group(1) or 0)
    millis = int(match.group(4).ljust(3, "0"))
    return hours * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + millis / 1000.0


def _format_cue_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


def shift_vtt(text: str, offset: float) -> str:
    """Re-base cue timings for a stream that was restarted at ``offset`` seconds.

    Cues that end before the offset are dropped entirely.
    """
    if offset <= 0:
        return text

    blocks_out: list[str] = []
    for block in re.split(r"\n\s*\n", text.strip("\n")):
        lines = block.split("\n")
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            blocks_out.append(block)
            continue

        line = lines[timing_index]
        matches = list(_VTT_TIME.finditer(line))
        if len(matches) < 2:
            blocks_out.append(block)
            continue

        start = _cue_seconds(matches[0]) - offset
        end = _cue_seconds(matches[1]) - offset
        if end <= 0:
            continue

        trailing = line[matches[1].end():]
        lines[timing_index] = f"{_format_cue_time(start)} --> {_format_cue_time(end)}{trailing}"
        blocks_out.append("\n".join(lines))

    return "\n\n".join(blocks_out) + "\n"


class SubtitleService:
    """Resolves track ids to WebVTT payloads, with an on-disk conversion cache."""

    def __init__(self, tools, cache_dir: Path):
        self.tools = tools
        self.cache_dir = cache_dir / "subtitles"

    def _cache_path(self, video_path: Path, track_id: str) -> Path | None:
        try:
            stat = video_path.stat()
        except OSError:
            return None
        key = f"{video_path}|{stat.st_mtime_ns}|{track_id}"
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.vtt"

    def get_vtt(self, video_path: Path, track_id: str, offset: float = 0.0) -> str | None:
        base = self._get_base_vtt(video_path, track_id)
        if base is None:
            return None
        return shift_vtt(base, offset)

    def _get_base_vtt(self, video_path: Path, track_id: str) -> str | None:
        cache_path = self._cache_path(video_path, track_id)
        if cache_path and cache_path.is_file():
            try:
                return cache_path.read_text(encoding="utf-8")
            except OSError:
                pass

        vtt = self._build_vtt(video_path, track_id)
        if vtt and cache_path:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(vtt, encoding="utf-8")
            except OSError as exc:
                log.debug("Could not cache subtitle: %s", exc)
        return vtt

    def _build_vtt(self, video_path: Path, track_id: str) -> str | None:
        kind, _, raw_index = track_id.partition(":")
        if not raw_index.isdigit():
            return None
        index = int(raw_index)

        if kind == "ext":
            candidates = _sidecar_candidates(video_path)
            if index >= len(candidates):
                return None
            return self._convert_file(candidates[index])
        if kind == "emb":
            return self._extract_embedded(video_path, index)
        return None

    def _convert_file(self, sub_path: Path) -> str | None:
        suffix = sub_path.suffix.lower()
        if suffix in FFMPEG_ONLY_EXTENSIONS:
            return self._ffmpeg_to_vtt(["-i", str(sub_path)])
        try:
            text = _decode_text(sub_path.read_bytes())
        except OSError as exc:
            log.warning("Could not read %s: %s", sub_path.name, exc)
            return None
        if suffix == ".vtt":
            return _ensure_vtt_header(text)
        return srt_to_vtt(text)

    def _extract_embedded(self, video_path: Path, index: int) -> str | None:
        if not self.tools.available:
            return None
        return self._ffmpeg_to_vtt(["-i", str(video_path), "-map", f"0:s:{index}"])

    def _ffmpeg_to_vtt(self, middle: list[str]) -> str | None:
        if not self.tools.available:
            return None
        cmd = [self.tools.ffmpeg, "-hide_banner", "-loglevel", "error",
               *middle, "-f", "webvtt", "pipe:1"]
        try:
            result = run_quiet(cmd, timeout=60)
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("Subtitle extraction failed: %s", exc)
            return None
        if result.returncode != 0 or not result.stdout:
            log.warning("Subtitle extraction produced no output")
            return None
        return _ensure_vtt_header(result.stdout.decode("utf-8", "replace"))
