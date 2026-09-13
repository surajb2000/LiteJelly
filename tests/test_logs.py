"""Tests for log verbosity, the rotating file and reading it back.

Run with:  python -m unittest discover -s tests
"""

import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly import logs, settings


class VerbosityTests(unittest.TestCase):
    def test_known_names_resolve(self):
        self.assertEqual(logs.resolve_verbosity("info"), logging.INFO)
        self.assertEqual(logs.resolve_verbosity("debug"), logging.DEBUG)
        self.assertEqual(logs.resolve_verbosity("trace"), logs.TRACE)

    def test_case_and_padding_are_forgiven(self):
        self.assertEqual(logs.resolve_verbosity("  DEBUG "), logging.DEBUG)

    def test_unknown_falls_back_to_info(self):
        for value in ("", None, "chatty", "warning"):
            self.assertEqual(logs.resolve_verbosity(value), logging.INFO)

    def test_info_is_the_quietest_option(self):
        # Warnings and errors must never be configurable away, or a failing
        # server looks like a healthy one.
        self.assertEqual(min(logs.VERBOSITY.values()), logs.TRACE)
        self.assertEqual(max(logs.VERBOSITY.values()), logging.INFO)

    def test_trace_is_below_debug(self):
        self.assertLess(logs.TRACE, logging.DEBUG)

    def test_trace_level_is_registered(self):
        logs.register_trace()
        self.assertEqual(logging.getLevelName(logs.TRACE), "TRACE")
        self.assertTrue(hasattr(logging.getLogger("litejelly.test"), "trace"))


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        root = logging.getLogger("litejelly")
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        self._tmp.cleanup()

    def test_log_file_lives_in_its_own_folder(self):
        path = logs.log_path(self.root)
        self.assertEqual(path.parent.name, "logs")
        self.assertEqual(path.name, "litejelly.log")

    def test_messages_reach_the_file(self):
        logs.configure(self.root, verbosity="info")
        logging.getLogger("litejelly.test").info("hello from the test")
        entries = logs.read_entries(self.root, 50)
        self.assertTrue(any("hello from the test" in e["message"] for e in entries))

    def test_debug_is_withheld_at_normal_verbosity(self):
        logs.configure(self.root, verbosity="info")
        logging.getLogger("litejelly.test").debug("noisy detail")
        entries = logs.read_entries(self.root, 50)
        self.assertFalse(any("noisy detail" in e["message"] for e in entries))

    def test_debug_appears_once_enabled(self):
        logs.configure(self.root, verbosity="debug")
        logging.getLogger("litejelly.test").debug("noisy detail")
        entries = logs.read_entries(self.root, 50)
        self.assertTrue(any("noisy detail" in e["message"] for e in entries))

    def test_warnings_are_recorded_at_every_verbosity(self):
        for verbosity in logs.VERBOSITY:
            logs.configure(self.root, verbosity=verbosity)
            logging.getLogger("litejelly.test").warning("watch out %s", verbosity)
        entries = logs.read_entries(self.root, 200)
        recorded = [e for e in entries if "watch out" in e["message"]]
        self.assertEqual(len(recorded), len(logs.VERBOSITY))

    def test_verbosity_changes_without_reconfiguring(self):
        logs.configure(self.root, verbosity="info")
        logging.getLogger("litejelly.test").debug("before")
        logs.set_verbosity("debug")
        logging.getLogger("litejelly.test").debug("after")
        messages = " ".join(e["message"] for e in logs.read_entries(self.root, 50))
        self.assertNotIn("before", messages)
        self.assertIn("after", messages)

    def test_file_can_be_turned_off(self):
        logs.configure(self.root, verbosity="info", to_file=False)
        logging.getLogger("litejelly.test").info("not written")
        self.assertFalse(logs.log_path(self.root).exists())
        self.assertIsNone(logs.current_file())

    def test_reconfiguring_does_not_stack_handlers(self):
        for _ in range(3):
            logs.configure(self.root, verbosity="info")
        root = logging.getLogger("litejelly")
        self.assertEqual(len(root.handlers), 2)  # console + file

    def test_messages_are_not_duplicated_by_the_root_logger(self):
        logs.configure(self.root, verbosity="info")
        logging.getLogger("litejelly.test").info("only once")
        entries = logs.read_entries(self.root, 50)
        matches = [e for e in entries if "only once" in e["message"]]
        self.assertEqual(len(matches), 1)

    def test_rotation_keeps_the_log_bounded(self):
        logs.configure(self.root, verbosity="info", max_mb=1, backups=1)
        logger = logging.getLogger("litejelly.test")
        for index in range(4000):
            logger.info("padding %s %s", index, "x" * 300)
        self.assertLessEqual(logs.file_size(), 2 * 1024 * 1024)
        rotated = list((self.root / "logs").glob("litejelly.log*"))
        self.assertLessEqual(len(rotated), 2)


class ReadEntriesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = logs.log_path(self.root)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, *lines):
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_parses_a_formatted_line(self):
        entry = logs.parse_line(
            "2026-09-13 14:22:01 INFO    litejelly.web: Indexed 9 videos")
        self.assertEqual(entry["level"], "INFO")
        self.assertEqual(entry["logger"], "litejelly.web")
        self.assertEqual(entry["message"], "Indexed 9 videos")

    def test_unparsable_lines_survive_as_messages(self):
        entry = logs.parse_line("  File \"server.py\", line 5, in main")
        self.assertEqual(entry["level"], "")
        self.assertIn("server.py", entry["message"])

    def test_missing_file_is_empty(self):
        self.assertEqual(logs.read_entries(self.root, 10), [])

    def test_only_the_tail_is_returned(self):
        self._write(*[f"2026-09-13 14:22:0{i % 10} INFO    a.b: line {i}"
                      for i in range(500)])
        entries = logs.read_entries(self.root, 10)
        self.assertEqual(len(entries), 10)
        self.assertEqual(entries[-1]["message"], "line 499")

    def test_filter_keeps_only_matching_levels(self):
        self._write(
            "2026-09-13 14:22:01 INFO    a.b: routine",
            "2026-09-13 14:22:02 WARNING a.b: careful",
            "2026-09-13 14:22:03 ERROR   a.b: broken",
        )
        messages = [e["message"] for e in logs.read_entries(self.root, 50, "warning")]
        self.assertEqual(messages, ["careful", "broken"])

    def test_filter_errors_only(self):
        self._write(
            "2026-09-13 14:22:01 INFO    a.b: routine",
            "2026-09-13 14:22:02 WARNING a.b: careful",
            "2026-09-13 14:22:03 ERROR   a.b: broken",
        )
        messages = [e["message"] for e in logs.read_entries(self.root, 50, "error")]
        self.assertEqual(messages, ["broken"])

    def test_traceback_stays_with_its_record(self):
        self._write(
            "2026-09-13 14:22:01 INFO    a.b: routine",
            "2026-09-13 14:22:03 ERROR   a.b: broken",
            "Traceback (most recent call last):",
            "  File \"x.py\", line 1",
        )
        entries = logs.read_entries(self.root, 50, "error")
        self.assertEqual(len(entries), 3)
        self.assertIn("Traceback", entries[1]["message"])

    def test_orphan_continuation_is_dropped_when_its_record_is_filtered_out(self):
        self._write(
            "  leading continuation with no record",
            "2026-09-13 14:22:03 ERROR   a.b: broken",
        )
        entries = logs.read_entries(self.root, 50, "error")
        self.assertEqual([e["message"] for e in entries], ["broken"])

    def test_no_filter_returns_everything(self):
        self._write(
            "2026-09-13 14:22:01 DEBUG   a.b: detail",
            "2026-09-13 14:22:02 INFO    a.b: routine",
        )
        self.assertEqual(len(logs.read_entries(self.root, 50)), 2)

    def test_invalid_utf8_does_not_raise(self):
        self.path.write_bytes(b"2026-09-13 14:22:01 INFO    a.b: caf\xe9\n")
        entries = logs.read_entries(self.root, 10)
        self.assertEqual(len(entries), 1)


