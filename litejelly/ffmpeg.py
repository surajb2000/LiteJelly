"""ffmpeg/ffprobe discovery, media probing and playback planning."""

from __future__ import annotations

import json
import logging
import math
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

FRAGMENT_MICROSECONDS = 2_000_000

# RFC 6381 codec strings for MediaSource.isTypeSupported. ffprobe profile
# names map to profile_idc (h264) or general_profile_idc + compatibility
# flags (hevc).
_H264_PROFILE_IDC = {
    "baseline": 0x42, "constrained baseline": 0x42, "main": 0x4D,
    "extended": 0x58, "high": 0x64, "high 10": 0x6E, "high 4:2:2": 0x7A,
    "high 4:4:4 predictive": 0xF4,
}
_HEVC_PROFILES = {"main": (1, "6"), "main 10": (2, "4"), "main still picture": (3, "C")}
_AAC_PROFILES = {"lc": "mp4a.40.2", "main": "mp4a.40.1", "he-aac": "mp4a.40.5",
                 "he-aacv2": "mp4a.40.29"}
_ENCODER_VIDEO_STRINGS = {"libx264": "avc1.640028", "h264": "avc1.640028",
                          "libx265": "hvc1.1.6.L120.B0", "hevc": "hvc1.1.6.L120.B0"}
_ENCODER_AUDIO_STRINGS = {"aac": "mp4a.40.2", "libfdk_aac": "mp4a.40.2", "libopus": "opus",
                          "opus": "opus", "libmp3lame": "mp4a.6B", "mp3": "mp4a.6B"}


def _video_codec_string(codec: str, profile: str, level: int) -> str:
    codec, profile = codec.lower(), profile.lower().strip()
    if codec == "h264":
        idc = _H264_PROFILE_IDC.get(profile, 0x64)
        constraint = 0x40 if profile.startswith("constrained") else 0x00
        lvl = level if 9 < level < 256 else 40
        return f"avc1.{idc:02X}{constraint:02X}{lvl:02X}"
    if codec == "hevc":
        idc, compat = _HEVC_PROFILES.get(profile, (1, "6"))
        return f"hvc1.{idc}.{compat}.L{level if level > 0 else 120}.B0"
    if codec == "vp9":
        digit = profile[-1] if profile[-1:].isdigit() else "0"
        return f"vp09.0{digit}.10.{'10' if digit in '23' else '08'}"
    if codec == "av1":
        return "av01.0.08M.08"
    return ""


def _audio_codec_string(codec: str, profile: str) -> str:
    codec = codec.lower()
    if codec == "aac":
        return _AAC_PROFILES.get(profile.lower().strip(), "mp4a.40.2")
    return {"mp3": "mp4a.6B", "opus": "opus", "flac": "flac",
            "ac3": "ac-3", "eac3": "ec-3"}.get(codec, "")


def stream_mime(info: MediaInfo, plan: PlaybackPlan, settings, burning: bool = False) -> str:
    """MIME type of the piped stream, or "" when it cannot be stated safely.

    An empty result tells the client to use a plain <video src> rather than
    MediaSource, so an unknown codec never breaks playback.
    """
    if plan.video_action == "copy" and not burning:
        video = _video_codec_string(info.video_codec, info.profile, info.level)
    else:
        video = _ENCODER_VIDEO_STRINGS.get(str(settings.video_codec).lower(), "")
    if not video:
        return ""
    parts = [video]
    if info.audio_codec:
        if plan.audio_action == "copy":
            audio = _audio_codec_string(info.audio_codec, info.audio_profile)
        else:
            audio = _ENCODER_AUDIO_STRINGS.get(str(settings.audio_codec).lower(), "")
        if not audio:
            return ""
        parts.append(audio)
    return f'video/mp4; codecs="{", ".join(parts)}"'


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


def find_binary(name: str, app_dir: Path, override: str | None = None) -> str | None:
    """Explicit path, then a copy next to the app, then PATH."""
    if override:
        candidate = Path(os.path.expandvars(os.path.expanduser(str(override))))
        if candidate.is_dir():
            candidate = candidate / _binary_name(name)
        if candidate.is_file():
            return str(candidate)
        log.warning("Configured %s path is not usable: %s", name, override)

    local = app_dir / _binary_name(name)
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    bin_dir = app_dir / "bin" / _binary_name(name)
    if bin_dir.is_file() and os.access(bin_dir, os.X_OK):
        return str(bin_dir)
    return shutil.which(name)


