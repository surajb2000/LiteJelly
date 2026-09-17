"""Browsing filters: what has been watched, and by genre.

The chips used to sort by container - MP4, MKV, Other - which answers a
question about the file rather than about the viewing.

Run with:  python -m unittest discover -s tests
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.library import Video

STATIC = Path(__file__).resolve().parent.parent / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


class PayloadTests(unittest.TestCase):
    """Genres live in the cached metadata, which the listing strips."""

    def _video(self, meta=None):
        return Video(id="a", name="n", title="t", filename="f.mkv", path="f.mkv",
                     dir_index=0, folder="", content_type="mixed", category="movies",
                     series_id="", size=1, size_human="1 B", modified="",
                     modified_ts=0.0, extension="mkv", meta=meta)

    def test_genres_are_published(self):
        data = self._video({"genres": ["Drama", "Crime"]}).to_dict()
        self.assertEqual(data["genres"], ["Drama", "Crime"])

    def test_a_video_without_metadata_still_has_the_field(self):
        self.assertEqual(self._video().to_dict()["genres"], [])
        self.assertEqual(self._video({}).to_dict()["genres"], [])

    def test_the_plot_is_still_not_sent(self):
        # The whole reason meta is stripped: plots run to thousands of
        # characters and the listing carries every video.
        data = self._video({"genres": ["Drama"], "plot": "x" * 4000}).to_dict()
        self.assertNotIn("meta", data)
        self.assertNotIn("plot", data)

    def test_a_long_genre_list_is_trimmed(self):
        many = [f"Genre {i}" for i in range(20)]
        self.assertEqual(len(self._video({"genres": many}).to_dict()["genres"]), 6)

    def test_blank_entries_are_dropped(self):
        data = self._video({"genres": ["Drama", "", "  "]}).to_dict()
        self.assertEqual(data["genres"], ["Drama"])

    def test_a_broken_record_does_not_break_the_listing(self):
        for junk in ({"genres": None}, {"genres": []}):
            self.assertEqual(self._video(junk).to_dict()["genres"], [])


class MarkupTests(unittest.TestCase):
    def test_the_container_chips_are_gone(self):
        markup = read("index.html")
        self.assertNotIn('data-filter="mkv"', markup)
        self.assertNotIn('data-filter="mp4"', markup)

    def test_the_watch_chips_cover_every_state(self):
        markup = read("index.html")
        for state in ("all", "unwatched", "started", "watched"):
            self.assertIn(f'data-watch="{state}"', markup)

    def test_the_chip_values_match_what_the_script_decides(self):
        # A chip whose value the filter never produces is a dead end.
        code = read("app.js")
        produced = set(re.findall(r"return '(unwatched|started|watched)'", code))
        produced |= set(re.findall(r"\? '(started)' : ", code))
        self.assertEqual(produced, {"unwatched", "started", "watched"})

    def test_the_hero_progress_bar_is_used(self):
        # It sat in the markup unused: the hero suggests unstarted titles, so
        # it only appears once everything has been begun.
        code = read("app.js")
        self.assertIn("heroProgressFill.style.width", code)


if __name__ == "__main__":
    unittest.main()
