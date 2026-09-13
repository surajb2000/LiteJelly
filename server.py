"""LiteJelly media server - entry point.

Run ``python server.py --help`` for options. ffmpeg/ffprobe are optional; drop
the binaries next to this file (or on PATH) to enable transcoding, thumbnails
and embedded-subtitle extraction.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from litejelly import __version__
from litejelly.config import load_config
from litejelly.web import Application, create_server, get_local_ip

log = logging.getLogger("litejelly")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="LiteJelly media server")
    parser.add_argument("--port", type=int, help="Port to listen on")
    parser.add_argument("--host", help="Address to bind (default 0.0.0.0)")
    parser.add_argument("--name", help="Display name shown in the UI")
    parser.add_argument("--dir", action="append", help="Media directory (repeatable)")
    parser.add_argument("--ffmpeg", help="Path to the ffmpeg binary (or its folder)")
    parser.add_argument("--ffprobe", help="Path to the ffprobe binary (or its folder)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument("--version", action="version", version=f"LiteJelly {__version__}")
    return parser.parse_args(argv)


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def print_banner(config, app: Application, video_count: int) -> None:
    tools = app.tools
    lines = [
        f"{config.server_name} {__version__}",
        f"Local:   http://127.0.0.1:{config.port}",
        f"Network: http://{get_local_ip()}:{config.port}",
        f"ffmpeg:  {tools.describe('ffmpeg')}",
        f"ffprobe: {tools.describe('ffprobe')}",
        f"Videos:  {video_count}",
        f"Admin:   http://127.0.0.1:{config.port}/admin",
        "Media directories:",
    ]
    for directory in config.media_dirs:
        lines.append(f"  - [{directory.content_type}] {directory.path}")

    if not tools.available:
        lines += [
            "",
            "ffmpeg was NOT detected. Only browser-native files (MP4/WebM with",
            "H.264+AAC) will play; MKV, AC3 audio, thumbnails and embedded",
            "subtitles are unavailable. Put ffmpeg next to server.py, on PATH,",
            "or set ffmpeg_path in config.json / pass --ffmpeg.",
        ]

    width = max(len(line) for line in lines) + 2
    print("=" * width)
    for line in lines:
        print(f" {line}")
    print("=" * width)


def main(argv=None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)

    app_dir = Path(__file__).resolve().parent
    config, warnings = load_config(app_dir, args)
    for warning in warnings:
        log.warning(warning)

    if not config.media_dirs:
        log.error("No readable media directories. Set media_dirs in config.json or pass --dir.")
        return 1

    config.cache_dir.mkdir(parents=True, exist_ok=True)

    app = Application(config)
    videos = app.library.scan(force=True)
    app.progress.prune({v.id for v in videos})
    app.library.start()

    try:
        httpd = create_server(app)
    except OSError as exc:
        log.error("Cannot bind %s:%s - %s", config.host, config.port, exc)
        app.shutdown()
        return 1

    print_banner(config, app, len(videos))

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        httpd.shutdown()
        httpd.server_close()
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
