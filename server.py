"""LiteJelly media server - entry point.

Run ``python server.py --help`` for options. ffmpeg/ffprobe are optional; drop
the binaries next to this file (or on PATH) to enable transcoding, thumbnails
and embedded-subtitle extraction.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import sys
from pathlib import Path

from litejelly import __version__
from litejelly.config import load_config
from litejelly.web import Application, create_server, get_local_ip

log = logging.getLogger("litejelly")


def restore_terminal_on_exit() -> None:
    """Put the terminal back the way we found it when the process ends.

    Subprocesses are started with stdin detached so they cannot disable echo,
    but a shell left with invisible input needs a manual `stty echo`, so this
    guards against anything else that touches the tty.
    """
    if os.name == "nt":
        return
    try:
        import termios
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except Exception:
        return  # Not a tty (piped, service-managed); nothing to restore.

    def restore():
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:
            pass

    atexit.register(restore)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="LiteJelly media server",
        epilog="Everything else (media folders, transcoding, ffmpeg paths) "
               "is configured at http://127.0.0.1:<port>/admin.",
    )
    parser.add_argument("--port", type=int, help="Port to listen on")
    parser.add_argument("--host", help="Address to bind (default 0.0.0.0)")
    parser.add_argument("--dir", action="append",
                        help="Media directory, for first run only (repeatable)")
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
    admin_url = f"http://127.0.0.1:{config.port}/admin"
    lines = [
        f"{config.server_name} {__version__}",
        f"Local:   http://127.0.0.1:{config.port}",
        f"Network: http://{get_local_ip()}:{config.port}",
        f"Admin:   {admin_url}",
        f"ffmpeg:  {tools.describe('ffmpeg')}",
        f"ffprobe: {tools.describe('ffprobe')}",
    ]

    if config.media_dirs:
        lines.append(f"Videos:  {video_count}")
        lines.append("Media directories:")
        for directory in config.media_dirs:
            lines.append(f"  - [{directory.content_type}] {directory.path}")
    else:
        lines += [
            "",
            "No media folders yet. Open the admin page to add one:",
            f"  {admin_url}",
            "",
            "The library stays empty until you do. The admin page is reachable",
            "only from this device unless you set an admin token on it.",
        ]

    if not tools.available:
        lines += [
            "",
            "ffmpeg was NOT detected. Only browser-native files (MP4/WebM with",
            "H.264+AAC) will play; MKV, AC3 audio, thumbnails and embedded",
            "subtitles are unavailable. Put ffmpeg next to server.py, on PATH,",
            "or set its path on the admin page.",
        ]

    width = max(len(line) for line in lines) + 2
    print("=" * width)
    for line in lines:
        print(f" {line}")
    print("=" * width)


def main(argv=None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    restore_terminal_on_exit()

    app_dir = Path(__file__).resolve().parent
    config, warnings = load_config(app_dir, args)
    for warning in warnings:
        log.warning(warning)

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
