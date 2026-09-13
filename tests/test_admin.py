"""Tests for settings validation, admin access control and the library rebuild.

Run with:  python -m unittest discover -s tests
"""

import json
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly import admin, settings
from litejelly.config import MediaDir, load_config
from litejelly.library import Library

# Several tests deliberately feed in bad input; their warnings are not failures.
logging.getLogger("litejelly").setLevel(logging.CRITICAL)
logging.getLogger("litejelly.settings").setLevel(logging.CRITICAL)
logging.getLogger("litejelly.admin").setLevel(logging.CRITICAL)
logging.getLogger("litejelly.library").setLevel(logging.CRITICAL)


class _Headers:
    """Minimal stand-in for http.client.HTTPMessage."""

    def __init__(self, **values):
        self._values = {k.lower(): v for k, v in values.items()}

    def get(self, name, default=None):
        return self._values.get(name.lower().replace("_", "-"), default)


class _Config:
    def __init__(self, admin_token=""):
        self.admin_token = admin_token


class LoopbackTests(unittest.TestCase):
    def test_ipv4_loopback(self):
        self.assertTrue(admin.is_loopback("127.0.0.1"))
        self.assertTrue(admin.is_loopback("127.1.2.3"))

    def test_ipv6_loopback(self):
        self.assertTrue(admin.is_loopback("::1"))
        self.assertTrue(admin.is_loopback("[::1]"))

    def test_mapped_ipv4_loopback(self):
        self.assertTrue(admin.is_loopback("::ffff:127.0.0.1"))

    def test_lan_addresses_are_not_loopback(self):
        for address in ("192.168.1.50", "10.0.0.2", "172.16.4.4", "8.8.8.8"):
            self.assertFalse(admin.is_loopback(address), address)

    def test_garbage_is_not_loopback(self):
        for address in ("", "localhost", "not-an-ip", "127.0.0.1.1"):
            self.assertFalse(admin.is_loopback(address), address)


class AdminAuthorizationTests(unittest.TestCase):
    """The admin API can change which directories are served, so a remote
    caller must never reach it by default."""

    def test_loopback_allowed_without_token(self):
        allowed, _ = admin.authorize(_Config(), "127.0.0.1", _Headers(), {})
        self.assertTrue(allowed)

    def test_remote_denied_without_token(self):
        allowed, reason = admin.authorize(_Config(), "192.168.1.50", _Headers(), {})
        self.assertFalse(allowed)
        self.assertIn("admin_token", reason)

    def test_remote_allowed_with_matching_header_token(self):
        allowed, _ = admin.authorize(
            _Config("s3cret"), "192.168.1.50",
            _Headers(**{"X-Admin-Token": "s3cret"}), {})
        self.assertTrue(allowed)

    def test_remote_allowed_with_matching_query_token(self):
        allowed, _ = admin.authorize(
            _Config("s3cret"), "192.168.1.50", _Headers(), {"token": ["s3cret"]})
        self.assertTrue(allowed)

    def test_remote_denied_with_wrong_token(self):
        allowed, reason = admin.authorize(
            _Config("s3cret"), "192.168.1.50",
            _Headers(**{"X-Admin-Token": "guess"}), {})
        self.assertFalse(allowed)
        self.assertIn("token", reason)

    def test_remote_denied_with_no_token_presented(self):
        allowed, _ = admin.authorize(_Config("s3cret"), "192.168.1.50", _Headers(), {})
        self.assertFalse(allowed)

    def test_loopback_still_allowed_when_token_configured(self):
        allowed, _ = admin.authorize(_Config("s3cret"), "127.0.0.1", _Headers(), {})
        self.assertTrue(allowed)

    def test_token_prefix_is_not_accepted(self):
        allowed, _ = admin.authorize(
            _Config("s3cret"), "192.168.1.50",
            _Headers(**{"X-Admin-Token": "s3c"}), {})
        self.assertFalse(allowed)


