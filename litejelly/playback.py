"""Request-local playback selection and presentation, independent of HTTP handling."""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from .config import Config
from .ffmpeg import (AUDIO_MODES, QUALITY_LADDER, FFmpegTools, MediaInfo,
                     PlaybackPlan, QualityLevel, resolve_quality, stream_mime)
from .library import Video
from .subtitles import discover as discover_subtitles, language_from_token


def _audio_delay_ms(info, plan, query: dict) -> float:
    """Clamp manual audio trim in milliseconds without adding automatic compensation."""
    try:
        delay = float(query.get("adelay", ["0"])[0])
    except (TypeError, ValueError):
        delay = 0.0
    return max(-5000.0, min(5000.0, delay))


def _audio_mode(query: dict) -> str:
    """Select a known dialogue mode, falling back to unfiltered audio."""
    mode = query.get("level", [""])[0].lower()
    return mode if mode in AUDIO_MODES else "off"


def _audio_index(info: MediaInfo, query: dict) -> int | None:
    """Return a valid requested track index, or None for the file's default."""
    raw = query.get("audio", [""])[0]
    try:
        index = int(raw)
    except (TypeError, ValueError):
        return None
    return index if any(track.index == index for track in info.audios) else None


_CHANNEL_NAMES = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}


def _audio_tracks(info: MediaInfo) -> list[dict]:
    """Describe tracks without storing a viewer's choice on shared probe metadata."""
    tracks = []
    for stream in info.audios:
        _, language = language_from_token(stream.language)
        parts = [stream.title or language or f"Track {stream.index + 1}"]
        if language and stream.title and language.lower() not in stream.title.lower():
            parts.append(language)
        detail = _CHANNEL_NAMES.get(stream.channels, f"{stream.channels}ch"
                                    if stream.channels else "")
        if detail:
            parts.append(detail)
        tracks.append({
            "index": stream.index,
            "label": " \u00b7 ".join(parts),
            "language": language,
            "codec": stream.codec,
            "channels": stream.channels,
            "default": stream.default,
        })
    return tracks


def _selection(tools: FFmpegTools, config: Config, path: Path, query: dict
               ) -> tuple[MediaInfo, QualityLevel, int | None, str, PlaybackPlan]:
    """Use shared file facts to construct a fresh plan for this request's options."""
    info = tools.probe(path)
    quality = resolve_quality(query.get("quality", [""])[0])
    audio_index = _audio_index(info, query)
    audio_mode = _audio_mode(query)
    plan = tools.plan_playback(info, config.allow_hevc_direct, quality, audio_index, audio_mode)
    return info, quality, audio_index, audio_mode, plan


def build_payload(tools: FFmpegTools, config: Config, video: Video, path: Path,
                  query: dict) -> dict:
    """Build media details and URLs; progress, navigation and skip data are added by the caller."""
    info, quality, audio_index, audio_mode, plan = _selection(tools, config, path, query)
    tracks = discover_subtitles(path, info)
    requested_sub = query.get("sub", [""])[0]
    burn_track = next((track for track in tracks
                       if track.id == requested_sub and track.burn_in_only), None)
    params = {"id": video.id}
    if quality.id != "auto":
        params["quality"] = quality.id
    if audio_index is not None:
        params["audio"] = audio_index
    if audio_mode != "off":
        params["level"] = audio_mode
    manual_offset = query.get("adelay", ["0"])[0]
    try:
        if float(manual_offset):
            params["adelay"] = manual_offset
    except (TypeError, ValueError):
        pass
    if burn_track is not None:
        mode, badge = "transcode", f"Burning in {burn_track.label}"
        params["sub"] = burn_track.id
        url = "/media/transcode?" + urllib.parse.urlencode(params)
    elif plan.mode == "direct":
        mode, badge = "direct", "Direct Play"
        url = "/media/stream?" + urllib.parse.urlencode(params)
    else:
        mode = plan.mode
        badge = "Remuxed" if plan.mode == "remux" else "Transcoded"
        url = "/media/transcode?" + urllib.parse.urlencode(params)
    out_width, out_height = tools.output_size(info, plan, config.transcode, quality)
    chosen = info.audio(audio_index)
    return {
        "id": video.id,
        "title": video.name,
        "filename": video.filename,
        "mode": mode,
        "badge": badge,
        "reason": plan.reason,
        "duration": info.duration,
        "width": info.width,
        "height": info.height,
        "output_width": out_width,
        "output_height": out_height,
        "video_codec": info.video_codec,
        "audio_codec": chosen.codec if chosen else info.audio_codec,
        "audio_tracks": _audio_tracks(info),
        "audio": chosen.index if chosen else -1,
        "audio_level": audio_mode,
        "video_action": plan.video_action,
        "audio_action": plan.audio_action,
        "audio_delay_ms": round(_audio_delay_ms(info, plan, query), 1),
        "reorder_delay_ms": round(info.reorder_delay * 1000.0, 1),
        "target_video_codec": config.transcode.video_codec,
        "target_audio_codec": config.transcode.audio_codec,
        "quality": quality.id,
        "qualities": [
            {"id": level.id, "label": level.label, "height": level.height}
            for level in QUALITY_LADDER
            if level.height == 0 or not info.height or level.height <= info.height
        ],
        "native_seek": mode == "direct",
        "exact_seek": plan.video_action == "encode",
        "mime": stream_mime(info, plan, config.transcode, burn_track is not None, audio_index),
        "url": url,
        "subtitles": [track.to_dict() for track in tracks],
    }


def seek_payload(tools: FFmpegTools, config: Config, path: Path, query: dict,
                 target: float) -> dict:
    """Find a restart point for an already-validated target in media seconds."""
    _info, _quality, _audio_index_value, _audio_mode_value, plan = _selection(tools, config, path, query)
    forward = query.get("dir", [""])[0] == "forward"
    start = target
    if plan.video_action == "copy" and target > 0:
        start = tools.seek_landing(path, target, forward=forward)
    return {
        "requested": target,
        "start": start,
        "exact": plan.video_action != "copy",
        "direction": "forward" if forward else "backward",
    }


def build_stream(tools: FFmpegTools, config: Config, video: Video, path: Path,
                 query: dict, start: float) -> tuple[list[str], str]:
    """Build one viewer's command from a resolved path and validated seek offset."""
    info, quality, audio_index, audio_mode, plan = _selection(tools, config, path, query)
    burn_index = None
    requested_sub = query.get("sub", [""])[0]
    if requested_sub.startswith("emb:"):
        tracks = discover_subtitles(path, info)
        track = next((track for track in tracks if track.id == requested_sub), None)
        if track is not None and track.burn_in_only:
            burn_index = int(requested_sub.split(":")[1])
    command = tools.build_stream_command(
        path, plan, config.transcode, start=start,
        burn_subtitle_index=burn_index, quality=quality,
        audio_delay_ms=_audio_delay_ms(info, plan, query), info=info,
        audio_index=audio_index, audio_mode=audio_mode,
    )
    return command, f"{video.name} @ {start:.0f}s ({plan.mode}/{quality.id})"
