"""Searching OpenSubtitles and keeping what it sends.

Every test here runs against a stub: no test in this suite touches the
network, and the real API needs an account nobody should have to own to run
the tests. What that buys is coverage of the parts that are ours - the
credential file, the hash, parsing, error wording, and the rule that anything
downloaded is checked exactly as strictly as anything uploaded.

What it does NOT prove is that the live API still looks like this. That leg is
unverified until it runs against a real key.

Run with:  python -m unittest discover -s tests
"""

import json
import sys
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly import opensubtitles as os_api
from litejelly import subtitles

SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello there.\n"

SEARCH_BODY = {
    "data": [
        {
            "attributes": {
                "language": "en",
                "release": "Guardians.of.the.Galaxy.2014.1080p.BluRay",
                "download_count": 48213,
                "hearing_impaired": False,
                "from_trusted": True,
                "uploader": {"name": "someone"},
                "files": [{"file_id": 4242, "file_name": "guardians.en.srt"}],
            }
        },
        {
            "attributes": {
                "language": "en",
                "release": "Guardians.of.the.Galaxy.2014.EXTENDED",
                "download_count": 12,
                "hearing_impaired": True,
                "from_trusted": False,
                "uploader": None,
                "files": [{"file_id": 99, "file_name": "other.srt"}],
            }
        },
        {"attributes": {"language": "en", "files": []}},        # no file at all
        {"attributes": {"language": "en", "files": [{"file_id": "nope"}]}},
        "not even a dict",
    ]
}


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def stub(responses):
    """Answer each urlopen in turn from a list of payloads."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        payload = responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, bytes):
            return FakeResponse(payload)
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    return fake_urlopen, calls


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_nothing_saved_reads_as_not_configured(self):
        self.assertFalse(os_api.load_account(self.root).configured)

    def test_it_survives_a_round_trip(self):
        os_api.save_account(self.root, os_api.Account("key", "user", "secret"))
        again = os_api.load_account(self.root)
        self.assertTrue(again.configured)
        self.assertEqual(again.username, "user")

    def test_the_password_is_never_in_the_public_view(self):
        account = os_api.Account("key", "user", "hunter2")
        public = json.dumps(account.to_public_dict())
        self.assertNotIn("hunter2", public)
        self.assertNotIn("key", json.loads(public).values())
        self.assertTrue(json.loads(public)["has_password"])

    def test_it_does_not_live_in_settings(self):
        # settings.json is exported and imported from the admin page, and a
        # third-party password must not travel with it.
        os_api.save_account(self.root, os_api.Account("key", "user", "secret"))
        self.assertTrue((self.root / "opensubtitles.json").is_file())
        self.assertFalse((self.root / "settings.json").exists())

    def test_a_corrupt_file_does_not_take_the_server_down(self):
        (self.root / "opensubtitles.json").write_text("{not json", encoding="utf-8")
        self.assertFalse(os_api.load_account(self.root).configured)


class HashTests(unittest.TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_a_file_too_small_to_hash_says_so(self):
        small = self.root / "tiny.mkv"
        small.write_bytes(b"x" * 1024)
        self.assertEqual(os_api.movie_hash(small), "")

    def test_a_missing_file_says_so(self):
        self.assertEqual(os_api.movie_hash(self.root / "nope.mkv"), "")

    def test_the_hash_is_sixteen_hex_digits(self):
        big = self.root / "big.mkv"
        big.write_bytes(bytes(range(256)) * 1024)
        digest = os_api.movie_hash(big)
        self.assertEqual(len(digest), 16)
        int(digest, 16)

    def test_the_same_bytes_hash_the_same_way(self):
        one, two = self.root / "a.mkv", self.root / "b.mkv"
        payload = bytes(range(256)) * 1024
        one.write_bytes(payload)
        two.write_bytes(payload)
        self.assertEqual(os_api.movie_hash(one), os_api.movie_hash(two))

    def test_different_bytes_hash_differently(self):
        one, two = self.root / "a.mkv", self.root / "b.mkv"
        one.write_bytes(bytes(range(256)) * 1024)
        two.write_bytes(bytes(range(255, -1, -1)) * 1024)
        self.assertNotEqual(os_api.movie_hash(one), os_api.movie_hash(two))


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.root = Path(self.dir.name)
        os_api.save_account(self.root, os_api.Account("key", "user", "secret"))
        self.client = os_api.OpenSubtitles(self.root)
        self.client._pace = lambda: None

    def tearDown(self):
        self.dir.cleanup()

    def test_results_are_parsed_and_rubbish_is_dropped(self):
        fake, calls = stub([SEARCH_BODY])
        with mock.patch("urllib.request.urlopen", fake):
            found = self.client.search("Guardians", "en", "abc123")
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].file_id, 4242)
        self.assertEqual(found[0].downloads, 48213)
        self.assertTrue(found[0].from_trusted)
        self.assertEqual(found[1].uploader, "")

    def test_the_file_hash_is_sent_when_there_is_one(self):
        fake, calls = stub([SEARCH_BODY])
        with mock.patch("urllib.request.urlopen", fake):
            self.client.search("Guardians", "en", "abc123")
        self.assertIn("moviehash=abc123", calls[0].full_url)

    def test_an_episode_is_searched_as_an_episode(self):
        fake, calls = stub([SEARCH_BODY])
        with mock.patch("urllib.request.urlopen", fake):
            self.client.search("Show", "en", "", season=2, episode=3)
        self.assertIn("season_number=2", calls[0].full_url)
        self.assertIn("episode_number=3", calls[0].full_url)

    def test_the_api_key_identifies_us(self):
        fake, calls = stub([SEARCH_BODY])
        with mock.patch("urllib.request.urlopen", fake):
            self.client.search("Guardians", "en")
        headers = {k.lower(): v for k, v in calls[0].header_items()}
        self.assertEqual(headers.get("Api-key".lower()), "key")
        # They reject the urllib default agent.
        self.assertIn("LiteJelly", headers.get("User-agent".lower(), ""))

    def test_searching_without_a_key_is_refused_before_any_request(self):
        client = os_api.OpenSubtitles(Path(self.dir.name) / "elsewhere")
        with self.assertRaises(os_api.OpenSubtitlesError):
            client.search("Guardians", "en")

    def test_a_quota_error_is_explained(self):
        error = urllib.error.HTTPError("u", 406, "Not Acceptable", {}, None)
        fake, _ = stub([error])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(os_api.OpenSubtitlesError) as caught:
                self.client.search("Guardians", "en")
        self.assertIn("quota", str(caught.exception))

    def test_a_bad_key_is_explained(self):
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        fake, _ = stub([error])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(os_api.OpenSubtitlesError) as caught:
                self.client.search("Guardians", "en")
        self.assertIn("API key", str(caught.exception))

    def test_their_own_message_is_passed_on(self):
        """Measured against the live API: a wrong key answers 403 with
        {"message": "You cannot consume this service"}, which tells you far
        more than the status code does."""
        body = BytesIO(b'{"message": "You cannot consume this service"}')
        error = urllib.error.HTTPError("u", 403, "Forbidden", {}, body)
        fake, _ = stub([error])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(os_api.OpenSubtitlesError) as caught:
                self.client.search("Guardians", "en")
        self.assertIn("You cannot consume this service", str(caught.exception))

    def test_an_error_with_no_body_still_reads_sensibly(self):
        error = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        fake, _ = stub([error])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(os_api.OpenSubtitlesError) as caught:
                self.client.search("Guardians", "en")
        self.assertNotIn("()", str(caught.exception))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.root = Path(self.dir.name)
        os_api.save_account(self.root, os_api.Account("key", "user", "secret"))
        self.client = os_api.OpenSubtitles(self.root)
        self.client._pace = lambda: None

    def tearDown(self):
        self.dir.cleanup()

    def test_it_signs_in_then_downloads(self):
        fake, calls = stub([
            {"token": "abc"},
            {"link": "https://dl.opensubtitles.com/x.srt", "file_name": "x.en.srt",
             "remaining": 4},
            SRT.encode("utf-8"),
        ])
        with mock.patch("urllib.request.urlopen", fake):
            data, name = self.client.download(4242)
        self.assertEqual(data, SRT.encode("utf-8"))
        self.assertEqual(name, "x.en.srt")
        self.assertTrue(calls[0].full_url.endswith("/login"))

    def test_the_token_is_reused_rather_than_fetched_each_time(self):
        fake, calls = stub([
            {"token": "abc"},
            {"link": "https://dl.opensubtitles.com/a.srt"}, SRT.encode("utf-8"),
            {"link": "https://dl.opensubtitles.com/b.srt"}, SRT.encode("utf-8"),
        ])
        with mock.patch("urllib.request.urlopen", fake):
            self.client.download(1)
            self.client.download(2)
        logins = [c for c in calls if c.full_url.endswith("/login")]
        self.assertEqual(len(logins), 1)

    def test_the_download_carries_the_token(self):
        fake, calls = stub([
            {"token": "abc"},
            {"link": "https://dl.opensubtitles.com/x.srt"}, SRT.encode("utf-8"),
        ])
        with mock.patch("urllib.request.urlopen", fake):
            self.client.download(4242)
        headers = {k.lower(): v for k, v in calls[1].header_items()}
        self.assertEqual(headers.get("authorization"), "Bearer abc")

    def test_a_link_that_is_not_https_is_refused(self):
        # The link comes from the API rather than from us, but following
        # anything it happens to return would be someone else's request.
        for link in ("http://dl.opensubtitles.com/x.srt",
                     "file:///etc/passwd",
                     "ftp://example.com/x.srt"):
            fake, _ = stub([{"token": "abc"}, {"link": link}])
            client = os_api.OpenSubtitles(self.root)
            client._pace = lambda: None
            with mock.patch("urllib.request.urlopen", fake):
                with self.assertRaises(os_api.OpenSubtitlesError):
                    client.download(1)

    def test_no_link_reports_what_the_api_said(self):
        fake, _ = stub([{"token": "abc"},
                        {"message": "You have used your downloads for today"}])
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(os_api.OpenSubtitlesError) as caught:
                self.client.download(1)
        self.assertIn("downloads for today", str(caught.exception))

    def test_signing_in_without_an_account_is_refused(self):
        os_api.save_account(self.root, os_api.Account("key", "", ""))
        client = os_api.OpenSubtitles(self.root)
        with self.assertRaises(os_api.OpenSubtitlesError):
            client.download(1)


class DownloadedFileTests(unittest.TestCase):
    """What comes back over the wire is not trusted any further than an upload."""

    def setUp(self):
        self.dir = TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.video = self.root / "Guardians of the Galaxy (2014).mkv"
        self.video.write_bytes(b"not really a video")

    def tearDown(self):
        self.dir.cleanup()

    def test_a_downloaded_subtitle_goes_through_the_same_check(self):
        saved = subtitles.save_sidecar(self.video, SRT.encode("utf-8"), "en")
        self.assertEqual(saved.name, "Guardians of the Galaxy (2014).en.srt")

    def test_something_that_is_not_a_subtitle_is_refused(self):
        with self.assertRaises(ValueError):
            subtitles.save_sidecar(self.video, b"<html>404</html>", "en")


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.web = (Path(__file__).resolve().parent.parent
                    / "litejelly" / "web.py").read_text(encoding="utf-8")

    def test_fetching_requires_an_admin_session(self):
        handler = self.web[self.web.index("def subtitle_fetch"):]
        self.assertIn("h.require_admin(query, write=True)",
                      handler[:handler.index("read_json_body")])

    def test_searching_requires_an_admin_session(self):
        # Picking a result writes a file, and searching spends the account's
        # own rate limit.
        handler = self.web[self.web.index("def subtitle_search"):]
        self.assertIn("h.require_admin(query", handler[:handler.index("resolve_video")])

    def test_a_downloaded_file_is_saved_through_save_sidecar(self):
        handler = self.web[self.web.index("def subtitle_fetch"):]
        handler = handler[:handler.index("def admin_opensubtitles")]
        self.assertIn("save_sidecar(path, data, language)", handler)

    def test_the_password_is_never_sent_back(self):
        handler = self.web[self.web.index("def admin_opensubtitles"):]
        handler = handler[:handler.index("def admin_opensubtitles_test")]
        self.assertIn("to_public_dict()", handler)
        self.assertNotIn(".password", handler.split("OpenSubtitlesAccount(")[0])


if __name__ == "__main__":
    unittest.main()
