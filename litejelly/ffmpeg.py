"""ffmpeg/ffprobe discovery, media probing and playback planning."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("litejelly.ffmpeg")

# Codecs every mainstream browser can decode natively.
DIRECT_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}
DIRECT_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis"}
# Containers a browser will accept from a plain byte-range stream.
DIRECT_CONTAINERS = {"mp4", "m4v", "webm"}

TEXT_SUBTITLE_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt",
    "text", "subviewer", "subviewer1", "microdvd",
}
BITMAP_SUBTITLE_CODECS = {
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub",
}

# Keep ffmpeg from flashing a console window on Windows.
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _binary_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _double_bitrate(rate: str) -> str:
    """'3000k' -> '6000k'; used for the rate-control buffer size."""
    text = str(rate).strip().lower()
    suffix = ""
    if text and text[-1] in "km":
        suffix, text = text[-1], text[:-1]
    try:
        return f"{int(float(text) * 2)}{suffix}"
    except ValueError:
        return "6000k"


def find_binary(name: str, app_dir: Path) -> str | None:
    """Look for a bundled binary next to the app first, then on PATH."""
    local = app_dir / _binary_name(name)
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    bin_dir = app_dir / "bin" / _binary_name(name)
    if bin_dir.is_file() and os.access(bin_dir, os.X_OK):
        return str(bin_dir)
    return shutil.which(name)


def run_quiet(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        creationflags=_CREATION_FLAGS,
    )


def popen_quiet(cmd: list[str], stdout=subprocess.PIPE) -> subprocess.Popen:
    return subprocess.Popen(
        cmd,
        stdout=stdout,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATION_FLAGS,
    )


@dataclass
class SubtitleStream:
    index: int          # index within subtitle streams only (usable as 0:s:N)
    codec: str
    language: str = ""
    title: str = ""
    forced: bool = False
    default: bool = False

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_SUBTITLE_CODECS

    @property
    def is_bitmap(self) -> bool:
        return self.codec in BITMAP_SUBTITLE_CODECS


@dataclass
class MediaInfo:
    duration: float = 0.0
    container: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    width: int = 0
    height: int = 0
    subtitles: list[SubtitleStream] = field(default_factory=list)
    probed: bool = False


@dataclass
class PlaybackPlan:
    mode: str                 # "direct" | "remux" | "transcode"
    video_action: str         # "copy" | "encode"
    audio_action: str         # "copy" | "encode"
    reason: str
    seekable: bool            # can the client seek without restarting the stream?


class FFmpegTools:
    """Locates the binaries and caches probe results per (path, mtime, size)."""

    def __init__(self, app_dir: Path, transcode_slots: int = 2, thumbnail_slots: int = 2):
        self.ffmpeg = find_binary("ffmpeg", app_dir)
        self.ffprobe = find_binary("ffprobe", app_dir)
        self.transcode_sem = threading.BoundedSemaphore(max(1, transcode_slots))
        self.thumbnail_sem = threading.BoundedSemaphore(max(1, thumbnail_slots))
        self._probe_cache: dict[tuple, MediaInfo] = {}
        self._probe_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.ffmpeg is not None

    @property
    def can_probe(self) -> bool:
        return self.ffprobe is not None

    def probe(self, path: Path) -> MediaInfo:
        """Probe a file, memoising on (path, mtime, size)."""
        try:
            stat = path.stat()
            key = (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            return MediaInfo()

        with self._probe_lock:
            cached = self._probe_cache.get(key)
        if cached is not None:
            return cached

        info = self._probe_uncached(path)

        with self._probe_lock:
            if len(self._probe_cache) > 2000:
                self._probe_cache.clear()
            self._probe_cache[key] = info
        return info

    def _probe_uncached(self, path: Path) -> MediaInfo:
        if not self.ffprobe:
            return MediaInfo(container=path.suffix.lstrip(".").lower())

        cmd = [
            self.ffprobe, "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ]
        try:
            result = run_quiet(cmd, timeout=25)
            if result.returncode != 0:
                log.warning("ffprobe failed for %s", path.name)
                return MediaInfo(container=path.suffix.lstrip(".").lower())
            payload = json.loads(result.stdout.decode("utf-8", "replace"))
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
            log.warning("ffprobe error for %s: %s", path.name, exc)
            return MediaInfo(container=path.suffix.lstrip(".").lower())

        info = MediaInfo(probed=True)
        fmt = payload.get("format") or {}
        try:
            info.duration = float(fmt.get("duration") or 0.0)
        except (TypeError, ValueError):
            info.duration = 0.0

        names = str(fmt.get("format_name") or "").split(",")
        info.container = path.suffix.lstrip(".").lower() or (names[0] if names else "")

        sub_index = 0
        for stream in payload.get("streams") or []:
            kind = stream.get("codec_type")
            codec = str(stream.get("codec_name") or "").lower()
            if kind == "video" and not info.video_codec:
                if stream.get("disposition", {}).get("attached_pic"):
                    continue
                info.video_codec = codec
                info.width = int(stream.get("width") or 0)
                info.height = int(stream.get("height") or 0)
            elif kind == "audio" and not info.audio_codec:
                info.audio_codec = codec
            elif kind == "subtitle":
                tags = stream.get("tags") or {}
                disp = stream.get("disposition") or {}
                info.subtitles.append(SubtitleStream(
                    index=sub_index,
                    codec=codec,
                    language=str(tags.get("language") or "").lower(),
                    title=str(tags.get("title") or ""),
                    forced=bool(disp.get("forced")),
                    default=bool(disp.get("default")),
                ))
                sub_index += 1
        return info

    def plan_playback(self, info: MediaInfo, allow_hevc_direct: bool = False) -> PlaybackPlan:
        """Pick the cheapest playback path the browser will accept."""
        if not self.available:
            return PlaybackPlan("direct", "copy", "copy", "ffmpeg unavailable", True)

        if not info.probed:
            if info.container in DIRECT_CONTAINERS:
                return PlaybackPlan("direct", "copy", "copy", "Container is browser-native", True)
            return PlaybackPlan("transcode", "encode", "encode", "Unknown format", False)

        video_ok = info.video_codec in DIRECT_VIDEO_CODECS
        if not video_ok and info.video_codec == "hevc" and allow_hevc_direct:
            video_ok = True
        audio_ok = info.audio_codec in DIRECT_AUDIO_CODECS or not info.audio_codec
        container_ok = info.container in DIRECT_CONTAINERS

        if container_ok and video_ok and audio_ok:
            return PlaybackPlan("direct", "copy", "copy", "Direct play", True)

        if video_ok:
            audio_action = "copy" if audio_ok else "encode"
            detail = "Remuxed" if audio_ok else f"Remuxed, {info.audio_codec or 'audio'} to aac"
            return PlaybackPlan("remux", "copy", audio_action, detail, False)

        return PlaybackPlan(
            "transcode", "encode", "encode",
            f"Transcoded from {info.video_codec or 'unknown'}", False,
        )

    def build_stream_command(
        self,
        path: Path,
        plan: PlaybackPlan,
        settings,
        start: float = 0.0,
        burn_subtitle_index: int | None = None,
    ) -> list[str]:
        """ffmpeg command producing a fragmented MP4 on stdout."""
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error"]
        if start > 0:
            cmd += ["-ss", f"{start:.3f}"]
        cmd += ["-i", str(path)]

        width, height = settings.size
        burning = burn_subtitle_index is not None

        if burning:
            # Bitmap subtitles have to be composited onto the video.
            cmd += [
                "-filter_complex",
                f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black[base];"
                f"[base][0:s:{burn_subtitle_index}]overlay[v]",
                "-map", "[v]", "-map", "0:a:0?",
            ]
        else:
            cmd += ["-map", "0:v:0", "-map", "0:a:0?"]

        if plan.video_action == "copy" and not burning:
            cmd += ["-c:v", "copy"]
        else:
            cmd += [
                "-c:v", settings.video_codec,
                "-preset", settings.preset,
                "-crf", str(settings.crf),
                "-maxrate", settings.max_video_bitrate,
                "-bufsize", _double_bitrate(settings.max_video_bitrate),
                "-pix_fmt", "yuv420p",
            ]
            if not burning:
                cmd += [
                    "-vf",
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
                ]

        if plan.audio_action == "copy":
            cmd += ["-c:a", "copy"]
        else:
            cmd += [
                "-c:a", settings.audio_codec,
                "-b:a", settings.audio_bitrate,
                "-ac", "2",
                "-ar", "48000",
            ]

        cmd += [
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4",
            "pipe:1",
        ]
        return cmd
