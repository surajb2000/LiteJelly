"""Tests for thumbnail queueing, caching and failure handling.

The TV showed no thumbnails at all because generation happened inside the
request: a browser with few connections held them open behind a worker
semaphore, and one failure blanked a card permanently.

Run with:  python -m unittest discover -s tests
"""

import logging
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.thumbnails import ThumbnailService

logging.getLogger("litejelly.thumbnails").setLevel(logging.CRITICAL)


class _Tools:
    """Stands in for FFmpegTools with a controllable, slow generator."""

    def __init__(self, available=True, succeed=True, delay=0.0):
        self.ffmpeg = "ffmpeg"
        self.available = available
        self.succeed = succeed
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()


def _patch(service, tools):
    """Replace ffmpeg with a stub so tests do not depend on a real binary."""
    def fake_generate(video_path, cache_path, duration):
        with tools._lock:
            tools.calls += 1
        if tools.delay:
            time.sleep(tools.delay)
        if tools.succeed:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b"\xff\xd8jpeg-ish")

    service._generate = fake_generate


class ThumbnailServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.video = self.root / "movie.mkv"
        self.video.write_bytes(b"x" * 2048)
        self.tools = _Tools()
        self.service = ThumbnailService(self.tools, self.root / ".cache", workers=2)
        _patch(self.service, self.tools)

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def _wait_for_thumb(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.service.cached(self.video)
            if found is not None:
                return found
            time.sleep(0.02)
        return None

    def test_cached_is_empty_before_generation(self):
        self.assertIsNone(self.service.cached(self.video))

    def test_request_returns_immediately(self):
        self.tools.delay = 0.4
        started = time.time()
        self.assertTrue(self.service.request(self.video))
        # The whole point: the caller is not held while ffmpeg runs.
        self.assertLess(time.time() - started, 0.2)

    def test_request_eventually_produces_a_thumbnail(self):
        self.service.request(self.video)
        self.assertIsNotNone(self._wait_for_thumb())

    def test_second_request_serves_the_cache(self):
        self.service.request(self.video)
        self._wait_for_thumb()
        self.service.request(self.video)
        time.sleep(0.1)
        self.assertEqual(self.tools.calls, 1)

    def test_duplicate_requests_are_collapsed(self):
        self.tools.delay = 0.3
        for _ in range(8):
            self.service.request(self.video)
        self._wait_for_thumb()
        self.assertEqual(self.tools.calls, 1)

    def test_missing_ffmpeg_is_refused_rather_than_queued(self):
        self.tools.available = False
        self.assertFalse(self.service.request(self.video))

    def test_missing_file_is_refused(self):
        self.assertFalse(self.service.request(self.root / "gone.mkv"))

    def test_repeated_failures_stop_being_retried(self):
        # Otherwise every page load re-queues work that cannot succeed.
        self.tools.succeed = False
        for _ in range(6):
            self.service.request(self.video)
            time.sleep(0.08)
        self.assertFalse(self.service.request(self.video))
        self.assertLessEqual(self.tools.calls, 3)

    def test_a_failure_does_not_poison_a_different_video(self):
        self.tools.succeed = False
        for _ in range(4):
            self.service.request(self.video)
            time.sleep(0.05)

        other = self.root / "other.mkv"
        other.write_bytes(b"y" * 2048)
        self.tools.succeed = True
        self.assertTrue(self.service.request(other))

    def test_cache_key_changes_when_the_file_changes(self):
        self.service.request(self.video)
        self._wait_for_thumb()
        first = self.service.cached(self.video)

        time.sleep(0.01)
        self.video.write_bytes(b"z" * 4096)
        self.assertIsNone(self.service.cached(self.video),
                          "a re-encoded file must not keep the old thumbnail")
        self.service.request(self.video)
        self._wait_for_thumb()
        self.assertNotEqual(first, self.service.cached(self.video))

    def test_many_videos_are_all_generated(self):
        videos = []
        for index in range(12):
            path = self.root / f"clip{index}.mkv"
            path.write_bytes(bytes([index]) * 2048)
            videos.append(path)
            self.service.request(path)

        deadline = time.time() + 8
        while time.time() < deadline:
            if all(self.service.cached(p) is not None for p in videos):
                break
            time.sleep(0.05)
        missing = [p.name for p in videos if self.service.cached(p) is None]
        self.assertEqual(missing, [])

    def test_get_without_a_timeout_does_not_block(self):
        self.tools.delay = 0.5
        started = time.time()
        self.assertIsNone(self.service.get(self.video))
        self.assertLess(time.time() - started, 0.2)

    def test_close_is_safe_to_call_twice(self):
        self.service.close()
        self.service.close()


if __name__ == "__main__":
    unittest.main()
