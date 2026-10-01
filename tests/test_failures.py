"""What happens when storage fails or many requests arrive at once.

Each test drives a real failure where one can be produced locally: a SQLite
page limit stands in for a full disk, and real threads race the progress store.
Mocks are used only where the operating system cannot be made to fail on cue.

Run with:  python -m unittest discover -s tests
"""

import errno
import logging
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly import settings
from litejelly.store import ProgressStore
from litejelly.subtitles import SubtitleService
from litejelly.thumbnails import ThumbnailService

logging.getLogger("litejelly").setLevel(logging.CRITICAL)

SRT = "1\n00:00:01,000 --> 00:00:03,000\nStill readable.\n"


def disk_full():
    return OSError(errno.ENOSPC, "No space left on device")


class ProgressStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ProgressStore(Path(self._tmp.name) / "progress.db")
        self.addCleanup(self.store.close)

    def test_a_full_disk_fails_the_write_but_keeps_history_and_recovers(self):
        """Measured 2026-10-01 with a SQLite page cap: the failed write raises
        'database or disk is full', no transaction is left open, and the next
        write succeeds once space returns."""
        self.store.save("kept", 100, 1000)
        pages = self.store._conn.execute("PRAGMA page_count").fetchone()[0]
        self.store._conn.execute(f"PRAGMA max_page_count = {pages}")
        with self.assertRaisesRegex(sqlite3.OperationalError, "full"):
            for index in range(5000):
                self.store.save("x" * 500 + str(index), 10, 100)
        self.assertFalse(self.store._conn.in_transaction)
        self.assertEqual(self.store.get("kept")["position"], 100)
        self.store._conn.execute("PRAGMA max_page_count = 1000000")
        self.store.save("after", 30, 100)
        self.assertEqual(self.store.get("after")["position"], 30)

    def test_concurrent_writers_and_readers_do_not_corrupt_progress(self):
        failures = []
        positions = [float(n) for n in range(20, 420)]
        start = threading.Barrier(9)

        def write(worker):
            try:
                start.wait(5)
                for index, position in enumerate(positions[worker::8]):
                    self.store.save("shared", position, 1000)
                    self.store.save(f"own-{worker}", index, 1000)
            except Exception as error:  # surfaced through the assertion below
                failures.append(error)

        def read():
            try:
                start.wait(5)
                for _ in range(200):
                    self.store.all()
                    self.store.get("shared")
            except Exception as error:
                failures.append(error)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
        threads.append(threading.Thread(target=read))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(failures, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        rows = self.store.all()
        self.assertEqual(sorted(rows), sorted(["shared"] + [f"own-{n}" for n in range(8)]))
        self.assertIn(rows["shared"]["position"], positions)


class SettingsStorageTests(unittest.TestCase):
    def test_a_failed_save_leaves_the_old_file_and_no_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings.save_overrides(root, {"server_name": "Before"})
            with mock.patch("litejelly.settings.os.replace", side_effect=disk_full()):
                with self.assertRaises(OSError):
                    settings.save_overrides(root, {"server_name": "After"})
            self.assertEqual(settings.load_overrides(root), {"server_name": "Before"})
            self.assertEqual(list(root.glob("*.tmp")), [])


class CacheWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.video = self.root / "movie.mkv"
        self.video.write_bytes(b"x" * 2048)

    def test_a_subtitle_is_still_served_when_its_cache_cannot_be_written(self):
        (self.root / "movie.en.srt").write_text(SRT, encoding="utf-8")
        service = SubtitleService(mock.Mock(available=False), self.root / ".cache")
        with mock.patch.object(Path, "write_text", side_effect=disk_full()):
            try:
                vtt = service.get_vtt(self.video, "ext:0")
            except OSError as error:
                self.fail(f"a cache that cannot be written must not stop the subtitle: {error}")
        self.assertIn("Still readable.", vtt)
        self.assertEqual(list((self.root / ".cache").rglob("*.vtt")), [])
        self.assertIn("Still readable.", service.get_vtt(self.video, "ext:0"))
        self.assertEqual(len(list((self.root / ".cache").rglob("*.vtt"))), 1)

    def test_a_thumbnail_that_cannot_reserve_space_fails_quietly(self):
        service = ThumbnailService(mock.Mock(available=True, ffmpeg="ffmpeg"), self.root / ".cache")
        self.addCleanup(service.close)
        target = service._cache_path(self.video)
        with mock.patch("litejelly.thumbnails.tempfile.mkstemp", side_effect=disk_full()), \
             mock.patch("litejelly.thumbnails.run_quiet") as run:
            service._generate(self.video, target, 600)
        run.assert_not_called()
        self.assertIsNone(service.cached(self.video))


if __name__ == "__main__":
    unittest.main()
