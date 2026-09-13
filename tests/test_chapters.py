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

from litejelly import web
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


class _FakeMetadata:
    """Only the cache-facing half of MetadataProviders that skip lookups use."""

    def __init__(self, aniskip=None, introdb=None, asked=()):
        self.aniskip = aniskip or {}
        self.introdb = introdb or {}
        self.asked = set(asked)

    def cached_skip_times(self, mal_id, episode, duration=0.0):
        return list(self.aniskip.get((mal_id, episode), []))

    def has_looked_up_skip(self, mal_id, episode):
        return ("aniskip", mal_id, episode) in self.asked

    def cached_intro_times(self, imdb_id, season=None, episode=None, duration=0.0):
        return list(self.introdb.get((imdb_id, season, episode), []))

    def has_looked_up_intro(self, imdb_id, season=None, episode=None):
        return ("introdb", imdb_id, season, episode) in self.asked


class _FakeEnricher:
    def __init__(self):
        self.skips = []
        self.intros = []

    def enqueue_skip(self, mal_id, episode):
        self.skips.append((mal_id, episode))

    def enqueue_intro(self, imdb_id, season, episode, duration=0.0):
        self.intros.append((imdb_id, season, episode))


class _FakeVideo:
    def __init__(self, imdb_id="", mal_id=0, season=None, episode=None):
        self.meta = {"imdb_id": imdb_id} if imdb_id else {}
        self.mal_id = mal_id
        self.season = season
        self.episode = episode


class _FakeApp:
    def __init__(self, metadata, enricher):
        self.metadata = metadata
        self.enricher = enricher


class SkipSourceTests(unittest.TestCase):
    """Which provider answers, and whether the client is told to ask again."""

    def setUp(self):
        self.enricher = _FakeEnricher()

    def _cached(self, metadata, video, duration=1800.0):
        return web._cached_skip(_FakeApp(metadata, self.enricher), video, duration)

    def test_aniskip_answers_for_anime(self):
        segment = {"start": 10.0, "end": 90.0, "label": "Skip intro", "kind": "intro"}
        meta = _FakeMetadata(aniskip={(123, 4): [segment]})
        found, pending = self._cached(meta, _FakeVideo(mal_id=123, episode=4))
        self.assertEqual(found, [segment])
        self.assertFalse(pending)

    def test_introdb_answers_for_live_action(self):
        segment = {"start": 77.0, "end": 123.0, "label": "Skip intro", "kind": "intro"}
        meta = _FakeMetadata(introdb={("tt09", 2, 1): [segment]})
        video = _FakeVideo(imdb_id="tt09", season=2, episode=1)
        found, pending = self._cached(meta, video)
        self.assertEqual(found, [segment])
        self.assertFalse(pending)

    def test_introdb_backs_up_aniskip(self):
        segment = {"start": 5.0, "end": 95.0, "label": "Skip intro", "kind": "intro"}
        meta = _FakeMetadata(introdb={("tt09", 1, 3): [segment]},
                             asked=[("aniskip", 123, 3)])
        video = _FakeVideo(imdb_id="tt09", mal_id=123, season=1, episode=3)
        found, pending = self._cached(meta, video)
        self.assertEqual(found, [segment], "AniSkip had nothing, so ask TheIntroDB")
        self.assertFalse(pending)

    def test_a_film_is_looked_up_without_an_episode(self):
        meta = _FakeMetadata()
        _, pending = self._cached(meta, _FakeVideo(imdb_id="tt0137523"))
        self.assertTrue(pending)

    def test_nothing_known_yet_is_reported_as_pending(self):
        video = _FakeVideo(imdb_id="tt09", season=1, episode=1)
        _, pending = self._cached(_FakeMetadata(), video)
        self.assertTrue(pending, "the client must ask again, not give up")

    def test_a_finished_lookup_with_no_segments_is_not_pending(self):
        # Otherwise the client polls forever for an episode that has no intro.
        meta = _FakeMetadata(asked=[("introdb", "tt09", 1, 1)])
        video = _FakeVideo(imdb_id="tt09", season=1, episode=1)
        found, pending = self._cached(meta, video)
        self.assertEqual(found, [])
        self.assertFalse(pending)

    def test_a_video_with_no_ids_is_never_pending(self):
        _, pending = self._cached(_FakeMetadata(), _FakeVideo())
        self.assertFalse(pending)

    def test_enqueue_asks_both_sources_it_can(self):
        video = _FakeVideo(imdb_id="tt09", mal_id=123, season=2, episode=5)
        web._enqueue_skip(_FakeApp(_FakeMetadata(), self.enricher), video, 1800.0)
        self.assertEqual(self.enricher.skips, [(123, 5)])
        self.assertEqual(self.enricher.intros, [("tt09", 2, 5)])

    def test_enqueue_skips_what_it_cannot_identify(self):
        web._enqueue_skip(_FakeApp(_FakeMetadata(), self.enricher), _FakeVideo(),
                          1800.0)
        self.assertEqual(self.enricher.skips, [])
        self.assertEqual(self.enricher.intros, [])

    def test_a_seasonless_episode_is_assumed_to_be_season_one(self):
        video = _FakeVideo(imdb_id="tt09", episode=7)
        self.assertEqual(web._skip_episode(video), (1, 7))

    def test_a_film_has_no_season_or_episode(self):
        self.assertEqual(web._skip_episode(_FakeVideo(imdb_id="tt09")), (None, None))

    def test_payload_shape(self):
        self.assertEqual(web._skip_payload([], True),
                         {"skip_segments": [], "skip_pending": True})


if __name__ == "__main__":
    unittest.main()
