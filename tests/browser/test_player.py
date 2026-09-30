"""Exercise the real client with deterministic API replies and a simulated media clock.

Run separately with the optional Playwright environment. These tests do not
certify codec decoding, GPU composition, or playback on a physical device.
"""

from __future__ import annotations

import mimetypes
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://litejelly.test"
VIDEO = {
    "id": "fixture", "title": "Fixture Film", "name": "Fixture Film",
    "filename": "Fixture Film.mkv", "category": "movies", "series_id": "",
    "extension": "mkv", "size": 1024, "modified_ts": 1,
    "modified": "2026-01-01T00:00:00Z", "genres": [],
}

MEDIA_CLOCK = """
(() => {
  const media = { time: 0, paused: true, width: 1920, height: 816, source: '', loads: [] };
  window.fixtureMedia = media;
  Object.defineProperties(HTMLMediaElement.prototype, {
    currentTime: { get() { return media.time; }, set(value) { media.time = value; } },
    paused: { get() { return media.paused; } },
    duration: { get() { return 600; } },
    readyState: { get() { return 4; } },
    buffered: { get() { return { length: 0 }; } },
    src: { get() { return media.source; }, set(value) { media.source = value; } }
  });
  Object.defineProperties(HTMLVideoElement.prototype, {
    videoWidth: { get() { return media.width; } },
    videoHeight: { get() { return media.height; } }
  });
  HTMLMediaElement.prototype.load = function () {
    media.time = 0;
    media.loads.push(media.source);
  };
  HTMLMediaElement.prototype.play = function () {
    media.paused = false;
    this.dispatchEvent(new Event('play'));
    return Promise.resolve();
  };
  HTMLMediaElement.prototype.pause = function () {
    media.paused = true;
    this.dispatchEvent(new Event('pause'));
  };
})();
"""


def playback_plan(query: dict) -> dict:
    """Return a direct initial plan or a classic-stream restart with a stable resume point."""
    level = query.get("level", ["off"])[0]
    return {
        "id": "fixture", "title": "Fixture Film", "duration": 600,
        "width": 1920, "height": 816, "mode": "direct" if level == "off" else "remux",
        "native_seek": level == "off", "exact_seek": level == "off", "mime": "",
        "url": "/media/fixture?level=" + level, "badge": "Fixture",
        "video_action": "copy", "audio_action": "encode", "quality": "auto",
        "qualities": [{"id": "auto", "label": "Auto", "height": 0}],
        "audio_tracks": [], "audio": -1, "audio_level": level, "subtitles": [],
        "resume": {"position": 120}, "next_id": "", "prev_id": "",
        "skip_segments": [], "skip_pending": False,
    }


