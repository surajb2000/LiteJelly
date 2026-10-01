"""Read-only checks of staged content; requires only Git and Python 3.10+."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath


MAX_ADDED_BYTES = 5 * 1024 * 1024
PRIVATE_FILES = {"credentials.json", "opensubtitles.json", "settings.json"}
GENERATED_DIRS = {".cache", ".thumbnails", ".venv", "venv", "__pycache__", "node_modules", "test-results"}
DATABASE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")


def git(root: Path, *arguments: str) -> bytes:
    """Read Git metadata or index blobs without changing the index or worktree."""
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True)
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(message or f"Git command failed: {' '.join(arguments)}")
    return result.stdout


def staged_entries(root: Path) -> list[tuple[str, str, str, bool]]:
    """Return changed index entries as (path, mode, object ID, newly added)."""
    changed = set(git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0"))
    added = set(git(root, "diff", "--cached", "--name-only", "--diff-filter=A", "-z").split(b"\0"))
    entries = []
    for record in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, object_id, stage = header.decode("ascii").split()
        if stage != "0":
            raise RuntimeError("Resolve unmerged index entries before committing")
        if raw_path in changed:
            entries.append((os.fsdecode(raw_path), mode, object_id, raw_path in added))
    return entries


def forbidden_path(name: str) -> bool:
    """Identify local credentials, generated state and bundled executables by path."""
    path = PurePosixPath(name.lower())
    if path.name in PRIVATE_FILES:
        return True
    if path.name == ".env" or (path.name.startswith(".env.") and path.name not in {".env.example", ".env.sample"}):
        return True
    if any(part in GENERATED_DIRS for part in path.parts[:-1]) or path.parts[0] == "data":
        return True
    return path.name in {"ffmpeg", "ffprobe", "ffplay"} or path.name.endswith(
        DATABASE_SUFFIXES + (".exe", ".dll", ".pyc", ".pyo"))


def reject_constant(value: str):
    """Reject NaN and Infinity, which Python's JSON parser otherwise accepts."""
    raise ValueError(f"Non-standard JSON constant: {value}")


def validate_content(name: str, payload: bytes) -> list[str]:
    """Check staged Python/JSON syntax without executing code or creating bytecode."""
    try:
        if name.lower().endswith(".py"):
            source = compile(payload, name, "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
            compile(source, name, "exec", dont_inherit=True)
        elif name.lower().endswith(".json"):
            json.loads(payload.decode("utf-8-sig"), parse_constant=reject_constant)
    except (SyntaxError, ValueError, UnicodeError) as error:
        return [f"{name}: {error}"]
    return []


def check_index(root: Path) -> list[str]:
    """Check staged whitespace, conflict markers, file policy and syntax; never rewrite."""
    problems = []
    whitespace = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--check"],
                                capture_output=True)
    if whitespace.returncode:
        detail = (whitespace.stdout + whitespace.stderr).decode("utf-8", "replace").strip()
        problems.append(detail or "Staged whitespace/conflict check failed")
    for name, mode, object_id, added in staged_entries(root):
        if forbidden_path(name):
            problems.append(f"{name}: local credentials, generated state or executable must not be committed")
            continue
        if mode not in {"100644", "100755"}:
            continue
        size = int(git(root, "cat-file", "-s", object_id))
        if added and size > MAX_ADDED_BYTES:
            problems.append(f"{name}: new file exceeds the 5 MiB commit limit")
            continue
        if name.lower().endswith((".py", ".json")):
            problems.extend(validate_content(name, git(root, "cat-file", "blob", object_id)))
    return problems


def main() -> int:
    """Report actionable index errors and return nonzero to block a commit."""
    if sys.version_info < (3, 10):
        print("LiteJelly pre-commit requires Python 3.10+", file=sys.stderr)
        return 1
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
        problems = check_index(root)
    except (OSError, RuntimeError, ValueError) as error:
        problems = [str(error)]
    if problems:
        print("LiteJelly pre-commit checks failed:", file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        print("Fix and stage the intended changes, then retry. No files were modified.", file=sys.stderr)
        return 1
    print("LiteJelly staged checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())