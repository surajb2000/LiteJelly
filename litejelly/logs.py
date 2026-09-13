"""Logging setup: verbosity, the rotating file, and reading it back.

Two different ideas are kept apart here. *Verbosity* decides what gets
recorded at all, and INFO is its floor: informational messages, warnings and
errors are always captured, and debug/trace are opt-in. *View levels* are what
the admin page filters the recorded lines by afterwards, which is where asking
for "errors only" belongs.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

TRACE = 5

# What may be captured. INFO is the floor, so warnings and errors are never
# silently dropped; anything quieter would only hide problems.
VERBOSITY = {
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": TRACE,
}
DEFAULT_VERBOSITY = "info"

# What the log viewer can filter by, which may be stricter than what we record.
VIEW_LEVELS = {
    "trace": TRACE,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

LOG_DIR = "logs"
LOG_FILE = "litejelly.log"

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# "2026-09-13 14:22:01 INFO    litejelly.web: message"
_LINE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(?P<level>[A-Z]+)\s+"
    r"(?P<logger>[\w.]+):\s"
    r"(?P<message>.*)$"
)

_file_handler: logging.handlers.RotatingFileHandler | None = None
_console_handler: logging.StreamHandler | None = None


def register_trace() -> None:
    logging.addLevelName(TRACE, "TRACE")
    if hasattr(logging.Logger, "trace"):
        return

    def trace(self, message, *args, **kwargs):
        if self.isEnabledFor(TRACE):
            self._log(TRACE, message, args, **kwargs)

    logging.Logger.trace = trace  # type: ignore[attr-defined]


def resolve_verbosity(name: str | None) -> int:
    return VERBOSITY.get(str(name or "").strip().lower(), logging.INFO)


def verbosity_name(value: int) -> str:
    for name, level in VERBOSITY.items():
        if level == value:
            return name
    return DEFAULT_VERBOSITY


def log_path(app_dir: Path) -> Path:
    return app_dir / LOG_DIR / LOG_FILE


def configure(app_dir: Path, verbosity: str = DEFAULT_VERBOSITY,
              to_file: bool = True, max_mb: int = 2,
              backups: int = 3) -> list[str]:
    """Install the console and file handlers. Returns any setup warnings."""
    global _file_handler, _console_handler

    register_trace()
    warnings: list[str] = []
    level = resolve_verbosity(verbosity)

    root = logging.getLogger("litejelly")
    root.setLevel(level)
    # Our handlers do the output; bubbling up would duplicate every line if
    # anything ever calls basicConfig.
    root.propagate = False

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    _console_handler = logging.StreamHandler(stream=sys.stderr)
    _console_handler.setFormatter(formatter)
    _console_handler.setLevel(level)
    root.addHandler(_console_handler)

    _file_handler = None
    if to_file:
        target = log_path(app_dir)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _file_handler = logging.handlers.RotatingFileHandler(
                target,
                maxBytes=max(1, max_mb) * 1024 * 1024,
                backupCount=max(0, backups),
                encoding="utf-8",
                delay=True,
            )
            _file_handler.setFormatter(formatter)
            _file_handler.setLevel(level)
            root.addHandler(_file_handler)
        except OSError as exc:
            warnings.append(f"Could not open the log file: {exc}")
            _file_handler = None

    return warnings


def set_verbosity(verbosity: str) -> int:
    """Change the live handlers without a restart. Returns the level applied."""
    level = resolve_verbosity(verbosity)
    logging.getLogger("litejelly").setLevel(level)
    for handler in (_console_handler, _file_handler):
        if handler is not None:
            handler.setLevel(level)
    return level


def current_file() -> Path | None:
    if _file_handler is None:
        return None
    return Path(_file_handler.baseFilename)


def file_size() -> int:
    path = current_file()
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def tail(path: Path, limit: int = 200) -> list[str]:
    """Last ``limit`` lines, read from the end so a large log stays cheap."""
    if limit <= 0 or not path.is_file():
        return []

    block = 8192
    data = b""
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            remaining = stream.tell()
            while remaining > 0 and data.count(b"\n") <= limit:
                step = min(block, remaining)
                remaining -= step
                stream.seek(remaining)
                data = stream.read(step) + data
    except OSError:
        return []

    return data.decode("utf-8", "replace").splitlines()[-limit:]


def parse_line(line: str) -> dict:
    """Split a formatted line so the viewer can filter and colour it."""
    match = _LINE.match(line)
    if not match:
        # Tracebacks and wrapped text belong to the record above them.
        return {"time": "", "level": "", "logger": "", "message": line}
    return match.groupdict()


def read_entries(app_dir: Path, limit: int = 200,
                 min_level: str | None = None) -> list[dict]:
    entries = [parse_line(line) for line in tail(log_path(app_dir), limit)]
    threshold = VIEW_LEVELS.get(str(min_level or "").strip().lower())
    if threshold is None:
        return entries

    kept: list[dict] = []
    for entry in entries:
        value = VIEW_LEVELS.get(entry["level"].lower())
        if value is None:
            # Continuation line: keep it only if its record was kept.
            if kept:
                kept.append(entry)
            continue
        if value >= threshold:
            kept.append(entry)
    return kept
