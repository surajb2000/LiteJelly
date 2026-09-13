"""Tests for local metadata: .nfo parsing and artwork discovery.

Local files are the foundation here, so these cover the shapes real libraries
produce, including the malformed ones. A bad .nfo must never break a scan.

Run with:  python -m unittest discover -s tests
"""

import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.config import MediaDir
from litejelly.library import Library
from litejelly.metadata import ArtworkIndex, Metadata, parse_nfo, read_nfo

logging.getLogger("litejelly.metadata").setLevel(logging.CRITICAL)
logging.getLogger("litejelly.library").setLevel(logging.CRITICAL)

MOVIE_NFO = """<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <title>Arrival</title>
  <plot>A linguist is recruited to communicate with alien visitors.</plot>
  <year>2016</year>
  <rating>7.9</rating>
  <runtime>116</runtime>
  <studio>Paramount</studio>
  <genre>Science Fiction</genre>
  <genre>Drama</genre>
</movie>
"""

EPISODE_NFO = """<?xml version="1.0"?>
<episodedetails>
  <title>Red Hair and Silver Tape</title>
  <showtitle>The Mentalist</showtitle>
  <season>1</season>
  <episode>2</episode>
  <plot>Jane investigates a death at a lake.</plot>
  <aired>2008-09-30</aired>
</episodedetails>
"""


class NfoParsingTests(unittest.TestCase):
    def test_reads_a_movie(self):
        meta = parse_nfo(MOVIE_NFO)
        self.assertEqual(meta.title, "Arrival")
        self.assertEqual(meta.year, 2016)
        self.assertEqual(meta.rating, 7.9)
        self.assertEqual(meta.runtime_minutes, 116)
        self.assertEqual(meta.studio, "Paramount")
        self.assertEqual(meta.genres, ["Science Fiction", "Drama"])
        self.assertIn("linguist", meta.plot)

    def test_reads_an_episode(self):
        meta = parse_nfo(EPISODE_NFO)
        self.assertEqual(meta.title, "Red Hair and Silver Tape")
        self.assertEqual(meta.season, 1)
        self.assertEqual(meta.episode, 2)
        self.assertEqual(meta.aired, "2008-09-30")

    def test_year_falls_back_to_the_aired_date(self):
        self.assertEqual(parse_nfo(EPISODE_NFO).year, 2008)

    def test_unknown_root_is_not_metadata(self):
        self.assertIsNone(parse_nfo("<something><title>No</title></something>"))

    def test_malformed_xml_is_not_an_error(self):
        # Plenty of .nfo files are just a scraper URL.
        self.assertIsNone(parse_nfo("https://www.imdb.com/title/tt2543164/"))
        self.assertIsNone(parse_nfo("<movie><title>Broken</movie>"))

    def test_empty_input(self):
        self.assertIsNone(parse_nfo(""))
        self.assertIsNone(parse_nfo("   "))
        self.assertIsNone(parse_nfo(None))

    def test_a_file_with_nothing_useful_is_ignored(self):
        self.assertIsNone(parse_nfo("<movie></movie>"))

    def test_absurd_values_are_rejected(self):
        meta = parse_nfo("<movie><title>X</title><year>99999</year>"
                         "<rating>431</rating><runtime>999999</runtime></movie>")
        self.assertIsNone(meta.year)
        self.assertIsNone(meta.rating)
        self.assertIsNone(meta.runtime_minutes)

    def test_non_numeric_values_do_not_raise(self):
        meta = parse_nfo("<movie><title>X</title><year>unknown</year>"
                         "<rating>n/a</rating><season>?</season></movie>")
        self.assertEqual(meta.title, "X")
        self.assertIsNone(meta.year)

    def test_rating_is_rounded(self):
        self.assertEqual(parse_nfo("<movie><rating>7.86666</rating></movie>").rating, 7.9)

    def test_duration_in_seconds_is_converted(self):
        meta = parse_nfo("<movie><title>X</title><durationinseconds>3600</durationinseconds></movie>")
        self.assertEqual(meta.runtime_minutes, 60)

    def test_plot_is_capped(self):
        meta = parse_nfo("<movie><plot>" + ("x" * 9000) + "</plot></movie>")
        self.assertLessEqual(len(meta.plot), 2000)

    def test_genres_are_capped(self):
        body = "".join(f"<genre>G{i}</genre>" for i in range(30))
        self.assertLessEqual(len(parse_nfo(f"<movie><title>X</title>{body}</movie>").genres), 8)

    def test_to_dict_is_serialisable(self):
        data = parse_nfo(MOVIE_NFO).to_dict()
        self.assertEqual(data["title"], "Arrival")
        self.assertIsInstance(data["genres"], list)


class ReadNfoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.video = self.root / "Arrival.2016.mkv"
        self.video.write_bytes(b"x" * 64)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_nfo_is_none(self):
        self.assertIsNone(read_nfo(self.video))

    def test_reads_the_sidecar(self):
        self.video.with_suffix(".nfo").write_text(MOVIE_NFO, encoding="utf-8")
        self.assertEqual(read_nfo(self.video).title, "Arrival")

    def test_byte_order_mark_is_tolerated(self):
        self.video.with_suffix(".nfo").write_text(MOVIE_NFO, encoding="utf-8-sig")
        self.assertEqual(read_nfo(self.video).title, "Arrival")

    def test_oversized_nfo_is_skipped(self):
        self.video.with_suffix(".nfo").write_text("<movie>" + "x" * 600_000 + "</movie>",
                                                  encoding="utf-8")
        self.assertIsNone(read_nfo(self.video))

    def test_undecodable_bytes_do_not_raise(self):
        self.video.with_suffix(".nfo").write_bytes(b"\xff\xfe\x00broken")
        self.assertIsNone(read_nfo(self.video))


class ArtworkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.show = self.root / "The Show"
        self.season = self.show / "Season 01"
        self.season.mkdir(parents=True)
        self.video = self.season / "The.Show.S01E01.mkv"
        self.video.write_bytes(b"x" * 64)
        self.index = ArtworkIndex()

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_artwork(self):
        self.assertIsNone(self.index.find(self.video))

    def test_folder_poster(self):
        target = self.season / "poster.jpg"
        target.write_bytes(b"img")
        self.assertEqual(self.index.find(self.video), target)

    def test_per_episode_thumb_wins_over_the_folder(self):
        (self.season / "poster.jpg").write_bytes(b"img")
        own = self.season / "The.Show.S01E01-thumb.jpg"
        own.write_bytes(b"img")
        self.assertEqual(ArtworkIndex().find(self.video), own)

    def test_falls_back_to_the_show_folder(self):
        target = self.show / "poster.jpg"
        target.write_bytes(b"img")
        self.assertEqual(self.index.find(self.video), target)

    def test_folder_jpg_is_accepted(self):
        target = self.season / "folder.png"
        target.write_bytes(b"img")
        self.assertEqual(self.index.find(self.video), target)

    def test_backdrop_is_separate_from_poster(self):
        (self.season / "poster.jpg").write_bytes(b"img")
        fanart = self.season / "fanart.jpg"
        fanart.write_bytes(b"img")
        index = ArtworkIndex()
        self.assertEqual(index.find(self.video, "backdrop"), fanart)
        self.assertEqual(index.find(self.video, "poster").name, "poster.jpg")

    def test_case_is_ignored(self):
        target = self.season / "POSTER.JPG"
        target.write_bytes(b"img")
        self.assertEqual(self.index.find(self.video), target)

    def test_unreadable_folder_does_not_raise(self):
        self.assertIsNone(ArtworkIndex().find(self.root / "gone" / "x.mkv"))

    def test_listing_is_cached_per_folder(self):
        (self.season / "poster.jpg").write_bytes(b"img")
        index = ArtworkIndex()
        index.find(self.video)
        # Added after the first lookup: the cache should still be in use.
        (self.season / "The.Show.S01E01-thumb.jpg").write_bytes(b"img")
        self.assertEqual(index.find(self.video).name, "poster.jpg")


class ScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.shows = self.root / "shows"
        self.shows.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _scan(self):
        return Library([MediaDir(str(self.shows), "shows")]).scan()

    def test_nfo_supplies_the_episode_title(self):
        video = self.shows / "The.Mentalist.S01E02.mkv"
        video.write_bytes(b"x" * 64)
        video.with_suffix(".nfo").write_text(EPISODE_NFO, encoding="utf-8")
        scanned = self._scan()[0]
        self.assertEqual(scanned.episode_title, "Red Hair and Silver Tape")
        self.assertEqual(scanned.season, 1)
        self.assertEqual(scanned.episode, 2)

    def test_nfo_does_not_erase_what_the_filename_got_right(self):
        # A plot-only .nfo must leave the episode numbering alone.
        video = self.shows / "The.Mentalist.S03E07.mkv"
        video.write_bytes(b"x" * 64)
        video.with_suffix(".nfo").write_text(
            "<episodedetails><plot>Something happens.</plot></episodedetails>",
            encoding="utf-8")
        scanned = self._scan()[0]
        self.assertEqual(scanned.season, 3)
        self.assertEqual(scanned.episode, 7)

    def test_plot_is_not_in_the_listing_payload(self):
        # Thousands of characters per item would dwarf the whole payload.
        video = self.shows / "Show.S01E01.mkv"
        video.write_bytes(b"x" * 64)
        video.with_suffix(".nfo").write_text(EPISODE_NFO, encoding="utf-8")
        payload = self._scan()[0].to_dict()
        self.assertNotIn("plot", payload)
        self.assertNotIn("meta", payload)
        self.assertNotIn("poster_path", payload)

    def test_poster_presence_is_reported(self):
        video = self.shows / "Show.S01E01.mkv"
        video.write_bytes(b"x" * 64)
        scanned = self._scan()[0]
        self.assertFalse(scanned.has_poster)

        (self.shows / "poster.jpg").write_bytes(b"img")
        scanned = self._scan()[0]
        self.assertTrue(scanned.has_poster)

    def test_a_broken_nfo_does_not_break_the_scan(self):
        for index in range(3):
            video = self.shows / f"Show.S01E0{index + 1}.mkv"
            video.write_bytes(b"x" * 64)
            video.with_suffix(".nfo").write_text("<<<not xml", encoding="utf-8")
        self.assertEqual(len(self._scan()), 3)

    def test_rating_reaches_the_listing(self):
        video = self.shows / "Arrival.2016.mkv"
        video.write_bytes(b"x" * 64)
        video.with_suffix(".nfo").write_text(MOVIE_NFO, encoding="utf-8")
        self.assertEqual(self._scan()[0].rating, 7.9)


if __name__ == "__main__":
    unittest.main()
