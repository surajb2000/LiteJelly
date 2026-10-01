"""Regression checks for the costs measured with tools/measure_library.py.

Each test pins the mechanism behind a measured cost, by counting work rather
than timing it, so the checks hold on any machine.

Run with:  python -m unittest discover -s tests
"""

import gzip
import http.client
import json
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly import subtitles
from litejelly.config import MediaDir, load_config
from litejelly.ffmpeg import MediaInfo
from litejelly.library import Library, Video
from litejelly.web import Application, _accepts_gzip, create_server

logging.getLogger("litejelly").setLevel(logging.CRITICAL)

SRT = "1\n00:00:01,000 --> 00:00:02,000\nline\n"


class SidecarDiscoveryTests(unittest.TestCase):
    """Measured 2026-10-01: discovering subtitles for a film in a folder of
    3,334 videos took 2,529 ms per playback start, because every file was
    resolved and statted before its extension was checked. After filtering
    by name first it took 62 ms."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        for index in range(300):
            (self.folder / f"Film {index:03d}.mkv").write_bytes(b"x")
        self.video = self.folder / "Film 000.mkv"

    def test_unrelated_files_are_never_resolved(self):
        real = subtitles.is_within
        with mock.patch("litejelly.subtitles.is_within", side_effect=real) as checked:
            self.assertEqual(subtitles.discover(self.video, MediaInfo()), [])
        self.assertLessEqual(checked.call_count, 2, "only candidate names may reach the filesystem")

    def test_sidecars_and_subs_folders_are_still_found_in_order(self):
        (self.folder / "Film 000.en.srt").write_text(SRT, encoding="utf-8")
        (self.folder / "Film 000.fr.srt").write_text(SRT, encoding="utf-8")
        (self.folder / "Film 001.de.srt").write_text(SRT, encoding="utf-8")
        (self.folder / "Film 000.empty.srt").write_bytes(b"")
        (self.folder / "Subs").mkdir()
        (self.folder / "Subs" / "track.es.srt").write_text(SRT, encoding="utf-8")
        (self.folder / "Extras").mkdir()
        (self.folder / "Extras" / "Film 000.it.srt").write_text(SRT, encoding="utf-8")
        tracks = subtitles.discover(self.video, MediaInfo())
        self.assertEqual([(t.id, t.label) for t in tracks],
                         [("ext:0", "English"), ("ext:1", "French"), ("ext:2", "Spanish")])


class LibraryListingTests(unittest.TestCase):
    """Measured 2026-10-01: building the public fields took 97 ms of each
    /api/library request at 10,000 files, although they only change per scan."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for name in ("Show.S01E01.mkv", "Show.S01E02.mkv", "Film.2020.mkv"):
            (root / name).write_bytes(b"x")
        self.root = root
        self.library = Library([MediaDir(str(root), "mixed")])
        self.library.scan(force=True)

    def test_the_listing_is_built_once_per_scan(self):
        with mock.patch.object(Video, "to_dict", autospec=True,
                               side_effect=Video.to_dict) as build:
            first = self.library.listing()
            second = self.library.listing()
        self.assertIs(first, second)
        self.assertEqual(build.call_count, 3)

    def test_a_new_scan_replaces_the_listing(self):
        before = self.library.listing()
        (self.root / "Show.S01E03.mkv").write_bytes(b"x")
        self.library.scan(force=True)
        after = self.library.listing()
        self.assertEqual(len(before), 3)
        self.assertEqual(len(after), 4)
        self.assertNotIn("path", after[0], "the listing stays the public view")


class CompressionTests(unittest.TestCase):
    """Measured 2026-10-01: a 10,000-file library is 5,436 KB of JSON and
    442 KB gzipped at level 1."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        media = root / "media"
        media.mkdir()
        for index in range(60):
            (media / f"Show.S01E{index + 1:02d}.mkv").write_bytes(b"x")
        (root / "config.json").write_text(json.dumps(
            {"host": "127.0.0.1", "port": 0, "media_dirs": [str(media)]}), encoding="utf-8")
        config, _ = load_config(root)
        cls.app = Application(config)
        cls.app.library.scan(force=True)
        cls.server = create_server(cls.app)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.shutdown()
        cls._tmp.cleanup()

    def get(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response, response.read()
        finally:
            connection.close()

    def test_a_large_listing_is_gzipped_for_clients_that_accept_it(self):
        _, plain = self.get("/api/library")
        response, body = self.get("/api/library", {"Accept-Encoding": "gzip, deflate"})
        self.assertEqual(response.getheader("Content-Encoding"), "gzip")
        self.assertEqual(response.getheader("Vary"), "Accept-Encoding")
        self.assertEqual(int(response.getheader("Content-Length")), len(body))
        self.assertLess(len(body), len(plain))
        self.assertEqual(json.loads(gzip.decompress(body))["videos"], json.loads(plain)["videos"])

    def test_clients_without_gzip_get_plain_json(self):
        for header in (None, "identity", "gzip;q=0", "br"):
            with self.subTest(header=header):
                response, body = self.get("/api/library",
                                          {"Accept-Encoding": header} if header else None)
                self.assertIsNone(response.getheader("Content-Encoding"))
                self.assertEqual(len(json.loads(body)["videos"]), 60)

    def test_small_replies_are_not_compressed(self):
        response, body = self.get("/api/config", {"Accept-Encoding": "gzip"})
        self.assertIsNone(response.getheader("Content-Encoding"))
        self.assertIn("server_name", json.loads(body))

    def test_accept_encoding_parsing(self):
        for header, expected in (("gzip", True), ("GZIP", True), ("deflate, gzip;q=0.5", True),
                                 ("gzip;q=0", False), ("gzip; q=0.0", False),
                                 ("gzip;q=bad", False), ("x-gzip", False), ("", False),
                                 (None, False)):
            with self.subTest(header=header):
                self.assertEqual(_accepts_gzip(header), expected)


if __name__ == "__main__":
    unittest.main()
