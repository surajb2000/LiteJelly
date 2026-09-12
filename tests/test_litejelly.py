"""Tests for path containment, range parsing, subtitle conversion and titles.

Run with:  python -m unittest discover -s tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.library import parse_title, human_size
from litejelly.paths import resolve_within, safe_resolve
from litejelly.subtitles import srt_to_vtt, shift_vtt, _language_from_token
from litejelly.web import parse_range


class PathContainmentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "media"
        (self.root / "shows").mkdir(parents=True)
        (self.root / "shows" / "ep1.mp4").write_bytes(b"data")
        self.outside = Path(self._tmp.name) / "secret.txt"
        self.outside.write_text("top secret")

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolves_nested_file(self):
        self.assertEqual(
            resolve_within(self.root, "shows/ep1.mp4"),
            (self.root / "shows" / "ep1.mp4").resolve(),
        )

    def test_rejects_parent_traversal(self):
        for attempt in ["../secret.txt", "shows/../../secret.txt", "..\\secret.txt"]:
            with self.subTest(attempt=attempt):
                self.assertIsNone(resolve_within(self.root, attempt))

    def test_rejects_absolute_paths(self):
        for attempt in ["/etc/passwd", "C:\\Windows\\win.ini", str(self.outside)]:
            with self.subTest(attempt=attempt):
                self.assertIsNone(resolve_within(self.root, attempt))

    def test_rejects_null_byte_and_empty(self):
        self.assertIsNone(resolve_within(self.root, "shows/ep1.mp4\x00.txt"))
        self.assertIsNone(resolve_within(self.root, ""))

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unsupported")
    def test_rejects_symlink_escape(self):
        link = self.root / "escape.txt"
        try:
            link.symlink_to(self.outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted")
        self.assertIsNone(resolve_within(self.root, "escape.txt"))

    def test_safe_resolve_honours_dir_index(self):
        path, index = safe_resolve([str(self.root)], "shows/ep1.mp4", 0)
        self.assertIsNotNone(path)
        self.assertEqual(index, 0)
        self.assertEqual(safe_resolve([str(self.root)], "shows/ep1.mp4", 5), (None, None))


class RangeParsingTests(unittest.TestCase):
    SIZE = 1000

    def test_no_header(self):
        self.assertIsNone(parse_range(None, self.SIZE))

    def test_simple_range(self):
        self.assertEqual(parse_range("bytes=0-499", self.SIZE), (0, 499))

    def test_open_ended_range(self):
        self.assertEqual(parse_range("bytes=500-", self.SIZE), (500, 999))

    def test_suffix_range(self):
        self.assertEqual(parse_range("bytes=-200", self.SIZE), (800, 999))

    def test_end_is_clamped_to_file_size(self):
        self.assertEqual(parse_range("bytes=0-99999999", self.SIZE), (0, 999))

    def test_invalid_ranges(self):
        for header in ["bytes=1000-", "bytes=900-800", "bytes=abc-def",
                       "items=0-10", "bytes=-0", "bytes=5"]:
            with self.subTest(header=header):
                self.assertEqual(parse_range(header, self.SIZE), "invalid")


class SubtitleTests(unittest.TestCase):
    def test_srt_converts_to_vtt(self):
        srt = (
            "1\r\n"
            "00:00:01,500 --> 00:00:04,000\r\n"
            "Hello there\r\n"
            "\r\n"
            "2\r\n"
            "01:02:03,250 --> 01:02:05,000\r\n"
            "General Kenobi\r\n"
        )
        vtt = srt_to_vtt(srt)
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("00:00:01.500 --> 00:00:04.000", vtt)
        self.assertIn("01:02:03.250 --> 01:02:05.000", vtt)
        self.assertIn("Hello there", vtt)
        self.assertNotIn("\r", vtt)

    def test_cue_numbers_are_dropped(self):
        vtt = srt_to_vtt("1\n00:00:01,000 --> 00:00:02,000\nLine\n")
        self.assertNotIn("\n1\n", vtt)

    def test_numeric_subtitle_text_is_kept(self):
        vtt = srt_to_vtt("1\n00:00:01,000 --> 00:00:02,000\n42\n")
        self.assertIn("42", vtt)

    def test_language_tokens(self):
        self.assertEqual(_language_from_token("eng"), ("en", "English"))
        self.assertEqual(_language_from_token("English"), ("en", "English"))
        self.assertEqual(_language_from_token("pt-BR")[1], "Portuguese")
        self.assertEqual(_language_from_token("zzz"), ("", ""))


class SubtitleShiftTests(unittest.TestCase):
    SAMPLE = (
        "WEBVTT\n\n"
        "00:00:01.500 --> 00:00:04.000\nfirst\n\n"
        "00:02:03.250 --> 00:02:05.000\nsecond\n"
    )

    def test_zero_offset_is_unchanged(self):
        self.assertEqual(shift_vtt(self.SAMPLE, 0), self.SAMPLE)

    def test_offset_rebases_cues(self):
        shifted = shift_vtt(self.SAMPLE, 120.0)
        self.assertIn("00:00:03.250 --> 00:00:05.000", shifted)
        self.assertIn("second", shifted)

    def test_passed_cues_are_dropped(self):
        shifted = shift_vtt(self.SAMPLE, 120.0)
        self.assertNotIn("first", shifted)

    def test_header_is_preserved(self):
        self.assertTrue(shift_vtt(self.SAMPLE, 5).startswith("WEBVTT"))

    def test_cue_settings_are_kept(self):
        text = "WEBVTT\n\n00:00:30.000 --> 00:00:32.000 line:90% align:center\nhi\n"
        shifted = shift_vtt(text, 10)
        self.assertIn("00:00:20.000 --> 00:00:22.000 line:90% align:center", shifted)


class TitleParsingTests(unittest.TestCase):
    def test_strips_release_noise(self):
        parsed = parse_title("The.Big.Movie.2019.1080p.BluRay.x264-GROUP.mkv")
        self.assertEqual(parsed["title"], "The Big Movie")
        self.assertEqual(parsed["year"], 2019)

    def test_detects_episodes(self):
        parsed = parse_title("Some.Show.S02E07.720p.WEB-DL.mkv")
        self.assertEqual(parsed["episode"], {"season": 2, "episode": 7})
        self.assertEqual(parsed["title"], "Some Show")

    def test_plain_names_survive(self):
        self.assertEqual(parse_title("holiday clip.mp4")["title"], "Holiday Clip")

    def test_never_returns_empty(self):
        self.assertTrue(parse_title("1080p.x264.mkv")["title"])

    def test_human_size(self):
        self.assertEqual(human_size(512), "512.0 B")
        self.assertTrue(human_size(5 * 1024 ** 3).endswith("GB"))


if __name__ == "__main__":
    unittest.main()
