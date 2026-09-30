"""A mutation counts only when the same tests run and an assertion fails."""

from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tools.mutation_runner import execute_suite, replacement, run_mutations, verdict


class VerdictTests(unittest.TestCase):
    def report(self):
        """Build a successful report for classification checks."""
        return dict(tests=3, failures=0, errors=0, skipped=0,
                    expected_failures=0, unexpected_successes=0)

    def test_passing_tests_are_a_surviving_mutation(self):
        self.assertEqual(verdict(self.report(), 3, 0), "SURVIVED")

    def test_only_assertion_failure_counts_as_caught(self):
        report = self.report()
        report["failures"] = 1
        self.assertEqual(verdict(report, 3, 1), "caught")

    def test_broken_runs_cannot_count_as_caught(self):
        for field in ("errors", "skipped", "expected_failures", "unexpected_successes"):
            with self.subTest(field=field):
                report = self.report()
                report[field] = 1
                report["failures"] = 1
                self.assertEqual(verdict(report, 3, 1), "INVALID")

    def test_incomplete_or_crashed_runs_are_invalid(self):
        for report, returncode in (({}, 1), (self.report(), 2), (self.report(), -1)):
            self.assertEqual(verdict(report, 3, returncode), "INVALID")
        self.assertEqual(verdict(self.report(), 4, 0), "INVALID")

    def test_malformed_report_fields_are_invalid(self):
        for value in (None, "1", True, -1):
            report = self.report()
            report["failures"] = value
            self.assertEqual(verdict(report, 3, 1), "INVALID")


class ReplacementTests(unittest.TestCase):
    def test_missing_ambiguous_and_no_op_targets_fail(self):
        for source, before, after in (("value", "missing", "x"),
                                      ("value value", "value", "x"),
                                      ("value", "value", "value")):
            with self.subTest(source=source, before=before):
                with self.assertRaises(ValueError):
                    replacement(source, {"from": before, "to": after})

    def test_newlines_and_unicode_are_preserved(self):
        self.assertEqual(replacement("caf\u00e9\r\nvalue\r\n",
                                     {"from": "value\n", "to": "changed\n"}),
                         "caf\u00e9\nchanged\n")


class ProcessTests(unittest.TestCase):
    def setUp(self):
        """Create a tiny repository whose test emits misleading diagnostic text."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for package in ("litejelly", "tests"):
            (self.root / package).mkdir()
            (self.root / package / "__init__.py").write_text("", encoding="utf-8")
        self.target = self.root / "litejelly" / "sample.py"
        self.target.write_bytes(b"def value():\r\n    return 1\r\n")
        self.test = self.root / "tests" / "test_sample.py"
        self.test.write_text(
            "import unittest\nfrom litejelly.sample import value\n"
            "class SampleTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        print('NativeCommandError: Error text is not a verdict')\n"
            "        self.assertEqual(value(), 1)\n", encoding="utf-8")

    def run_case(self, before, after):
        """Run one mutation and capture its verdict without changing the fixture repo."""
        original = self.target.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = run_mutations(self.root, "tests.test_sample", [{
                "name": "probe", "file": "litejelly/sample.py", "from": before, "to": after,
            }])
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(list(self.root.rglob("*.mutbak")), [])
        return result, output.getvalue()

    def test_only_assertion_failures_count_in_real_processes(self):
        for after, expected, exitcode in (("return 2", "caught", 0),
                                           ("return int(1)", "SURVIVED", 1),
                                           ("raise RuntimeError('fault')", "INVALID", 1),
                                           ("return (", "INVALID", 1)):
            with self.subTest(after=after):
                result, output = self.run_case("return 1", after)
                self.assertEqual(result, exitcode, output)
                self.assertIn("probe: " + expected, output)

    def test_missing_target_fails_the_command(self):
        result, output = self.run_case("missing", "replacement")
        self.assertEqual(result, 1)
        self.assertIn("TARGET MISSING", output)

    def test_failing_baseline_prevents_mutation(self):
        self.target.write_bytes(b"def value():\n    return 0\n")
        result, output = self.run_case("return 0", "return 2")
        self.assertEqual(result, 1)
        self.assertIn("BASELINE FAILED", output)
        self.assertNotIn("probe:", output)

    def test_no_tests_is_not_a_passing_baseline(self):
        self.test.write_text("", encoding="utf-8")
        result, output = self.run_case("return 1", "return 2")
        self.assertEqual(result, 1)
        self.assertIn("BASELINE FAILED", output)

    def test_timed_out_process_is_invalid(self):
        with mock.patch("tools.mutation_runner.subprocess.run",
                        side_effect=subprocess.TimeoutExpired("python", 120)):
            report, returncode, output = execute_suite(self.root, "tests.test_sample",
                                                     self.root / "report.json")
        self.assertEqual(verdict(report, 1, returncode), "INVALID")
        self.assertIn("timed out", output)


if __name__ == "__main__":
    unittest.main()
