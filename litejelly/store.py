"""SQLite-backed playback progress, shared across every device on the LAN."""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("litejelly.store")

# Below this, treat playback as "not started"; above, as "finished".
MIN_RESUME_SECONDS = 15.0
FINISHED_FRACTION = 0.96
# Longer than any real recording; anything past it is a broken client.
MAX_SECONDS = 366 * 24 * 3600.0


def _seconds(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(MAX_SECONDS, number))


def migrate_database(destination: Path, legacy: Path | None) -> None:
    """Copy legacy progress, including its WAL, atomically without deleting the source."""
    if destination.exists() or legacy is None or not legacy.is_file():
        return
    lock = destination.with_suffix(".migration-lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    temporary = None
    try:
        if destination.exists():
            return
        descriptor, name = tempfile.mkstemp(dir=destination.parent, suffix=".db.tmp")
        os.close(descriptor)
        temporary = Path(name)
        source = sqlite3.connect(legacy.resolve().as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


class ProgressStore:
    def __init__(self, db_path: Path, legacy_path: Path | None = None):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        migrate_database(db_path, legacy_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS progress (
                video_id   TEXT PRIMARY KEY,
                position   REAL NOT NULL,
                duration   REAL NOT NULL DEFAULT 0,
                finished   INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS media_identity (
                root TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                video_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY (root, relative_path)
            )
        """)
        self._conn.commit()

    def identify_media(self, videos) -> None:
        """Assign durable IDs per root/path, preserving legacy IDs on the first catalog."""
        if not videos:
            return
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT root, relative_path, video_id FROM media_identity").fetchall()
            known = {(row["root"], row["relative_path"]): row["video_id"] for row in rows}
            used = set(known.values())
            first_catalog = not rows
            pending = []
            for video in videos:
                key = (video.root_path, video.path)
                identifier = known.get(key)
                if identifier is None:
                    identifier = video.legacy_id if first_catalog and video.legacy_id not in used else video.id
                    if identifier in used:
                        raise ValueError("Media identity collision")
                    known[key] = identifier
                    used.add(identifier)
                    pending.append((*key, identifier))
                video.id = identifier
            self._conn.executemany("INSERT INTO media_identity VALUES (?, ?, ?)", pending)

    def save(self, video_id: str, position: float, duration: float = 0.0,
             finished: bool | None = None) -> dict:
        # An infinity stored here comes back out of every later response, and
        # json.dumps writes it as a bare Infinity that no browser will parse.
        position = _seconds(position)
        duration = _seconds(duration)
        if finished is None:
            finished = duration > 0 and position >= duration * FINISHED_FRACTION
        if finished:
            position = 0.0

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO progress (video_id, position, duration, finished, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    position=excluded.position,
                    duration=MAX(excluded.duration, progress.duration),
                    finished=excluded.finished,
                    updated_at=excluded.updated_at
                """,
                (video_id, position, duration, int(finished), time.time()),
            )
            self._conn.commit()
        return {"position": position, "duration": duration, "finished": bool(finished)}

    def get(self, video_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM progress WHERE video_id = ?", (video_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def all(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM progress").fetchall()
        return {row["video_id"]: self._row_to_dict(row) for row in rows}

    def clear(self, video_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM progress WHERE video_id = ?", (video_id,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "video_id": row["video_id"],
            "position": row["position"],
            "duration": row["duration"],
            "finished": bool(row["finished"]),
            "updated_at": row["updated_at"],
        }
