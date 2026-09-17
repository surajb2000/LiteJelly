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

    def test_the_hero_never_shows_progress(self):
        """The hero is the suggestion slot and Continue watching sits directly
        beneath it. A progress bar there showed the same title twice, one above
        the other, which is why the element was removed rather than wired up."""
        self.assertNotIn("hero-progress", read("index.html"))
        self.assertNotIn("hero-progress", read("app.js"))
        self.assertNotIn("hero-progress", read("style.css"))

    def test_the_hero_does_not_claim_to_be_a_recommendation(self):
        # The pick is ranked by artwork and rotated by the calendar. Nothing
        # in it knows anything about taste, so it must not say "suggested".
        code = read("app.js")
        self.assertNotIn("Suggested show", code)
        self.assertNotIn("Suggested film", code)
        self.assertIn("have not started", code)

    def test_started_is_judged_per_show_not_per_episode(self):
        # Counting only the episode let a series you were three episodes into
        # come back as something new, through an episode you had not reached.
        code = read("app.js")
        pool = between(code, "function suggestionPool()", "function heroSubject")
        self.assertIn("video.series_id || video.id", pool)
        self.assertIn("if (!group.started) {", pool)
        # Per-episode progress is read once, while tallying. A second lookup
        # means the choice itself went back to being per-episode.
        self.assertEqual(pool.count("state.progress[video.id]"), 1)

    def test_a_part_watched_show_is_never_offered_as_new(self):
        # It is in Continue watching already; only a finished show comes back.
        code = read("app.js")
        pool = between(code, "function suggestionPool()", "function heroSubject")
        self.assertIn("group.done === group.total", pool)

    def test_the_shortlist_is_a_rail_and_excludes_the_hero(self):
        # Eight candidates were ranked and seven thrown away, which left one
        # slot pretending to be the whole idea.
        code = read("app.js")
        entries = between(code, "function suggestionEntries", "function buildRail")
        self.assertIn("if (key === skip) return;", entries)
        # Counts have to come from the whole library: the pool holds one
        # episode per show, so a card built from it would claim one episode.
        self.assertIn("groupIntoSeries(state.videos)", entries)
        self.assertIn("Start something new", code)


def between(text, start, end):
    head = text.index(start)
    return text[head:text.index(end, head)]


if __name__ == "__main__":
    unittest.main()
