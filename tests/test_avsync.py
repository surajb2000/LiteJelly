"""Regression tests for the A/V synchronisation fixes.

These cover bugs that were found by measuring real output, not by reading the
code, and each one produced audible desync. They are pinned here so a future
refactor cannot quietly reintroduce them.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.config import TranscodeSettings
from litejelly.ffmpeg import FFmpegTools, MediaInfo, PlaybackPlan, resolve_quality
from litejelly.web import _audio_delay_ms


def _tools():
    tools = FFmpegTools(Path("."))
    # The command builder is pure string assembly; pin the binary so the test
    # does not depend on whether ffmpeg is installed.
    tools.ffmpeg = "ffmpeg"
    tools.ffprobe = "ffprobe"
    return tools


COPY_PLAN = PlaybackPlan("remux", "copy", "encode", "Remuxed", False)
ENCODE_PLAN = PlaybackPlan("transcode", "encode", "encode", "Transcoded", False)
DIRECT_PLAN = PlaybackPlan("direct", "copy", "copy", "Direct play", True)


def _arg_after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class AccurateSeekTests(unittest.TestCase):
    """Measured root cause of a ~10 s split: ffmpeg's default accurate seek
    trims audio to the exact -ss while copied video can only start at a
    keyframe. -ss 30 gave video at 20.02 s and audio at 29.96 s."""

    def setUp(self):
        self.tools = _tools()
        self.settings = TranscodeSettings()

    def test_copied_video_disables_accurate_seek(self):
        cmd = self.tools.build_stream_command(
            Path("movie.mkv"), COPY_PLAN, self.settings, start=30.0)
        self.assertIn("-noaccurate_seek", cmd)

    def test_direct_copy_disables_accurate_seek(self):
        cmd = self.tools.build_stream_command(
            Path("movie.mp4"), DIRECT_PLAN, self.settings, start=30.0)
        self.assertIn("-noaccurate_seek", cmd)

    def test_encoded_video_keeps_accurate_seek(self):
        # Re-encoded video is trimmed to the exact timestamp too, so accurate
        # seek is correct there and gives a tighter landing.
        cmd = self.tools.build_stream_command(
            Path("movie.mkv"), ENCODE_PLAN, self.settings, start=30.0)
        self.assertNotIn("-noaccurate_seek", cmd)

    def test_no_seek_flag_when_starting_at_zero(self):
        cmd = self.tools.build_stream_command(
            Path("movie.mkv"), COPY_PLAN, self.settings, start=0.0)
        self.assertNotIn("-noaccurate_seek", cmd)
        self.assertNotIn("-ss", cmd)

    def test_seek_flags_precede_the_input(self):
        # -ss and -noaccurate_seek only apply to the input if they come before
        # -i; after it they would become slow output-side options.
        cmd = self.tools.build_stream_command(
            Path("movie.mkv"), COPY_PLAN, self.settings, start=30.0)
        self.assertLess(cmd.index("-noaccurate_seek"), cmd.index("-i"))
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))


class SeekTimeTests(unittest.TestCase):
    """The requested time must reach ffmpeg unchanged. Snapping it onto a
    keyframe made ffmpeg rewind a whole GOP: -ss 25 produced 961 packets,
    -ss 19.94 produced 1200."""

    def setUp(self):
        self.tools = _tools()
        self.settings = TranscodeSettings()

    def test_requested_time_is_passed_through(self):
        for start in (25.0, 19.94, 1234.567):
            cmd = self.tools.build_stream_command(
                Path("movie.mkv"), COPY_PLAN, self.settings, start=start)
            self.assertAlmostEqual(float(_arg_after(cmd, "-ss")), start, places=3)

    def test_genpts_is_set_before_seeking(self):
        cmd = self.tools.build_stream_command(
            Path("movie.mkv"), COPY_PLAN, self.settings, start=30.0)
        self.assertEqual(_arg_after(cmd, "-fflags"), "+genpts")
        self.assertLess(cmd.index("-fflags"), cmd.index("-ss"))

    def test_timestamps_are_rebased(self):
        cmd = self.tools.build_stream_command(
            Path("movie.mkv"), COPY_PLAN, self.settings, start=30.0)
        self.assertEqual(_arg_after(cmd, "-avoid_negative_ts"), "make_zero")


class AudioDelayTests(unittest.TestCase):
    """Automatic B-frame compensation was measured to overshoot (-44 ms became
    +38 ms) and was removed. Only the manual control may shift audio."""

    def setUp(self):
        self.info = MediaInfo(
            duration=600.0, container="matroska", video_codec="h264",
            audio_codec="ac3", width=1920, height=1080,
            has_b_frames=3, fps=23.976, probed=True,
        )

    def test_b_frames_alone_do_not_shift_audio(self):
        self.assertEqual(_audio_delay_ms(self.info, COPY_PLAN, {}), 0.0)

    def test_reorder_delay_is_reported_but_not_applied(self):
        # Still surfaced to the client as diagnostic information.
        self.assertGreater(self.info.reorder_delay, 0)
        self.assertEqual(_audio_delay_ms(self.info, COPY_PLAN, {}), 0.0)

    def test_manual_offset_is_honoured(self):
        self.assertEqual(_audio_delay_ms(self.info, COPY_PLAN, {"adelay": ["250"]}), 250.0)
        self.assertEqual(_audio_delay_ms(self.info, COPY_PLAN, {"adelay": ["-250"]}), -250.0)

    def test_offset_is_clamped(self):
        self.assertEqual(_audio_delay_ms(self.info, COPY_PLAN, {"adelay": ["99999"]}), 5000.0)
        self.assertEqual(_audio_delay_ms(self.info, COPY_PLAN, {"adelay": ["-99999"]}), -5000.0)

    def test_garbage_offset_is_ignored(self):
        self.assertEqual(_audio_delay_ms(self.info, COPY_PLAN, {"adelay": ["soon"]}), 0.0)


class AudioFilterTests(unittest.TestCase):
    def setUp(self):
        self.tools = _tools()
        self.settings = TranscodeSettings()

    def _filters(self, **kwargs):
        cmd = self.tools.build_stream_command(
            Path("movie.mkv"), COPY_PLAN, self.settings, **kwargs)
        return _arg_after(cmd, "-af")

    def test_resampler_keeps_audio_locked_to_the_video_clock(self):
        self.assertIn("aresample=async=1", self._filters())

    def test_no_delay_filter_without_an_offset(self):
        filters = self._filters()
        self.assertNotIn("adelay", filters)
        self.assertNotIn("atrim", filters)

    def test_positive_offset_pads_the_front(self):
        self.assertIn("adelay=250", self._filters(audio_delay_ms=250.0))

    def test_negative_offset_trims_the_front(self):
        filters = self._filters(audio_delay_ms=-250.0)
        self.assertIn("atrim=start=0.250", filters)
        self.assertIn("asetpts=PTS-STARTPTS", filters)

    def test_copied_audio_is_never_filtered(self):
        cmd = self.tools.build_stream_command(
            Path("movie.mp4"), DIRECT_PLAN, self.settings, audio_delay_ms=250.0)
        self.assertNotIn("-af", cmd)
        self.assertEqual(_arg_after(cmd, "-c:a"), "copy")


class ScalingTests(unittest.TestCase):
    """Every source was once re-encoded to 1280x720 with black bars baked in."""

    def setUp(self):
        self.tools = _tools()
        self.settings = TranscodeSettings()

    def test_no_padding_is_applied(self):
        cmd = self.tools.build_stream_command(
            Path("movie.mkv"), ENCODE_PLAN, self.settings)
        self.assertNotIn("pad=", _arg_after(cmd, "-vf"))

    def test_scale_filter_never_upscales(self):
        video_filter = _arg_after(self.tools.build_stream_command(
            Path("movie.mkv"), ENCODE_PLAN, self.settings), "-vf")
        self.assertIn("min(1280,iw)", video_filter)
        self.assertIn("min(720,ih)", video_filter)

    def test_original_quality_does_not_scale_or_cap_bitrate(self):
        cmd = self.tools.build_stream_command(
            Path("movie.mkv"), ENCODE_PLAN, self.settings,
            quality=resolve_quality("original"))
        self.assertNotIn("-vf", cmd)
        self.assertNotIn("-maxrate", cmd)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tools = _tools()

    def _info(self, **kwargs):
        base = dict(duration=600.0, container="matroska", video_codec="h264",
                    audio_codec="aac", width=1920, height=1080, probed=True)
        base.update(kwargs)
        return MediaInfo(**base)

    def test_native_container_direct_plays(self):
        plan = self.tools.plan_playback(self._info(container="mp4"))
        self.assertEqual(plan.mode, "direct")
        self.assertTrue(plan.seekable)

    def test_mkv_with_supported_codecs_is_remuxed(self):
        plan = self.tools.plan_playback(self._info())
        self.assertEqual(plan.mode, "remux")
        self.assertEqual(plan.video_action, "copy")

    def test_unsupported_audio_is_re_encoded_but_video_is_kept(self):
        plan = self.tools.plan_playback(self._info(audio_codec="ac3"))
        self.assertEqual(plan.video_action, "copy")
        self.assertEqual(plan.audio_action, "encode")

    def test_hevc_is_transcoded_unless_allowed(self):
        self.assertEqual(self.tools.plan_playback(self._info(video_codec="hevc")).mode,
                         "transcode")
        self.assertEqual(
            self.tools.plan_playback(self._info(video_codec="hevc"),
                                     allow_hevc_direct=True).video_action,
            "copy")

    def test_choosing_a_rung_always_re_encodes(self):
        # The escape hatch: if a stream copy misbehaves on a client, picking a
        # numbered quality must actually re-encode, even at the source size.
        plan = self.tools.plan_playback(self._info(height=720),
                                        quality=resolve_quality("720p"))
        self.assertEqual(plan.video_action, "encode")
        self.assertFalse(plan.seekable)

    def test_auto_and_original_leave_the_plan_alone(self):
        for quality in ("auto", "original"):
            plan = self.tools.plan_playback(self._info(), quality=resolve_quality(quality))
            self.assertEqual(plan.video_action, "copy")


if __name__ == "__main__":
    unittest.main()
