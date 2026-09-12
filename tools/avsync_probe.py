"""A/V sync diagnostic harness.

Builds a reference clip shaped like a real rip (HEVC, B-frames, 10s GOP, AC3)
and reports where each stream actually starts after a seek, so sync problems
can be measured instead of inferred.

    python tools/avsync_probe.py .probe 25 32 47.5

"video starts" and "audio starts" are derived from the delivered duration, so
they show the true content position, not what the player was told.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from litejelly.config import TranscodeSettings
from litejelly.ffmpeg import FFmpegTools, resolve_quality

DURATION = 60
GOP_SECONDS = 10
FRAME_RATE = 24000 / 1001


def build_reference(ffmpeg: str, path: Path) -> None:
    gop = str(int(GOP_SECONDS * FRAME_RATE))
    result = subprocess.run([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size=1280x720:rate=24000/1001:duration={DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=1000:duration={DURATION}",
        "-c:v", "libx265", "-preset", "ultrafast",
        "-x265-params", "log-level=none:bframes=2",
        "-g", gop, "-keyint_min", gop, "-sc_threshold", "0",
        "-pix_fmt", "yuv420p", "-c:a", "ac3", "-shortest", str(path),
    ], capture_output=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.decode("utf-8", "replace")[:500])


def stream_duration(ffprobe: str, path: Path, kind: str) -> float:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", kind,
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout.strip()
    try:
        return float(out.splitlines()[0])
    except (ValueError, IndexError):
        return float("nan")


def main(argv: list[str]) -> int:
    work = Path(argv[1] if len(argv) > 1 else ".probe")
    seeks = [float(value) for value in argv[2:]] or [25.0, 32.0, 47.5]
    work.mkdir(parents=True, exist_ok=True)

    tools = FFmpegTools(REPO)
    if not tools.available or not tools.can_probe:
        print("ffmpeg and ffprobe are required.")
        return 1

    source = work / "reference.mkv"
    if not source.exists():
        print("building reference clip...")
        build_reference(tools.ffmpeg, source)

    info = tools.probe(source)
    quality = resolve_quality("original")
    plan = tools.plan_playback(info, allow_hevc_direct=True, quality=quality)

    print(f"reference : {info.width}x{info.height} {info.video_codec}/{info.audio_codec} "
          f"{info.duration:.1f}s  plan={plan.mode}\n")
    print(f"{'asked for':>10s} {'predicted':>10s} {'video at':>10s} {'audio at':>10s} "
          f"{'clock error':>12s} {'A/V':>8s}")
    print("-" * 66)

    worst = 0.0
    for seek in seeks:
        predicted = tools.seek_landing(source, seek)
        out = work / f"seek_{seek:.0f}.mp4"
        # The raw target goes to ffmpeg; predicted is only for the client clock.
        cmd = tools.build_stream_command(source, plan, TranscodeSettings(),
                                         start=seek, quality=quality)
        subprocess.run(cmd[:-1] + [str(out)], capture_output=True, check=False)

        video_at = info.duration - stream_duration(tools.ffprobe, out, "v")
        audio_at = info.duration - stream_duration(tools.ffprobe, out, "a")
        clock_error = video_at - predicted
        av_gap = (audio_at - video_at) * 1000
        worst = max(worst, abs(clock_error))
        print(f"{seek:10.2f} {predicted:10.2f} {video_at:10.2f} {audio_at:10.2f} "
              f"{clock_error:10.2f}s {av_gap:6.0f}ms")
        out.unlink(missing_ok=True)

    print(f"\nworst clock error: {worst:.2f}s")
    print("This is the gap between where the player thinks the stream starts "
          "and where it really does.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