class ConsoleOutputTests(unittest.TestCase):
    """The terminal is quiet by default, but never silent about problems."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        root = logging.getLogger("litejelly")
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        self._tmp.cleanup()

    def _console_level(self):
        return logs._console_handler.level

    def test_console_is_quiet_by_default(self):
        logs.configure(self.root, verbosity="info")
        self.assertEqual(self._console_level(), logging.WARNING)

    def test_warnings_still_reach_the_console_when_off(self):
        logs.configure(self.root, verbosity="info", to_console=False)
        self.assertLessEqual(self._console_level(), logging.WARNING)

    def test_console_mirrors_everything_when_on(self):
        logs.configure(self.root, verbosity="info", to_console=True)
        self.assertEqual(self._console_level(), logging.INFO)

    def test_console_follows_verbosity_when_on(self):
        logs.configure(self.root, verbosity="debug", to_console=True)
        self.assertEqual(self._console_level(), logging.DEBUG)

    def test_debug_does_not_leak_to_a_quiet_console(self):
        logs.configure(self.root, verbosity="debug", to_console=False)
        self.assertEqual(self._console_level(), logging.WARNING)

    def test_console_can_be_toggled_live(self):
        logs.configure(self.root, verbosity="info", to_console=False)
        logs.set_verbosity("info", to_console=True)
        self.assertEqual(self._console_level(), logging.INFO)
        logs.set_verbosity("info", to_console=False)
        self.assertEqual(self._console_level(), logging.WARNING)

    def test_the_file_still_records_everything_when_the_console_is_quiet(self):
        logs.configure(self.root, verbosity="debug", to_console=False)
        logging.getLogger("litejelly.test").debug("written anyway")
        entries = logs.read_entries(self.root, 50)
        self.assertTrue(any("written anyway" in e["message"] for e in entries))


class LogSettingsValidationTests(unittest.TestCase):
    def test_verbosity_whitelist(self):
        _, errors = settings.validate({"log_verbosity": "shout"})
        self.assertTrue(errors)

    def test_verbosity_accepted(self):
        clean, errors = settings.validate({"log_verbosity": "trace"})
        self.assertEqual(errors, [])
        self.assertEqual(clean["log_verbosity"], "trace")

    def test_quieter_than_info_is_rejected(self):
        # Offering these would contradict "warnings and errors are always kept".
        for value in ("warning", "error", "critical"):
            _, errors = settings.validate({"log_verbosity": value})
            self.assertTrue(errors, value)

    def test_file_toggle_is_boolean(self):
        clean, errors = settings.validate({"log_to_file": False})
        self.assertEqual(errors, [])
        self.assertIs(clean["log_to_file"], False)

    def test_console_toggle_is_boolean(self):
        clean, errors = settings.validate({"log_to_console": True})
        self.assertEqual(errors, [])
        self.assertIs(clean["log_to_console"], True)

    def test_console_toggle_applies_without_a_restart(self):
        self.assertEqual(settings.restart_required({}, {"log_to_console": True}), [])

    def test_rotation_bounds(self):
        _, errors = settings.validate({"log_max_mb": 0})
        self.assertTrue(errors)
        _, errors = settings.validate({"log_backups": 99})
        self.assertTrue(errors)
        clean, errors = settings.validate({"log_max_mb": 5, "log_backups": 0})
        self.assertEqual(errors, [])
        self.assertEqual(clean["log_max_mb"], 5)
        self.assertEqual(clean["log_backups"], 0)

    def test_log_verbosity_is_not_restart_required(self):
        self.assertEqual(settings.restart_required({}, {"log_verbosity": "debug"}), [])


if __name__ == "__main__":
    unittest.main()