class PlayerTests(unittest.TestCase):
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
        """Release browser processes after all test contexts have closed."""
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        """Isolate storage, API history, delayed replies and JavaScript errors per test."""
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800},
                                                reduced_motion="reduce")
        self.addCleanup(self.context.close)
        self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.context.add_init_script(MEDIA_CLOCK)
        self.requests = []
        self.pending_plans = []
        self.pending_seeks = []
        self.errors = []
        self.held_plans = False
        self.held_seeks = False
        self.without_clock_guard = False
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.context.route("**/*", self.respond)

    def tearDown(self):
        """Keep failure evidence and reject unexpected application exceptions."""
        for pending in (self.pending_plans, self.pending_seeks):
            while pending:
                route, _data = pending.pop()
                route.abort()
        artifacts = ROOT / "test-results" / "browser"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(artifacts / f"{self._testMethodName}.png"))
        self.context.tracing.stop(path=str(artifacts / f"{self._testMethodName}.zip"))
        self.assertEqual(self.errors, [])

    def respond(self, route):
        """Serve repository assets and fixture APIs; no request can reach a real server."""
        parsed = urlparse(route.request.url)
        query = parse_qs(parsed.query)
        self.requests.append(parsed.path)
        if parsed.netloc != "litejelly.test":
            self.errors.append("Unexpected external request: " + parsed.netloc)
            route.abort()
            return
        if parsed.path == "/" or parsed.path.startswith("/static/"):
            relative = "index.html" if parsed.path == "/" else parsed.path[len("/static/"):]
            target = (ROOT / "static" / relative).resolve()
            if ROOT / "static" not in target.parents or not target.is_file():
                route.fulfill(status=404, body="Missing test asset")
                return
            payload = target.read_bytes()
            if relative == "app.js":
                source = payload.decode("utf-8")
                marker = "  if (document.readyState === 'loading') {"
                self.assertEqual(source.count(marker), 1)
                source = source.replace(marker,
                    "  window.fixturePlayer = { state, paintCues, stopSubtitleTicker };\n" + marker)
                if self.without_clock_guard:
                    guard = "if (state.restartAt !== null) return state.restartAt;"
                    self.assertEqual(source.count(guard), 1)
                    source = source.replace(guard, "")
                payload = source.encode("utf-8")
            route.fulfill(status=200, body=payload,
                          content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            return
        if parsed.path == "/api/playback":
            plan = playback_plan(query)
            if self.held_plans:
                self.pending_plans.append((route, plan))
                return
            data = plan
        elif parsed.path == "/api/seekpoint":
            data = {"start": float(query.get("t", [0])[0])}
            if self.held_seeks:
                self.pending_seeks.append((route, data))
                return
        elif parsed.path == "/api/library":
            data = {"videos": [VIDEO], "progress": {}, "continue_watching": [], "status": {}}
        elif parsed.path == "/api/config":
            data = {"server_name": "LiteJelly", "ffmpeg_available": True}
        elif parsed.path == "/api/trickplay":
            data = {"status": "off"}
        elif parsed.path == "/api/details":
            data = {"metadata": {"plot": "Fixture"}}
        elif parsed.path in ("/api/thumbnail", "/favicon.ico"):
            route.fulfill(status=404, body="No fixture image")
            return
        elif parsed.path == "/api/progress":
            data = {"ok": True}
        else:
            self.errors.append("Unexpected fixture route: " + parsed.path)
            route.abort()
            return
        route.fulfill(json=data)

    def open_player(self):
        """Start playback through the home UI and deliver initial metadata explicitly."""
        self.page.goto(ORIGIN)
        self.page.locator("#hero-play").click()
        self.page.wait_for_function("window.fixtureMedia.loads.length === 1")
        self.page.locator("#video-player").dispatch_event("loadedmetadata")
        self.page.locator("#video-player").dispatch_event("canplay")
        self.page.locator("#video-player").dispatch_event("playing")
        self.page.locator("#video-player").dispatch_event("timeupdate")
        expect(self.page.locator("#current-time")).to_have_text("2:00")

    def change_dialogue(self):
        """Dispatch a control action without adding pointer movement or timing delays."""
        with self.page.expect_request("**/api/playback?*"):
            self.page.locator("#btn-level").dispatch_event("click")

    def release(self, pending, index=0):
        """Complete a selected request, allowing deliberate response reordering."""
        route, data = pending.pop(index)
        route.fulfill(json=data)

    def restart_position_during_teardown(self):
        """Return the second restart target while the first awaits its seek response."""
        self.open_player()
        self.held_seeks = True
        self.change_dialogue()
        self.page.wait_for_function("window.fixturePlayer.state.playback.audio_level === 'boost'")
        self.page.evaluate("window.fixtureMedia.time = 0")
        self.change_dialogue()
        self.page.wait_for_function("window.fixturePlayer.state.playback.audio_level === 'night'")
        position = self.page.evaluate("window.fixturePlayer.state.restartAt")
        while self.pending_seeks:
            self.release(self.pending_seeks)
        return position

    def test_library_renders_and_searches(self):
        self.page.goto(ORIGIN)
        expect(self.page.locator("#hero-title")).to_have_text("Fixture Film")
        self.page.locator("#search-input").fill("fixture")
        expect(self.page.locator("#video-grid .card")).to_have_count(1)
        self.page.locator("#search-input").fill("not present")
        expect(self.page.locator("#video-grid .card")).to_have_count(0)

    def test_quick_dialogue_changes_keep_the_resume_point(self):
        self.assertEqual(self.restart_position_during_teardown(), 120)
        self.page.wait_for_function("window.fixtureMedia.source.includes('level=night')")
        self.assertIn("ss=120.00", self.page.evaluate("window.fixtureMedia.source"))

    def test_clock_regression_is_detected_by_the_same_journey(self):
        self.without_clock_guard = True
        self.assertEqual(self.restart_position_during_teardown(), 0)

    def test_latest_dialogue_response_wins(self):
        self.open_player()
        self.held_plans = True
        self.change_dialogue()
        self.change_dialogue()
        self.page.wait_for_function("window.fixturePlayer.state.restartToken === 2")
        self.assertEqual(len(self.pending_plans), 2)
        self.release(self.pending_plans, 1)
        self.page.wait_for_function("window.fixtureMedia.source.includes('level=night')")
        self.release(self.pending_plans)
        self.page.evaluate("() => Promise.resolve()")
        self.assertEqual(self.page.evaluate("window.fixturePlayer.state.playback.audio_level"), "night")
        self.assertEqual(self.page.evaluate("window.fixtureMedia.loads.length"), 2)

    def test_subtitles_remain_on_picture_when_controls_hide(self):
        self.open_player()
        self.page.evaluate("""() => {
          fixturePlayer.stopSubtitleTicker();
          fixturePlayer.paintCues([new VTTCue(0, 600, 'Visible subtitle fixture')]);
        }""")
        for width, height in ((1280, 800), (960, 640), (390, 844)):
            with self.subTest(viewport=(width, height)):
                self.page.set_viewport_size({"width": width, "height": height})
                self.page.locator("#video-player").dispatch_event("loadedmetadata")
                for hidden in (False, True):
                    self.page.locator("#player").dispatch_event("mouseleave" if hidden else "mousemove")
                    self.page.wait_for_function(
                        "hidden => document.querySelector('#osd').classList.contains('hidden') === hidden",
                        arg=hidden)
                    self.page.wait_for_function("""() => {
                      const video = document.querySelector('#video-player');
                      const box = video.getBoundingClientRect();
                      const pictureHeight = Math.min(box.height, box.width * video.videoHeight / video.videoWidth);
                      const top = box.top + (box.height - pictureHeight) / 2;
                      const cue = document.querySelector('.subtitle-cue').getBoundingClientRect();
                      return cue.height > 0 && cue.top >= top && cue.bottom <= top + pictureHeight;
                    }""")
                    expect(self.page.locator(".subtitle-cue")).to_be_visible()

    def test_exit_clears_media_and_subtitles(self):
        self.open_player()
        self.page.locator("#player-back-btn").click()
        expect(self.page.locator("#player")).to_be_hidden()
        self.assertIsNone(self.page.evaluate("window.fixturePlayer.state.playback"))
        self.assertIsNone(self.page.evaluate("window.fixturePlayer.state.subtitleTimer"))
        expect(self.page.locator("#subtitle-layer")).to_be_empty()

    def test_exit_while_a_plan_is_loading_does_not_revive_playback(self):
        self.held_plans = True
        self.page.goto(ORIGIN)
        with self.page.expect_request("**/api/playback?*"):
            self.page.locator("#hero-play").click()
        self.page.locator("#player-back-btn").click()
        self.assertEqual(len(self.pending_plans), 1)
        self.release(self.pending_plans)
        self.page.evaluate("() => new Promise(resolve => setTimeout(resolve, 0))")
        expect(self.page.locator("#player")).to_be_hidden()
        self.assertIsNone(self.page.evaluate("window.fixturePlayer.state.playback"))
        self.assertEqual(self.page.evaluate("window.fixtureMedia.source"), "")

    def test_exit_during_restart_discards_the_late_response(self):
        self.open_player()
        self.held_plans = True
        self.change_dialogue()
        self.page.locator("#player-back-btn").click()
        loads = self.page.evaluate("window.fixtureMedia.loads.length")
        self.release(self.pending_plans)
        self.page.evaluate("() => new Promise(resolve => setTimeout(resolve, 0))")
        self.assertIsNone(self.page.evaluate("window.fixturePlayer.state.playback"))
        self.assertEqual(self.page.evaluate("window.fixtureMedia.loads.length"), loads)

    def seek(self, seconds):
        """Commit a seek through the real slider handlers."""
        self.page.locator("#seek-range").evaluate("""(slider, seconds) => {
          slider.value = String(seconds / 600 * 1000);
          slider.dispatchEvent(new Event('input', {bubbles:true}));
          slider.dispatchEvent(new Event('change', {bubbles:true}));
        }""", seconds)

    def test_reversed_seek_responses_keep_the_latest_target(self):
        self.open_player()
        self.change_dialogue()
        self.page.wait_for_function("window.fixtureMedia.source.includes('level=boost')")
        self.held_seeks = True
        with self.page.expect_request("**/api/seekpoint?*"):
            self.seek(240)
        with self.page.expect_request("**/api/seekpoint?*"):
            self.seek(360)
        self.page.evaluate("() => new Promise(resolve => setTimeout(resolve, 0))")
        self.assertEqual(len(self.pending_seeks), 2)
        self.release(self.pending_seeks, 1)
        self.page.wait_for_function("window.fixtureMedia.source.includes('ss=360.00')")
        self.release(self.pending_seeks)
        self.page.evaluate("() => new Promise(resolve => setTimeout(resolve, 0))")
        self.assertIn("ss=360.00", self.page.evaluate("window.fixtureMedia.source"))

    def test_seek_during_plan_request_updates_the_restart_target(self):
        self.open_player()
        self.held_plans = True
        self.change_dialogue()
        self.seek(360)
        self.release(self.pending_plans)
        self.page.wait_for_function("window.fixtureMedia.source.includes('level=boost')")
        self.assertIn("ss=360.00", self.page.evaluate("window.fixtureMedia.source"))


if __name__ == "__main__":
    unittest.main()
