"""Drive the real admin page against fixture APIs.

The key status used to read "Ready" after one typed letter. Only the provider's
answer, relayed by /api/admin/metadata/test, may say that now.
"""

from __future__ import annotations

import json
import mimetypes
import unittest
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://litejelly.test"
SETTINGS = {
    "server_name": "LiteJelly", "port": 8000, "host": "0.0.0.0", "media_dirs": [],
    "scan_interval": 60, "thumbnail_workers": 2, "allow_hevc_direct": False,
    "trickplay": True, "trickplay_interval": 10, "online_metadata": True,
    "tmdb_api_key": "", "omdb_api_key": "saved-but-wrong", "stream_buffer_mb": 8,
    "ffmpeg_path": "", "ffprobe_path": "", "log_verbosity": "info", "log_to_file": True,
    "log_to_console": False, "log_max_mb": 2, "log_backups": 3,
    "transcode": {"preset": "veryfast", "hwaccel": "none", "crf": 22, "max_concurrent": 2,
                  "max_video_bitrate": "3000k", "audio_bitrate": "160k",
                  "resolution": "1280x720"},
}
KEY_ANSWERS = {
    "good": {"ok": True, "state": "valid", "detail": ""},
    "offline": {"ok": False, "state": "failed", "detail": "TMDb: Could not reach the service"},
}


class AdminKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Launch one headless browser without touching the user's Chrome profile."""
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        """Isolate storage and API history, and fail on any page exception."""
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.key_checks = []
        self.sign_in_ok = True
        self.account = {"configured": True, "username": "viewer", "has_key": True,
                        "has_password": True, "verified": False, "error": ""}
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.context.route("**/*", self.respond)

    def tearDown(self):
        artifacts = ROOT / "test-results" / "browser"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(artifacts / f"{self._testMethodName}.png"))
        self.assertEqual(self.errors, [])

    def respond(self, route):
        """Serve repository assets and fixture admin APIs; nothing reaches a real server."""
        parsed = urlparse(route.request.url)
        if parsed.netloc != "litejelly.test":
            self.errors.append("Unexpected external request: " + parsed.netloc)
            route.abort()
            return
        if parsed.path == "/admin" or parsed.path.startswith("/static/"):
            relative = "admin.html" if parsed.path == "/admin" else parsed.path[len("/static/"):]
            target = (ROOT / "static" / relative).resolve()
            if ROOT / "static" not in target.parents or not target.is_file():
                route.fulfill(status=404, body="Missing test asset")
                return
            route.fulfill(status=200, body=target.read_bytes(),
                          content_type=mimetypes.guess_type(target.name)[0] or "text/plain")
            return
        if parsed.path == "/api/admin/session":
            data = {"state": "ready", "username": "admin"}
        elif parsed.path == "/api/admin/settings":
            data = {"settings": SETTINGS, "content_types": ["mixed"], "presets": ["veryfast"],
                    "hwaccels": ["none"], "restart_required_fields": ["port", "host"],
                    "library": {"count": 0}, "metadata": {"enabled": True, "records": 0,
                                                          "shows": []},
                    "ffmpeg": {"available": True}, "version": "test"}
        elif parsed.path == "/api/admin/metadata/test":
            body = json.loads(route.request.post_data or "{}")
            self.key_checks.append((body.get("provider"), body.get("key")))
            data = KEY_ANSWERS.get(body.get("key"), {
                "ok": False, "state": "rejected", "detail": "TMDb: Invalid API key"})
        elif parsed.path == "/api/admin/opensubtitles":
            data = {"ok": True, "opensubtitles": self.account}
        elif parsed.path == "/api/admin/opensubtitles/test":
            if self.sign_in_ok:
                data = {"ok": True, "username": "viewer"}
            else:
                route.fulfill(status=502, json={"ok": False,
                                                "error": "You cannot consume this service"})
                return
        elif parsed.path == "/favicon.ico":
            route.fulfill(status=404, body="")
            return
        else:
            self.errors.append("Unexpected fixture route: " + parsed.path)
            route.abort()
            return
        route.fulfill(json=data)

    def open_admin(self):
        self.page.goto(ORIGIN + "/admin")
        expect(self.page.locator("#tmdb_api_key")).to_be_visible()

    def test_typing_a_key_is_not_called_ready(self):
        self.open_admin()
        field = self.page.locator("#tmdb_api_key")
        pill = self.page.locator("#tmdb-status")
        expect(pill).to_have_text("Needs key")
        field.press_sequentially("x")
        expect(pill).to_have_text("Not checked")
        self.assertNotIn(("tmdb", "x"), self.key_checks, "typing alone must not send the key")

    def test_only_the_providers_answer_decides(self):
        self.open_admin()
        field = self.page.locator("#tmdb_api_key")
        pill = self.page.locator("#tmdb-status")
        for key, shown in (("good", "Ready"), ("wrong", "Rejected"), ("offline", "Couldn\u2019t check")):
            with self.subTest(key=key):
                field.fill(key)
                field.press("Tab")
                expect(pill).to_have_text(shown)
        expect(self.page.locator("#tmdb-check")).to_contain_text("Could not reach")

    def test_a_saved_key_is_checked_when_the_page_opens(self):
        self.open_admin()
        expect(self.page.locator("#omdb-status")).to_have_text("Rejected")
        expect(self.page.locator("#omdb-check")).to_contain_text("Invalid API key")
        self.assertIn(("omdb", "saved-but-wrong"), self.key_checks)
        self.assertNotIn("tmdb", [provider for provider, _key in self.key_checks])

    def test_test_button_rechecks_the_same_key(self):
        self.open_admin()
        self.page.locator("#omdb-test").click()
        expect(self.page.locator("#omdb-status")).to_have_text("Rejected")
        self.assertEqual(self.key_checks.count(("omdb", "saved-but-wrong")), 2)

    def test_opensubtitles_is_ready_only_after_signing_in(self):
        self.open_admin()
        state = self.page.locator("#os-state")
        expect(state).to_have_text("Saved, not checked yet")
        self.page.locator("#os-test").click()
        expect(state).to_have_text("Ready, signed in as viewer")
        self.sign_in_ok = False
        self.page.locator("#os-test").click()
        expect(state).to_have_text("Sign-in failed: You cannot consume this service")


if __name__ == "__main__":
    unittest.main()
