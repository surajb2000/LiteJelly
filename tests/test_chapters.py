"""Tests for chapter markers and the Skip intro segments they produce.

Chapters come from the file itself, so the risk is not missing them but
trusting them: a chapter called "Intro" that spans the whole episode would
skip the episode.

Run with:  python -m unittest discover -s tests
"""

import json
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.chapters import Chapter, parse_chapters, read_chapters, skippable

logging.getLogger("litejelly.chapters").setLevel(logging.CRITICAL)


def _payload(*chapters):
    return json.dumps({"chapters": [
        {"start_time": str(start), "end_time": str(end), "tags": {"title": title}}
        for start, end, title in chapters
    ]})


class ParseTests(unittest.TestCase):
    def test_reads_chapters(self):
        chapters = parse_chapters(_payload((0, 90, "Intro"), (90, 1400, "Episode")))
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0].title, "Intro")
        self.assertEqual(chapters[0].start, 0)
        self.assertEqual(chapters[0].end, 90)

    def test_missing_titles_are_fine(self):
        payload = json.dumps({"chapters": [{"start_time": "0", "end_time": "10"}]})
        self.assertEqual(parse_chapters(payload)[0].title, "")

    def test_no_chapters(self):
        self.assertEqual(parse_chapters('{"chapters": []}'), [])
        self.assertEqual(parse_chapters("{}"), [])

    def test_malformed_input_is_not_an_error(self):
        for bad in ("", None, "not json", "[]", '{"chapters": "no"}'):
            self.assertEqual(parse_chapters(bad), [])

    def test_unparsable_times_are_skipped(self):
        payload = json.dumps({"chapters": [
            {"start_time": "abc", "end_time": "10"},
            {"start_time": "0", "end_time": "10"},
        ]})
        self.assertEqual(len(parse_chapters(payload)), 1)

    def test_zero_length_chapters_are_dropped(self):
        self.assertEqual(parse_chapters(_payload((10, 10, "Nothing"))), [])

    def test_backwards_chapters_are_dropped(self):
        self.assertEqual(parse_chapters(_payload((90, 10, "Wrong"))), [])

    def test_long_titles_are_capped(self):
        chapters = parse_chapters(_payload((0, 10, "x" * 500)))
        self.assertLessEqual(len(chapters[0].title), 120)


class SkippableTests(unittest.TestCase):
    def test_intro_is_offered(self):
        segments = skippable([Chapter(0, 90, "Intro")], 1400)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["label"], "Skip intro")
        self.assertEqual(segments[0]["end"], 90)

    def test_various_intro_names(self):
        for title in ("Intro", "opening", "OP", "Theme", "Titles", "Recap",
                      "Previously"):
            self.assertEqual(len(skippable([Chapter(0, 60, title)], 1400)), 1, title)

    def test_the_episode_body_is_not_skippable(self):
        self.assertEqual(skippable([Chapter(90, 1400, "Episode")], 1400), [])

    def test_an_intro_spanning_the_episode_is_refused(self):
        # A mislabelled chapter would otherwise skip the whole thing.
        self.assertEqual(skippable([Chapter(0, 1400, "Intro")], 1400), [])

    def test_very_short_chapters_are_ignored(self):
        self.assertEqual(skippable([Chapter(0, 2, "Intro")], 1400), [])

    def test_closing_credits_are_offered(self):
        segments = skippable([Chapter(1300, 1400, "Credits")], 1400)
        self.assertEqual(segments[0]["label"], "Skip credits")

    def test_an_early_ending_chapter_is_not_credits(self):
        # "ED" partway through an anime rip is the ending theme, not the end.
        self.assertEqual(skippable([Chapter(100, 190, "ED")], 1400), [])

    def test_credits_without_a_known_duration_are_still_offered(self):
        self.assertEqual(len(skippable([Chapter(1300, 1400, "Ending")], 0)), 1)

    def test_untitled_chapters_are_never_skipped(self):
        # Without a title there is nothing to say what is being skipped.
        self.assertEqual(skippable([Chapter(0, 90, "")], 1400), [])

    def test_several_segments(self):
        segments = skippable([
            Chapter(0, 30, "Recap"),
            Chapter(30, 120, "Opening"),
            Chapter(120, 1300, "Part A"),
            Chapter(1300, 1400, "Ending"),
        ], 1400)
        self.assertEqual([s["label"] for s in segments],
                         ["Skip intro", "Skip intro", "Skip credits"])

    def test_no_chapters_means_no_buttons(self):
        self.assertEqual(skippable([], 1400), [])


class ReadChaptersTests(unittest.TestCase):
    def test_without_ffprobe_there_is_nothing(self):
        self.assertEqual(read_chapters(None, Path("movie.mkv")), [])

    def test_uses_the_runner(self):
        captured = {}

        def runner(cmd):
            captured["cmd"] = cmd
            return _payload((0, 90, "Intro"))

        chapters = read_chapters("ffprobe", Path("movie.mkv"), runner=runner)
        self.assertEqual(len(chapters), 1)
        self.assertIn("-show_chapters", captured["cmd"])

    def test_a_failing_probe_is_not_an_error(self):
        def runner(cmd):
            raise OSError("ffprobe exploded")

        self.assertEqual(read_chapters("ffprobe", Path("movie.mkv"), runner=runner), [])


if __name__ == "__main__":
    unittest.main()
