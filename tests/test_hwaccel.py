"""Hardware encoding: the table, the fallbacks and the self-test.

The whole feature exists because a build advertising an encoder proves
nothing. Measured on one laptop, ffmpeg listed NVENC, AMF, VAAPI, D3D12 and
Vulkan, and not one of them could encode a frame.

Run with:  python -m unittest discover -s tests
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.config import TranscodeSettings
from litejelly.ffmpeg import (HWACCEL_CHOICES, HWACCELS, FFmpegTools,
                              PlaybackPlan, _encoder_for)
from litejelly.settings import validate

ENCODE_PLAN = PlaybackPlan("transcode", "encode", "encode", "Transcoded", False)
COPY_PLAN = PlaybackPlan("remux", "copy", "encode", "Remuxed", False)


def _tools(listed=()):
    tools = FFmpegTools(Path("."))
    tools.ffmpeg = "ffmpeg"
    tools.ffprobe = "ffprobe"
    tools._encoders = set(listed)
    return tools


class TableTests(unittest.TestCase):
    def test_software_keeps_preset_and_crf(self):
        encoder, args = _encoder_for("none", TranscodeSettings(crf=21))
        self.assertEqual(encoder, "libx264")
        self.assertIn("-crf", args)
        self.assertIn("21", args)

    def test_no_hardware_encoder_is_given_a_crf(self):
        # None of them understand it, and passing it makes ffmpeg refuse.
        for name in HWACCELS:
            _, args = _encoder_for(name, TranscodeSettings(crf=21))
            self.assertNotIn("-crf", args, name)

    def test_quality_is_carried_across_in_whatever_the_encoder_speaks(self):
        _, nvenc = _encoder_for("nvenc", TranscodeSettings(crf=21))
        self.assertEqual(nvenc[nvenc.index("-cq") + 1], "21")
        _, qsv = _encoder_for("qsv", TranscodeSettings(crf=21))
        self.assertEqual(qsv[qsv.index("-global_quality") + 1], "21")

    def test_media_foundation_quality_runs_the_other_way(self):
        # It wants 0-100 where higher is better, the reverse of a CRF.
        _, good = _encoder_for("mediafoundation", TranscodeSettings(crf=1))
        _, poor = _encoder_for("mediafoundation", TranscodeSettings(crf=50))
        self.assertGreater(int(good[good.index("-quality") + 1]),
                           int(poor[poor.index("-quality") + 1]))

    def test_an_encoder_without_a_quality_scale_gets_a_bitrate(self):
        _, args = _encoder_for("mediacodec", TranscodeSettings(max_video_bitrate="2500k"))
        self.assertEqual(args[args.index("-b:v") + 1], "2500k")

    def test_an_unknown_option_names_no_encoder(self):
        self.assertEqual(_encoder_for("magic", TranscodeSettings()), ("", []))


class SelectionTests(unittest.TestCase):
    def test_a_listed_encoder_is_used(self):
        tools = _tools(["libx264", "h264_qsv"])
        encoder, args = tools.video_encoder(TranscodeSettings(hwaccel="qsv"))
        self.assertEqual(encoder, "h264_qsv")
        self.assertIn("-global_quality", args)

    def test_an_absent_encoder_falls_back_to_software(self):
        # Asking for NVENC on a machine without it must not stop playback.
        tools = _tools(["libx264"])
        encoder, args = tools.video_encoder(TranscodeSettings(hwaccel="nvenc"))
        self.assertEqual(encoder, "libx264")
        self.assertIn("-crf", args)

    def test_the_command_carries_the_chosen_encoder(self):
        tools = _tools(["libx264", "h264_qsv"])
        cmd = tools.build_stream_command(
            Path("m.mkv"), ENCODE_PLAN, TranscodeSettings(hwaccel="qsv"))
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "h264_qsv")
        self.assertNotIn("-crf", cmd)

    def test_only_software_gets_a_pixel_format(self):
        # Hardware encoders take their own surface formats.
        tools = _tools(["libx264", "h264_qsv"])
        software = tools.build_stream_command(
            Path("m.mkv"), ENCODE_PLAN, TranscodeSettings())
        hardware = tools.build_stream_command(
            Path("m.mkv"), ENCODE_PLAN, TranscodeSettings(hwaccel="qsv"))
        self.assertIn("-pix_fmt", software)
        self.assertNotIn("-pix_fmt", hardware)

    def test_a_stream_copy_is_untouched_by_the_setting(self):
        tools = _tools(["libx264", "h264_qsv"])
        cmd = tools.build_stream_command(
            Path("m.mkv"), COPY_PLAN, TranscodeSettings(hwaccel="qsv"))
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")


class SelfTestTests(unittest.TestCase):
    def setUp(self):
        self.tools = _tools(["libx264", "h264_nvenc"])

    def _with_ffmpeg(self, returncode, stderr=b"", delay=0.0):
        def fake_run(cmd, timeout=None):
            return subprocess.CompletedProcess(cmd, returncode, b"", stderr)
        return mock.patch("litejelly.ffmpeg.run_quiet", fake_run)

    def test_a_working_encoder_reports_how_fast_it_was(self):
        with self._with_ffmpeg(0):
            result = self.tools.test_encoder("nvenc", seconds=3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["encoder"], "h264_nvenc")
        self.assertGreater(result["speed"], 0)

    def test_a_broken_encoder_reports_ffmpeg_own_words(self):
        with self._with_ffmpeg(1, b"[h264_nvenc] Cannot load nvcuda.dll\n"):
            result = self.tools.test_encoder("nvenc", seconds=3)
        self.assertFalse(result["ok"])
        self.assertIn("nvcuda", result["detail"])

    def test_an_unlisted_encoder_is_not_even_tried(self):
        result = self.tools.test_encoder("videotoolbox", seconds=3)
        self.assertFalse(result["ok"])
        self.assertIn("no such encoder", result["detail"])

    def test_an_unknown_option_is_refused(self):
        self.assertFalse(self.tools.test_encoder("magic")["ok"])

    def test_without_ffmpeg_there_is_nothing_to_test(self):
        tools = _tools()
        tools.ffmpeg = None
        self.assertFalse(tools.test_encoder("nvenc")["ok"])


class AutoTests(unittest.TestCase):
    """Auto must not stall the first play, and must not pick something slower
    than software - measured, libx264 at veryfast beat Quick Sync on one
    laptop, 7.2x against 5.4x."""

    def test_software_is_used_until_the_answer_is_in(self):
        tools = _tools(["libx264", "h264_qsv"])
        with mock.patch("litejelly.ffmpeg.threading.Thread") as thread:
            choice = tools._auto_hwaccel()
        self.assertEqual(choice, "none")
        thread.assert_called_once()
        self.assertTrue(thread.return_value.start.called,
                        "the probe has to run in the background")

    def test_the_fastest_wins(self):
        tools = _tools(["libx264", "h264_qsv"])
        speeds = {"none": 4.0, "qsv": 9.0}
        tools.test_encoder = lambda name, seconds=10: {
            "ok": name in speeds, "speed": speeds.get(name, 0), "encoder": name,
            "detail": ""}
        tools._resolve_auto()
        self.assertEqual(tools._auto_choice, "qsv")

    def test_software_is_kept_when_hardware_is_slower(self):
        tools = _tools(["libx264", "h264_qsv"])
        speeds = {"none": 7.2, "qsv": 5.4}
        tools.test_encoder = lambda name, seconds=10: {
            "ok": name in speeds, "speed": speeds.get(name, 0), "encoder": name,
            "detail": ""}
        tools._resolve_auto()
        self.assertEqual(tools._auto_choice, "none")

    def test_a_broken_encoder_never_wins(self):
        tools = _tools(["libx264", "h264_nvenc"])
        tools.test_encoder = lambda name, seconds=10: (
            {"ok": True, "speed": 3.0, "encoder": name, "detail": ""} if name == "none"
            else {"ok": False, "encoder": name, "detail": "Conversion failed!"})
        tools._resolve_auto()
        self.assertEqual(tools._auto_choice, "none")


class SettingsTests(unittest.TestCase):
    def test_every_offered_choice_is_accepted(self):
        for choice in HWACCEL_CHOICES:
            clean, errors = validate({"transcode": {"hwaccel": choice}})
            self.assertEqual(errors, [], choice)
            self.assertEqual(clean["transcode"]["hwaccel"], choice)

    def test_anything_else_is_refused(self):
        _, errors = validate({"transcode": {"hwaccel": "magic"}})
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
