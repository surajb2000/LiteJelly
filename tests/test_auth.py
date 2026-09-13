"""Tests for admin accounts: hashing, storage, sessions and lockout.

This is the boundary that protects the filesystem, so the tests here are less
about features and more about the ways it must not give way.

Run with:  python -m unittest discover -s tests
"""

import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly import auth

logging.getLogger("litejelly.auth").setLevel(logging.CRITICAL)

# Hashing is deliberately slow; most tests do not need the real cost.
FAST = 1000


class PasswordHashingTests(unittest.TestCase):
    def test_correct_password_verifies(self):
        stored = auth.hash_password("correct horse", iterations=FAST)
        self.assertTrue(auth.verify_password("correct horse", stored))

    def test_wrong_password_is_rejected(self):
        stored = auth.hash_password("correct horse", iterations=FAST)
        self.assertFalse(auth.verify_password("Correct horse", stored))
        self.assertFalse(auth.verify_password("", stored))
        self.assertFalse(auth.verify_password("correct horse ", stored))

    def test_the_password_is_never_stored(self):
        stored = auth.hash_password("sup3rsecret", iterations=FAST)
        self.assertNotIn("sup3rsecret", stored)

    def test_same_password_hashes_differently(self):
        # A per-password salt, so identical passwords do not look identical.
        first = auth.hash_password("same", iterations=FAST)
        second = auth.hash_password("same", iterations=FAST)
        self.assertNotEqual(first, second)
        self.assertTrue(auth.verify_password("same", first))
        self.assertTrue(auth.verify_password("same", second))

    def test_hash_records_its_parameters(self):
        stored = auth.hash_password("x", iterations=4321)
        algorithm, iterations, _salt, _digest = stored.split("$")
        self.assertTrue(algorithm.startswith("pbkdf2_"))
        self.assertEqual(iterations, "4321")

    def test_garbage_stored_value_is_rejected_not_raised(self):
        for bad in ("", "nonsense", "a$b$c", "a$b$c$d", "pbkdf2_sha256$x$y$z", None):
            self.assertFalse(auth.verify_password("whatever", bad))

    def test_unicode_passwords_work(self):
        stored = auth.hash_password("ünïcødé-пароль-🔐", iterations=FAST)
        self.assertTrue(auth.verify_password("ünïcødé-пароль-🔐", stored))

    def test_default_iteration_count_is_substantial(self):
        # Cheap hashing would make an offline crack of a stolen file trivial.
        self.assertGreaterEqual(auth.PBKDF2_ITERATIONS, 100_000)


class ValidationTests(unittest.TestCase):
    def test_short_password_rejected(self):
        self.assertTrue(auth.check_password_strength("short"))

    def test_minimum_length_accepted(self):
        self.assertEqual(auth.check_password_strength("a" * auth.MIN_PASSWORD_LENGTH), [])

    def test_absurdly_long_password_rejected(self):
        # Unbounded input into a deliberately slow hash is a denial of service.
        self.assertTrue(auth.check_password_strength("a" * 5000))

    def test_passphrases_are_not_blocked_by_composition_rules(self):
        self.assertEqual(auth.check_password_strength("correct horse battery staple"), [])

    def test_username_rules(self):
        self.assertTrue(auth.check_username("a"))
        self.assertTrue(auth.check_username("has space"))
        self.assertTrue(auth.check_username("x" * 100))
        self.assertEqual(auth.check_username("admin"), [])


class CredentialStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _credentials(self):
        return auth.Credentials("admin", auth.hash_password("password1", iterations=FAST),
                                time.time())

    def test_round_trip(self):
        auth.save_credentials(self.root, self._credentials())
        loaded = auth.load_credentials(self.root)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.username, "admin")
        self.assertTrue(auth.verify_password("password1", loaded.password_hash))

    def test_missing_file_means_no_account(self):
        self.assertIsNone(auth.load_credentials(self.root))

    def test_corrupt_file_means_no_account(self):
        auth.credentials_path(self.root).write_text("{ broken", encoding="utf-8")
        self.assertIsNone(auth.load_credentials(self.root))

    def test_incomplete_file_means_no_account(self):
        auth.credentials_path(self.root).write_text('{"username": "admin"}',
                                                    encoding="utf-8")
        self.assertIsNone(auth.load_credentials(self.root))

    def test_stored_file_contains_no_plaintext(self):
        auth.save_credentials(self.root, self._credentials())
        content = auth.credentials_path(self.root).read_text(encoding="utf-8")
        self.assertNotIn("password1", content)
        self.assertIn("password_hash", content)

    def test_credentials_live_outside_settings(self):
        # settings.json is served to the admin page; credentials never are.
        path = auth.credentials_path(self.root)
        self.assertNotIn("settings", path.name)
        self.assertEqual(path.parent, self.root)

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_file_is_not_world_readable(self):
        auth.save_credentials(self.root, self._credentials())
        mode = auth.credentials_path(self.root).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_saving_twice_leaves_no_temp_file(self):
        auth.save_credentials(self.root, self._credentials())
        auth.save_credentials(self.root, self._credentials())
        leftovers = [p.name for p in self.root.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    def test_byte_order_mark_is_tolerated(self):
        credentials = self._credentials()
        auth.credentials_path(self.root).write_text(
            json.dumps(credentials.to_dict()), encoding="utf-8-sig")
        self.assertIsNotNone(auth.load_credentials(self.root))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.sessions = auth.SessionStore()

    def test_created_session_validates(self):
        token = self.sessions.create("admin")
        self.assertEqual(self.sessions.validate(token), "admin")

    def test_unknown_token_is_rejected(self):
        self.assertIsNone(self.sessions.validate("nonsense"))
        self.assertIsNone(self.sessions.validate(""))
        self.assertIsNone(self.sessions.validate(None))

    def test_tokens_are_long_and_unique(self):
        tokens = {self.sessions.create("admin") for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        self.assertTrue(all(len(t) >= 32 for t in tokens))

    def test_revoked_session_stops_working(self):
        token = self.sessions.create("admin")
        self.sessions.revoke(token)
        self.assertIsNone(self.sessions.validate(token))

    def test_revoke_all_signs_everyone_out(self):
        tokens = [self.sessions.create("admin") for _ in range(5)]
        self.sessions.revoke_all()
        for token in tokens:
            self.assertIsNone(self.sessions.validate(token))

    def test_expired_session_is_rejected(self):
        store = auth.SessionStore(lifetime=0.05)
        token = store.create("admin")
        time.sleep(0.1)
        self.assertIsNone(store.validate(token))

    def test_use_extends_the_session(self):
        store = auth.SessionStore(lifetime=0.3)
        token = store.create("admin")
        for _ in range(4):
            time.sleep(0.1)
            self.assertEqual(store.validate(token), "admin")

    def test_expired_sessions_do_not_accumulate(self):
        store = auth.SessionStore(lifetime=0.01)
        for _ in range(20):
            store.create("admin")
        time.sleep(0.05)
        for _ in range(20):
            store.validate("nope")
        self.assertLessEqual(store.count(), 20)

    def test_concurrent_use_is_safe(self):
        errors = []

        def hammer():
            try:
                for _ in range(100):
                    token = self.sessions.create("admin")
                    self.sessions.validate(token)
                    self.sessions.revoke(token)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        self.throttle = auth.LoginThrottle(max_failures=3, lockout=0.2)

    def test_not_locked_initially(self):
        self.assertEqual(self.throttle.locked_for("1.2.3.4"), 0.0)

    def test_locks_after_the_limit(self):
        for _ in range(3):
            self.throttle.record_failure("1.2.3.4")
        self.assertGreater(self.throttle.locked_for("1.2.3.4"), 0)

    def test_below_the_limit_is_not_locked(self):
        for _ in range(2):
            self.throttle.record_failure("1.2.3.4")
        self.assertEqual(self.throttle.locked_for("1.2.3.4"), 0.0)

    def test_lock_expires(self):
        for _ in range(3):
            self.throttle.record_failure("1.2.3.4")
        time.sleep(0.25)
        self.assertEqual(self.throttle.locked_for("1.2.3.4"), 0.0)

    def test_success_clears_the_count(self):
        for _ in range(2):
            self.throttle.record_failure("1.2.3.4")
        self.throttle.record_success("1.2.3.4")
        self.assertEqual(self.throttle.remaining_attempts("1.2.3.4"), 3)

    def test_lockout_is_per_address(self):
        for _ in range(3):
            self.throttle.record_failure("1.2.3.4")
        self.assertEqual(self.throttle.locked_for("5.6.7.8"), 0.0)

    def test_remaining_attempts_counts_down(self):
        self.assertEqual(self.throttle.remaining_attempts("1.2.3.4"), 3)
        self.throttle.record_failure("1.2.3.4")
        self.assertEqual(self.throttle.remaining_attempts("1.2.3.4"), 2)

    def test_default_limit_is_tight_enough_to_matter(self):
        self.assertLessEqual(auth.MAX_FAILURES, 10)
        self.assertGreaterEqual(auth.LOCKOUT_SECONDS, 60)


if __name__ == "__main__":
    unittest.main()
