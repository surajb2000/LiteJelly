"""Adding a subtitle file from the player.

Writing into a media folder is a different kind of operation from anything
else the player does, so the rules are pinned here rather than left to
inspection: an admin session is required, the file has to look like a subtitle
before it is written, and the name on disk is built from the video's own path
rather than from anything the client sent.

Run with:  python -m unittest discover -s tests
"""

import base64
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly import subtitles

STATIC = Path(__file__).resolve().parent.parent / "static"

SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:03,000\n"
    "Hello there.\n\n"
    "2\n"
    "00:00:04,500 --> 00:00:06,000\n"
    "General Kenobi.\n"
)
VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello there.\n"
ASS = (
    "[Script Info]\nTitle: Test\n\n"
    "[V4+ Styles]\nFormat: Name\n\n"
    "[Events]\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Hello\n"
)


class SniffTests(unittest.TestCase):
    def test_subrip_is_recognised(self):
        self.assertEqual(subtitles.sniff_format(SRT), "srt")

    def test_webvtt_is_recognised(self):
        self.assertEqual(subtitles.sniff_format(VTT), "vtt")

    def test_substation_is_recognised(self):
        self.assertEqual(subtitles.sniff_format(ASS), "ass")

    def test_a_byte_order_mark_does_not_hide_the_header(self):
        self.assertEqual(subtitles.sniff_format("\ufeffWEBVTT\n\n"), "vtt")

    def test_dots_instead_of_commas_still_read_as_subrip(self):
        # Plenty of files in the wild use the WebVTT separator in an .srt.
        self.assertEqual(
            subtitles.sniff_format("1\n00:00:01.000 --> 00:00:03.000\nHi\n"), "srt")

    def test_something_that_is_not_a_subtitle_is_refused(self):
        self.assertIsNone(subtitles.sniff_format("#!/bin/sh\nrm -rf /\n"))
        self.assertIsNone(subtitles.sniff_format("<html><body>nope</body></html>"))
        self.assertIsNone(subtitles.sniff_format(""))

    def test_an_executable_named_like_a_subtitle_is_still_refused(self):
        # The extension the browser sends is not evidence of anything.
        self.assertIsNone(subtitles.sniff_format("MZ\x90\x00\x03\x00\x00\x00"))


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.video = self.root / "The Long Road (2019).mkv"
        self.video.write_bytes(b"not really a video")

    def tearDown(self):
        self.dir.cleanup()

    def test_it_lands_beside_the_video_with_the_language_in_the_name(self):
        saved = subtitles.save_sidecar(self.video, SRT.encode("utf-8"), "en")
        self.assertEqual(saved.parent, self.root)
        self.assertEqual(saved.name, "The Long Road (2019).en.srt")

    def test_the_extension_comes_from_the_content_not_the_claim(self):
        saved = subtitles.save_sidecar(self.video, VTT.encode("utf-8"), "en")
        self.assertEqual(saved.suffix, ".vtt")

    def test_an_unknown_language_is_marked_rather_than_guessed(self):
        saved = subtitles.save_sidecar(self.video, SRT.encode("utf-8"), "")
        self.assertEqual(saved.name, "The Long Road (2019).und.srt")

    def test_a_second_file_does_not_overwrite_the_first(self):
        first = subtitles.save_sidecar(self.video, SRT.encode("utf-8"), "en")
        second = subtitles.save_sidecar(self.video, SRT.encode("utf-8"), "en")
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file() and second.is_file())

    def test_a_language_cannot_carry_a_path_into_the_name(self):
        for attempt in ("../../etc/passwd", "en/../..", "..", "e n", "EN-US-x"):
            saved = subtitles.save_sidecar(self.video, SRT.encode("utf-8"), attempt)
            self.assertEqual(saved.parent, self.root)
            self.assertNotIn("..", saved.name)

    def test_a_file_that_is_not_a_subtitle_is_refused(self):
        with self.assertRaises(ValueError):
            subtitles.save_sidecar(self.video, b"#!/bin/sh\nrm -rf /\n", "en")

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(ValueError):
            subtitles.save_sidecar(self.video, b"", "en")

    def test_an_oversized_file_is_refused(self):
        payload = SRT.encode("utf-8") + b"x" * subtitles.MAX_SUBTITLE_BYTES
        with self.assertRaises(ValueError):
            subtitles.save_sidecar(self.video, payload, "en")

    def test_a_windows_encoded_file_is_stored_as_utf8(self):
        # The point of decoding on the server: the browser would have to guess.
        payload = SRT.replace("Hello there.", "Caf\xe9 ouvert").encode("cp1252")
        saved = subtitles.save_sidecar(self.video, payload, "fr")
        self.assertIn("Caf\xe9 ouvert", saved.read_text(encoding="utf-8"))

    def test_a_crlf_file_does_not_gain_a_second_carriage_return(self):
        """Measured in a browser: the cues kept their timings and lost every
        line of text. Writing text that already held \\r\\n on Windows turned
        it into \\r\\r\\n, and the stray \\r reads as the blank line that ends
        a cue, so each cue was left with no payload at all."""
        saved = subtitles.save_sidecar(
            self.video, SRT.replace("\n", "\r\n").encode("utf-8"), "en")
        raw = saved.read_bytes()
        self.assertNotIn(b"\r", raw)
        # The text must still sit directly under its timing.
        self.assertIn(b"00:00:01,000 --> 00:00:03,000\nHello there.", raw)

    def test_nothing_half_written_is_left_behind(self):
        subtitles.save_sidecar(self.video, SRT.encode("utf-8"), "en")
        leftovers = [p.name for p in self.root.iterdir() if p.suffix == ".part"]
        self.assertEqual(leftovers, [])

    def test_the_saved_file_is_discoverable_and_gets_an_id(self):
        saved = subtitles.save_sidecar(self.video, SRT.encode("utf-8"), "en")
        self.assertTrue(subtitles.track_id_for(self.video, saved).startswith("ext:"))


