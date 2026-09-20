"""Restarting the stream without losing your place.

Changing dialogue levelling, audio track, quality or audio delay rebuilds the
plan and restarts the pipe. Every one of those paths used to read the clock,
await the new plan, then call startPlayback with the time it had read.

Two things went wrong when the presses came quickly. The clock reads 0 for as
long as the restart is in flight - measured at 130ms on a fast desktop, and it
is the seekpoint round trip plus ffmpeg spin-up, so longer on a phone - so the
second press captured 0 and the film started over. And with two restarts in
flight the slower response could land last and win.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STATIC = Path(__file__).resolve().parent.parent / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def body(text: str, start: str, end: str) -> str:
    head = text.index(start)
    return text[head:text.index(end, head + len(start))]


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.code = read("app.js")

    def test_the_clock_reports_the_target_while_a_restart_is_in_flight(self):
        # Measured with the guard removed: a second press 700ms after the
        # first sent ss=0.00 and the player landed at 0:00.
        display = body(self.code, "function displayTime()", "function displayDuration")
        self.assertIn("if (state.restartAt !== null) return state.restartAt;", display)
        # The guard has to come before the element is read, or it reports the 0.
        self.assertLess(display.index("state.restartAt"), display.index("el.video.currentTime"))

    def test_only_the_newest_restart_can_finish(self):
        restart = body(self.code, "async function restartStream", "\n  }")
        self.assertIn("++state.restartToken", restart)
        self.assertLess(restart.index("token !== state.restartToken"),
                        restart.index("startPlayback("))

    def test_every_restart_path_goes_through_the_helper(self):
        # Four copies of "read the clock, await, startPlayback" is where the
        # race lived. One copy now, inside restartStream.
        self.assertEqual(self.code.count("const at = displayTime();"), 1)
        for caller in ("cycleLevel", "selectAudio", "selectQuality", "applyAudioOffset"):
            fn = body(self.code, "function " + caller + "(", "\n  }")
            self.assertIn("restartStream(", fn)
            self.assertNotIn("startPlayback(", fn)

    def test_the_target_is_released_by_the_new_source_not_the_old_one(self):
        # Clearing this on timeupdate fires while the OLD source is still
        # playing at the target, which drops the guard before the teardown it
        # exists for. Measured: the 0:00 window came back unchanged.
        self.assertIn("video.addEventListener('loadedmetadata', () => { state.restartAt = null; });",
                      self.code)
        tick = body(self.code, "video.addEventListener('timeupdate'", "});")
        self.assertNotIn("restartAt", tick)

    def test_a_deliberate_seek_takes_the_clock_back(self):
        # Otherwise a pending restart would keep reporting its own target and
        # the seek would be thrown away.
        seek = body(self.code, "function seekTo(", "function seekBy")
        self.assertIn("state.restartAt = null;", seek)

    def test_a_fresh_play_carries_no_target(self):
        start = body(self.code, "function startPlayback(plan, startAt)", "state.activeSubtitle")
        self.assertIn("typeof startAt === 'number' ? startAt : null", start)


if __name__ == "__main__":
    unittest.main()
