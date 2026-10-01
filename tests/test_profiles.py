"""Viewer profiles: separate watch history per person, chosen per screen."""

from __future__ import annotations

import http.client
import json
import logging
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly import auth
from litejelly.config import load_config
from litejelly.ffmpeg import MediaInfo
from litejelly.store import (DEFAULT_PROFILE_NAME, MAX_PROFILE_NAME, MAX_PROFILES,
                             ProfileError, ProgressStore, UnknownProfile)
from litejelly.web import Application, create_server

for name in ("litejelly", "litejelly.web", "litejelly.admin", "litejelly.store"):
    logging.getLogger(name).setLevel(logging.CRITICAL)

ADMIN_USER = "testadmin"
ADMIN_PASSWORD = "test-password-123"
WRITE_HEADERS = {"Content-Type": "application/json", "X-LiteJelly-Admin": "1"}

SINGLE_VIEWER_SCHEMA = """
    CREATE TABLE progress (
        video_id   TEXT PRIMARY KEY,
        position   REAL NOT NULL,
        duration   REAL NOT NULL DEFAULT 0,
        finished   INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL
    )
"""


def single_viewer_database(path: Path, rows, schema: str = SINGLE_VIEWER_SCHEMA) -> None:
    """A database as every release before profiles wrote it."""
    conn = sqlite3.connect(path)
    conn.execute(schema)
    conn.executemany("INSERT INTO progress VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "litejelly.db"

    def open(self) -> ProgressStore:
        store = ProgressStore(self.path)
        self.addCleanup(store.close)
        return store

    def test_existing_history_moves_to_the_first_profile(self):
        single_viewer_database(self.path, [("a", 120.0, 600.0, 0, 1.0),
                                           ("b", 0.0, 900.0, 1, 2.0)])
        store = self.open()
        self.assertEqual(store.list_profiles(), [{"id": 1, "name": DEFAULT_PROFILE_NAME}])
        history = store.all()
        self.assertEqual({key: row["position"] for key, row in history.items()},
                         {"a": 120.0, "b": 0.0})
        self.assertTrue(history["b"]["finished"])
        self.assertEqual(store.get("a", 1), history["a"])

    def test_opening_twice_changes_nothing(self):
        single_viewer_database(self.path, [("a", 120.0, 600.0, 0, 1.0)])
        self.open().close()
        store = self.open()
        self.assertEqual(len(store.list_profiles()), 1)
        self.assertEqual({key: row["position"] for key, row in store.all().items()},
                         {"a": 120.0})

    def test_a_failed_migration_leaves_the_old_table_as_it_was(self):
        # A hand-edited database without the NOT NULL makes the copy fail halfway.
        loose = SINGLE_VIEWER_SCHEMA.replace("REAL NOT NULL,", "REAL,", 1)
        single_viewer_database(self.path, [("a", 120.0, 600.0, 0, 1.0),
                                           ("b", None, 600.0, 0, 2.0)], schema=loose)
        with self.assertRaises(sqlite3.IntegrityError):
            self.open()
        conn = sqlite3.connect(self.path)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            columns = [row[1] for row in conn.execute("PRAGMA table_info(progress)")]
            rows = conn.execute("SELECT video_id FROM progress ORDER BY video_id").fetchall()
        finally:
            conn.close()
        self.assertEqual(tables, {"progress"})
        self.assertNotIn("profile_id", columns)
        self.assertEqual(rows, [("a",), ("b",)])

    def test_a_new_database_starts_with_one_profile(self):
        store = self.open()
        self.assertEqual(store.list_profiles(), [{"id": 1, "name": DEFAULT_PROFILE_NAME}])
        self.assertEqual(store.all(), {})


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.store = ProgressStore(Path(self._tmp.name) / "litejelly.db")
        self.addCleanup(self.store.close)

    def test_each_profile_keeps_its_own_history(self):
        guest = self.store.create_profile("Guest")["id"]
        self.store.save("film", 300, 600)
        self.store.save("film", 45, 600, profile=guest)
        self.store.save("other", 600, 600, profile=guest)
        self.assertEqual(self.store.get("film")["position"], 300)
        self.assertEqual(self.store.get("film", guest)["position"], 45)
        self.assertEqual(set(self.store.all()), {"film"})
        self.assertEqual(set(self.store.all(guest)), {"film", "other"})
        self.store.clear("film", guest)
        self.assertIsNone(self.store.get("film", guest))
        self.assertIsNotNone(self.store.get("film"))

    def test_deleting_a_profile_takes_only_its_history(self):
        guest = self.store.create_profile("Guest")["id"]
        self.store.save("film", 300, 600)
        self.store.save("film", 45, 600, profile=guest)
        self.store.save("other", 50, 600, profile=guest)
        self.assertEqual(self.store.delete_profile(guest), 2)
        self.assertEqual([p["name"] for p in self.store.list_profiles()], [DEFAULT_PROFILE_NAME])
        self.assertEqual(self.store.get("film")["position"], 300)
        with self.assertRaises(UnknownProfile):
            self.store.all(guest)

    def test_the_last_profile_cannot_be_deleted(self):
        with self.assertRaises(ProfileError):
            self.store.delete_profile(1)
        self.assertEqual(len(self.store.list_profiles()), 1)

    def test_the_default_moves_when_the_first_profile_is_deleted(self):
        guest = self.store.create_profile("Guest")["id"]
        self.store.save("film", 45, 600, profile=guest)
        self.store.delete_profile(1)
        self.assertEqual(self.store.default_profile(), guest)
        self.assertEqual(self.store.resolve_profile(""), guest)
        self.assertEqual(self.store.get("film")["position"], 45)

    def test_saving_for_a_deleted_profile_leaves_no_row_behind(self):
        guest = self.store.create_profile("Guest")["id"]
        self.store.delete_profile(guest)
        with self.assertRaises(UnknownProfile):
            self.store.save("film", 45, 600, profile=guest)
        conn = sqlite3.connect(Path(self._tmp.name) / "litejelly.db")
        try:
            count = conn.execute("SELECT COUNT(*) FROM progress").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_resolving_what_a_request_names(self):
        guest = self.store.create_profile("Guest")["id"]
        self.assertEqual(self.store.resolve_profile(None), 1)
        self.assertEqual(self.store.resolve_profile(""), 1)
        self.assertEqual(self.store.resolve_profile(str(guest)), guest)
        self.assertEqual(self.store.resolve_profile(guest), guest)
        for bad in ("999", "-1", "1.0", "Guest", "1 OR 1=1", "9" * 40, True):
            with self.subTest(value=bad):
                self.assertIsNone(self.store.resolve_profile(bad))

    def test_names_are_checked(self):
        for bad in ("", "   ", "x" * (MAX_PROFILE_NAME + 1), "bell\x07", "Ana\u202egnp.exe"):
            with self.subTest(name=bad):
                with self.assertRaises(ProfileError):
                    self.store.create_profile(bad)
        self.assertEqual(self.store.create_profile("  Ana \t  Maria ")["name"], "Ana Maria")
        self.assertEqual(len(self.store.create_profile("x" * MAX_PROFILE_NAME)["name"]),
                         MAX_PROFILE_NAME)

    def test_names_are_unique_whatever_the_case(self):
        guest = self.store.create_profile("Guest")["id"]
        with self.assertRaises(ProfileError):
            self.store.create_profile("GUEST")
        with self.assertRaises(ProfileError):
            self.store.create_profile("home")
        with self.assertRaises(ProfileError):
            self.store.rename_profile(guest, "Home")
        self.assertEqual(self.store.rename_profile(guest, "guest")["name"], "guest")
        # SQLite's NOCASE folds ASCII only.
        self.store.create_profile("\u00c9mile")
        with self.assertRaises(ProfileError):
            self.store.create_profile("\u00e9mile")
        with self.assertRaises(UnknownProfile):
            self.store.rename_profile(999, "Nobody")

    def test_there_is_a_limit(self):
        for index in range(MAX_PROFILES - 1):
            self.store.create_profile(f"Viewer {index}")
        with self.assertRaises(ProfileError):
            self.store.create_profile("One too many")
        self.assertEqual(len(self.store.list_profiles()), MAX_PROFILES)


class ProfileHttpTests(unittest.TestCase):
    """A server of its own, since these tests add and delete profiles."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        media = root / "media"
        media.mkdir()
        (media / "Some.Show.S01E01.mkv").write_bytes(b"x" * 1024)
        (root / "config.json").write_text(json.dumps({
            "port": 0, "host": "127.0.0.1", "media_dirs": [str(media)],
        }), encoding="utf-8")
        config, _ = load_config(root)
        config.cache_dir.mkdir(parents=True, exist_ok=True)
        cls.app = Application(config)
        cls.app.library.scan(force=True)
        cls.app.credentials = auth.Credentials(
            username=ADMIN_USER,
            password_hash=auth.hash_password(ADMIN_PASSWORD, iterations=1000),
        )
        cls.httpd = create_server(cls.app)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.video = cls.app.library.videos[0]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.shutdown()
        cls._tmp.cleanup()

    def setUp(self):
        self.app.throttle.record_success("127.0.0.1")
        # Every test starts from a fresh install: one profile, nothing watched.
        store = self.app.progress
        with store._lock, store._conn:
            store._conn.execute("DELETE FROM progress")
            store._conn.execute("DELETE FROM profiles")
            store._conn.execute("INSERT INTO profiles VALUES (1, ?, 0)",
                                (DEFAULT_PROFILE_NAME,))

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body),
                         headers=headers or ({} if body is None else WRITE_HEADERS))
            response = conn.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {}), response
        finally:
            conn.close()

    def cookie(self):
        status, _, response = self.call("POST", "/api/admin/login",
                                        {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        self.assertEqual(status, 200)
        return response.getheader("Set-Cookie").split(";")[0]

    def admin(self, path, body, cookie):
        return self.call("POST", path, body, {**WRITE_HEADERS, "Cookie": cookie})

    def test_profiles_are_listed_for_any_screen(self):
        guest = self.app.progress.create_profile("Guest")["id"]
        status, data, _ = self.call("GET", "/api/profiles")
        self.assertEqual(status, 200)
        self.assertEqual(data["profiles"], [{"id": 1, "name": DEFAULT_PROFILE_NAME},
                                            {"id": guest, "name": "Guest"}])
        self.assertEqual(data["default"], 1)

    @staticmethod
    def position(rows, video):
        return (rows.get(video) or {}).get("position")

    def test_progress_follows_the_profile_through_every_route(self):
        guest = self.app.progress.create_profile("Guest")["id"]
        video = self.video.id
        status, _, _ = self.call("POST", "/api/progress",
                                 {"id": video, "position": 200, "duration": 1000,
                                  "profile": guest})
        self.assertEqual(status, 200)

        _, mine, _ = self.call("GET", f"/api/library?profile={guest}")
        _, theirs, _ = self.call("GET", "/api/library?profile=1")
        _, unnamed, _ = self.call("GET", "/api/library")
        self.assertEqual(self.position(mine["progress"], video), 200)
        self.assertEqual([entry["id"] for entry in mine["continue_watching"]], [video])
        self.assertEqual(theirs["progress"], {})
        self.assertEqual(theirs["continue_watching"], [])
        self.assertEqual(unnamed["progress"], {})

        _, row, _ = self.call("GET", f"/api/progress?id={video}&profile={guest}")
        self.assertEqual(row.get("position"), 200)
        _, row, _ = self.call("GET", f"/api/progress?id={video}")
        self.assertEqual(row, {})

        series = self.video.series_id
        _, page, _ = self.call("GET", f"/api/series?id={series}&profile={guest}")
        self.assertEqual(page["episodes"][0]["position"], 200)
        _, page, _ = self.call("GET", f"/api/series?id={series}")
        self.assertEqual(page["episodes"][0]["position"], 0)

        info = MediaInfo(probed=True, duration=1000, container="mp4", video_codec="h264",
                         audio_codec="aac", width=1280, height=720)
        with mock.patch.object(self.app.tools, "probe", return_value=info), \
             mock.patch("litejelly.playback.discover_subtitles", return_value=[]):
            _, plan, _ = self.call("GET", f"/api/playback?id={video}&profile={guest}")
            self.assertEqual(plan["resume"].get("position"), 200)
            _, plan, _ = self.call("GET", f"/api/playback?id={video}")
            self.assertEqual(plan["resume"], {})

    def test_a_request_without_a_profile_uses_the_default(self):
        status, _, _ = self.call("POST", "/api/progress",
                                 {"id": self.video.id, "position": 90, "duration": 1000})
        self.assertEqual(status, 200)
        self.assertEqual(self.position(self.app.progress.all(1), self.video.id), 90)

    def test_an_unknown_profile_is_refused_everywhere(self):
        video = self.video.id
        for method, path, body in (
                ("GET", "/api/library?profile=77", None),
                ("GET", "/api/progress?profile=77", None),
                ("GET", f"/api/series?id={self.video.series_id}&profile=77", None),
                ("GET", f"/api/playback?id={video}&profile=77", None),
                ("GET", "/api/library?profile=abc", None),
                ("POST", "/api/progress",
                 {"id": video, "position": 10, "duration": 100, "profile": 77})):
            with self.subTest(path=path):
                status, data, _ = self.call(method, path, body)
                self.assertEqual(status, 404)
                self.assertEqual(data["error"], "Unknown profile")
        self.assertIsNone(self.app.progress.get(video))

    def test_a_profile_deleted_mid_request_is_a_404_not_a_crash(self):
        with mock.patch.object(self.app.progress, "resolve_profile", return_value=77):
            status, data, _ = self.call("GET", "/api/library?profile=77")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "Unknown profile")

    def test_another_site_cannot_write_progress_for_a_profile(self):
        guest = self.app.progress.create_profile("Guest")["id"]
        status, _, _ = self.call("POST", "/api/progress",
                                 {"id": self.video.id, "position": 10, "duration": 100,
                                  "profile": guest},
                                 {"Content-Type": "application/json",
                                  "Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertIsNone(self.app.progress.get(self.video.id, guest))

    def test_only_the_admin_changes_who_exists(self):
        guest = self.app.progress.create_profile("Guest")["id"]
        for path, body in (("/api/admin/profiles", {"name": "Intruder"}),
                           ("/api/admin/profiles/rename", {"id": guest, "name": "Mine"}),
                           ("/api/admin/profiles/delete", {"id": guest})):
            with self.subTest(path=path, signed_in=False):
                status, _, _ = self.call("POST", path, body)
                self.assertEqual(status, 401)
        cookie = self.cookie()
        for path, body in (("/api/admin/profiles", {"name": "Intruder"}),
                           ("/api/admin/profiles/rename", {"id": guest, "name": "Mine"}),
                           ("/api/admin/profiles/delete", {"id": guest})):
            with self.subTest(path=path, cross_site=True):
                status, _, _ = self.call("POST", path, body,
                                         {"Content-Type": "application/json",
                                          "Cookie": cookie,
                                          "Origin": "http://evil.example"})
                self.assertEqual(status, 403)
        self.assertEqual([p["name"] for p in self.app.progress.list_profiles()],
                         [DEFAULT_PROFILE_NAME, "Guest"])

    def test_the_admin_adds_renames_and_deletes(self):
        cookie = self.cookie()
        status, data, _ = self.admin("/api/admin/profiles", {"name": "Guest"}, cookie)
        self.assertEqual(status, 200)
        guest = data["profile"]["id"]
        self.assertEqual([p["name"] for p in data["profiles"]], [DEFAULT_PROFILE_NAME, "Guest"])

        status, data, _ = self.admin("/api/admin/profiles", {"name": "guest"}, cookie)
        self.assertEqual(status, 400)
        self.assertIn("already exists", data["error"])

        status, data, _ = self.admin("/api/admin/profiles/rename",
                                     {"id": guest, "name": "Kids"}, cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["profile"], {"id": guest, "name": "Kids"})

        self.app.progress.save(self.video.id, 50, 100, profile=guest)
        status, data, _ = self.admin("/api/admin/profiles/delete", {"id": guest}, cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["removed"], 1)
        self.assertEqual(data["profiles"], [{"id": 1, "name": DEFAULT_PROFILE_NAME}])

    def test_a_delete_must_name_its_profile(self):
        cookie = self.cookie()
        for body in ({}, {"id": ""}, {"id": 999}, {"id": "Home"}):
            with self.subTest(body=body):
                status, _, _ = self.admin("/api/admin/profiles/delete", body, cookie)
                self.assertEqual(status, 404)
        status, data, _ = self.admin("/api/admin/profiles/delete", {"id": 1}, cookie)
        self.assertEqual(status, 409)
        self.assertIn("last profile", data["error"])
        self.assertEqual(len(self.app.progress.list_profiles()), 1)


if __name__ == "__main__":
    unittest.main()
