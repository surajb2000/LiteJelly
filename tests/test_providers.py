"""Tests for the online metadata providers.

No test here touches the network: every response is a recorded shape captured
from the live services. What matters is that a slow, broken or absent service
degrades the library rather than breaking it.

Run with:  python -m unittest discover -s tests
"""

import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.providers import (
    MetadataCache, MetadataProviders, SeriesInfo, episode_key, parse_anilist,
    parse_aniskip, parse_tvmaze, strip_html,
)

logging.getLogger("litejelly.providers").setLevel(logging.CRITICAL)

# Trimmed from a real api.tvmaze.com response.
TVMAZE = {
    "name": "The Mentalist",
    "premiered": "2008-09-23",
    "rating": {"average": 8.2},
    "genres": ["Drama", "Crime", "Mystery"],
    "externals": {"thetvdb": 82459, "imdb": "tt1196946"},
    "image": {"medium": "https://static.tvmaze.com/m.jpg",
              "original": "https://static.tvmaze.com/o.jpg"},
    "summary": "<p>Patrick Jane, an <b>independent</b> consultant.</p>",
    "_embedded": {"episodes": [
        {"season": 1, "number": 1, "name": "Pilot", "airdate": "2008-09-23",
         "rating": {"average": 7.9}, "summary": "<p>First case.</p>",
         "image": {"original": "https://static.tvmaze.com/e1.jpg"}},
        {"season": 1, "number": 2, "name": "Red Hair and Silver Tape",
         "airdate": "2008-09-30", "rating": {"average": None},
         "summary": None, "image": None},
    ]},
}

# Trimmed from a real graphql.anilist.co response.
ANILIST = {"data": {"Media": {
    "idMal": 269,
    "title": {"romaji": "BLEACH", "english": "Bleach"},
    "averageScore": 79,
    "description": "Ichigo Kurosaki is a <i>rather normal</i> student.",
    "coverImage": {"large": "https://s4.anilist.co/cover.png"},
}}}

# Trimmed from a real api.aniskip.com response.
ANISKIP = {"found": True, "results": [
    {"interval": {"startTime": 28.783, "endTime": 118.783}, "skipType": "op"},
    {"interval": {"startTime": 1389.96, "endTime": 1461.0}, "skipType": "ed"},
], "statusCode": 200}


class StripHtmlTests(unittest.TestCase):
    def test_tags_are_removed(self):
        self.assertEqual(strip_html("<p>Hello <b>there</b></p>"), "Hello there")

    def test_entities_are_decoded(self):
        self.assertEqual(strip_html("Tom &amp; Jerry&#39;s"), "Tom & Jerry's")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(strip_html("<p>a</p>\n\n   <p>b</p>"), "a b")

    def test_empty_input(self):
        self.assertEqual(strip_html(""), "")
        self.assertEqual(strip_html(None), "")


class TvMazeParsingTests(unittest.TestCase):
    def setUp(self):
        self.info = parse_tvmaze(TVMAZE)

    def test_show_fields(self):
        self.assertEqual(self.info.source, "TVmaze")
        self.assertEqual(self.info.title, "The Mentalist")
        self.assertEqual(self.info.rating, 8.2)
        self.assertEqual(self.info.year, 2008)
        self.assertEqual(self.info.imdb_id, "tt1196946")
        self.assertEqual(self.info.genres, ["Drama", "Crime", "Mystery"])

    def test_summary_is_plain_text(self):
        self.assertNotIn("<", self.info.summary)
        self.assertIn("independent", self.info.summary)

    def test_prefers_the_original_image(self):
        self.assertTrue(self.info.poster_url.endswith("o.jpg"))

    def test_episodes_are_keyed_by_number(self):
        self.assertIn("s1e1", self.info.episodes)
        self.assertEqual(self.info.episodes["s1e1"]["title"], "Pilot")
        self.assertEqual(self.info.episodes["s1e1"]["rating"], 7.9)

    def test_missing_episode_fields_do_not_raise(self):
        second = self.info.episodes["s1e2"]
        self.assertIsNone(second["rating"])
        self.assertEqual(second["summary"], "")
        self.assertEqual(second["image"], "")

    def test_round_trips_through_the_cache_format(self):
        restored = SeriesInfo.from_dict(self.info.to_dict())
        self.assertEqual(restored.title, self.info.title)
        self.assertEqual(restored.episodes, self.info.episodes)

    def test_garbage_does_not_raise(self):
        self.assertEqual(parse_tvmaze({}).title, "")


