"""Filesystem containment helpers.

Every path that comes from a request is funnelled through :func:`safe_resolve`,
which resolves symlinks before checking containment so a symlink inside a media
directory cannot be used to escape it.
"""

from __future__ import annotations

from pathlib import Path


def resolve_within(base: Path, relative: str) -> Path | None:
    """Resolve ``relative`` under ``base``, or return None if it escapes."""
    if not relative:
        return None
    if "\x00" in relative:
        return None

    candidate = Path(relative.replace("\\", "/"))
    if candidate.is_absolute() or candidate.drive:
        return None
    if any(part == ".." for part in candidate.parts):
        return None

    try:
        base_real = base.resolve(strict=True)
        target = (base_real / candidate).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None

    if target != base_real and base_real not in target.parents:
        return None
    return target


def safe_resolve(bases: list[str], relative: str, dir_index: int | None = None):
    """Resolve ``relative`` against one or all media roots.

    Returns ``(absolute_path, dir_index)`` or ``(None, None)``.
    """
    if not relative:
        return None, None

    if dir_index is not None:
        if not 0 <= dir_index < len(bases):
            return None, None
        indices = [dir_index]
    else:
        indices = range(len(bases))

    for idx in indices:
        target = resolve_within(Path(bases[idx]), relative)
        if target is not None and target.is_file():
            return target, idx
    return None, None


def is_within(base: Path, target: Path) -> bool:
    try:
        base_real = base.resolve()
        target_real = target.resolve()
    except (OSError, RuntimeError):
        return False
    return target_real == base_real or base_real in target_real.parents
