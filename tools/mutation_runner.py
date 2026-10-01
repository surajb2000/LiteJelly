"""Run targeted mutations in an isolated copy and require assertion failures."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def verdict(report: dict, baseline_count: int, returncode: int) -> str:
    """Classify assertions separately from test errors, skips and process failures."""
    fields = ("tests", "failures", "errors", "skipped", "expected_failures", "unexpected_successes")
    if any(type(report.get(field)) is not int or report[field] < 0 for field in fields):
        return "INVALID"
    if (report.get("tests") != baseline_count or report.get("errors")
            or report.get("skipped") or report.get("expected_failures")
            or report.get("unexpected_successes")):
        return "INVALID"
    if report.get("failures", 0) > 0 and returncode == 1:
        return "caught"
    if report.get("failures") == 0 and returncode == 0:
        return "SURVIVED"
    return "INVALID"


def replacement(source: str, mutation: dict) -> str:
    """Replace one unambiguous target, accepting either source newline style."""
    source = source.replace("\r\n", "\n")
    before = mutation["from"].replace("\r\n", "\n")
    after = mutation["to"].replace("\r\n", "\n")
    count = source.count(before) if before else 0
    if count != 1:
        raise ValueError(f"TARGET {'MISSING' if not count else 'AMBIGUOUS'} ({count} matches)")
    if before == after:
        raise ValueError("NO-OP mutation")
    return source.replace(before, after, 1)


def run_worker(suite: str, report_path: Path) -> int:
    """Run unittest in a child interpreter and write a machine-readable result."""
    sys.path.insert(0, str(Path.cwd()))
    with contextlib.redirect_stdout(sys.stderr):
        tests = unittest.defaultTestLoader.loadTestsFromNames(suite.split(","))
        result = unittest.TextTestRunner(stream=sys.stderr).run(tests)
    report = {
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


def execute_suite(root: Path, suite: str, report_path: Path) -> tuple[dict, int, str]:
    """Collect a fresh subprocess report; timeouts and missing reports are invalid."""
    report_path.unlink(missing_ok=True)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    try:
        process = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker", suite,
             "--report", str(report_path)],
            cwd=root, env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {}, -1, "Test process timed out"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            report = {}
    except (OSError, ValueError):
        report = {}
    return report, process.returncode, process.stdout + process.stderr


def run_mutations(root: Path, suite: str, mutations: list[dict]) -> int:
    """Require a clean baseline and fail on survivors, invalid runs or missing targets."""
    if not mutations:
        print("No mutations supplied", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="litejelly-mutations-") as directory:
        sandbox = Path(directory).resolve()
        for name in ("litejelly", "static", "tests", "tools"):
            source = root / name
            if source.is_dir():
                shutil.copytree(source, sandbox / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.mutbak"))
        if (root / "server.py").is_file():
            shutil.copy2(root / "server.py", sandbox / "server.py")
        report_path = sandbox / "result.json"
        report, returncode, output = execute_suite(sandbox, suite, report_path)
        count = report.get("tests", 0)
        if not count or verdict(report, count, returncode) != "SURVIVED":
            print("BASELINE FAILED\n" + output, file=sys.stderr)
            return 1
        print(f"Baseline: {count} tests passed")
        failed = False
        for mutation in mutations:
            name = mutation.get("name", "unnamed")
            platform = mutation.get("platform")
            if platform and platform != sys.platform:
                print(f"{name}: SKIPPED (requires {platform})")
                continue
            target = sandbox / mutation["file"]
            try:
                target = target.resolve()
                if sandbox not in target.parents:
                    raise ValueError("Target outside the sandbox")
                original = target.read_bytes()
                changed = replacement(original.decode("utf-8"), mutation)
            except (OSError, ValueError, KeyError) as error:
                print(f"{name}: {error}")
                failed = True
                continue
            try:
                target.write_bytes(changed.encode("utf-8"))
                report, returncode, output = execute_suite(sandbox, suite, report_path)
                status = verdict(report, count, returncode)
                print(f"{name}: {status}")
                if status != "caught":
                    failed = True
                    if status == "INVALID":
                        print(output, file=sys.stderr)
            finally:
                target.write_bytes(original)
        return int(failed)


def main() -> int:
    """Read a mutation manifest from stdin or run the isolated unittest worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite")
    parser.add_argument("--worker")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.worker and args.report:
        return run_worker(args.worker, args.report)
    if not args.suite:
        parser.error("--suite is required")
    try:
        mutations = json.load(sys.stdin)
        if isinstance(mutations, dict):
            mutations = [mutations]
        if not isinstance(mutations, list):
            raise ValueError("Expected a list of mutations")
    except ValueError as error:
        parser.error(str(error))
    return run_mutations(Path(__file__).resolve().parent.parent, args.suite, mutations)


if __name__ == "__main__":
    sys.exit(main())
