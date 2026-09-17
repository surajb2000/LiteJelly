"""Regression tests for the A/V synchronisation fixes.

These cover bugs that were found by measuring real output, not by reading the
code, and each one produced audible desync. They are pinned here so a future
refactor cannot quietly reintroduce them.

Run with:  python -m unittest discover -s tests
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.config import TranscodeSettings
from litejelly.ffmpeg import (FFmpegTools, MediaInfo, PlaybackPlan, resolve_quality,
                              stream_mime)
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


class TerminalSafetyTests(unittest.TestCase):
    """ffmpeg switches the controlling terminal to no-echo so it can read its
    interactive keys. Inheriting stdin left Termux with invisible input after
    Ctrl+C, needing a manual `stty echo`."""

    def test_stream_command_declines_stdin(self):
        cmd = _tools().build_stream_command(
            Path("movie.mkv"), COPY_PLAN, TranscodeSettings())
        self.assertIn("-nostdin", cmd)

    def test_popen_detaches_stdin(self):
        import inspect
        from litejelly.ffmpeg import popen_quiet, run_quiet

        for func in (popen_quiet, run_quiet):
            source = inspect.getsource(func)
            self.assertIn("stdin=subprocess.DEVNULL", source, func.__name__)


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


class FakeProbeFile:
    """Stands in for ffprobe on a file with a 10 s GOP.

    ``entry_points`` are real IDR frames; ``cra`` frames are flagged as
    keyframes but seeking to one rewinds to the previous entry point, which is
    what x265's open-GOP output does.
    """

    def __init__(self, entry_points, cra=()):
        self.entry_points = sorted(entry_points)
        self.cra = sorted(cra)
        self.calls = []

    def landing(self, target):
        return max([t for t in self.entry_points if t <= target + 1e-6], default=0.0)

    def __call__(self, cmd, timeout=None):
        self.calls.append(cmd)
        interval = cmd[cmd.index("-read_intervals") + 1]
        start_text, _, span_text = interval.partition("%+")
        start = float(start_text)
        if "-skip_frame" in cmd:
            span = float(span_text)
            first = self.landing(start)
            frames = [t for t in self.entry_points + self.cra if first <= t < start + span]
        else:
            frames = [self.landing(start)]
        stdout = "".join(f"{t:.6f}\n" for t in sorted(frames)).encode()
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=b"")


class ForwardSeekTests(unittest.TestCase):
    """Skipping an intro used the backward landing and dropped the viewer back
    inside it by up to a GOP (skip to 47.5 on a 10 s GOP started at 40)."""

    def setUp(self):
        self.tools = _tools()
        self.path = Path("show.mkv")

    def _run(self, probe, target, forward):
        with mock.patch("litejelly.ffmpeg.run_quiet", probe):
            return self.tools.seek_landing(self.path, target, forward=forward)

    def test_backward_landing_is_unchanged(self):
        probe = FakeProbeFile([0, 10, 20, 30, 40, 50])
        self.assertEqual(self._run(probe, 47.5, False), 40.0)

    def test_forward_landing_never_precedes_the_target(self):
        probe = FakeProbeFile([0, 10, 20, 30, 40, 50])
        self.assertEqual(self._run(probe, 47.5, True), 50.0)

    def test_forward_on_an_entry_point_stays_put(self):
        probe = FakeProbeFile([0, 10, 20, 30, 40, 50])
        self.assertEqual(self._run(probe, 40.0, True), 40.0)
        self.assertEqual(len(probe.calls), 1, "no keyframe scan was needed")

    def test_forward_keeps_a_landing_just_short_of_the_target(self):
        # Measured on the reference clip: a "10 s" GOP at 23.976 fps puts
        # keyframes at 39.873, not 40.0. Skipping to 40 must not jump to 49.8.
        probe = FakeProbeFile([0, 9.968, 19.937, 29.905, 39.873, 49.841])
        self.assertEqual(self._run(probe, 40.0, True), 39.873)
        self.assertEqual(self._run(probe, 47.5, True), 49.841)

    def test_forward_rejects_open_gop_cra_frames(self):
        probe = FakeProbeFile([0, 10, 20, 30, 40, 50], cra=[45])
        self.assertEqual(self._run(probe, 43.0, True), 50.0)

    def test_forward_widens_the_window_for_long_gops(self):
        probe = FakeProbeFile([0, 40, 80])
        self.assertEqual(self._run(probe, 43.0, True), 80.0)
        scans = [c for c in probe.calls if "-skip_frame" in c]
        self.assertEqual(len(scans), 2)

    def test_forward_falls_back_to_backward_when_nothing_follows(self):
        probe = FakeProbeFile([0, 40])
        self.assertEqual(self._run(probe, 43.0, True), 40.0)

    def test_forward_rounds_up_so_ss_cannot_undershoot(self):
        probe = FakeProbeFile([0, 40, 47.5475])
        landed = self._run(probe, 43.0, True)
        self.assertGreaterEqual(landed, 47.5475)
        self.assertLess(landed, 47.5475 + 0.001)

    def test_directions_are_cached_separately(self):
        probe = FakeProbeFile([0, 10, 20, 30, 40, 50])
        self.assertEqual(self._run(probe, 47.5, False), 40.0)
        self.assertEqual(self._run(probe, 47.5, True), 50.0)
        self.assertEqual(self._run(probe, 47.5, False), 40.0)


class MseStreamTests(unittest.TestCase):
    """The piped fMP4 must be something MediaSource can append and describe."""

    def setUp(self):
        self.tools = _tools()
        self.settings = TranscodeSettings()

    def _info(self, **kwargs):
        base = dict(duration=600.0, container="matroska", video_codec="h264",
                    audio_codec="aac", profile="High", level=40,
                    audio_profile="LC", probed=True)
        base.update(kwargs)
        return MediaInfo(**base)

    def test_fragments_are_time_bounded(self):
        # Measured: frag_keyframe alone gave 7 fragments for a 60 s clip with a
        # 10 s GOP; with -frag_duration 2000000 it gave 31.
        cmd = self.tools.build_stream_command(Path("m.mkv"), COPY_PLAN, self.settings)
        self.assertEqual(_arg_after(cmd, "-frag_duration"), "2000000")
        self.assertIn("empty_moov", _arg_after(cmd, "-movflags"))
        self.assertIn("default_base_moof", _arg_after(cmd, "-movflags"))

    def test_copied_hevc_is_tagged_hvc1(self):
        cmd = self.tools.build_stream_command(
            Path("m.mkv"), COPY_PLAN, self.settings, info=self._info(video_codec="hevc"))
        self.assertEqual(_arg_after(cmd, "-tag:v"), "hvc1")

    def test_copied_h264_is_not_retagged(self):
        cmd = self.tools.build_stream_command(
            Path("m.mkv"), COPY_PLAN, self.settings, info=self._info())
        self.assertNotIn("-tag:v", cmd)

    def test_encoded_hevc_is_not_tagged(self):
        cmd = self.tools.build_stream_command(
            Path("m.mkv"), ENCODE_PLAN, self.settings, info=self._info(video_codec="hevc"))
        self.assertNotIn("-tag:v", cmd)

    def test_h264_copy_mime_carries_profile_and_level(self):
        copy_both = PlaybackPlan("remux", "copy", "copy", "", False)
        self.assertEqual(stream_mime(self._info(), copy_both, self.settings),
                         'video/mp4; codecs="avc1.640028, mp4a.40.2"')
        self.assertEqual(
            stream_mime(self._info(profile="Main", level=31), copy_both, self.settings),
            'video/mp4; codecs="avc1.4D001F, mp4a.40.2"')
        self.assertEqual(
            stream_mime(self._info(profile="Constrained Baseline", level=30),
                        copy_both, self.settings),
            'video/mp4; codecs="avc1.42401E, mp4a.40.2"')

    def test_hevc_copy_mime(self):
        copy_both = PlaybackPlan("remux", "copy", "copy", "", False)
        self.assertEqual(
            stream_mime(self._info(video_codec="hevc", profile="Main", level=120),
                        copy_both, self.settings),
            'video/mp4; codecs="hvc1.1.6.L120.B0, mp4a.40.2"')
        self.assertEqual(
            stream_mime(self._info(video_codec="hevc", profile="Main 10", level=153),
                        copy_both, self.settings),
            'video/mp4; codecs="hvc1.2.4.L153.B0, mp4a.40.2"')

    def test_encoded_audio_is_described_by_the_encoder(self):
        self.assertEqual(
            stream_mime(self._info(audio_codec="ac3", audio_profile=""), COPY_PLAN,
                        self.settings),
            'video/mp4; codecs="avc1.640028, mp4a.40.2"')

    def test_encoded_video_is_described_by_the_encoder(self):
        self.assertEqual(stream_mime(self._info(video_codec="mpeg4"), ENCODE_PLAN,
                                     self.settings),
                         'video/mp4; codecs="avc1.640028, mp4a.40.2"')

    def test_burned_subtitles_mean_encoded_video(self):
        self.assertEqual(
            stream_mime(self._info(), COPY_PLAN, self.settings, burning=True),
            'video/mp4; codecs="avc1.640028, mp4a.40.2"')

    def test_video_only_source_has_no_audio_string(self):
        plan = PlaybackPlan("remux", "copy", "copy", "", False)
        self.assertEqual(stream_mime(self._info(audio_codec=""), plan, self.settings),
                         'video/mp4; codecs="avc1.640028"')

    def test_unknown_codecs_give_no_mime(self):
        copy_both = PlaybackPlan("remux", "copy", "copy", "", False)
        self.assertEqual(stream_mime(self._info(video_codec="vp8"), copy_both,
                                     self.settings), "")
        self.assertEqual(stream_mime(self._info(audio_codec="vorbis"), copy_both,
                                     self.settings), "")
        odd = TranscodeSettings(video_codec="libsvtav1")
        self.assertEqual(stream_mime(self._info(), ENCODE_PLAN, odd), "")

    def test_unknown_profile_falls_back_to_a_playable_string(self):
        copy_both = PlaybackPlan("remux", "copy", "copy", "", False)
        self.assertEqual(stream_mime(self._info(profile="", level=0), copy_both,
                                     self.settings),
                         'video/mp4; codecs="avc1.640028, mp4a.40.2"')


if __name__ == "__main__":
    unittest.main()
