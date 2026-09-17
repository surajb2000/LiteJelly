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

from litejelly import auth
from litejelly.config import load_config
from litejelly.ffmpeg import MediaInfo
from litejelly.web import Application, create_server

for name in ("litejelly", "litejelly.web", "litejelly.library", "litejelly.admin",
             "litejelly.auth", "litejelly.thumbnails"):
    logging.getLogger(name).setLevel(logging.CRITICAL)

ADMIN_USER = "testadmin"
ADMIN_PASSWORD = "test-password-123"
WRITE_HEADERS = {"Content-Type": "application/json", "X-LiteJelly-Admin": "1"}


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

        # A real account, hashed cheaply so the suite stays quick.
        cls.app.credentials = auth.Credentials(
            username=ADMIN_USER,
            password_hash=auth.hash_password(ADMIN_PASSWORD, iterations=1000),
        )

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

    def setUp(self):
        self.app.throttle.record_success("127.0.0.1")

    def connect(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def sign_in(self, conn, password=ADMIN_PASSWORD, username=ADMIN_USER):
        """Returns the session cookie header value, or None when refused."""
        conn.request("POST", "/api/admin/login",
                     body=json.dumps({"username": username, "password": password}),
                     headers=WRITE_HEADERS)
        response = conn.getresponse()
        raw = response.getheader("Set-Cookie")
        response.read()
        if response.status != 200 or not raw:
            return None
        return raw.split(";")[0]

    def authed(self):
        conn = self.connect()
        cookie = self.sign_in(conn)
        self.assertIsNotNone(cookie, "sign-in failed")
        return conn, cookie

    def video_id(self, conn):
        conn.request("GET", "/api/library")
        return json.loads(conn.getresponse().read())["videos"][0]["id"]

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
        self.assertEqual(first.status, 401)  # not signed in
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

    def test_admin_is_unreachable_without_a_session(self):
        # Loopback is no longer enough; the account is the boundary now.
        conn = self.connect()
        conn.request("GET", "/api/admin/settings")
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        conn.close()

    def test_admin_is_reachable_once_signed_in(self):
        conn, cookie = self.authed()
        conn.request("GET", "/api/admin/settings", headers={"Cookie": cookie})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("media_dirs", json.loads(response.read())["settings"])
        conn.close()

    def test_wrong_password_is_refused(self):
        conn = self.connect()
        self.assertIsNone(self.sign_in(conn, password="not-the-password"))
        conn.close()

    def test_wrong_username_is_refused(self):
        conn = self.connect()
        self.assertIsNone(self.sign_in(conn, username="someone-else"))
        conn.close()

    def test_session_cookie_is_http_only(self):
        conn = self.connect()
        conn.request("POST", "/api/admin/login",
                     body=json.dumps({"username": ADMIN_USER,
                                      "password": ADMIN_PASSWORD}),
                     headers=WRITE_HEADERS)
        response = conn.getresponse()
        raw = response.getheader("Set-Cookie")
        response.read()
        self.assertIn("HttpOnly", raw)
        self.assertIn("SameSite=Strict", raw)
        conn.close()

    def test_forged_cookie_is_refused(self):
        conn = self.connect()
        conn.request("GET", "/api/admin/settings",
                     headers={"Cookie": "litejelly_admin=made-up-token"})
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        conn.close()

    def test_signing_out_invalidates_the_session(self):
        conn, cookie = self.authed()
        conn.request("POST", "/api/admin/logout", body="{}",
                     headers=dict(WRITE_HEADERS, Cookie=cookie))
        conn.getresponse().read()

        conn.request("GET", "/api/admin/settings", headers={"Cookie": cookie})
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        conn.close()

    def test_session_endpoint_reports_login_state(self):
        conn = self.connect()
        conn.request("GET", "/api/admin/session")
        payload = json.loads(conn.getresponse().read())
        self.assertEqual(payload["state"], "login")
        self.assertNotIn("password_hash", json.dumps(payload))
        conn.close()

    def test_setup_is_refused_once_an_account_exists(self):
        conn = self.connect()
        conn.request("POST", "/api/admin/setup",
                     body=json.dumps({"username": "intruder",
                                      "password": "password123"}),
                     headers=WRITE_HEADERS)
        response = conn.getresponse()
        self.assertEqual(response.status, 409)
        response.read()
        conn.close()
        self.assertEqual(self.app.credentials.username, ADMIN_USER)

    def test_admin_write_needs_a_same_origin_marker(self):
        conn, cookie = self.authed()
        conn.request("POST", "/api/admin/settings",
                     body=json.dumps({"server_name": "Den"}),
                     headers={"Content-Type": "application/json",
                              "Cookie": cookie,
                              "Origin": "http://evil.example"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_repeated_failures_lock_the_address_out(self):
        conn = self.connect()
        for _ in range(auth.MAX_FAILURES):
            self.sign_in(conn, password="wrong")
        conn.request("POST", "/api/admin/login",
                     body=json.dumps({"username": ADMIN_USER,
                                      "password": ADMIN_PASSWORD}),
                     headers=WRITE_HEADERS)
        response = conn.getresponse()
        # Even the right password is refused while locked out.
        self.assertEqual(response.status, 429)
        response.read()
        conn.close()

    def test_unknown_route_is_a_json_404(self):
        conn = self.connect()
        conn.request("GET", "/api/nope")
        response = conn.getresponse()
        self.assertEqual(response.status, 404)
        self.assertIn("error", json.loads(response.read()))
        conn.close()

    def test_logs_endpoint_reports_the_current_verbosity(self):
        conn, cookie = self.authed()
        conn.request("GET", "/api/admin/logs?lines=10", headers={"Cookie": cookie})
        payload = json.loads(conn.getresponse().read())
        self.assertIn("entries", payload)
        self.assertEqual(payload["verbosity_options"], ["info", "debug", "trace"])
        conn.close()

    def test_logs_endpoint_is_admin_guarded(self):
        # Log lines carry absolute paths, so they are not public.
        conn = self.connect()
        conn.request("GET", "/api/admin/logs")
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        conn.close()

    def test_log_line_limit_is_capped(self):
        conn, cookie = self.authed()
        conn.request("GET", "/api/admin/logs?lines=999999", headers={"Cookie": cookie})
        payload = json.loads(conn.getresponse().read())
        self.assertLessEqual(len(payload["entries"]), 2000)
        conn.close()

    def test_bad_line_count_falls_back(self):
        conn, cookie = self.authed()
        conn.request("GET", "/api/admin/logs?lines=plenty", headers={"Cookie": cookie})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()

    def test_directory_browser_is_admin_guarded(self):
        # Otherwise it is a filesystem listing for anyone on the network.
        conn = self.connect()
        conn.request("GET", "/api/admin/browse")
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        conn.close()

    def test_log_requests_are_not_themselves_logged(self):
        # Auto-refresh polls this endpoint; logging it would bury the content.
        conn, cookie = self.authed()
        conn.request("GET", "/api/admin/logs?lines=5", headers={"Cookie": cookie})
        first = json.loads(conn.getresponse().read())["entries"]
        conn.request("GET", "/api/admin/logs?lines=5", headers={"Cookie": cookie})
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

    def test_seekpoint_forwards_the_direction(self):
        conn = self.connect()
        conn.request("GET", "/api/library")
        video_id = json.loads(conn.getresponse().read())["videos"][0]["id"]

        tools = self.app.tools
        seen = []
        original = (tools.probe, tools.seek_landing, tools.ffmpeg)
        tools.probe = lambda path: MediaInfo(
            duration=600.0, container="matroska", video_codec="h264",
            audio_codec="aac", probed=True)
        tools.seek_landing = lambda path, target, forward=False: (
            seen.append(forward) or (50.0 if forward else 40.0))
        tools.ffmpeg = tools.ffmpeg or "ffmpeg"
        try:
            conn.request("GET", f"/api/seekpoint?id={video_id}&t=47.5&dir=forward")
            ahead = json.loads(conn.getresponse().read())
            conn.request("GET", f"/api/seekpoint?id={video_id}&t=47.5")
            behind = json.loads(conn.getresponse().read())
        finally:
            tools.probe, tools.seek_landing, tools.ffmpeg = original
            conn.close()

        self.assertEqual(seen, [True, False])
        self.assertEqual((ahead["start"], ahead["direction"]), (50.0, "forward"))
        self.assertEqual((behind["start"], behind["direction"]), (40.0, "backward"))

    def test_settings_export_is_admin_guarded(self):
        # settings.json names every media directory on the machine.
        conn = self.connect()
        conn.request("GET", "/api/admin/settings/export")
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        conn.close()

    def test_settings_export_downloads_a_named_file(self):
        conn, cookie = self.authed()
        conn.request("GET", "/api/admin/settings/export", headers={"Cookie": cookie})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        disposition = response.getheader("Content-Disposition") or ""
        payload = json.loads(response.read())
        conn.close()
        self.assertIn("attachment", disposition)
        self.assertIn(".json", disposition)
        self.assertIsInstance(payload, dict)
        self.assertNotIn("password_hash", json.dumps(payload))

    def test_settings_import_applies_a_backup(self):
        conn, cookie = self.authed()
        conn.request("POST", "/api/admin/settings/import",
                     body=json.dumps({"settings": {"server_name": "Restored"}}),
                     headers=dict(WRITE_HEADERS, Cookie=cookie))
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read())
        conn.close()
        self.assertTrue(payload["ok"])
        self.assertEqual(self.app.config.server_name, "Restored")

    def test_settings_import_refuses_rubbish(self):
        conn, cookie = self.authed()
        before = self.app.config.server_name
        conn.request("POST", "/api/admin/settings/import",
                     body=json.dumps({"settings": {"port": "not a port"}}),
                     headers=dict(WRITE_HEADERS, Cookie=cookie))
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn("errors", json.loads(response.read()))
        conn.close()
        self.assertEqual(self.app.config.server_name, before)

    def test_settings_import_is_admin_guarded(self):
        conn = self.connect()
        conn.request("POST", "/api/admin/settings/import",
                     body=json.dumps({"settings": {"server_name": "Hijacked"}}),
                     headers=WRITE_HEADERS)
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        conn.close()

    def test_metadata_endpoints_are_admin_guarded(self):
        conn = self.connect()
        for path in ("/api/admin/metadata/clear", "/api/admin/metadata/forget"):
            conn.request("POST", path, body="{}", headers=WRITE_HEADERS)
            response = conn.getresponse()
            self.assertEqual(response.status, 401, path)
            response.read()
        conn.close()

    def test_metadata_clear_reports_when_lookups_are_off(self):
        # Online metadata is opt-in, so there is nothing cached to clear.
        conn, cookie = self.authed()
        conn.request("POST", "/api/admin/metadata/clear", body="{}",
                     headers=dict(WRITE_HEADERS, Cookie=cookie))
        response = conn.getresponse()
        self.assertEqual(response.status, 503)
        response.read()
        conn.close()

    def test_admin_settings_lists_shows_for_the_cache_reset(self):
        conn, cookie = self.authed()
        conn.request("GET", "/api/admin/settings", headers={"Cookie": cookie})
        payload = json.loads(conn.getresponse().read())
        conn.close()
        meta = payload["metadata"]
        self.assertIn("records", meta)
        titles = [show["title"] for show in meta["shows"]]
        self.assertIn("Some Show", titles)

    def test_infinity_cannot_poison_the_library(self):
        """Measured: json.loads accepts a bare Infinity, max(0.0, inf) keeps it,
        SQLite stores it, and json.dumps writes it back out as Infinity, which
        no browser will parse. One unauthenticated POST broke every client's
        library until the row was deleted by hand."""
        conn = self.connect()
        video_id = self.video_id(conn)

        for bad in ('{"id": "%s", "position": 0, "duration": Infinity}' % video_id,
                    '{"id": "%s", "position": NaN, "duration": 1}' % video_id,
                    '{"id": "%s", "position": -Infinity, "duration": 1}' % video_id):
            conn.request("POST", "/api/progress", body=bad, headers=WRITE_HEADERS)
            response = conn.getresponse()
            self.assertEqual(response.status, 400, bad)
            response.read()

        conn.request("GET", "/api/library")
        payload = conn.getresponse().read().decode()
        conn.close()
        for token in ("Infinity", "NaN"):
            self.assertNotIn(token, payload, "the library must stay parseable")
        json.loads(payload, parse_constant=_no_constants)

    def test_a_huge_position_is_clamped_rather_than_stored(self):
        conn = self.connect()
        video_id = self.video_id(conn)
        conn.request("POST", "/api/progress",
                     body=json.dumps({"id": video_id, "position": 1e30,
                                      "duration": 1e30}),
                     headers=WRITE_HEADERS)
        saved = json.loads(conn.getresponse().read())
        conn.close()
        self.assertLess(saved["position"], 1e30)
        self.assertLess(saved["duration"], 1e30)

    def test_another_site_cannot_write_progress(self):
        # No account guards this route, so a page you visit could otherwise
        # post to the server on your network.
        conn = self.connect()
        video_id = self.video_id(conn)
        conn.request("POST", "/api/progress",
                     body=json.dumps({"id": video_id, "position": 10, "duration": 100}),
                     headers={"Content-Type": "application/json",
                              "Origin": "http://evil.example"})
        response = conn.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        conn.close()

    def test_another_site_cannot_trigger_a_rescan(self):
        conn = self.connect()
        conn.request("POST", "/api/rescan", body="{}",
                     headers={"Content-Type": "application/json",
                              "Origin": "http://evil.example"})
        response = conn.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        conn.close()

    def test_the_player_can_still_save_progress(self):
        # sendBeacon cannot set headers, so a same-origin post has to pass on
        # the Origin alone.
        conn = self.connect()
        video_id = self.video_id(conn)
        host = f"127.0.0.1:{self.port}"
        conn.request("POST", "/api/progress",
                     body=json.dumps({"id": video_id, "position": 30, "duration": 100}),
                     headers={"Content-Type": "text/plain", "Origin": f"http://{host}"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        conn.close()

    def test_rescan_is_not_free_to_ask_for(self):
        """A forced rescan walks every media folder, so one page could
        otherwise keep the disk busy indefinitely."""
        self.app._last_rescan = 0.0
        conn = self.connect()
        conn.request("POST", "/api/rescan", body="{}", headers=WRITE_HEADERS)
        first = conn.getresponse()
        self.assertEqual(first.status, 200)
        first.read()

        conn.request("POST", "/api/rescan", body="{}", headers=WRITE_HEADERS)
        second = conn.getresponse()
        self.assertEqual(second.status, 429)
        self.assertTrue(second.getheader("Retry-After"))
        second.read()
        conn.close()
        self.app._last_rescan = 0.0

    def test_an_admin_can_still_rescan_at_will(self):
        conn, cookie = self.authed()
        self.app._last_rescan = 0.0
        for _ in range(2):
            conn.request("POST", "/api/rescan", body="{}",
                         headers=dict(WRITE_HEADERS, Cookie=cookie))
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
        conn.close()
        self.app._last_rescan = 0.0

    def test_a_finished_stream_is_not_tracked(self):
        encoder = _FakeProcess()
        self.app.track_stream(encoder)
        self.app.forget_stream(encoder)
        self.assertNotIn(encoder, self.app._streams)


class ShutdownTests(unittest.TestCase):
    """Stopping the server does not reap ffmpeg on Windows, so an encoder could
    keep running against the media files after it exited."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "media").mkdir()
        (root / "config.json").write_text(json.dumps({
            "port": 0, "host": "127.0.0.1", "media_dirs": [str(root / "media")],
        }), encoding="utf-8")
        config, _ = load_config(root)
        config.cache_dir.mkdir(parents=True, exist_ok=True)
        self.app = Application(config)

    def tearDown(self):
        self._tmp.cleanup()

    def test_shutdown_reaps_a_running_encoder(self):
        encoder = _FakeProcess()
        self.app.track_stream(encoder)
        self.app.shutdown()
        self.assertTrue(encoder.terminated)

    def test_shutdown_without_streams_is_fine(self):
        self.app.shutdown()


class _FakeProcess:
    """Stands in for a running ffmpeg without spawning one."""

    stdout = None

    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _no_constants(name):
    raise AssertionError(f"library payload contained {name}")


if __name__ == "__main__":
    unittest.main()