def _scale_filter(width: int, height: int) -> str:
    """Fit inside the box without ever upscaling, keeping dimensions even.

    No padding: black bars baked into the stream would waste bitrate, and the
    player letterboxes on its own.
    """
    return (
        f"scale=w='min({width},iw)':h='min({height},ih)'"
        ":force_original_aspect_ratio=decrease"
        ",scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )


def _parse_times(stdout: bytes) -> list[float]:
    """Seconds from ffprobe ``csv=p=0`` output, skipping N/A and side-data noise."""
    times = []
    for line in stdout.decode("utf-8", "replace").splitlines():
        try:
            times.append(float(line.strip().rstrip(",")))
        except ValueError:
            continue
    return times


def run_quiet(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        creationflags=_CREATION_FLAGS,
    )


def popen_quiet(cmd: list[str], stdout=subprocess.PIPE) -> subprocess.Popen:
    # stdin must not be inherited: ffmpeg puts the controlling terminal into
    # no-echo mode to read its interactive keys, and killing the server before
    # ffmpeg restores it leaves the shell with invisible input.
    return subprocess.Popen(
        cmd,
        stdout=stdout,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
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
    has_b_frames: int = 0
    fps: float = 0.0
    profile: str = ""          # ffprobe wording, e.g. "High", "Main 10"
    level: int = 0             # h264: 40 = 4.0; hevc: 120 = 4.0
    audio_profile: str = ""    # aac only: "LC", "HE-AAC", "HE-AACv2"
    subtitles: list[SubtitleStream] = field(default_factory=list)
    probed: bool = False

    @property
    def reorder_delay(self) -> float:
        """Seconds the muxer pushes copied video later to keep DTS <= PTS.

        Matroska stores no DTS, so muxing a B-frame stream into MP4 shifts the
        video by the reorder depth while the audio stays put.
        """
        if self.has_b_frames > 0 and self.fps > 0:
            return self.has_b_frames / self.fps
        return 0.0


@dataclass
class PlaybackPlan:
    mode: str                 # "direct" | "remux" | "transcode"
    video_action: str         # "copy" | "encode"
    audio_action: str         # "copy" | "encode"
    reason: str
    seekable: bool            # can the client seek without restarting the stream?


@dataclass
class QualityLevel:
    id: str
    label: str
    width: int = 0            # 0 means "keep the source resolution"
    height: int = 0
    bitrate: str = ""


# "auto" follows config.json; "original" never downscales.
QUALITY_LADDER = [
    QualityLevel("auto", "Auto"),
    QualityLevel("original", "Original"),
    QualityLevel("1080p", "1080p", 1920, 1080, "8000k"),
    QualityLevel("720p", "720p", 1280, 720, "3000k"),
    QualityLevel("480p", "480p", 854, 480, "1500k"),
    QualityLevel("360p", "360p", 640, 360, "800k"),
]
QUALITY_BY_ID = {level.id: level for level in QUALITY_LADDER}


def resolve_quality(quality_id: str | None) -> QualityLevel:
    return QUALITY_BY_ID.get((quality_id or "auto").lower(), QUALITY_BY_ID["auto"])


def fit_within(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    """Scale down to fit the box, preserving aspect ratio. Never upscales."""
    if not width or not height or not max_width or not max_height:
        return width, height
    if width <= max_width and height <= max_height:
        return width, height
    ratio = min(max_width / width, max_height / height)
    return max(2, int(width * ratio) // 2 * 2), max(2, int(height * ratio) // 2 * 2)


class FFmpegTools:
    """Locates the binaries and caches probe results per (path, mtime, size)."""

    def __init__(self, app_dir: Path, transcode_slots: int = 2, thumbnail_slots: int = 2,
                 ffmpeg_path: str | None = None, ffprobe_path: str | None = None):
        self.ffmpeg = find_binary("ffmpeg", app_dir, ffmpeg_path)
        self.ffprobe = find_binary("ffprobe", app_dir, ffprobe_path)
        # The two ship together, so fall back to ffmpeg's own directory.
        if self.ffmpeg and not self.ffprobe:
            sibling = Path(self.ffmpeg).parent / _binary_name("ffprobe")
            if sibling.is_file():
                self.ffprobe = str(sibling)
        self.transcode_sem = threading.BoundedSemaphore(max(1, transcode_slots))
        self.thumbnail_sem = threading.BoundedSemaphore(max(1, thumbnail_slots))
        self._probe_cache: dict[tuple, MediaInfo] = {}
        self._probe_lock = threading.Lock()
        self._keyframe_cache: dict[tuple, float] = {}

    @property
    def available(self) -> bool:
        return self.ffmpeg is not None

    @property
    def can_probe(self) -> bool:
        return self.ffprobe is not None

    def describe(self, which: str = "ffmpeg") -> str:
        """Human-readable '<version> (<path>)', for the startup banner."""
        binary = self.ffmpeg if which == "ffmpeg" else self.ffprobe
        if not binary:
            return "NOT FOUND"
        try:
            result = run_quiet([binary, "-version"], timeout=10)
            first = result.stdout.decode("utf-8", "replace").splitlines()[0]
            version = first.split(" version ")[-1].split(" ")[0]
        except (subprocess.SubprocessError, OSError, IndexError):
            version = "unknown version"
        return f"{version}  ({binary})"

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
                info.has_b_frames = int(stream.get("has_b_frames") or 0)
                info.profile = str(stream.get("profile") or "")
                try:
                    info.level = int(stream.get("level") or 0)
                except (TypeError, ValueError):
                    info.level = 0
                rate = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "")
                numerator, _, denominator = rate.partition("/")
                try:
                    if denominator and float(denominator):
                        info.fps = float(numerator) / float(denominator)
                except ValueError:
                    info.fps = 0.0
            elif kind == "audio" and not info.audio_codec:
                info.audio_codec = codec
                info.audio_profile = str(stream.get("profile") or "")
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

    def plan_playback(self, info: MediaInfo, allow_hevc_direct: bool = False,
                      quality: QualityLevel | None = None) -> PlaybackPlan:
        """Pick the cheapest playback path the browser will accept."""
        plan = self._natural_plan(info, allow_hevc_direct)

        # Picking a numbered rung always re-encodes, even at the source size:
        # it is the escape hatch when a stream copy misbehaves on a client.
        if (self.available and quality is not None and quality.height
                and plan.video_action == "copy"):
            downscaling = bool(info.height) and info.height > quality.height
            reason = (f"Downscaled to {quality.label} on request" if downscaling
                      else f"Re-encoded at {quality.label} on request")
            return PlaybackPlan("transcode", "encode", "encode", reason, False)
        return plan

    def _natural_plan(self, info: MediaInfo, allow_hevc_direct: bool) -> PlaybackPlan:
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

    def _keyframe_key(self, path: Path):
        try:
            stat = path.stat()
        except OSError:
            return None
        return (str(path), stat.st_mtime_ns)

    def seek_landing(self, path: Path, target: float, forward: bool = False,
                     _window: float = 30.0, _tolerance: float = 2.0) -> float:
        """Where a seek to ``target`` will actually start.

        ffprobe performs the same seek as ffmpeg, so this matches the stream the
        client receives. A keyframe index cannot be used instead: open-GOP
        encodes (x265's default) mark CRA frames as keyframes even though they
        are not valid entry points, and passing one as -ss makes ffmpeg rewind
        a whole GOP.

        ``forward`` picks the first valid entry point at or after ``target``
        instead, so a skip never lands well inside the segment being skipped.
        A landing just short of the target is kept: on the measured 10 s GOP,
        skipping to 40.0 would otherwise drop 10 s of content to avoid 0.13 s
        of intro tail.
        """
        if not self.ffprobe or target <= 0:
            return 0.0

        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        cache_key = (str(path), mtime, round(target, 1), forward)
        with self._probe_lock:
            cached = self._keyframe_cache.get(cache_key)
        if cached is not None:
            return cached

        resolved = self._probe_landing(path, target)
        if forward and resolved < target - _tolerance:
            for candidate in self._keyframes_after(path, target, _window):
                # Round up so the -ss string cannot fall a hair before the frame.
                entry = math.ceil(candidate * 1000) / 1000
                # A CRA frame seeks back a whole GOP; a real entry point lands on itself.
                if self._probe_landing(path, entry) >= candidate - 0.05:
                    resolved = entry
                    break

        with self._probe_lock:
            if len(self._keyframe_cache) > 500:
                self._keyframe_cache.clear()
            self._keyframe_cache[cache_key] = resolved
        return resolved

    def _probe_landing(self, path: Path, target: float) -> float:
        cmd = [
            self.ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "frame=pts_time",
            "-read_intervals", f"{target:.3f}%+#1",
            "-of", "csv=p=0", str(path),
        ]
        try:
            result = run_quiet(cmd, timeout=20)
        except (subprocess.SubprocessError, OSError) as exc:
            log.debug("Seek probe failed for %s: %s", path.name, exc)
            return target
        times = _parse_times(result.stdout)
        return times[0] if times else target

    def _keyframes_after(self, path: Path, target: float, window: float) -> list[float]:
        """Keyframe times at or after ``target``, widening once for long GOPs."""
        for span in (window, window * 3):
            cmd = [
                self.ffprobe, "-v", "error",
                "-skip_frame", "nokey",
                "-select_streams", "v:0",
                "-show_entries", "frame=pts_time",
                "-read_intervals", f"{target:.3f}%+{span:.0f}",
                "-of", "csv=p=0", str(path),
            ]
            try:
                result = run_quiet(cmd, timeout=30)
            except (subprocess.SubprocessError, OSError) as exc:
                log.debug("Keyframe probe failed for %s: %s", path.name, exc)
                return []
            times = sorted(t for t in _parse_times(result.stdout) if t >= target - 0.01)
            if times:
                return times
        return []

    def output_size(self, info: MediaInfo, plan: PlaybackPlan, settings,
                    quality: QualityLevel | None = None) -> tuple[int, int]:
        """Resolution the client will actually receive."""
        if plan.video_action == "copy":
            return info.width, info.height
        if quality is not None and quality.id == "original":
            return info.width, info.height
        if quality is not None and quality.height:
            return fit_within(info.width, info.height, quality.width, quality.height)
        return fit_within(info.width, info.height, *settings.size)

    def build_stream_command(
        self,
        path: Path,
        plan: PlaybackPlan,
        settings,
        start: float = 0.0,
        burn_subtitle_index: int | None = None,
        quality: QualityLevel | None = None,
        audio_delay_ms: float = 0.0,
        info: MediaInfo | None = None,
    ) -> list[str]:
        """ffmpeg command producing a fragmented MP4 on stdout."""
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        # MKV timestamps are often sparse; regenerate them before seeking.
        cmd += ["-fflags", "+genpts"]
        if start > 0:
            # Accurate seek trims the audio to the exact timestamp, but copied
            # video can only begin at a keyframe, which splits them by up to a
            # whole GOP. Re-encoded video is trimmed too, so it stays accurate.
            if plan.video_action == "copy":
                cmd += ["-noaccurate_seek"]
            cmd += ["-ss", f"{start:.3f}"]
        cmd += ["-i", str(path)]

        keep_source = quality is not None and quality.id == "original"
        if quality is not None and quality.height:
            width, height, max_rate = quality.width, quality.height, quality.bitrate
        else:
            width, height = settings.size
            max_rate = settings.max_video_bitrate

        burning = burn_subtitle_index is not None
        scale = _scale_filter(width, height)

        if burning:
            # Composite bitmap subtitles at native size, then scale the result;
            # scaling first would misplace the overlay.
            if keep_source:
                graph = f"[0:v][0:s:{burn_subtitle_index}]overlay[v]"
            else:
                graph = f"[0:v][0:s:{burn_subtitle_index}]overlay[ov];[ov]{scale}[v]"
            cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?"]
        else:
            cmd += ["-map", "0:v:0", "-map", "0:a:0?"]

        if plan.video_action == "copy" and not burning:
            cmd += ["-c:v", "copy"]
            if info is not None and info.video_codec == "hevc":
                # Browsers refuse the default hev1 sample entry.
                cmd += ["-tag:v", "hvc1"]
        else:
            cmd += [
                "-c:v", settings.video_codec,
                "-preset", settings.preset,
                "-crf", str(settings.crf),
                "-pix_fmt", "yuv420p",
            ]
            if not keep_source:
                cmd += ["-maxrate", max_rate, "-bufsize", _double_bitrate(max_rate)]
            if not burning and not keep_source:
                cmd += ["-vf", scale]

        if plan.audio_action == "copy":
            cmd += ["-c:a", "copy"]
        else:
            cmd += [
                "-c:a", settings.audio_codec,
                "-b:a", settings.audio_bitrate,
                "-ac", "2",
                "-ar", "48000",
            ]
            # Copied video keeps source timestamps while the audio is rebuilt,
            # so pad/trim the audio to stay locked to the video clock.
            if not burning:
                filters = ["aresample=async=1"]
                if audio_delay_ms > 0.5:
                    filters.append(f"adelay={audio_delay_ms:.0f}:all=1")
                elif audio_delay_ms < -0.5:
                    filters.append(f"atrim=start={abs(audio_delay_ms) / 1000:.3f}")
                    filters.append("asetpts=PTS-STARTPTS")
                cmd += ["-af", ",".join(filters)]

        cmd += [
            "-avoid_negative_ts", "make_zero",
            # Measured: frag_keyframe alone gave one fragment per GOP (10 s on
            # the reference clip); MSE cannot play a fragment until it is whole.
            "-frag_duration", str(FRAGMENT_MICROSECONDS),
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4",
            "pipe:1",
        ]
        return cmd
