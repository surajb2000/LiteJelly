"""Dialogue levelling: the filter, the plan it forces and the API.

A film whose whispers are inaudible and whose explosions are not is the
common complaint on television speakers. Measured on a clip alternating loud
and 22 dB quieter passages: untouched the gap is 22 dB, boost closes it to
10.5 and night to 5.5.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.config import TranscodeSettings
from litejelly.ffmpeg import AUDIO_MODES, AudioStream, FFmpegTools, MediaInfo
from litejelly.web import _audio_mode


def _tools():
    tools = FFmpegTools(Path("."))
    tools.ffmpeg = "ffmpeg"
    tools.ffprobe = "ffprobe"
    return tools


def _arg_after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.tools = _tools()
        self.settings = TranscodeSettings()
        self.info = MediaInfo(
            duration=600.0, container="matroska", video_codec="h264",
            audio_codec="ac3", width=1920, height=1080, probed=True,
            audios=[AudioStream(0, "ac3", language="eng", default=True)],
        )

    def test_off_adds_nothing(self):
        plan = self.tools.plan_playback(self.info)
        cmd = self.tools.build_stream_command(
            Path("m.mkv"), plan, self.settings, audio_mode="off")
        self.assertNotIn("dynaudnorm", " ".join(cmd))

    def test_each_level_uses_the_measured_filter(self):
        for mode in ("boost", "night"):
            plan = self.tools.plan_playback(self.info, audio_mode=mode)
            cmd = self.tools.build_stream_command(
                Path("m.mkv"), plan, self.settings, audio_mode=mode)
            self.assertIn(AUDIO_MODES[mode], _arg_after(cmd, "-af"), mode)

    def test_night_is_stronger_than_boost(self):
        self.assertNotEqual(AUDIO_MODES["boost"], AUDIO_MODES["night"])
        self.assertIn("g=31", AUDIO_MODES["night"])

    def test_loudnorm_is_not_used(self):
        # Measured: in one pass it made everything quieter and cost six times
        # as much CPU as dynaudnorm.
        self.assertNotIn("loudnorm", " ".join(AUDIO_MODES.values()))

    def test_the_sync_filters_are_kept_alongside(self):
        plan = self.tools.plan_playback(self.info, audio_mode="boost")
        cmd = self.tools.build_stream_command(
            Path("m.mkv"), plan, self.settings, audio_mode="boost")
        chain = _arg_after(cmd, "-af")
        self.assertIn("aresample=async=1", chain)
        self.assertIn("dynaudnorm", chain)

    def test_it_survives_a_burned_in_subtitle(self):
        # The audio filter chain is skipped for burn-in, but levelling is the
        # one filter that still has to be there.
        plan = self.tools.plan_playback(self.info, audio_mode="boost")
        cmd = self.tools.build_stream_command(
            Path("m.mkv"), plan, self.settings, burn_subtitle_index=0, audio_mode="boost")
        self.assertIn("dynaudnorm", _arg_after(cmd, "-af"))

    def test_a_manual_delay_still_applies(self):
        plan = self.tools.plan_playback(self.info, audio_mode="boost")
        cmd = self.tools.build_stream_command(
            Path("m.mkv"), plan, self.settings, audio_delay_ms=250, audio_mode="boost")
        chain = _arg_after(cmd, "-af")
        self.assertIn("adelay=250", chain)
        self.assertIn("dynaudnorm", chain)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tools = _tools()

    def _info(self, **kwargs):
        base = dict(duration=600.0, container="mp4", video_codec="h264",
                    audio_codec="aac", width=1920, height=1080, probed=True)
        base.update(kwargs)
        # The planner reads the track list, so it has to agree with the summary.
        base.setdefault("audios", [AudioStream(0, base["audio_codec"], default=True)]
                        if base["audio_codec"] else [])
        return MediaInfo(**base)

    def test_levelling_takes_a_file_out_of_direct_play(self):
        # A filter cannot be applied to a stream the server is not touching.
        self.assertEqual(self.tools.plan_playback(self._info()).mode, "direct")
        plan = self.tools.plan_playback(self._info(), audio_mode="boost")
        self.assertEqual(plan.mode, "remux")
        self.assertEqual(plan.audio_action, "encode")
        self.assertEqual(plan.video_action, "copy")

    def test_the_video_is_still_never_re_encoded_for_it(self):
        plan = self.tools.plan_playback(self._info(container="matroska"),
                                        audio_mode="night")
        self.assertEqual(plan.video_action, "copy")

    def test_audio_already_being_re_encoded_is_unaffected(self):
        without = self.tools.plan_playback(self._info(audio_codec="ac3"))
        with_level = self.tools.plan_playback(self._info(audio_codec="ac3"),
                                              audio_mode="boost")
        self.assertEqual(without.audio_action, with_level.audio_action)

    def test_a_silent_file_is_left_alone(self):
        plan = self.tools.plan_playback(self._info(audio_codec=""), audio_mode="boost")
        self.assertEqual(plan.mode, "direct")


class QueryTests(unittest.TestCase):
    def test_known_levels_are_accepted(self):
        for mode in AUDIO_MODES:
            self.assertEqual(_audio_mode({"level": [mode]}), mode)

    def test_anything_else_is_off(self):
        for raw in ("", "loud", "LOUDER", "1"):
            self.assertEqual(_audio_mode({"level": [raw]}), "off")
        self.assertEqual(_audio_mode({}), "off")

    def test_it_is_not_case_sensitive(self):
        self.assertEqual(_audio_mode({"level": ["NIGHT"]}), "night")


if __name__ == "__main__":
    unittest.main()
