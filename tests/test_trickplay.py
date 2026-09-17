"""Trickplay sheet planning, keys and serving.

The scrub bar used to show a time and nothing else. One sheet per file holds
every preview frame, so the client fetches a single image rather than asking
the server per position.

Run with:  python -m unittest discover -s tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.trickplay import COLUMNS, MAX_TILES, TrickplayService, _jpeg_size


class _Tools:
    def __init__(self, available=True):
        self.ffmpeg = "ffmpeg"
        self.available = available


def _jpeg(width: int, height: int) -> bytes:
    """Smallest thing that parses as a JPEG of a given size."""
    return (b"\xff\xd8"
            + b"\xff\xc0" + (11).to_bytes(2, "big") + b"\x08"
            + height.to_bytes(2, "big") + width.to_bytes(2, "big")
            + b"\x03\x01\x11\x00")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = TrickplayService(_Tools(), Path(self._tmp.name), interval=10)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_half_hour_episode_fits_one_grid(self):
        interval, count, rows = self.service.plan(24 * 60)
        self.assertEqual(interval, 10)
        self.assertEqual(count, 144)
        self.assertEqual(rows, 15)

    def test_the_interval_widens_rather_than_growing_the_sheet(self):
        # A sheet has to stay a sane download, so a long film loses detail
        # instead of producing thousands of tiles.
        interval, count, _ = self.service.plan(6 * 3600)
        self.assertLessEqual(count, MAX_TILES)
        self.assertGreater(interval, 10)

    def test_a_short_clip_still_gets_a_tile(self):
        interval, count, rows = self.service.plan(7)
        self.assertEqual((count, rows), (1, 1))
        self.assertGreater(interval, 0)

    def test_rows_cover_every_tile(self):
        for duration in (60, 599, 600, 601, 3600):
            _, count, rows = self.service.plan(duration)
            self.assertGreaterEqual(rows * COLUMNS, count, duration)


class KeyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.video = self.root / "movie.mkv"
        self.video.write_bytes(b"x" * 4096)
        self.service = TrickplayService(_Tools(), self.root / ".cache", interval=10)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_key_is_stable_for_an_unchanged_file(self):
        self.assertEqual(self.service.key_for(self.video),
                         self.service.key_for(self.video))

    def test_settings_change_the_key(self):
        other = TrickplayService(_Tools(), self.root / ".cache", interval=5)
        self.assertNotEqual(self.service.key_for(self.video), other.key_for(self.video))

    def test_an_edited_file_gets_a_new_key(self):
        before = self.service.key_for(self.video)
        self.video.write_bytes(b"y" * 8192)
        self.assertNotEqual(before, self.service.key_for(self.video))

    def test_a_missing_file_has_no_key(self):
        self.assertIsNone(self.service.key_for(self.root / "gone.mkv"))

    def test_a_made_up_key_serves_nothing(self):
        # The key names a file in the cache, so it is never trusted as given.
        for bogus in ("", "../../etc/passwd", "z" * 20, "abc", "a" * 40):
            self.assertIsNone(self.service.sheet_path(bogus))


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.video = self.root / "movie.mkv"
        self.video.write_bytes(b"x" * 4096)
        self.service = TrickplayService(_Tools(), self.root / ".cache", interval=10)
        self.commands = []

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def _run(self, produce=True, fail_fast_path=False, width=1600, height=360):
        def fake_run(cmd, timeout=None):
            self.commands.append(cmd)
            fast = "-skip_frame" in cmd
            if produce and not (fast and fail_fast_path):
                target = Path(cmd[-1])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_jpeg(width, height))
                return subprocess.CompletedProcess(cmd, 0, b"", b"")
            return subprocess.CompletedProcess(cmd, 1, b"", b"failed")

        with mock.patch("litejelly.trickplay.run_quiet", fake_run):
            return self.service._generate(self.service.key_for(self.video),
                                          self.video, 360.0)

    def test_only_keyframes_are_decoded(self):
        self.assertTrue(self._run())
        self.assertIn("-skip_frame", self.commands[0])
        self.assertEqual(self.commands[0][self.commands[0].index("-skip_frame") + 1],
                         "nokey")

    def test_a_clip_shorter_than_the_interval_falls_back_to_a_full_decode(self):
        # Measured: with -skip_frame nokey, fps emits no frames at all for a
        # clip shorter than one interval, so ffmpeg writes nothing.
        self.assertTrue(self._run(fail_fast_path=True))
        self.assertEqual(len(self.commands), 2)
        self.assertNotIn("-skip_frame", self.commands[1])

    def test_the_sheet_records_the_tile_size_it_really_has(self):
        self._run(width=1600, height=360)
        sheet = self.service.cached(self.video)
        self.assertEqual((sheet.tile_width, sheet.tile_height), (160, 90))
        self.assertEqual((sheet.columns, sheet.rows), (10, 4))
        self.assertEqual(sheet.count, 36)

    def test_a_failure_leaves_nothing_behind(self):
        self.assertFalse(self._run(produce=False))
        self.assertIsNone(self.service.cached(self.video))
        self.assertEqual(list(self.service.cache_dir.glob("*.part.jpg")), [])

    def test_a_ready_sheet_is_served_by_key(self):
        self._run()
        sheet = self.service.cached(self.video)
        self.assertIsNotNone(self.service.sheet_path(sheet.key))

    def test_a_corrupt_record_is_ignored(self):
        self._run()
        key = self.service.key_for(self.video)
        (self.service.cache_dir / f"{key}.json").write_text("{ broken", encoding="utf-8")
        self.assertIsNone(self.service.cached(self.video))

    def test_a_record_without_its_picture_is_ignored(self):
        self._run()
        key = self.service.key_for(self.video)
        (self.service.cache_dir / f"{key}.jpg").unlink()
        self.assertIsNone(self.service.cached(self.video))


class RequestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.video = self.root / "movie.mkv"
        self.video.write_bytes(b"x" * 4096)
        self.service = TrickplayService(_Tools(), self.root / ".cache")
        # Nothing should start a worker in these tests.
        self.service._ensure_worker = lambda: None

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def test_nothing_is_queued_when_the_feature_is_off(self):
        self.service.enabled = False
        self.assertFalse(self.service.request(self.video, 600))

    def test_nothing_is_queued_without_ffmpeg(self):
        self.service.tools = _Tools(available=False)
        self.assertFalse(self.service.request(self.video, 600))

    def test_an_unknown_duration_is_not_queued(self):
        self.assertFalse(self.service.request(self.video, 0))

    def test_the_same_file_is_only_queued_once(self):
        self.assertTrue(self.service.request(self.video, 600))
        self.assertTrue(self.service.request(self.video, 600))
        self.assertEqual(self.service._queue.qsize(), 1)

    def test_a_repeatedly_failing_file_is_given_up_on(self):
        from litejelly.trickplay import MAX_FAILURES

        key = self.service.key_for(self.video)
        self.service._failed[key] = MAX_FAILURES
        self.assertFalse(self.service.request(self.video, 600))


class JpegSizeTests(unittest.TestCase):
    """The tile size has to come from the picture itself; ffmpeg rounds the
    scale to even numbers, so the requested width is not always what it made."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sheet.jpg"

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_the_frame_header(self):
        self.path.write_bytes(_jpeg(1600, 360))
        self.assertEqual(_jpeg_size(self.path), (1600, 360))

    def test_rejects_something_that_is_not_a_jpeg(self):
        self.path.write_bytes(b"not a picture at all")
        self.assertIsNone(_jpeg_size(self.path))

    def test_rejects_a_truncated_file(self):
        self.path.write_bytes(_jpeg(1600, 360)[:6])
        self.assertIsNone(_jpeg_size(self.path))

    def test_missing_file_is_not_an_error(self):
        self.assertIsNone(_jpeg_size(self.path))


if __name__ == "__main__":
    unittest.main()