class AniListParsingTests(unittest.TestCase):
    def test_fields(self):
        info = parse_anilist(ANILIST)
        self.assertEqual(info.source, "AniList")
        self.assertEqual(info.title, "Bleach")
        self.assertEqual(info.mal_id, 269)
        self.assertNotIn("<", info.summary)

    def test_score_is_converted_to_ten_point(self):
        # AniList scores out of 100; everything else here is out of 10.
        self.assertEqual(parse_anilist(ANILIST).rating, 7.9)

    def test_empty_response(self):
        self.assertEqual(parse_anilist({"data": {"Media": None}}).title, "")
        self.assertEqual(parse_anilist({}).title, "")


class AniSkipParsingTests(unittest.TestCase):
    def test_opening_and_ending(self):
        segments = parse_aniskip(ANISKIP, 1500)
        self.assertEqual([s["label"] for s in segments],
                         ["Skip intro", "Skip credits"])
        self.assertAlmostEqual(segments[0]["start"], 28.783)

    def test_not_found(self):
        self.assertEqual(parse_aniskip({"found": False, "results": []}), [])
        self.assertEqual(parse_aniskip({}), [])

    def test_segments_past_the_end_are_refused(self):
        # Another release can be cut differently. Clamping would turn "skip the
        # opening" into "skip to the end of the file".
        self.assertEqual(parse_aniskip(ANISKIP, 100), [])

    def test_a_small_overrun_is_tolerated(self):
        payload = {"found": True, "results": [
            {"interval": {"startTime": 1400, "endTime": 1502}, "skipType": "ed"}]}
        self.assertEqual(len(parse_aniskip(payload, 1500)), 1)

    def test_unknown_duration_keeps_the_segments(self):
        self.assertEqual(len(parse_aniskip(ANISKIP, 0)), 2)

    def test_unknown_skip_types_are_ignored(self):
        payload = {"found": True, "results": [
            {"interval": {"startTime": 0, "endTime": 60}, "skipType": "mixed-op-ed"}]}
        self.assertEqual(parse_aniskip(payload, 1500), [])

    def test_tiny_segments_are_ignored(self):
        payload = {"found": True, "results": [
            {"interval": {"startTime": 10, "endTime": 12}, "skipType": "op"}]}
        self.assertEqual(parse_aniskip(payload, 1500), [])

    def test_malformed_intervals_do_not_raise(self):
        payload = {"found": True, "results": [
            {"interval": {"startTime": "x", "endTime": 60}, "skipType": "op"},
            {"skipType": "op"},
            "nonsense",
        ]}
        self.assertEqual(parse_aniskip(payload, 1500), [])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = MetadataCache(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip(self):
        self.cache.put("tvmaze", "The Mentalist", {"title": "x"})
        self.assertEqual(self.cache.get("tvmaze", "The Mentalist"), {"title": "x"})

    def test_missing_entry(self):
        self.assertIsNone(self.cache.get("tvmaze", "Nothing"))

    def test_keys_are_case_insensitive(self):
        self.cache.put("tvmaze", "The Mentalist", {"title": "x"})
        self.assertIsNotNone(self.cache.get("tvmaze", "the mentalist"))

    def test_a_miss_is_remembered(self):
        # Otherwise every scan re-asks for a show that does not exist.
        self.cache.put("tvmaze", "Unknown Show", None, miss=True)
        self.assertEqual(self.cache.get("tvmaze", "Unknown Show"), {})

    def test_namespaces_do_not_collide(self):
        self.cache.put("tvmaze", "Bleach", {"source": "tv"})
        self.cache.put("anilist", "Bleach", {"source": "anilist"})
        self.assertEqual(self.cache.get("tvmaze", "Bleach")["source"], "tv")
        self.assertEqual(self.cache.get("anilist", "Bleach")["source"], "anilist")

    def test_corrupt_entry_is_ignored(self):
        self.cache.put("tvmaze", "X", {"a": 1})
        for path in Path(self._tmp.name).rglob("*.json"):
            path.write_text("{ broken", encoding="utf-8")
        self.assertIsNone(self.cache.get("tvmaze", "X"))

    def test_stale_entry_is_ignored(self):
        path = Path(self._tmp.name) / "tvmaze"
        path.mkdir(parents=True, exist_ok=True)
        self.cache.put("tvmaze", "X", {"a": 1})
        for entry in path.glob("*.json"):
            payload = json.loads(entry.read_text(encoding="utf-8"))
            payload["fetched_at"] = time.time() - 400 * 24 * 3600
            entry.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(self.cache.get("tvmaze", "X"))

    def test_clear_removes_everything(self):
        self.cache.put("tvmaze", "X", {"a": 1})
        self.assertGreaterEqual(self.cache.clear(), 1)
        self.assertIsNone(self.cache.get("tvmaze", "X"))


class _FakeFetcher:
    """Stands in for the network, and records what would have been called."""

    def __init__(self, responses=None, raise_on_call=False):
        self.responses = responses or {}
        self.calls = []
        self.raise_on_call = raise_on_call

    def _match(self, url):
        for fragment, payload in self.responses.items():
            if fragment in url:
                return payload
        return None

    def fetch_json(self, url, data=None, content_type=""):
        if self.raise_on_call:
            raise AssertionError("the network must not be used here")
        self.calls.append(url)
        return self._match(url)

    def fetch(self, url, data=None, content_type=""):
        if self.raise_on_call:
            raise AssertionError("the network must not be used here")
        self.calls.append(url)
        payload = self._match(url)
        return b"image-bytes" if payload is None else json.dumps(payload).encode()


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _providers(self, responses=None, **kwargs):
        return MetadataProviders(self.root, _FakeFetcher(responses, **kwargs))

    def test_series_lookup_and_cache(self):
        providers = self._providers({"singlesearch": TVMAZE})
        info = providers.series("The Mentalist")
        self.assertEqual(info.title, "The Mentalist")
        self.assertEqual(len(providers.fetcher.calls), 1)

        # Second call must be served from disk.
        again = providers.series("The Mentalist")
        self.assertEqual(again.title, "The Mentalist")
        self.assertEqual(len(providers.fetcher.calls), 1)

    def test_anime_uses_anilist(self):
        providers = self._providers({"graphql": ANILIST})
        info = providers.series("Bleach", anime=True)
        self.assertEqual(info.mal_id, 269)
        self.assertIn("graphql", providers.fetcher.calls[0])

    def test_a_miss_is_not_retried(self):
        providers = self._providers({})
        self.assertIsNone(providers.series("Nothing At All"))
        self.assertIsNone(providers.series("Nothing At All"))
        self.assertEqual(len(providers.fetcher.calls), 1)

    def test_cached_series_never_calls_out(self):
        # Scanning uses this, so it must be incapable of blocking.
        providers = self._providers({"singlesearch": TVMAZE})
        providers.series("The Mentalist")
        offline = MetadataProviders(self.root, _FakeFetcher(raise_on_call=True))
        self.assertEqual(offline.cached_series("The Mentalist").title, "The Mentalist")

    def test_cached_series_is_none_when_unknown(self):
        offline = MetadataProviders(self.root, _FakeFetcher(raise_on_call=True))
        self.assertIsNone(offline.cached_series("Never Heard Of It"))

    def test_has_looked_up_reports_a_remembered_miss(self):
        providers = self._providers({})
        self.assertFalse(providers.has_looked_up("Nothing"))
        providers.series("Nothing")
        self.assertTrue(providers.has_looked_up("Nothing"))

    def test_skip_times_are_cached(self):
        providers = self._providers({"skip-times": ANISKIP})
        first = providers.skip_times(269, 1, 1500)
        self.assertEqual(len(first), 2)
        providers.skip_times(269, 1, 1500)
        self.assertEqual(len(providers.fetcher.calls), 1)

    def test_cached_skip_times_never_calls_out(self):
        providers = self._providers({"skip-times": ANISKIP})
        providers.skip_times(269, 1, 1500)
        offline = MetadataProviders(self.root, _FakeFetcher(raise_on_call=True))
        self.assertEqual(len(offline.cached_skip_times(269, 1, 1500)), 2)

    def test_cached_skip_times_empty_when_unknown(self):
        offline = MetadataProviders(self.root, _FakeFetcher(raise_on_call=True))
        self.assertEqual(offline.cached_skip_times(1, 1), [])

    def test_empty_titles_are_not_looked_up(self):
        providers = self._providers({}, raise_on_call=True)
        self.assertIsNone(providers.series("   "))

    def test_artwork_is_downloaded_once(self):
        providers = self._providers({})
        path = providers.artwork("https://static.tvmaze.com/o.jpg")
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())
        calls = len(providers.fetcher.calls)
        providers.artwork("https://static.tvmaze.com/o.jpg")
        self.assertEqual(len(providers.fetcher.calls), calls)

    def test_artwork_must_be_https(self):
        providers = self._providers({}, raise_on_call=True)
        self.assertIsNone(providers.artwork("http://example.com/x.jpg"))
        self.assertIsNone(providers.artwork("file:///etc/passwd"))
        self.assertIsNone(providers.artwork(""))

    def test_artwork_path_does_not_download(self):
        offline = MetadataProviders(self.root, _FakeFetcher(raise_on_call=True))
        self.assertIsNone(offline.artwork_path("https://static.tvmaze.com/o.jpg"))

    def test_episode_key(self):
        self.assertEqual(episode_key(1, 2), "s1e2")
        self.assertEqual(episode_key(None, None), "s0e0")


if __name__ == "__main__":
    unittest.main()
