"""SQLite-backed playback progress, kept per viewer profile and shared across devices."""

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

DEFAULT_PROFILE_NAME = "Home"
MAX_PROFILES = 12
MAX_PROFILE_NAME = 24


class ProfileError(ValueError):
    """A profile change the store refuses; the message is safe to show."""


class UnknownProfile(LookupError):
    pass


def clean_profile_name(value) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise ProfileError("A profile needs a name")
    if len(name) > MAX_PROFILE_NAME:
        raise ProfileError(f"Profile names are at most {MAX_PROFILE_NAME} characters")
    if any(not ch.isprintable() for ch in name):
        raise ProfileError("Profile names cannot contain control characters")
    return name


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
        try:
            self._prepare_profiles()
        except BaseException:
            self._conn.close()
            raise
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS media_identity (
                root TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                video_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY (root, relative_path)
            )
        """)
        self._conn.commit()

    def _prepare_profiles(self) -> None:
        """Create the profile tables, moving single-viewer progress to the first profile."""
        conn = self._conn
        # Explicit, because sqlite3 would otherwise run the DDL outside the transaction.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS profiles (
                    id         INTEGER PRIMARY KEY,
                    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    created_at REAL NOT NULL
                )
            """)
            if conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0:
                conn.execute("INSERT INTO profiles (name, created_at) VALUES (?, ?)",
                             (DEFAULT_PROFILE_NAME, time.time()))
            default = conn.execute("SELECT MIN(id) FROM profiles").fetchone()[0]
            columns = [row[1] for row in conn.execute("PRAGMA table_info(progress)")]
            if columns and "profile_id" not in columns:
                conn.execute("ALTER TABLE progress RENAME TO progress_single")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS progress (
                    profile_id INTEGER NOT NULL,
                    video_id   TEXT NOT NULL,
                    position   REAL NOT NULL,
                    duration   REAL NOT NULL DEFAULT 0,
                    finished   INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (profile_id, video_id)
                )
            """)
            if columns and "profile_id" not in columns:
                conn.execute("""
                    INSERT INTO progress
                    SELECT ?, video_id, position, duration, finished, updated_at
                    FROM progress_single
                """, (default,))
                conn.execute("DROP TABLE progress_single")
                log.info("Watch history moved to the %s profile", DEFAULT_PROFILE_NAME)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # --- profiles --------------------------------------------------------
    def list_profiles(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT id, name FROM profiles ORDER BY id").fetchall()
        return [{"id": row["id"], "name": row["name"]} for row in rows]

    def default_profile(self) -> int:
        with self._lock:
            return self._default_profile()

    def _default_profile(self) -> int:
        return self._conn.execute("SELECT MIN(id) FROM profiles").fetchone()[0]

    def resolve_profile(self, value) -> int | None:
        """The profile a request means: blank is the default, anything unknown is None."""
        text = str(value if value is not None else "").strip()
        with self._lock:
            if not text:
                return self._default_profile()
            if not text.isdigit() or len(text) > 12:
                return None
            row = self._conn.execute("SELECT id FROM profiles WHERE id = ?",
                                     (int(text),)).fetchone()
        return row["id"] if row else None

    def _name_taken(self, name: str, ignore: int | None = None) -> bool:
        folded = name.casefold()
        return any(row["name"].casefold() == folded and row["id"] != ignore
                   for row in self._conn.execute("SELECT id, name FROM profiles"))

    def create_profile(self, name) -> dict:
        name = clean_profile_name(name)
        with self._lock, self._conn:
            count = self._conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
            if count >= MAX_PROFILES:
                raise ProfileError(f"At most {MAX_PROFILES} profiles")
            if self._name_taken(name):
                raise ProfileError("A profile with that name already exists")
            cursor = self._conn.execute(
                "INSERT INTO profiles (name, created_at) VALUES (?, ?)", (name, time.time()))
        return {"id": cursor.lastrowid, "name": name}

    def rename_profile(self, profile_id: int, name) -> dict:
        name = clean_profile_name(name)
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM profiles WHERE id = ?",
                                  (profile_id,)).fetchone() is None:
                raise UnknownProfile(profile_id)
            if self._name_taken(name, ignore=profile_id):
                raise ProfileError("A profile with that name already exists")
            self._conn.execute("UPDATE profiles SET name = ? WHERE id = ?", (name, profile_id))
        return {"id": profile_id, "name": name}

    def delete_profile(self, profile_id: int) -> int:
        """Remove a profile and its watch history; returns how many entries went with it."""
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM profiles WHERE id = ?",
                                  (profile_id,)).fetchone() is None:
                raise UnknownProfile(profile_id)
            if self._conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] <= 1:
                raise ProfileError("The last profile cannot be deleted")
            removed = self._conn.execute("DELETE FROM progress WHERE profile_id = ?",
                                         (profile_id,)).rowcount
            self._conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        return removed

    def _profile_or_default(self, profile: int | None) -> int:
        if profile is None:
            return self._default_profile()
        if self._conn.execute("SELECT 1 FROM profiles WHERE id = ?",
                              (profile,)).fetchone() is None:
            raise UnknownProfile(profile)
        return profile

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
             finished: bool | None = None, profile: int | None = None) -> dict:
        # An infinity stored here comes back out of every later response, and
        # json.dumps writes it as a bare Infinity that no browser will parse.
        position = _seconds(position)
        duration = _seconds(duration)
        if finished is None:
            finished = duration > 0 and position >= duration * FINISHED_FRACTION
        if finished:
            position = 0.0

        with self._lock:
            # Checked under the same lock as delete_profile, so no row outlives its profile.
            profile = self._profile_or_default(profile)
            self._conn.execute(
                """
                INSERT INTO progress (profile_id, video_id, position, duration, finished, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, video_id) DO UPDATE SET
                    position=excluded.position,
                    duration=MAX(excluded.duration, progress.duration),
                    finished=excluded.finished,
                    updated_at=excluded.updated_at
                """,
                (profile, video_id, position, duration, int(finished), time.time()),
            )
            self._conn.commit()
        return {"position": position, "duration": duration, "finished": bool(finished)}

    def get(self, video_id: str, profile: int | None = None) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM progress WHERE profile_id = ? AND video_id = ?",
                (self._profile_or_default(profile), video_id),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def all(self, profile: int | None = None) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM progress WHERE profile_id = ?",
                                      (self._profile_or_default(profile),)).fetchall()
        return {row["video_id"]: self._row_to_dict(row) for row in rows}

    def clear(self, video_id: str, profile: int | None = None) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM progress WHERE profile_id = ? AND video_id = ?",
                               (self._profile_or_default(profile), video_id))
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
