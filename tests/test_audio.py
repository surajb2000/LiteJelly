"""Audio track selection: probing, planning, mapping and the API payload.

A dual-audio release is the common case for anime, where the file carries the
original and the dub and ffmpeg's default choice is often the wrong one.

Run with:  python -m unittest discover -s tests
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.config import TranscodeSettings
from litejelly.ffmpeg import AudioStream, FFmpegTools, MediaInfo, stream_mime
from litejelly.web import _audio_index, _audio_tracks


def _tools():
    tools = FFmpegTools(Path("."))
    tools.ffmpeg = "ffmpeg"
    tools.ffprobe = "ffprobe"
    return tools


def _payload(*audio_streams):
    return {
        "format": {"duration": "600.0", "format_name": "matroska,webm"},
        "streams": [{
            "codec_type": "video", "codec_name": "h264", "profile": "High",
            "level": 40, "width": 1920, "height": 1080, "has_b_frames": 2,
            "avg_frame_rate": "24000/1001",
        }] + list(audio_streams),
    }


def _audio(name, language, channels=2, default=False, title="", profile=""):
    return {
        "codec_type": "audio", "codec_name": name, "channels": channels,
        "profile": profile, "tags": {"language": language, "title": title},
        "disposition": {"default": 1 if default else 0},
    }


class TrackParsingTests(unittest.TestCase):
    """ffprobe returns every stream; only the first audio one used to survive."""

    def setUp(self):
        self.tools = _tools()

    def _info(self, payload):
        # Exercise the real parser by faking only the subprocess boundary.
        import subprocess
        from unittest import mock

        completed = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(payload).encode(), stderr=b"")
        with mock.patch("litejelly.ffmpeg.run_quiet", return_value=completed):
            return self.tools._probe_uncached(Path("show.mkv"))

    def test_every_audio_stream_is_kept(self):
        info = self._info(_payload(
            _audio("aac", "jpn", default=True),
            _audio("ac3", "eng", channels=6),
        ))
        self.assertEqual([t.index for t in info.audios], [0, 1])
        self.assertEqual([t.language for t in info.audios], ["jpn", "eng"])
        self.assertEqual([t.channels for t in info.audios], [2, 6])

    def test_summary_fields_follow_the_default_track(self):
        info = self._info(_payload(
            _audio("ac3", "eng"),
            _audio("aac", "jpn", default=True, profile="LC"),
        ))
        self.assertEqual(info.audio_codec, "aac")
        self.assertEqual(info.audio_profile, "LC")

    def test_first_track_wins_when_none_is_marked_default(self):
        info = self._info(_payload(_audio("ac3", "eng"), _audio("aac", "jpn")))
        self.assertEqual(info.audio_codec, "ac3")
        self.assertEqual(info.audio(None).index, 0)

    def test_video_only_file_has_no_audio(self):
        info = self._info(_payload())
        self.assertEqual(info.audios, [])
        self.assertIsNone(info.audio())
        self.assertEqual(info.audio_codec, "")


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tools = _tools()
        self.settings = TranscodeSettings()
        self.info = MediaInfo(
            duration=600.0, container="mp4", video_codec="h264", profile="High",
            level=40, width=1920, height=1080, probed=True,
            audios=[
                AudioStream(0, "aac", profile="LC", language="jpn", default=True),
                AudioStream(1, "ac3", language="eng", channels=6),
            ],
            audio_codec="aac", audio_profile="LC",
        )

    def test_default_track_still_direct_plays(self):
        self.assertEqual(self.tools.plan_playback(self.info).mode, "direct")

    def test_choosing_the_other_track_leaves_direct_play(self):
        # The browser plays the file as muxed; another track needs ffmpeg.
        plan = self.tools.plan_playback(self.info, audio_index=1)
        self.assertEqual(plan.mode, "remux")
        self.assertEqual(plan.video_action, "copy")
        self.assertEqual(plan.audio_action, "encode")

    def test_choosing_the_default_track_explicitly_is_not_a_change(self):
        self.assertEqual(self.tools.plan_playback(self.info, audio_index=0).mode, "direct")

    def test_plan_uses_the_selected_codec(self):
        self.info.audios[1] = AudioStream(1, "aac", profile="LC", language="eng")
        plan = self.tools.plan_playback(self.info, audio_index=1)
        self.assertEqual(plan.audio_action, "copy")

    def test_command_maps_the_selected_track(self):
        plan = self.tools.plan_playback(self.info, audio_index=1)
        cmd = self.tools.build_stream_command(
            Path("show.mkv"), plan, self.settings, info=self.info, audio_index=1)
        self.assertIn("0:a:1?", cmd)
        self.assertNotIn("0:a:0?", cmd)

    def test_command_maps_the_first_track_by_default(self):
        plan = self.tools.plan_playback(self.info)
        cmd = self.tools.build_stream_command(
            Path("show.mkv"), plan, self.settings, info=self.info)
        self.assertIn("0:a:0?", cmd)

    def test_burned_in_subtitles_still_map_the_selected_track(self):
        plan = self.tools.plan_playback(self.info, audio_index=1)
        cmd = self.tools.build_stream_command(
            Path("show.mkv"), plan, self.settings, info=self.info,
            burn_subtitle_index=0, audio_index=1)
        self.assertIn("0:a:1?", cmd)

    def test_mime_describes_the_selected_track(self):
        plan = self.tools.plan_playback(self.info, audio_index=1)
        self.assertEqual(stream_mime(self.info, plan, self.settings, False, 1),
                         'video/mp4; codecs="avc1.640028, mp4a.40.2"')


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.info = MediaInfo(
            probed=True,
            audios=[
                AudioStream(0, "aac", language="jpn", channels=2, default=True),
                AudioStream(1, "ac3", language="eng", channels=6, title="Commentary"),
                AudioStream(2, "dts", language="", channels=8),
            ],
        )

    def test_unknown_index_falls_back_to_the_default(self):
        for raw in ("", "9", "-1", "two", None):
            query = {} if raw is None else {"audio": [raw]}
            self.assertIsNone(_audio_index(self.info, query))

    def test_known_index_is_accepted(self):
        self.assertEqual(_audio_index(self.info, {"audio": ["1"]}), 1)

    def test_labels_name_the_language_and_layout(self):
        labels = [t["label"] for t in _audio_tracks(self.info)]
        self.assertEqual(labels[0], "Japanese \u00b7 Stereo")
        self.assertEqual(labels[1], "Commentary \u00b7 English \u00b7 5.1")
        self.assertEqual(labels[2], "Track 3 \u00b7 7.1")

    def test_payload_carries_what_the_menu_needs(self):
        track = _audio_tracks(self.info)[1]
        self.assertEqual(track["index"], 1)
        self.assertEqual(track["codec"], "ac3")
        self.assertEqual(track["language"], "English")
        self.assertFalse(track["default"])


if __name__ == "__main__":
    unittest.main()
