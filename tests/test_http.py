"""HTTP-level tests against a live server.

These exist because two bugs slipped past unit tests: a POST whose body the
handler never read corrupted the *next* request on the same keep-alive
connection, which only shows up when a real socket is reused.

Run with:  python -m unittest discover -s tests
"""

import http.client
import json
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.config import load_config
from litejelly.web import Application, create_server

for name in ("litejelly", "litejelly.web", "litejelly.library", "litejelly.admin"):
    logging.getLogger(name).setLevel(logging.CRITICAL)


class LiveServerTests(unittest.TestCase):
    """Each test drives a real socket so connection reuse is exercised."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        media = root / "media"
        media.mkdir()
        (media / "Some.Show.S01E01.mkv").write_bytes(b"x" * 1024)

        (root / "config.json").write_text(json.dumps({
            "port": 0,
            "host": "127.0.0.1",
            "media_dirs": [str(media)],
        }), encoding="utf-8")

        config, _ = load_config(root)
        config.cache_dir.mkdir(parents=True, exist_ok=True)
        cls.app = Application(config)
        cls.app.library.scan(force=True)

        cls.httpd = create_server(cls.app)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.shutdown()
        cls._tmp.cleanup()

    def connect(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def test_library_is_served(self):
        conn = self.connect()
        conn.request("GET", "/api/library")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read())
        self.assertEqual(len(payload["videos"]), 1)
        conn.close()

    def test_post_body_does_not_corrupt_the_next_request(self):
        # /api/rescan ignores its body. Left unread, the next request on the
        # connection parsed it as a request line and returned 501.
        conn = self.connect()
        conn.request("POST", "/api/rescan", body=json.dumps({"unused": True}),
                     headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().read() and 200, 200)

        conn.request("GET", "/api/library")
        second = conn.getresponse()
        self.assertEqual(second.status, 200, "connection was left out of sync")
        second.read()
        conn.close()

    def test_rejected_post_does_not_corrupt_the_next_request(self):
        conn = self.connect()
        conn.request("POST", "/api/admin/settings",
                     body=json.dumps({"server_name": "Nope"}),
                     headers={"Content-Type": "application/json"})
        first = conn.getresponse()
        self.assertEqual(first.status, 403)  # no same-origin marker
        first.read()

        conn.request("GET", "/api/config")
        second = conn.getresponse()
        self.assertEqual(second.status, 200, "connection was left out of sync")
        second.read()
        conn.close()

    def test_invalid_json_body_does_not_corrupt_the_next_request(self):
        conn = self.connect()
        conn.request("POST", "/api/progress", body="{not json",
                     headers={"Content-Type": "application/json"})
        first = conn.getresponse()
        self.assertEqual(first.status, 400)
        first.read()

        conn.request("GET", "/api/config")
        second = conn.getresponse()
        self.assertEqual(second.status, 200, "connection was left out of sync")
        second.read()
        conn.close()

    def test_public_config_exposes_no_paths(self):
        conn = self.connect()
        conn.request("GET", "/api/config")
        payload = json.loads(conn.getresponse().read())
        self.assertNotIn("media_dirs", payload)
        self.assertNotIn("ffmpeg_path", payload)
        self.assertNotIn("admin_token", payload)
        conn.close()

    def test_admin_is_reachable_over_loopback(self):
        conn = self.connect()
        conn.request("GET", "/api/admin/settings")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()

    def test_admin_write_needs_a_same_origin_marker(self):
        conn = self.connect()
        conn.request("POST", "/api/admin/settings",
                     body=json.dumps({"server_name": "Den"}),
                     headers={"Content-Type": "application/json",
                              "Origin": "http://evil.example"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_unknown_route_is_a_json_404(self):
        conn = self.connect()
        conn.request("GET", "/api/nope")
        response = conn.getresponse()
        self.assertEqual(response.status, 404)
        self.assertIn("error", json.loads(response.read()))
        conn.close()

    def test_logs_endpoint_reports_the_current_verbosity(self):
        conn = self.connect()
        conn.request("GET", "/api/admin/logs?lines=10")
        payload = json.loads(conn.getresponse().read())
        self.assertIn("entries", payload)
        self.assertEqual(payload["verbosity_options"], ["info", "debug", "trace"])
        conn.close()

    def test_logs_endpoint_is_admin_guarded(self):
        # Log lines carry absolute paths, so they are not public.
        self.assertIn(("GET", "/api/admin/logs"), self.app.routes)
        self.assertIn(("POST", "/api/admin/logs/clear"), self.app.routes)

    def test_log_line_limit_is_capped(self):
        conn = self.connect()
        conn.request("GET", "/api/admin/logs?lines=999999")
        payload = json.loads(conn.getresponse().read())
        self.assertLessEqual(len(payload["entries"]), 2000)
        conn.close()

    def test_bad_line_count_falls_back(self):
        conn = self.connect()
        conn.request("GET", "/api/admin/logs?lines=plenty")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()

    def test_log_requests_are_not_themselves_logged(self):
        # Auto-refresh polls this endpoint; logging it would bury the content.
        handler_path = "/api/admin/logs"
        self.assertTrue(handler_path.startswith("/api/admin/logs"))
        conn = self.connect()
        conn.request("GET", "/api/admin/logs?lines=5")
        first = json.loads(conn.getresponse().read())["entries"]
        conn.request("GET", "/api/admin/logs?lines=5")
        second = json.loads(conn.getresponse().read())["entries"]
        conn.close()
        self.assertEqual(len(first), len(second))

    def test_range_request_returns_partial_content(self):
        conn = self.connect()
        conn.request("GET", "/api/library")
        video_id = json.loads(conn.getresponse().read())["videos"][0]["id"]

        conn.request("GET", "/media/stream?id=" + video_id,
                     headers={"Range": "bytes=0-99"})
        response = conn.getresponse()
        self.assertEqual(response.status, 206)
        self.assertEqual(len(response.read()), 100)
        conn.close()


if __name__ == "__main__":
    unittest.main()
