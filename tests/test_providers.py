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
    parse_aniskip, parse_introdb, parse_omdb, parse_tmdb, parse_tvmaze, redact,
    strip_html,
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

# From TMDb's own documented example response.
TMDB_MOVIE = {"page": 1, "results": [{
    "id": 550,
    "title": "Fight Club",
    "original_title": "Fight Club",
    "overview": "A ticking-time-bomb insomniac and a slippery soap salesman.",
    "poster_path": "/pB8BM7pdSp6B6Ih7QZ4DrQ3PmJK.jpg",
    "backdrop_path": "/hZkgoQYus5vegHoetLkCJzb17zJ.jpg",
    "release_date": "1999-10-15",
    "vote_average": 8.433,
    "vote_count": 26279,
}]}

# OMDb's documented response shape.
OMDB = {"Title": "Fight Club", "Year": "1999", "imdbRating": "8.8",
        "imdbID": "tt0137523", "Response": "True"}

# Recorded live from api.theintrodb.org: Breaking Bad s2e1 (tt0903747).
INTRODB_TV = {
    "tmdb_id": 1396,
    "type": "tv",
    "season": 2,
    "episode": 1,
    "intro": [{"start_ms": 77000, "end_ms": 123369}],
    "credits": [{"start_ms": 2785000, "end_ms": None}],
}

# Recorded live: Fight Club (tt0137523). Note the null intro start.
INTRODB_MOVIE = {
    "tmdb_id": 550,
    "type": "movie",
    "intro": [{"start_ms": None, "end_ms": 119000}],
    "credits": [{"start_ms": 8177000, "end_ms": 8348000}],
}


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


class TmdbParsingTests(unittest.TestCase):
    def test_movie_fields(self):
        info = parse_tmdb(TMDB_MOVIE, "movie")
        self.assertEqual(info.source, "TMDb")
        self.assertEqual(info.title, "Fight Club")
        self.assertEqual(info.year, 1999)
        self.assertEqual(info.rating, 8.4)
        self.assertTrue(info.poster_url.startswith("https://image.tmdb.org/t/p/"))
        self.assertIn("insomniac", info.summary)

    def test_tv_uses_name_and_first_air_date(self):
        payload = {"results": [{"name": "Breaking Bad", "first_air_date": "2008-01-20",
                                "overview": "Chemistry.", "vote_average": 8.9,
                                "poster_path": "/x.jpg"}]}
        info = parse_tmdb(payload, "tv")
        self.assertEqual(info.title, "Breaking Bad")
        self.assertEqual(info.year, 2008)

    def test_no_results(self):
        self.assertIsNone(parse_tmdb({"results": []}, "movie"))
        self.assertIsNone(parse_tmdb({}, "movie"))

    def test_untitled_result_is_refused(self):
        self.assertIsNone(parse_tmdb({"results": [{"overview": "x"}]}, "movie"))

    def test_zero_votes_is_not_a_rating(self):
        payload = {"results": [{"title": "Unrated", "vote_average": 0}]}
        self.assertIsNone(parse_tmdb(payload, "movie").rating)

    def test_a_relative_poster_path_is_required(self):
        payload = {"results": [{"title": "X", "poster_path": "http://evil/x.jpg"}]}
        self.assertEqual(parse_tmdb(payload, "movie").poster_url, "")


class OmdbParsingTests(unittest.TestCase):
    def test_rating_and_id(self):
        rating, imdb_id = parse_omdb(OMDB)
        self.assertEqual(rating, 8.8)
        self.assertEqual(imdb_id, "tt0137523")

    def test_failed_response(self):
        self.assertEqual(parse_omdb({"Response": "False", "Error": "Movie not found!"}),
                         (None, ""))
        self.assertEqual(parse_omdb({}), (None, ""))

    def test_not_available_rating(self):
        rating, imdb_id = parse_omdb({"Response": "True", "imdbID": "tt1",
                                      "imdbRating": "N/A"})
        self.assertIsNone(rating)
        self.assertEqual(imdb_id, "tt1")

    def test_nonsense_rating_does_not_raise(self):
        self.assertEqual(parse_omdb({"Response": "True", "imdbRating": "great"})[0], None)

    def test_out_of_range_rating_is_refused(self):
        self.assertIsNone(parse_omdb({"Response": "True", "imdbRating": "88"})[0])


