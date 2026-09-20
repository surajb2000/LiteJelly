"""Measures the line endings that come out of each subtitle conversion path.

The upload path had a \\r\\r\\n bug: a stray \\r reads as the blank line that
ends a cue, so cues keep their timings and lose their text. This asks whether
the same thing can happen to subtitles that were already there.

Run from the repo root:  python tools/subtitle_newline_probe.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.config import load_config
from litejelly.ffmpeg import FFmpegTools
from litejelly.subtitles import SubtitleService

SRT_BODY = (
    "1\r\n00:00:01,000 --> 00:00:03,000\r\nFirst line.\r\n\r\n"
    "2\r\n00:00:04,000 --> 00:00:06,000\r\nSecond line.\r\n"
)
VTT_BODY = (
    "WEBVTT\r\n\r\n00:00:01.000 --> 00:00:03.000\r\nFirst line.\r\n\r\n"
    "00:00:04.000 --> 00:00:06.000\r\nSecond line.\r\n"
)
ASS_BODY = (
    "[Script Info]\r\nScriptType: v4.00+\r\n\r\n"
    "[V4+ Styles]\r\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
    "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
    "MarginL, MarginR, MarginV, Encoding\r\n"
    "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
    "0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\r\n\r\n"
    "[Events]\r\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
    "Effect, Text\r\n"
    "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,First line.\r\n"
    "Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,Second line.\r\n"
)


def describe(label, text, cache_dir):
    stray = text.count("\r\r\n") if text else 0
    crlf = text.count("\r\n") if text else 0
    # A cue whose text survived has a line directly under its timing.
    joined = "-->" in (text or "") and not stray
    print(f"  {label:<22} chars={len(text or ''):>5}  "
          f"\\r\\r\\n={stray:<3} \\r\\n={crlf:<3} cue text intact={joined}")
    caches = sorted(Path(cache_dir).glob("*.vtt"))
    for path in caches:
        raw = path.read_bytes()
        print(f"    on disk {path.name[:10]}… bytes={len(raw):>5} "
              f"\\r\\r\\n={raw.count(b'chr')if False else raw.count(bytes([13,13,10])):<3} "
              f"\\r\\n={raw.count(bytes([13,10]))}")


def main():
    root = Path(tempfile.mkdtemp(prefix="lj-newline-"))
    video = root / "Probe (2024).mkv"
    video.write_bytes(b"x" * 4096)

    config = load_config(Path(__file__).resolve().parent.parent)[0]
    tools = FFmpegTools(config.app_dir, ffmpeg_path=config.ffmpeg_path,
                        ffprobe_path=config.ffprobe_path)
    print(f"ffmpeg available: {tools.available}")
    print(f"os.linesep is {ascii(__import__('os').linesep)}\n")

    cases = [
        ("existing .srt (CRLF)", ".srt", SRT_BODY),
        ("existing .vtt (CRLF)", ".vtt", VTT_BODY),
        ("existing .ass (CRLF)", ".ass", ASS_BODY),
    ]
    for label, suffix, body in cases:
        for old in root.glob("Probe (2024)*"):
            if old.suffix != ".mkv":
                old.unlink()
        sidecar = root / f"Probe (2024).en{suffix}"
        sidecar.write_bytes(body.encode("utf-8"))

        cache = root / f"cache{suffix.replace('.', '')}"
        service = SubtitleService(tools, cache)
        print(label)
        describe("served", service.get_vtt(video, "ext:0"), cache / "subtitles")
        print()


if __name__ == "__main__":
    main()