class CsrfTests(unittest.TestCase):
    def test_same_origin_accepted(self):
        headers = _Headers(Origin="http://127.0.0.1:8000", Host="127.0.0.1:8000")
        self.assertTrue(admin.same_origin(headers, "127.0.0.1:8000"))

    def test_foreign_origin_rejected(self):
        headers = _Headers(Origin="http://evil.example", Host="127.0.0.1:8000")
        self.assertFalse(admin.same_origin(headers, "127.0.0.1:8000"))

    def test_missing_origin_needs_custom_header(self):
        self.assertFalse(admin.same_origin(_Headers(), "127.0.0.1:8000"))
        self.assertTrue(admin.same_origin(
            _Headers(**{"X-LiteJelly-Admin": "1"}), "127.0.0.1:8000"))


class SettingsValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.media = self.root / "media"
        self.media.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_keys_are_ignored(self):
        clean, errors = settings.validate({"totally_made_up": 1, "server_name": "Hi"})
        self.assertEqual(clean, {"server_name": "Hi"})
        self.assertEqual(errors, [])

    def test_empty_server_name_rejected(self):
        _, errors = settings.validate({"server_name": "   "})
        self.assertTrue(errors)

    def test_overlong_server_name_rejected(self):
        _, errors = settings.validate({"server_name": "x" * 65})
        self.assertTrue(errors)

    def test_scan_interval_bounds(self):
        _, errors = settings.validate({"scan_interval": 1})
        self.assertTrue(errors)
        clean, errors = settings.validate({"scan_interval": 30})
        self.assertEqual(clean["scan_interval"], 30)
        self.assertEqual(errors, [])

    def test_non_numeric_rejected(self):
        _, errors = settings.validate({"thumbnail_workers": "many"})
        self.assertTrue(errors)

    def test_port_bounds(self):
        _, errors = settings.validate({"port": 0})
        self.assertTrue(errors)
        _, errors = settings.validate({"port": 70000})
        self.assertTrue(errors)
        clean, _ = settings.validate({"port": 8123})
        self.assertEqual(clean["port"], 8123)

    def test_media_dir_accepts_plain_string(self):
        clean, errors = settings.validate({"media_dirs": [str(self.media)]})
        self.assertEqual(errors, [])
        self.assertEqual(clean["media_dirs"][0]["content_type"], "mixed")

    def test_media_dir_content_type_whitelist(self):
        clean, errors = settings.validate({
            "media_dirs": [{"path": str(self.media), "content_type": "rootkit"}]})
        self.assertTrue(errors)
        self.assertEqual(clean["media_dirs"][0]["content_type"], "mixed")

    def test_media_dir_valid_content_type(self):
        clean, errors = settings.validate({
            "media_dirs": [{"path": str(self.media), "content_type": "anime"}]})
        self.assertEqual(errors, [])
        self.assertEqual(clean["media_dirs"][0]["content_type"], "anime")

    def test_missing_media_dir_reported(self):
        _, errors = settings.validate({"media_dirs": [str(self.root / "nope")]})
        self.assertTrue(any("Not found" in e for e in errors))

    def test_file_is_not_a_directory(self):
        target = self.root / "movie.mkv"
        target.write_text("x", encoding="utf-8")
        _, errors = settings.validate({"media_dirs": [str(target)]})
        self.assertTrue(errors)

    def test_duplicate_media_dirs_collapse(self):
        clean, _ = settings.validate({
            "media_dirs": [str(self.media), str(self.media) + "//"]})
        self.assertEqual(len(clean["media_dirs"]), 1)

    def test_empty_media_dirs_allowed(self):
        # The server now starts with no library and sends the user to /admin,
        # so clearing the list is a legitimate state rather than an error.
        clean, errors = settings.validate({"media_dirs": []})
        self.assertEqual(errors, [])
        self.assertEqual(clean["media_dirs"], [])

    def test_media_dirs_must_be_a_list(self):
        _, errors = settings.validate({"media_dirs": {"path": str(self.media)}})
        self.assertTrue(errors)

    def test_resolution_format(self):
        _, errors = settings.validate({"transcode": {"resolution": "big"}})
        self.assertTrue(errors)
        clean, errors = settings.validate({"transcode": {"resolution": "1920X1080"}})
        self.assertEqual(errors, [])
        self.assertEqual(clean["transcode"]["resolution"], "1920x1080")

    def test_preset_whitelist(self):
        _, errors = settings.validate({"transcode": {"preset": "turbo"}})
        self.assertTrue(errors)
        clean, errors = settings.validate({"transcode": {"preset": "Slow"}})
        self.assertEqual(clean["transcode"]["preset"], "slow")

    def test_crf_bounds(self):
        _, errors = settings.validate({"transcode": {"crf": 99}})
        self.assertTrue(errors)
        clean, _ = settings.validate({"transcode": {"crf": 20}})
        self.assertEqual(clean["transcode"]["crf"], 20)

    def test_ffmpeg_path_must_exist(self):
        _, errors = settings.validate({"ffmpeg_path": str(self.root / "ffmpeg.exe")})
        self.assertTrue(errors)

    def test_blank_ffmpeg_path_means_autodetect(self):
        clean, errors = settings.validate({"ffmpeg_path": "  "})
        self.assertEqual(errors, [])
        self.assertEqual(clean["ffmpeg_path"], "")

    def test_body_must_be_an_object(self):
        _, errors = settings.validate(["media_dirs"])
        self.assertTrue(errors)

    def test_admin_token_minimum_length(self):
        _, errors = settings.validate({"admin_token": "short"})
        self.assertTrue(errors)

    def test_admin_token_rejects_spaces(self):
        _, errors = settings.validate({"admin_token": "has spaces here"})
        self.assertTrue(errors)

    def test_admin_token_accepted(self):
        clean, errors = settings.validate({"admin_token": "a-long-enough-token"})
        self.assertEqual(errors, [])
        self.assertEqual(clean["admin_token"], "a-long-enough-token")

    def test_admin_token_can_be_cleared(self):
        clean, errors = settings.validate({"admin_token": ""})
        self.assertEqual(errors, [])
        self.assertEqual(clean["admin_token"], "")


