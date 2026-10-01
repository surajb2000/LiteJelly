"""Commit guards inspect the index, not unstaged changes, and never mutate either."""

from __future__ import annotations

import subprocess
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.pre_commit import MAX_ADDED_BYTES, check_index, git


class StagedChecksTests(unittest.TestCase):
    def setUp(self):
        """Create an isolated Git index so checks cannot affect the developer's work."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True, capture_output=True)
        git(self.root, "config", "core.autocrlf", "false")

    def stage(self, name: str, payload: bytes) -> Path:
        """Create and stage one fixture file, preserving arbitrary filenames and bytes."""
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        git(self.root, "add", "--", name)
        return target

    def test_clean_python_and_json_pass_without_execution(self):
        self.stage("example file.py", b"raise RuntimeError('must not execute')\n")
        self.stage("config.json", b'{"enabled": true}\n')
        self.assertEqual(check_index(self.root), [])
        self.assertEqual(list(self.root.rglob("*.pyc")), [])

    def test_unstaged_fix_does_not_hide_invalid_staged_python(self):
        target = self.stage("sample.py", b"def broken(\n")
        target.write_bytes(b"value = 1\n")
        tree = git(self.root, "write-tree")
        self.assertTrue(any("sample.py" in error for error in check_index(self.root)))
        self.assertEqual(git(self.root, "write-tree"), tree)
        self.assertEqual(target.read_bytes(), b"value = 1\n")

    def test_unstaged_error_does_not_block_valid_staged_python(self):
        target = self.stage("sample.py", b"value = 1\n")
        target.write_bytes(b"def broken(\n")
        self.assertEqual(check_index(self.root), [])

    def test_invalid_and_nonstandard_json_are_rejected(self):
        for payload in (b'{"missing":}', b'{"value": Infinity}', b'{"value": NaN}'):
            with self.subTest(payload=payload):
                self.stage("config.json", payload)
                self.assertTrue(any("config.json" in error for error in check_index(self.root)))

    def test_credentials_generated_state_and_binaries_are_rejected(self):
        for name in ("credentials.json", "opensubtitles.json", ".env", "data/progress.db",
                     ".cache/image.jpg", "ffmpeg.exe"):
            self.stage(name, b"fixture\n")
        problems = check_index(self.root)
        self.assertEqual(len(problems), 6)

    def test_whitespace_and_conflict_markers_are_rejected(self):
        self.stage("notes.txt", b"<<<<<<< HEAD\nvalue  \n=======\nother\n>>>>>>> branch\n")
        self.assertTrue(check_index(self.root))

    def test_new_large_files_are_rejected(self):
        self.stage("oversized.bin", b"x" * (MAX_ADDED_BYTES + 1))
        self.assertTrue(any("5 MiB" in error for error in check_index(self.root)))

    def test_deleted_files_are_not_loaded_from_the_worktree(self):
        target = self.stage("sample.py", b"value = 1\n")
        target.unlink()
        self.assertEqual(check_index(self.root), [])
        git(self.root, "rm", "--cached", "sample.py")
        self.assertEqual(check_index(self.root), [])

    def test_git_invokes_the_hook_and_blocks_then_allows_a_commit(self):
        """Exercise Git's real hook launcher using only an isolated fixture repository."""
        source_root = Path(__file__).resolve().parent.parent
        for name in (".githooks/pre-commit", "tools/pre_commit.py"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source_root / name).read_bytes())
        (self.root / ".githooks" / "pre-commit").chmod(0o755)
        environment = dict(os.environ, LITEJELLY_PYTHON=sys.executable,
                           GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        command = ["git", "-c", "core.hooksPath=.githooks", "-c", "user.name=Hook Test",
                   "-c", "user.email=hook@example.invalid", "commit", "--no-gpg-sign",
                   "-m", "Fixture commit"]
        self.stage("sample.py", b"return 1\n")
        blocked = subprocess.run(command, cwd=self.root, env=environment, capture_output=True)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn(b"pre-commit checks failed", blocked.stderr)
        self.stage("sample.py", b"value = 1\n")
        accepted = subprocess.run(command, cwd=self.root, env=environment, capture_output=True)
        self.assertEqual(accepted.returncode, 0, accepted.stderr.decode("utf-8", "replace"))
        self.assertIn(b"staged checks passed", accepted.stdout + accepted.stderr)

    def test_env_templates_remain_committable(self):
        self.stage(".env.example", b"API_KEY=\n")
        self.assertEqual(check_index(self.root), [])


if __name__ == "__main__":
    unittest.main()