class SecretRedactionTests(unittest.TestCase):
    """Keys travel in the query string, and failures log the URL."""

    def test_api_key_is_hidden(self):
        self.assertNotIn("s3cret", redact(
            "https://api.themoviedb.org/3/search/movie?api_key=s3cret&query=x"))

    def test_omdb_key_is_hidden(self):
        self.assertNotIn("s3cret", redact("https://www.omdbapi.com/?apikey=s3cret&t=x"))

    def test_the_rest_of_the_url_survives(self):
        self.assertIn("query=fight", redact(
            "https://api.themoviedb.org/3/search/movie?api_key=s3cret&query=fight"))

    def test_urls_without_keys_are_untouched(self):
        url = "https://api.tvmaze.com/singlesearch/shows?q=x"
        self.assertEqual(redact(url), url)


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


class KeyedProviderTests(unittest.TestCase):
    """TMDb and OMDb need a key, so they must stay inert without one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_films_are_skipped_without_a_tmdb_key(self):
        providers = MetadataProviders(self.root, _FakeFetcher(raise_on_call=True))
        self.assertIsNone(providers.movie("Fight Club", 1999))

    def test_film_lookup_with_a_key(self):
        fetcher = _FakeFetcher({"search/movie": TMDB_MOVIE})
        providers = MetadataProviders(self.root, fetcher, tmdb_key="k")
        info = providers.movie("Fight Club", 1999)
        self.assertEqual(info.title, "Fight Club")
        self.assertEqual(info.source, "TMDb")

    def test_film_lookup_is_cached(self):
        fetcher = _FakeFetcher({"search/movie": TMDB_MOVIE})
        providers = MetadataProviders(self.root, fetcher, tmdb_key="k")
        providers.movie("Fight Club", 1999)
        providers.movie("Fight Club", 1999)
        self.assertEqual(len(fetcher.calls), 1)

    def test_cached_movie_never_calls_out(self):
        fetcher = _FakeFetcher({"search/movie": TMDB_MOVIE})
        MetadataProviders(self.root, fetcher, tmdb_key="k").movie("Fight Club", 1999)
        offline = MetadataProviders(self.root, _FakeFetcher(raise_on_call=True))
        self.assertEqual(offline.cached_movie("Fight Club", 1999).title, "Fight Club")

    def test_tmdb_backs_up_tvmaze_for_shows(self):
        # TVmaze misses; TMDb answers because a key is present.
        fetcher = _FakeFetcher({"search/tv": {"results": [
            {"name": "Obscure Show", "first_air_date": "2020-01-01",
             "vote_average": 7.0, "overview": "x"}]}})
        providers = MetadataProviders(self.root, fetcher, tmdb_key="k")
        info = providers.series("Obscure Show")
        self.assertEqual(info.source, "TMDb")

    def test_imdb_rating_is_added_when_omdb_has_a_key(self):
        fetcher = _FakeFetcher({"singlesearch": TVMAZE, "omdbapi": OMDB})
        providers = MetadataProviders(self.root, fetcher, omdb_key="k")
        info = providers.series("The Mentalist")
        self.assertEqual(info.imdb_rating, 8.8)
        self.assertEqual(info.rating, 8.2, "the source's own rating is kept too")

    def test_no_omdb_key_means_no_imdb_rating(self):
        fetcher = _FakeFetcher({"singlesearch": TVMAZE})
        providers = MetadataProviders(self.root, fetcher)
        self.assertIsNone(providers.series("The Mentalist").imdb_rating)

    def test_imdb_rating_survives_the_cache(self):
        fetcher = _FakeFetcher({"singlesearch": TVMAZE, "omdbapi": OMDB})
        MetadataProviders(self.root, fetcher, omdb_key="k").series("The Mentalist")
        offline = MetadataProviders(self.root, _FakeFetcher(raise_on_call=True))
        self.assertEqual(offline.cached_series("The Mentalist").imdb_rating, 8.8)

    def test_the_key_is_sent_but_not_in_the_cache_key(self):
        fetcher = _FakeFetcher({"search/movie": TMDB_MOVIE})
        providers = MetadataProviders(self.root, fetcher, tmdb_key="s3cret")
        providers.movie("Fight Club", 1999)
        self.assertIn("api_key=s3cret", fetcher.calls[0])
        for path in self.root.rglob("*.json"):
            self.assertNotIn("s3cret", path.read_text(encoding="utf-8"))


class IntroDbParsingTests(unittest.TestCase):
    """Fixtures recorded from live api.theintrodb.org responses."""

    def test_breaking_bad_episode(self):
        segments = parse_introdb(INTRODB_TV, duration=2820.0)
        self.assertEqual([s["kind"] for s in segments], ["intro", "credits"])
        intro = segments[0]
        self.assertAlmostEqual(intro["start"], 77.0)
        self.assertAlmostEqual(intro["end"], 123.369)
        self.assertEqual(intro["label"], "Skip intro")

    def test_null_end_means_the_end_of_the_file(self):
        credits = parse_introdb(INTRODB_TV, duration=2820.0)[1]
        self.assertAlmostEqual(credits["start"], 2785.0)
        self.assertAlmostEqual(credits["end"], 2820.0)

    def test_null_end_is_unusable_without_a_duration(self):
        # Nothing to resolve "to the end" against, so it must not be guessed.
        segments = parse_introdb(INTRODB_TV, duration=0.0)
        self.assertEqual([s["kind"] for s in segments], ["intro"])

    def test_null_start_means_the_beginning(self):
        segments = parse_introdb(INTRODB_MOVIE, duration=8400.0)
        self.assertAlmostEqual(segments[0]["start"], 0.0)
        self.assertAlmostEqual(segments[0]["end"], 119.0)

    def test_movie_credits_with_both_ends(self):
        credits = parse_introdb(INTRODB_MOVIE, duration=8400.0)[1]
        self.assertAlmostEqual(credits["start"], 8177.0)
        self.assertAlmostEqual(credits["end"], 8348.0)

    def test_absent_segment_types_are_simply_missing(self):
        payload = {"credits": [{"start_ms": 2666000, "end_ms": None}]}
        segments = parse_introdb(payload, duration=2700.0)
        self.assertEqual([s["kind"] for s in segments], ["credits"])

    def test_empty_and_junk_payloads(self):
        self.assertEqual(parse_introdb({}, 100.0), [])
        self.assertEqual(parse_introdb(None, 100.0), [])
        self.assertEqual(parse_introdb({"intro": "nonsense"}, 100.0), [])
        self.assertEqual(parse_introdb({"intro": [None, 7]}, 100.0), [])

    def test_segments_shorter_than_five_seconds_are_dropped(self):
        payload = {"intro": [{"start_ms": 1000, "end_ms": 4000}]}
        self.assertEqual(parse_introdb(payload, 600.0), [])

    def test_times_past_the_end_mean_a_different_cut(self):
        # Clamping would turn "skip the intro" into "skip the episode".
        payload = {"intro": [{"start_ms": 700000, "end_ms": 760000}]}
        self.assertEqual(parse_introdb(payload, 600.0), [])

    def test_an_end_just_past_the_file_is_tolerated(self):
        payload = {"intro": [{"start_ms": 500000, "end_ms": 602000}]}
        segment = parse_introdb(payload, 600.0)[0]
        self.assertAlmostEqual(segment["end"], 600.0)

    def test_reversed_times_are_dropped(self):
        payload = {"intro": [{"start_ms": 90000, "end_ms": 10000}]}
        self.assertEqual(parse_introdb(payload, 600.0), [])

    def test_unparsable_times_are_dropped(self):
        payload = {"intro": [{"start_ms": "soon", "end_ms": 90000}]}
        self.assertEqual(parse_introdb(payload, 600.0), [])

    def test_segments_come_back_in_playback_order(self):
        payload = {
            "credits": [{"start_ms": 500000, "end_ms": 560000}],
            "intro": [{"start_ms": 10000, "end_ms": 70000}],
            "recap": [{"start_ms": 80000, "end_ms": 140000}],
        }
        order = [s["kind"] for s in parse_introdb(payload, 600.0)]
        self.assertEqual(order, ["intro", "recap", "credits"])

    def test_every_segment_carries_a_kind_and_a_label(self):
        for segment in parse_introdb(INTRODB_MOVIE, 8400.0):
            self.assertIn(segment["kind"], ("intro", "recap", "credits", "preview"))
            self.assertTrue(segment["label"].startswith("Skip "))


class IntroDbLookupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_episode_lookup(self):
        fetcher = _FakeFetcher({"theintrodb": INTRODB_TV})
        providers = MetadataProviders(self.root, fetcher)
        segments = providers.intro_times("tt0903747", 2, 1, 2820.0)
        self.assertEqual(len(segments), 2)
        self.assertIn("imdb_id=tt0903747", fetcher.calls[0])
        self.assertIn("season=2", fetcher.calls[0])
        self.assertIn("episode=1", fetcher.calls[0])

    def test_duration_is_sent_to_identify_the_release(self):
        fetcher = _FakeFetcher({"theintrodb": INTRODB_TV})
        MetadataProviders(self.root, fetcher).intro_times("tt0903747", 2, 1, 2820.0)
        self.assertIn("duration_ms=2820000", fetcher.calls[0])

    def test_film_lookup_sends_no_episode(self):
        fetcher = _FakeFetcher({"theintrodb": INTRODB_MOVIE})
        providers = MetadataProviders(self.root, fetcher)
        self.assertEqual(len(providers.intro_times("tt0137523", None, None, 8400.0)), 2)
        self.assertNotIn("season=", fetcher.calls[0])

    def test_the_answer_is_cached(self):
        fetcher = _FakeFetcher({"theintrodb": INTRODB_TV})
        providers = MetadataProviders(self.root, fetcher)
        providers.intro_times("tt0903747", 2, 1, 2820.0)
        providers.intro_times("tt0903747", 2, 1, 2820.0)
        self.assertEqual(len(fetcher.calls), 1)

    def test_cached_answer_needs_no_network(self):
        MetadataProviders(self.root, _FakeFetcher({"theintrodb": INTRODB_TV})) \
            .intro_times("tt0903747", 2, 1, 2820.0)
        offline = MetadataProviders(self.root, _FakeFetcher(raise_on_call=True))
        self.assertEqual(len(offline.cached_intro_times("tt0903747", 2, 1, 2820.0)), 2)

    def test_a_miss_is_remembered_so_it_is_not_re_asked(self):
        fetcher = _FakeFetcher({"theintrodb": {}})
        providers = MetadataProviders(self.root, fetcher)
        self.assertEqual(providers.intro_times("tt0903747", 9, 9, 2820.0), [])
        self.assertTrue(providers.has_looked_up_intro("tt0903747", 9, 9))
        providers.intro_times("tt0903747", 9, 9, 2820.0)
        self.assertEqual(len(fetcher.calls), 1)

    def test_unasked_episodes_are_not_marked_as_looked_up(self):
        providers = MetadataProviders(self.root, _FakeFetcher(raise_on_call=True))
        self.assertFalse(providers.has_looked_up_intro("tt0903747", 1, 1))

    def test_episodes_are_cached_separately(self):
        fetcher = _FakeFetcher({"theintrodb": INTRODB_TV})
        providers = MetadataProviders(self.root, fetcher)
        providers.intro_times("tt0903747", 2, 1, 2820.0)
        providers.intro_times("tt0903747", 2, 2, 2820.0)
        self.assertEqual(len(fetcher.calls), 2)

    def test_a_bad_imdb_id_never_reaches_the_network(self):
        providers = MetadataProviders(self.root, _FakeFetcher(raise_on_call=True))
        for bad in ("", "550", "tt", "nope", "tt12", "'; DROP TABLE --"):
            self.assertEqual(providers.intro_times(bad, 1, 1, 100.0), [], bad)
            self.assertEqual(providers.cached_intro_times(bad, 1, 1), [], bad)

    def test_uppercase_imdb_ids_are_accepted(self):
        fetcher = _FakeFetcher({"theintrodb": INTRODB_MOVIE})
        providers = MetadataProviders(self.root, fetcher)
        self.assertTrue(providers.intro_times("TT0137523", None, None, 8400.0))


if __name__ == "__main__":
    unittest.main()
