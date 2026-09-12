"""SQLite-backed playback progress, shared across every device on the LAN."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("litejelly.store")

# Below this, treat playback as "not started"; above, as "finished".
MIN_RESUME_SECONDS = 15.0
FINISHED_FRACTION = 0.96


class ProgressStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
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
        self._conn.commit()

    def save(self, video_id: str, position: float, duration: float = 0.0,
             finished: bool | None = None) -> dict:
        position = max(0.0, float(position))
        duration = max(0.0, float(duration))
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

    def continue_watching(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM progress
                WHERE finished = 0 AND position >= ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (MIN_RESUME_SECONDS, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def clear(self, video_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM progress WHERE video_id = ?", (video_id,))
            self._conn.commit()

    def prune(self, known_ids: set[str]) -> None:
        """Drop rows for videos that are no longer in the library."""
        with self._lock:
            rows = self._conn.execute("SELECT video_id FROM progress").fetchall()
            stale = [(r["video_id"],) for r in rows if r["video_id"] not in known_ids]
            if stale:
                self._conn.executemany("DELETE FROM progress WHERE video_id = ?", stale)
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