class RestartRequiredTests(unittest.TestCase):
    def test_changed_port_needs_restart(self):
        self.assertEqual(settings.restart_required({"port": 8000}, {"port": 9000}), ["port"])

    def test_unchanged_port_does_not(self):
        self.assertEqual(settings.restart_required({"port": 8000}, {"port": 8000}), [])

    def test_absent_key_does_not(self):
        self.assertEqual(settings.restart_required({"port": 8000}, {}), [])

    def test_other_fields_apply_live(self):
        self.assertEqual(settings.restart_required({}, {"server_name": "New"}), [])

    def test_host_and_port_reported_together(self):
        changed = settings.restart_required(
            {"port": 8000, "host": "0.0.0.0"},
            {"port": 9000, "host": "127.0.0.1"})
        self.assertEqual(sorted(changed), ["host", "port"])


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_settings_live_beside_config_not_in_cache(self):
        path = settings.settings_path(self.root)
        self.assertEqual(path.parent, self.root)
        self.assertNotIn(".cache", str(path))

    def test_round_trip(self):
        settings.save_overrides(self.root, {"server_name": "Den", "port": 9001})
        self.assertEqual(settings.load_overrides(self.root),
                         {"server_name": "Den", "port": 9001})

    def test_missing_file_is_empty(self):
        self.assertEqual(settings.load_overrides(self.root), {})

    def test_corrupt_file_is_ignored(self):
        settings.settings_path(self.root).write_text("{ not json", encoding="utf-8")
        self.assertEqual(settings.load_overrides(self.root), {})

    def test_non_object_file_is_ignored(self):
        settings.settings_path(self.root).write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(settings.load_overrides(self.root), {})

    def test_byte_order_mark_is_tolerated(self):
        # Notepad and PowerShell's Set-Content write a BOM; plain utf-8 chokes
        # on it and the file would be silently ignored.
        settings.settings_path(self.root).write_text(
            '{"server_name": "Den"}', encoding="utf-8-sig")
        self.assertEqual(settings.load_overrides(self.root), {"server_name": "Den"})

    def test_save_does_not_leave_temp_files(self):
        settings.save_overrides(self.root, {"server_name": "Den"})
        leftovers = [p.name for p in self.root.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])


class ConfigLayeringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.movies = self.root / "movies"
        self.shows = self.root / "shows"
        self.movies.mkdir()
        self.shows.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_config(self, data):
        (self.root / "config.json").write_text(json.dumps(data), encoding="utf-8")

    def test_plain_string_dirs_still_work(self):
        self._write_config({"media_dirs": [str(self.movies)]})
        config, warnings = load_config(self.root)
        self.assertEqual(len(config.media_dirs), 1)
        self.assertEqual(config.media_dirs[0].content_type, "mixed")
        self.assertEqual(warnings, [])

    def test_tagged_dirs_are_parsed(self):
        self._write_config({"media_dirs": [
            {"path": str(self.movies), "content_type": "movies"},
            {"path": str(self.shows), "content_type": "shows", "label": "TV"},
        ]})
        config, _ = load_config(self.root)
        self.assertEqual([d.content_type for d in config.media_dirs], ["movies", "shows"])
        self.assertEqual(config.media_dirs[1].display_name, "TV")

    def test_unknown_content_type_falls_back_with_a_warning(self):
        self._write_config({"media_dirs": [
            {"path": str(self.movies), "content_type": "everything"}]})
        config, warnings = load_config(self.root)
        self.assertEqual(config.media_dirs[0].content_type, "mixed")
        self.assertTrue(any("everything" in w for w in warnings))

    def test_settings_override_config_file(self):
        self._write_config({"server_name": "FromConfig", "media_dirs": [str(self.movies)]})
        settings.save_overrides(self.root, {"server_name": "FromSettings"})
        config, _ = load_config(self.root)
        self.assertEqual(config.server_name, "FromSettings")

    def test_settings_override_media_dirs(self):
        self._write_config({"media_dirs": [str(self.movies)]})
        settings.save_overrides(self.root, {
            "media_dirs": [{"path": str(self.shows), "content_type": "anime"}]})
        config, _ = load_config(self.root)
        self.assertEqual(config.media_paths, [str(self.shows)])
        self.assertEqual(config.media_dirs[0].content_type, "anime")

    def test_transcode_overrides_merge_rather_than_replace(self):
        self._write_config({
            "media_dirs": [str(self.movies)],
            "transcode": {"crf": 18, "preset": "slow"},
        })
        settings.save_overrides(self.root, {"transcode": {"crf": 24}})
        config, _ = load_config(self.root)
        self.assertEqual(config.transcode.crf, 24)
        self.assertEqual(config.transcode.preset, "slow")

    def test_cli_arguments_beat_settings(self):
        class Args:
            port = 7777
            host = None
            dir = None

        self._write_config({"media_dirs": [str(self.movies)]})
        settings.save_overrides(self.root, {"server_name": "FromSettings", "port": 9001})
        config, _ = load_config(self.root, Args())
        self.assertEqual(config.port, 7777)
        self.assertEqual(config.server_name, "FromSettings")

    def test_no_media_dirs_is_a_valid_first_run_state(self):
        # Previously this silently fell back to ~/Videos, which indexed files
        # the user never asked to share.
        self._write_config({})
        config, warnings = load_config(self.root)
        self.assertEqual(config.media_dirs, [])
        self.assertEqual(warnings, [])

    def test_duplicate_dirs_collapse(self):
        self._write_config({"media_dirs": [str(self.movies), str(self.movies)]})
        config, _ = load_config(self.root)
        self.assertEqual(len(config.media_dirs), 1)

    def test_config_with_a_byte_order_mark_still_loads(self):
        (self.root / "config.json").write_text(
            json.dumps({"server_name": "Den"}), encoding="utf-8-sig")
        config, warnings = load_config(self.root)
        self.assertEqual(config.server_name, "Den")
        self.assertEqual(warnings, [])


