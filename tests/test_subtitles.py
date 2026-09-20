"""Keeping subtitles on screen for the whole film.

Cue timings used to be re-based by the server for whatever time the pipe had
been restarted at, so the entire subtitle file was fetched again on every
single seek. Measured in a browser: eight seeks cost eight fetches.

That made a fragile thing frequent. A track whose fetch fails ends up in
readyState ERROR with zero cues, and nothing retried it, so subtitles were
gone for the rest of the film. Toggling them off and on could not bring them
back either - measured: after one aborted fetch, toggling left the track at
readyState 3 with cues 0 and nothing painted.

displayTime() is already absolute on every transport, so the cues are now
fetched once in the file's own timeline and looked up against that. Eight
seeks now cost one fetch, and a failed fetch is retried.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly import subtitles

STATIC = Path(__file__).resolve().parent.parent / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def body(text: str, start: str, end: str) -> str:
    head = text.index(start)
    return text[head:text.index(end, head + len(start))]


class ExistingFileTests(unittest.TestCase):
    """Subtitles that were already on disk, not ones added through the player.

    A file downloaded from anywhere is usually CRLF. Converting it kept those
    endings, and writing the conversion on Windows turned each \\r\\n into
    \\r\\r\\n, which came back out of the cache as a blank line between a
    timing and its text - and a blank line is what ends a cue. Measured: the
    first serve was fine and every serve after it had cues with timings and no
    words at all.
    """

    SRT = ("1\r\n00:00:01,000 --> 00:00:03,000\r\nFirst line.\r\n\r\n"
           "2\r\n00:00:04,000 --> 00:00:06,000\r\nSecond line.\r\n")
    VTT = ("WEBVTT\r\n\r\n00:00:01.000 --> 00:00:03.000\r\nFirst line.\r\n\r\n"
           "00:00:04.000 --> 00:00:06.000\r\nSecond line.\r\n")

    def setUp(self):
        self.dir = TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.video = self.root / "Probe (2024).mkv"
        self.video.write_bytes(b"x" * 4096)
        self.service = subtitles.SubtitleService(None, self.root / "cache")

    def tearDown(self):
        self.dir.cleanup()

    def _serve_twice(self, suffix, payload):
        (self.root / f"Probe (2024).en{suffix}").write_bytes(payload.encode("utf-8"))
        first = self.service.get_vtt(self.video, "ext:0")
        return first, self.service.get_vtt(self.video, "ext:0")

    def test_a_crlf_vtt_keeps_its_text_when_served_again(self):
        first, second = self._serve_twice(".vtt", self.VTT)
        for text in (first, second):
            self.assertIn("00:00:01.000 --> 00:00:03.000\nFirst line.", text)
            self.assertIn("00:00:04.000 --> 00:00:06.000\nSecond line.", text)

    def test_a_crlf_srt_keeps_its_text_when_served_again(self):
        first, second = self._serve_twice(".srt", self.SRT)
        for text in (first, second):
            self.assertIn("00:00:01.000 --> 00:00:03.000\nFirst line.", text)

    def test_nothing_carries_a_carriage_return_to_the_browser(self):
        for suffix, payload in ((".vtt", self.VTT), (".srt", self.SRT)):
            with self.subTest(suffix=suffix):
                for old in self.root.glob("Probe (2024).en.*"):
                    old.unlink()
                first, second = self._serve_twice(suffix, payload)
                self.assertNotIn("\r", first)
                self.assertNotIn("\r", second)

    def test_the_cached_copy_is_written_without_carriage_returns(self):
        self._serve_twice(".vtt", self.VTT)
        cached = list((self.root / "cache" / "subtitles").glob("*.vtt"))
        self.assertTrue(cached)
        self.assertNotIn(b"\r", cached[0].read_bytes())

    def test_a_conversion_from_an_older_version_is_not_reused(self):
        # An install that already ran the broken writer has poisoned files in
        # the cache, and nothing would ever rewrite them.
        path = self.service._cache_path(self.video, "emb:0")
        original = subtitles.CACHE_VERSION
        try:
            subtitles.CACHE_VERSION = original + 1
            self.assertNotEqual(self.service._cache_path(self.video, "emb:0"), path)
        finally:
            subtitles.CACHE_VERSION = original


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.code = read("app.js")

    def test_cues_are_fetched_in_the_files_own_timeline(self):
        # An offset in the URL is what tied the cue list to one restart and
        # forced a refetch on the next.
        attach = body(self.code, "function addSubtitleTrack", "el.video.appendChild")
        self.assertIn("API.subtitle", attach)
        self.assertNotIn("offset=", attach)

    def test_cues_are_looked_up_against_absolute_time(self):
        # video.currentTime restarts at zero on the classic pipe, so it only
        # matched cues while they were re-based to match it.
        frame = body(self.code, "function renderSubtitleFrame", "function cuesAt")
        self.assertIn("displayTime()", frame)
        self.assertNotIn("el.video.currentTime", frame)

    def test_a_restart_reuses_the_loaded_cues(self):
        attach = body(self.code, "function attachSubtitleTracks", "const SUBTITLE_RETRIES")
        self.assertIn("state.subtitleSignature === subtitleSignature(plan)", attach)
        self.assertIn("applyActiveSubtitle();", attach)

    def test_only_a_different_file_throws_the_cues_away(self):
        # clearSubtitleTracks on every restart is what forced the refetch.
        menu = body(self.code, "function buildSubtitleMenu", "renderSubtitleMenu();")
        self.assertIn("state.subtitleSignature !== subtitleSignature(plan)", menu)

    def test_a_restart_does_not_deselect_the_track(self):
        # Measured: a disabled track drops its cues (readyState 0, cues null),
        # so resetting the choice on every restart forced them to be fetched
        # again even when the file had not changed.
        start = body(self.code, "function startPlayback", "function resolveLanding")
        self.assertNotIn("state.activeSubtitle = 'off'", start)


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.code = read("app.js")

    def test_a_failed_track_is_retried(self):
        add = body(self.code, "function addSubtitleTrack", "function applyActiveSubtitle")
        self.assertIn("addEventListener('error'", add)
        self.assertIn("SUBTITLE_RETRIES[attempt]", add)
        self.assertIn("addSubtitleTrack(plan, track, attempt + 1)", add)

    def test_the_retry_gives_up_and_says_so(self):
        add = body(self.code, "function addSubtitleTrack", "function applyActiveSubtitle")
        self.assertIn("attempt >= SUBTITLE_RETRIES.length", add)
        self.assertIn("showToast", add)

    def test_a_dead_track_element_is_replaced_not_reused(self):
        # Re-enabling a track that failed loads nothing; only a new element
        # fetches again.
        add = body(self.code, "function addSubtitleTrack", "function applyActiveSubtitle")
        self.assertIn("element.remove()", add)

    def test_the_retry_does_not_leave_two_copies(self):
        # A rebuild can land while the retry is waiting; without this guard
        # the browser ended up with two track elements for one track.
        add = body(self.code, "function addSubtitleTrack", "function applyActiveSubtitle")
        self.assertIn("node.dataset.trackId === track.id", add)


if __name__ == "__main__":
    unittest.main()
