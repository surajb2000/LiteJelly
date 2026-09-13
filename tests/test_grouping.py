"""Tests for library grouping: categories, series identity and episode order.

Run with:  python -m unittest discover -s tests
"""

import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.config import MediaDir
from litejelly.library import (
    Library, display_name, parse_title, resolve_category, series_key,
)

logging.getLogger("litejelly.library").setLevel(logging.CRITICAL)


class EpisodeTitleTests(unittest.TestCase):
    def test_text_after_the_marker_becomes_the_episode_title(self):
        parsed = parse_title("The.Mentalist.S01E22.Red.Johns.Footsteps.1080p.mkv")
        self.assertEqual(parsed["title"], "The Mentalist")
        self.assertEqual(parsed["episode_title"], "Red Johns Footsteps")

    def test_missing_episode_title_is_empty_not_none(self):
        parsed = parse_title("The.Mentalist.S01E22.1080p.BluRay.x265.mkv")
        self.assertEqual(parsed["episode_title"], "")

    def test_release_noise_is_stripped_from_the_episode_title(self):
        parsed = parse_title("Show.S01E02.The.Reveal.720p.WEB-DL.x264-GROUP.mkv")
        self.assertEqual(parsed["episode_title"], "The Reveal")

    def test_movies_have_no_episode_title(self):
        self.assertEqual(parse_title("Inception.2010.1080p.mkv")["episode_title"], "")

    def test_series_title_excludes_the_episode_title(self):
        # Regression: the series title must not absorb the episode name, or
        # every episode becomes its own series.
        a = parse_title("The.Mentalist.S01E01.Pilot.mkv")
        b = parse_title("The.Mentalist.S01E02.Red.Hair.mkv")
        self.assertEqual(a["title"], b["title"])


class DisplayLabelTests(unittest.TestCase):
    def test_label_includes_the_episode_title_when_known(self):
        label = display_name(parse_title("The.Mentalist.S01E22.Red.Johns.Footsteps.mkv"))
        self.assertEqual(label, "The Mentalist · S01E22 · Red Johns Footsteps")

    def test_label_falls_back_to_the_code(self):
        label = display_name(parse_title("The.Mentalist.S01E22.1080p.x265.mkv"))
        self.assertEqual(label, "The Mentalist · S01E22")


class CategoryTests(unittest.TestCase):
    def test_tagged_folder_wins_over_the_filename(self):
        self.assertEqual(resolve_category("anime", True), "anime")
        self.assertEqual(resolve_category("anime", False), "anime")
        self.assertEqual(resolve_category("movies", True), "movies")

    def test_mixed_folder_is_judged_by_the_filename(self):
        self.assertEqual(resolve_category("mixed", True), "shows")
        self.assertEqual(resolve_category("mixed", False), "movies")

    def test_unknown_content_type_behaves_like_mixed(self):
        self.assertEqual(resolve_category("", True), "shows")
        self.assertEqual(resolve_category("", False), "movies")


class SeriesKeyTests(unittest.TestCase):
    def test_same_series_gives_the_same_key(self):
        self.assertEqual(series_key("shows", "The Mentalist"),
                         series_key("shows", "The Mentalist"))

    def test_punctuation_and_case_do_not_split_a_series(self):
        self.assertEqual(series_key("shows", "Marvel's Agents of S.H.I.E.L.D."),
                         series_key("shows", "Marvels Agents of SHIELD"))

    def test_different_series_differ(self):
        self.assertNotEqual(series_key("shows", "The Mentalist"),
                            series_key("shows", "The Office"))

    def test_same_title_in_different_categories_stays_apart(self):
        self.assertNotEqual(series_key("shows", "Bleach"), series_key("anime", "Bleach"))

    def test_key_is_short_and_stable(self):
        self.assertEqual(len(series_key("shows", "Anything")), 16)


class ScanGroupingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.shows = self.root / "shows"
        self.movies = self.root / "movies"
        self.anime = self.root / "anime"
        for folder in (self.shows, self.movies, self.anime):
            folder.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, folder: Path, *names: str):
        for name in names:
            (folder / name).write_bytes(b"x" * 64)

    def _scan(self, *dirs):
        library = Library(list(dirs))
        return library.scan()

    def test_episodes_share_one_series_id(self):
        self._write(self.shows,
                    "The.Mentalist.S01E01.1080p.mkv",
                    "The.Mentalist.S01E02.1080p.mkv",
                    "The.Mentalist.S02E01.1080p.mkv")
        videos = self._scan(MediaDir(str(self.shows), "shows"))
        ids = {v.series_id for v in videos}
        self.assertEqual(len(ids), 1)
        self.assertNotIn("", ids)

    def test_movies_have_no_series_id(self):
        self._write(self.movies, "Inception.2010.1080p.mkv", "Arrival.2016.1080p.mkv")
        videos = self._scan(MediaDir(str(self.movies), "movies"))
        self.assertEqual({v.series_id for v in videos}, {""})
        self.assertEqual({v.category for v in videos}, {"movies"})

    def test_anime_folder_tags_every_episode(self):
        self._write(self.anime, "Bleach.S01E01.mkv", "Bleach.S01E02.mkv")
        videos = self._scan(MediaDir(str(self.anime), "anime"))
        self.assertEqual({v.category for v in videos}, {"anime"})

    def test_mixed_folder_splits_by_filename(self):
        self._write(self.root / "shows",
                    "Some.Show.S01E01.mkv", "Inception.2010.mkv")
        videos = self._scan(MediaDir(str(self.shows), "mixed"))
        by_name = {v.filename: v.category for v in videos}
        self.assertEqual(by_name["Some.Show.S01E01.mkv"], "shows")
        self.assertEqual(by_name["Inception.2010.mkv"], "movies")

    def test_same_show_in_two_folders_groups_together(self):
        self._write(self.shows, "The.Mentalist.S01E01.mkv")
        self._write(self.anime, "The.Mentalist.S01E02.mkv")
        videos = self._scan(MediaDir(str(self.shows), "shows"),
                            MediaDir(str(self.anime), "shows"))
        self.assertEqual(len({v.series_id for v in videos}), 1)

    def test_episode_order_is_by_season_then_episode(self):
        self._write(self.shows,
                    "Show.S02E01.mkv", "Show.S01E10.mkv",
                    "Show.S01E09.mkv", "Show.S01E02.mkv")
        videos = self._scan(MediaDir(str(self.shows), "shows"))
        ordered = sorted(videos, key=lambda v: (v.season, v.episode))
        self.assertEqual([(v.season, v.episode) for v in ordered],
                         [(1, 2), (1, 9), (1, 10), (2, 1)])

    def test_structured_fields_are_separate_from_the_display_label(self):
        # Grouping must not depend on parsing a composed string back apart.
        self._write(self.shows, "The.Mentalist.S01E22.Red.Johns.Footsteps.mkv")
        video = self._scan(MediaDir(str(self.shows), "shows"))[0]
        self.assertEqual(video.title, "The Mentalist")
        self.assertEqual(video.season, 1)
        self.assertEqual(video.episode, 22)
        self.assertEqual(video.episode_title, "Red Johns Footsteps")
        self.assertIn("S01E22", video.name)

    def test_every_video_carries_a_category(self):
        self._write(self.shows, "Show.S01E01.mkv")
        self._write(self.movies, "Film.2020.mkv")
        videos = self._scan(MediaDir(str(self.shows), "shows"),
                            MediaDir(str(self.movies), "movies"))
        self.assertTrue(all(v.category in ("shows", "movies", "anime") for v in videos))

    def test_to_dict_exposes_the_grouping_fields(self):
        self._write(self.shows, "Show.S01E01.mkv")
        payload = self._scan(MediaDir(str(self.shows), "shows"))[0].to_dict()
        for key in ("title", "category", "series_id", "episode_title", "season", "episode"):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