class CacheTests(unittest.TestCase):
    """The converted VTT is cached, and "ext:0" is not a stable name."""

    def setUp(self):
        self.dir = TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.video = self.root / "Harbour Lights (2021).mkv"
        self.video.write_bytes(b"not really a video")
        self.service = subtitles.SubtitleService(None, self.root / "cache")

    def tearDown(self):
        self.dir.cleanup()

    def test_replacing_the_first_sidecar_changes_the_cache_key(self):
        # The video's mtime does not move when a subtitle is added beside it,
        # so keying on that alone served the previous conversion for the new
        # file - which is exactly what an upload does.
        first = subtitles.save_sidecar(self.video, SRT.encode("utf-8"), "en")
        before = self.service._cache_path(self.video, "ext:0")
        first.unlink()

        other = SRT.replace("Hello there.", "Something else entirely.")
        subtitles.save_sidecar(self.video, other.encode("utf-8"), "en")
        after = self.service._cache_path(self.video, "ext:0")
        self.assertNotEqual(before, after)

    def test_an_embedded_track_keeps_a_stable_key(self):
        # Nothing about an embedded track changes unless the video does.
        one = self.service._cache_path(self.video, "emb:2")
        two = self.service._cache_path(self.video, "emb:2")
        self.assertEqual(one, two)


class NameTests(unittest.TestCase):
    def test_a_language_is_taken_from_the_uploaded_name(self):
        self.assertEqual(subtitles.language_from_name("Movie.2019.eng.srt"), "en")
        self.assertEqual(subtitles.language_from_name("Movie.french.srt"), "fr")

    def test_a_name_with_no_language_says_so(self):
        self.assertEqual(subtitles.language_from_name("subtitles.srt"), "")


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.web = (Path(__file__).resolve().parent.parent
                    / "litejelly" / "web.py").read_text(encoding="utf-8")

    def test_the_upload_requires_an_admin_session(self):
        # It writes into a media folder, so the Origin check the progress
        # route uses is not enough on its own.
        handler = self.web[self.web.index("def subtitle_upload"):]
        handler = handler[:handler.index("send_json")]
        self.assertIn("h.require_admin(query, write=True)", handler)

    def test_the_upload_has_its_own_size_limit(self):
        handler = self.web[self.web.index("def subtitle_upload"):]
        self.assertIn("limit=MAX_UPLOAD_BYTES", handler[:handler.index("send_json")])
        # Raising the shared limit would loosen every other write route.
        self.assertIn("MAX_BODY_BYTES = 64 * 1024", self.web)

    def test_base64_is_decoded_strictly(self):
        handler = self.web[self.web.index("def subtitle_upload"):]
        self.assertIn("validate=True", handler[:handler.index("send_json")])


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.code = (STATIC / "app.js").read_text(encoding="utf-8")

    def test_adding_is_offered_when_there_are_no_tracks(self):
        # A file with no subtitles at all is exactly when you need to add one,
        # and the menu used to return before it could offer anything.
        render = self.code[self.code.index("function renderSubtitleMenu"):
                           self.code.index("function appendSubtitleSources")]
        self.assertIn("appendSubtitleSources(menu);", render)
        self.assertNotIn("return;", render.split("popup-empty")[1])

    def test_a_signed_out_viewer_is_told_what_to_do(self):
        upload = self.code[self.code.index("async function uploadSubtitle"):]
        upload = upload[:upload.index("function adoptSubtitleTracks")]
        self.assertIn("401", upload)
        self.assertIn("403", upload)
        self.assertIn("admin page", upload)

    def test_the_new_track_list_replaces_the_old_one(self):
        # Adding a sidecar renumbers every ext: id, so patching the list in
        # place would leave the ids pointing at the wrong files.
        adopt = self.code[self.code.index("function adoptSubtitleTracks"):]
        adopt = adopt[:adopt.index("async function selectSubtitle")]
        self.assertIn("clearSubtitleTracks()", adopt)
        self.assertIn("attachSubtitleTracks()", adopt)


if __name__ == "__main__":
    unittest.main()