class PublicConfigTests(unittest.TestCase):
    """The library API is unauthenticated, so it must not describe the host."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "movies").mkdir()
        (self.root / "config.json").write_text(json.dumps({
            "media_dirs": [str(self.root / "movies")],
            "admin_token": "s3cret",
            "ffmpeg_path": "",
        }), encoding="utf-8")
        self.config, _ = load_config(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_public_dict_has_no_paths_or_secrets(self):
        public = self.config.to_public_dict()
        self.assertEqual(set(public), {"server_name", "version"})
        blob = json.dumps(public)
        self.assertNotIn("s3cret", blob)
        self.assertNotIn(str(self.root), blob)

    def test_admin_dict_has_the_details(self):
        admin_view = self.config.to_admin_dict()
        self.assertIn("media_dirs", admin_view)
        self.assertIn("transcode", admin_view)

    def test_admin_dict_echoes_the_token_for_the_remote_url(self):
        # Only ever served behind require_admin, and the page needs it to show
        # a working remote link.
        self.assertEqual(self.config.to_admin_dict()["admin_token"], "s3cret")


class LibraryReconfigureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.first = self.root / "first"
        self.second = self.root / "second"
        self.first.mkdir()
        self.second.mkdir()
        (self.first / "Alpha.2020.1080p.mkv").write_bytes(b"x" * 32)
        (self.second / "Beta.S01E02.mkv").write_bytes(b"x" * 32)

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_tags_videos_with_the_directory_content_type(self):
        library = Library([MediaDir(str(self.first), "movies")])
        videos = library.scan()
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0].content_type, "movies")

    def test_set_media_dirs_changes_what_is_indexed(self):
        library = Library([MediaDir(str(self.first), "movies")])
        library.scan()
        self.assertEqual([v.filename for v in library.videos], ["Alpha.2020.1080p.mkv"])

        library.set_media_dirs([MediaDir(str(self.second), "shows")])
        library.scan()
        self.assertEqual([v.filename for v in library.videos], ["Beta.S01E02.mkv"])
        self.assertEqual(library.videos[0].content_type, "shows")

    def test_set_media_dirs_clears_the_signature_so_the_rescan_is_not_skipped(self):
        library = Library([MediaDir(str(self.first), "movies")])
        library.scan()
        library.set_media_dirs([MediaDir(str(self.second), "shows")])
        # Without clearing the fingerprint the second scan would short-circuit
        # and keep serving the previous directory's files.
        self.assertEqual(library._signature, {})

    def test_absolute_path_resolves_against_the_new_directory(self):
        library = Library([MediaDir(str(self.first), "movies")])
        library.scan()
        library.set_media_dirs([MediaDir(str(self.second), "shows")])
        library.scan()
        video = library.videos[0]
        self.assertEqual(library.absolute_path(video),
                         (self.second / "Beta.S01E02.mkv").resolve())

    def test_absolute_path_returns_none_for_a_stale_dir_index(self):
        library = Library([MediaDir(str(self.first)), MediaDir(str(self.second))])
        library.scan()
        stale = [v for v in library.videos if v.dir_index == 1][0]
        library.set_media_dirs([MediaDir(str(self.first))])
        self.assertIsNone(library.absolute_path(stale))

    def test_concurrent_reconfigure_does_not_raise(self):
        library = Library([MediaDir(str(self.first), "movies")])
        errors = []

        def flip(index):
            try:
                for _ in range(20):
                    target = self.first if index % 2 else self.second
                    library.set_media_dirs([MediaDir(str(target))])
                    library.scan()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=flip, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
