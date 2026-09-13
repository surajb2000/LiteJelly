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
import time
from pathlib import Path

from litejelly import __version__
from litejelly import auth
from litejelly import logs as log_setup
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
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Log debug detail for this run")
    parser.add_argument("--reset-admin", action="store_true",
                        help="Set the admin username and password, then exit")
    parser.add_argument("--version", action="version", version=f"LiteJelly {__version__}")
    return parser.parse_args(argv)


def reset_admin(app_dir: Path) -> int:
    """Recovery path for a forgotten password, run on the server itself."""
    import getpass

    existing = auth.load_credentials(app_dir)
    if existing is not None:
        print(f"Replacing the existing admin account '{existing.username}'.")

    try:
        username = input("Username: ").strip()
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1

    if password != confirm:
        print("The two passwords do not match.")
        return 1

    problems = auth.check_username(username) + auth.check_password_strength(password)
    if problems:
        for problem in problems:
            print(problem)
        return 1

    try:
        auth.save_credentials(app_dir, auth.Credentials(
            username=username,
            password_hash=auth.hash_password(password),
            updated_at=time.time(),
        ))
    except OSError as exc:
        print(f"Could not save the account: {exc}")
        return 1

    print(f"Admin account set to '{username}'.")
    return 0


def configure_logging(app_dir: Path, config=None, verbose: bool = False) -> list[str]:
    """Console plus a rotating file. --verbose only overrides for this run."""
    if config is None:
        # Before the config is read, mirror everything so startup problems show.
        return log_setup.configure(app_dir, verbosity="debug" if verbose else "info",
                                   to_console=True)
    return log_setup.configure(
        app_dir,
        verbosity="debug" if verbose else config.log_verbosity,
        to_file=config.log_to_file,
        max_mb=config.log_max_mb,
        backups=config.log_backups,
        to_console=verbose or config.log_to_console,
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
            "The library stays empty until you do.",
        ]

    if app.credentials is None:
        lines += [
            "",
            "No admin account yet. Open the admin page on THIS machine to",
            "choose a username and password:",
            f"  {admin_url}",
            "",
            "Until then the settings cannot be changed from anywhere.",
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
    # stdout is block-buffered when it is not a terminal, and this banner
    # carries the setup URL, so it must not sit in a buffer.
    sys.stdout.flush()


def main(argv=None) -> int:
    args = parse_args(argv)
    app_dir = Path(__file__).resolve().parent

    if args.reset_admin:
        return reset_admin(app_dir)

    # Log to the console first so config problems are visible, then reconfigure
    # with the settings that were just read.
    configure_logging(app_dir, verbose=args.verbose)
    restore_terminal_on_exit()

    config, warnings = load_config(app_dir, args)
    warnings += configure_logging(app_dir, config, verbose=args.verbose)
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
