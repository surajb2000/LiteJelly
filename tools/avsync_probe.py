"""A/V sync diagnostic harness.

Builds a reference clip shaped like a real rip (HEVC, B-frames, 10s GOP, AC3).
The audio beeps for 50ms on every whole second, so the audio's true position in
the delivered stream is measured from content rather than inferred from stream
durations, which cannot tell prepended silence apart from real audio.

    python tools/avsync_probe.py .probe 25 32 47.5
    python tools/avsync_probe.py .probe --compare 25 32
"""

from __future__ import annotations

import array
import math
import subprocess
import sys
import wave
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
    beeps = (r"aevalsrc='if(lt(mod(t\,1)\,0.05)\,sin(2*PI*1000*t)\,0)'"
             f":d={DURATION}:s=48000")
    result = subprocess.run([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size=1280x720:rate=24000/1001:duration={DURATION}",
        "-f", "lavfi", "-i", beeps,
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


def first_beep(ffmpeg: str, path: Path, work: Path) -> float:
    """Where the first beep starts inside the delivered stream.

    Decoded to a WAV and scanned sample by sample; silencedetect misses a gap
    shorter than its minimum duration, which is exactly the case here.
    """
    wav = work / "probe_tmp.wav"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(path), "-t", "5", "-ac", "1", "-ar", "8000",
                    "-c:a", "pcm_s16le", str(wav)], capture_output=True)
    if not wav.exists():
        return float("nan")
    try:
        with wave.open(str(wav), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, OSError):
        return float("nan")
    finally:
        wav.unlink(missing_ok=True)

    samples = array.array("h")
    samples.frombytes(frames[:len(frames) // 2 * 2])
    peak = max((abs(s) for s in samples), default=0)
    if peak < 200:
        return float("nan")
    threshold = peak // 4
    guard = max(1, rate // 50)          # 20ms of quiet before a real onset

    for index in range(guard, len(samples)):
        if abs(samples[index]) > threshold and abs(samples[index - guard]) <= threshold:
            return index / rate
    return float("nan")


def measure(tools, source, info, plan, quality, seek, delay_ms, work):
    """Return (video_start, clock_error, av_error_ms) for a single seek."""
    predicted = tools.seek_landing(source, seek)
    out = work / "probe_tmp.mp4"
    cmd = tools.build_stream_command(source, plan, TranscodeSettings(),
                                     start=seek, quality=quality,
                                     audio_delay_ms=delay_ms)
    subprocess.run(cmd[:-1] + [str(out)], capture_output=True, check=False)

    video_at = info.duration - stream_duration(tools.ffprobe, out, "v")
    clock_error = video_at - predicted
    # Beeps fire on whole seconds of source time, so the first beep after the
    # video's true start is due this far into the stream.
    due = math.ceil(video_at) - video_at
    actual = first_beep(tools.ffmpeg, out, work)
    out.unlink(missing_ok=True)
    return video_at, clock_error, (actual - due) * 1000


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    compare = "--compare" in argv
    work = Path(args[0] if args else ".probe")
    seeks = [float(value) for value in args[1:]] or [25.0, 32.0, 47.5]
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
    # Candidate compensation, for comparison only; the server applies none.
    candidate = info.reorder_delay * 1000.0 if plan.video_action == "copy" else 0.0

    print(f"reference : {info.width}x{info.height} {info.video_codec}/{info.audio_codec} "
          f"{info.duration:.1f}s  plan={plan.mode}")
    print(f"reorder   : has_b_frames={info.has_b_frames} fps={info.fps:.3f} "
          f"-> {candidate:.1f} ms\n")

    if compare:
        print("A/V error (positive means audio lags video):")
        print(f"{'seek':>8s} {'no compensation':>18s} {'compensated':>14s}")
        print("-" * 42)
        for seek in seeks:
            _, _, without = measure(tools, source, info, plan, quality, seek, 0.0, work)
            _, _, with_auto = measure(tools, source, info, plan, quality, seek, candidate, work)
            print(f"{seek:8.2f} {without:16.0f}ms {with_auto:12.0f}ms")
        return 0

    print(f"{'asked for':>10s} {'stream at':>10s} {'clock err':>10s} {'A/V error':>10s}")
    print("-" * 44)
    worst_clock = worst_av = 0.0
    for seek in seeks:
        # Mirrors the server, which applies no automatic compensation.
        video_at, clock_error, av_error = measure(
            tools, source, info, plan, quality, seek, 0.0, work)
        worst_clock = max(worst_clock, abs(clock_error))
        if av_error == av_error:
            worst_av = max(worst_av, abs(av_error))
        print(f"{seek:10.2f} {video_at:10.2f} {clock_error:9.2f}s {av_error:8.0f}ms")

    print(f"\nworst clock error: {worst_clock:.2f}s")
    print(f"worst A/V error  : {worst_av:.0f}ms  (positive means audio lags video)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
