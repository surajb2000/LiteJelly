"""Media identity and progress survive cache deletion, folder changes and upgrades."""

from __future__ import annotations

import shutil
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from litejelly.config import Config, MediaDir
from litejelly.library import Library, _make_id
from litejelly.store import ProgressStore


class IdentityTests(unittest.TestCase):
    def setUp(self):
        """Prepare two roots with the same relative filename and isolated durable storage."""
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.first, self.second = self.root / "first", self.root / "second"
        self.first.mkdir()
        self.second.mkdir()
        for directory in (self.first, self.second):
            (directory / "Movie.mp4").write_bytes(directory.name.encode())
        self.config = Config(app_dir=self.root)
        self.store = ProgressStore(self.config.db_path)
        self.addCleanup(self.store.close)
        self.library = Library([MediaDir(str(self.first)), MediaDir(str(self.second))],
                               identify=self.store.identify_media)

    def test_reordering_roots_preserves_ids_and_paths_during_and_after_scan(self):
        videos = self.library.scan()
        video = next(item for item in videos if item.dir_index == 0)
        original_id = video.id
        self.store.save(original_id, 120, 600)
        self.library.set_media_dirs([MediaDir(str(self.second)), MediaDir(str(self.first))])
        self.assertEqual(self.library.absolute_path(video), self.first / "Movie.mp4")
        self.library.scan()
        current = self.library.get(original_id)
        self.assertIsNotNone(current)
        self.assertEqual(self.library.absolute_path(current), self.first / "Movie.mp4")
        self.assertEqual(self.store.get(original_id)["position"], 120)

    def test_replacing_a_root_does_not_reassign_an_existing_id(self):
        video = self.library.scan()[0]
        root = Path(video.root_path)
        other = self.root / "third"
        other.mkdir()
        (other / "Movie.mp4").write_bytes(b"different film")
        self.library.set_media_dirs([MediaDir(str(other))])
        self.assertIsNone(self.library.absolute_path(video))
        replacement = self.library.scan()[0]
        self.assertNotEqual(replacement.id, video.id)
        self.assertNotEqual(root, Path(replacement.root_path))

    def test_catalog_survives_restart_and_preserves_initial_legacy_ids(self):
        self.library.scan()
        expected = _make_id(0, "Movie.mp4")
        self.assertIsNotNone(self.library.get(expected))
        self.assertEqual(self.library.absolute_path(self.library.get(expected)), self.first / "Movie.mp4")
        reopened = Library([MediaDir(str(self.second)), MediaDir(str(self.first))],
                           identify=self.store.identify_media)
        reopened.scan()
        self.assertIsNotNone(reopened.get(expected))
        self.assertEqual(reopened.absolute_path(reopened.get(expected)), self.first / "Movie.mp4")

    def test_internal_roots_and_migration_ids_are_not_public(self):
        payload = self.library.scan()[0].to_dict()
        self.assertNotIn("root_path", payload)
        self.assertNotIn("legacy_id", payload)

    def test_in_place_edits_invalidate_automatic_scans(self):
        self.library.scan()
        video = self.first / "Movie.mp4"
        before = video.stat()
        video.write_bytes(b"longer replacement")
        os.utime(video, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        self.library.scan()
        item = next(item for item in self.library.videos if item.root_path == os.path.normcase(str(self.first)))
        self.assertEqual(item.size, len(b"longer replacement"))


class MigrationTests(unittest.TestCase):
    def test_failed_migration_keeps_legacy_and_can_be_retried(self):
        with TemporaryDirectory() as directory:
            config = Config(app_dir=Path(directory))
            legacy = ProgressStore(config.legacy_db_path)
            legacy.save("old", 123, 600)
            legacy.close()
            with mock.patch("litejelly.store.os.replace", side_effect=OSError("disk problem")):
                with self.assertRaises(OSError):
                    ProgressStore(config.db_path, config.legacy_db_path)
            self.assertFalse(config.db_path.exists())
            self.assertTrue(config.legacy_db_path.exists())
            self.assertEqual(list(config.db_path.parent.iterdir()), [])
            store = ProgressStore(config.db_path, config.legacy_db_path)
            try:
                self.assertEqual(store.get("old")["position"], 123)
            finally:
                store.close()

    def test_wal_progress_is_copied_once_and_survives_cache_deletion(self):
        with TemporaryDirectory() as directory:
            config = Config(app_dir=Path(directory))
            legacy = ProgressStore(config.legacy_db_path)
            try:
                legacy.save("old", 123, 600)
                current = ProgressStore(config.db_path, config.legacy_db_path)
                try:
                    self.assertEqual(current.get("old")["position"], 123)
                    current.save("old", 240, 600)
                finally:
                    current.close()
                legacy.save("old", 300, 600)
                current = ProgressStore(config.db_path, config.legacy_db_path)
                try:
                    self.assertEqual(current.get("old")["position"], 240)
                finally:
                    current.close()
                self.assertTrue(config.legacy_db_path.exists())
            finally:
                legacy.close()
            shutil.rmtree(config.cache_dir)
            current = ProgressStore(config.db_path)
            try:
                self.assertEqual(current.get("old")["position"], 240)
            finally:
                current.close()


if __name__ == "__main__":
    unittest.main()