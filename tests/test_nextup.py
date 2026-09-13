"""Tests for what plays next and what appears in Continue watching.

This logic lives on the server precisely so these cases are covered: the last
episode of a show, films, gaps in numbering, and a series that should occupy
one row rather than twenty.

Run with:  python -m unittest discover -s tests
"""

import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.config import MediaDir
from litejelly.library import Library, build_continue_watching, next_episode

logging.getLogger("litejelly.library").setLevel(logging.CRITICAL)


class _Fixture(unittest.TestCase):
    """A small library: one show with two seasons, one short show, two films."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.shows = self.root / "shows"
        self.movies = self.root / "movies"
        self.shows.mkdir()
        self.movies.mkdir()

        for name in ("Mentalist.S01E01.mkv", "Mentalist.S01E02.mkv",
                     "Mentalist.S01E10.mkv", "Mentalist.S02E01.mkv",
                     "Solo.S01E01.mkv"):
            (self.shows / name).write_bytes(b"x" * 512)
        for name in ("Arrival.2016.mkv", "Inception.2010.mkv"):
            (self.movies / name).write_bytes(b"x" * 512)

        library = Library([MediaDir(str(self.shows), "shows"),
                           MediaDir(str(self.movies), "movies")])
        self.videos = library.scan()
        self.by_file = {v.filename: v for v in self.videos}

    def tearDown(self):
        self._tmp.cleanup()

    def video(self, filename):
        return self.by_file[filename]

    def progress(self, video, position=0.0, duration=100.0,
                 finished=False, updated_at=None):
        return {
            "video_id": video.id,
            "position": position,
            "duration": duration,
            "finished": finished,
            "updated_at": updated_at if updated_at is not None else time.time(),
        }


class NextEpisodeTests(_Fixture):
    def test_returns_the_following_episode(self):
        following = next_episode(self.videos, self.video("Mentalist.S01E01.mkv"))
        self.assertEqual(following.filename, "Mentalist.S01E02.mkv")

    def test_crosses_a_season_boundary(self):
        following = next_episode(self.videos, self.video("Mentalist.S01E10.mkv"))
        self.assertEqual(following.filename, "Mentalist.S02E01.mkv")

    def test_gaps_in_numbering_are_skipped_over(self):
        # E02 to E10 with nothing between: the next present episode wins.
        following = next_episode(self.videos, self.video("Mentalist.S01E02.mkv"))
        self.assertEqual(following.filename, "Mentalist.S01E10.mkv")

    def test_last_episode_has_no_next(self):
        self.assertIsNone(next_episode(self.videos, self.video("Mentalist.S02E01.mkv")))

    def test_only_episode_has_no_next(self):
        self.assertIsNone(next_episode(self.videos, self.video("Solo.S01E01.mkv")))

    def test_a_film_has_no_next(self):
        self.assertIsNone(next_episode(self.videos, self.video("Arrival.2016.mkv")))

    def test_does_not_wander_into_another_series(self):
        following = next_episode(self.videos, self.video("Solo.S01E01.mkv"))
        self.assertIsNone(following, "next must stay inside the series")

    def test_none_is_handled(self):
        self.assertIsNone(next_episode(self.videos, None))

    def test_video_missing_from_the_library_has_no_next(self):
        stale = self.video("Mentalist.S01E01.mkv")
        remaining = [v for v in self.videos if v.id != stale.id]
        self.assertIsNone(next_episode([v for v in remaining
                                        if v.series_id != stale.series_id], stale))

    def test_walking_the_whole_series_visits_each_episode_once(self):
        current = self.video("Mentalist.S01E01.mkv")
        seen = [current.filename]
        while True:
            current = next_episode(self.videos, current)
            if current is None:
                break
            self.assertNotIn(current.filename, seen, "next must not loop")
            seen.append(current.filename)
        self.assertEqual(seen, ["Mentalist.S01E01.mkv", "Mentalist.S01E02.mkv",
                                "Mentalist.S01E10.mkv", "Mentalist.S02E01.mkv"])


class ContinueWatchingTests(_Fixture):
    def test_empty_without_progress(self):
        self.assertEqual(build_continue_watching(self.videos, {}), [])

    def test_part_watched_episode_appears(self):
        video = self.video("Mentalist.S01E01.mkv")
        rows = build_continue_watching(self.videos, {video.id: self.progress(video, 40)})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], video.id)
        self.assertEqual(rows[0]["position"], 40)
        self.assertFalse(rows[0]["next_up"])

    def test_a_series_occupies_one_row(self):
        # The clutter this exists to fix: five episodes of one show.
        progress = {}
        for index, name in enumerate(["Mentalist.S01E01.mkv", "Mentalist.S01E02.mkv",
                                      "Mentalist.S01E10.mkv"]):
            video = self.video(name)
            progress[video.id] = self.progress(video, 30, updated_at=100 + index)
        rows = build_continue_watching(self.videos, progress)
        self.assertEqual(len(rows), 1)

    def test_the_newest_episode_of_a_series_is_the_one_shown(self):
        progress = {}
        older = self.video("Mentalist.S01E01.mkv")
        newer = self.video("Mentalist.S01E02.mkv")
        progress[older.id] = self.progress(older, 30, updated_at=100)
        progress[newer.id] = self.progress(newer, 30, updated_at=200)
        rows = build_continue_watching(self.videos, progress)
        self.assertEqual(rows[0]["id"], newer.id)

    def test_finished_episode_becomes_the_next_one(self):
        video = self.video("Mentalist.S01E01.mkv")
        rows = build_continue_watching(
            self.videos, {video.id: self.progress(video, 0, finished=True)})
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["next_up"])
        self.assertEqual(rows[0]["id"], self.video("Mentalist.S01E02.mkv").id)

    def test_finished_last_episode_disappears(self):
        video = self.video("Mentalist.S02E01.mkv")
        rows = build_continue_watching(
            self.videos, {video.id: self.progress(video, 0, finished=True)})
        self.assertEqual(rows, [])

    def test_finished_film_disappears(self):
        video = self.video("Arrival.2016.mkv")
        rows = build_continue_watching(
            self.videos, {video.id: self.progress(video, 0, finished=True)})
        self.assertEqual(rows, [])

    def test_next_up_skips_past_episodes_already_finished(self):
        # E01 and E02 both watched, so the row should offer the first episode
        # that has not been seen rather than giving up.
        first = self.video("Mentalist.S01E01.mkv")
        second = self.video("Mentalist.S01E02.mkv")
        rows = build_continue_watching(self.videos, {
            first.id: self.progress(first, 0, finished=True, updated_at=200),
            second.id: self.progress(second, 0, finished=True, updated_at=100),
        })
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["next_up"])
        self.assertEqual(rows[0]["id"], self.video("Mentalist.S01E10.mkv").id)

    def test_a_fully_watched_series_disappears(self):
        progress = {}
        for index, name in enumerate(["Mentalist.S01E01.mkv", "Mentalist.S01E02.mkv",
                                      "Mentalist.S01E10.mkv", "Mentalist.S02E01.mkv"]):
            video = self.video(name)
            progress[video.id] = self.progress(video, 0, finished=True,
                                               updated_at=100 + index)
        self.assertEqual(build_continue_watching(self.videos, progress), [])

    def test_a_few_seconds_in_is_not_resumable(self):
        video = self.video("Mentalist.S01E01.mkv")
        rows = build_continue_watching(self.videos, {video.id: self.progress(video, 5)})
        self.assertEqual(rows, [])

    def test_films_appear_individually(self):
        arrival = self.video("Arrival.2016.mkv")
        inception = self.video("Inception.2010.mkv")
        rows = build_continue_watching(self.videos, {
            arrival.id: self.progress(arrival, 40, updated_at=100),
            inception.id: self.progress(inception, 40, updated_at=200),
        })
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], inception.id)

    def test_newest_series_comes_first(self):
        mentalist = self.video("Mentalist.S01E01.mkv")
        solo = self.video("Solo.S01E01.mkv")
        rows = build_continue_watching(self.videos, {
            mentalist.id: self.progress(mentalist, 40, updated_at=100),
            solo.id: self.progress(solo, 40, updated_at=300),
        })
        self.assertEqual([row["id"] for row in rows], [solo.id, mentalist.id])

    def test_limit_is_honoured(self):
        progress = {}
        for index, video in enumerate(self.videos):
            progress[video.id] = self.progress(video, 40, updated_at=index)
        rows = build_continue_watching(self.videos, progress, limit=2)
        self.assertEqual(len(rows), 2)

    def test_progress_for_a_removed_video_is_ignored(self):
        rows = build_continue_watching(self.videos, {
            "ghost": {"video_id": "ghost", "position": 50, "duration": 100,
                      "finished": False, "updated_at": 999},
        })
        self.assertEqual(rows, [])

    def test_missing_updated_at_does_not_raise(self):
        video = self.video("Arrival.2016.mkv")
        rows = build_continue_watching(self.videos, {
            video.id: {"video_id": video.id, "position": 40,
                       "duration": 100, "finished": False},
        })
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
