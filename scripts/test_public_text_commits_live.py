#!/usr/bin/env python3
"""Tests of scripts/public_text_commits_live.py on local repositories (--source, no network), under a made-up key.

  - a commit inside the window with a finding: printed as its kind and keyed id only; the log names neither the
    repository nor the commit; exit 1
  - the same hit acknowledged by its keyed id: exit 0
  - a commit older than the window: not read
  - no key: exit 2
  - the gap guard: a previous successful run older than the window less one day is exit 2; none (or one before the
    pass existed) is a first run with an explicit look-back
  - 0 commits read while a repository was pushed to inside the window: a problem, not a pass
  - --resolve maps an id back to repository and commit on a key holder's machine, and is refused under CI
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "public_text_commits_live.py"
KEY = "6b" * 32
TODAY = "2026-12-01"
# a word the unkeyed half finds (the internal tracker's name), built here so this file does not hold it
TRACKER = "Back" + "log"
NAME = "ALICE-Sample"

sys.path.insert(0, str(HERE))
import public_text_commits_live as pcl  # noqa: E402


def git(repo: Path, *args: str, date: str | None = None) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(repo), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          encoding="utf-8", env=env).stdout


class CommitsLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name, "src")
        self.repo = self.src / NAME
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# a clean file\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "start", date="2026-10-01T00:00:00Z")
        self.home = Path(self.tmp.name, "home")
        self.home.mkdir()
        self.ack = Path(self.tmp.name, "ack.toml")
        self.ack.write_text("[acknowledged]\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, text: str, message: str, date: str) -> str:
        (self.repo / "notes.md").write_text(text, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message, date=date)
        return git(self.repo, "rev-parse", "HEAD").strip()

    def run_live(self, *argv: str, key: bool = True, ci: bool = False) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}
        if key:
            env["ALICE_PRIVATE_NAME_HMAC_KEY"] = KEY
        if ci:
            env["CI"] = "true"
        return subprocess.run([sys.executable, str(SCRIPT), "--source", str(self.src), "--today", TODAY,
                               "--ack", str(self.ack), *argv], capture_output=True, text=True, encoding="utf-8",
                              env=env)

    def test_a_hit_is_printed_as_kind_and_keyed_id_only(self):
        sha = self.commit("clean\n", f"see the {TRACKER} row", "2026-11-29T00:00:00Z")
        p = self.run_live("--last-success", "none")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertRegex(p.stdout, r"(?m)^found: internal tracker [0-9a-f]{16}$")
        self.assertIn("1 commits read since 2026-11-24 (7 days), 1 hits, 0 acknowledged, 1 new", p.stdout)
        # the public log names neither the repository nor the commit
        self.assertNotIn(NAME, p.stdout + p.stderr)
        self.assertNotIn(sha[:12], p.stdout + p.stderr)
        self.assertNotIn(sha[:7], p.stdout + p.stderr)

    def test_an_acknowledged_hit_passes(self):
        sha = self.commit("clean\n", f"see the {TRACKER} row", "2026-11-29T00:00:00Z")
        hid = pcl.hit_id(bytes.fromhex(KEY), NAME, sha[:12], "internal tracker")
        self.ack.write_text(f'[acknowledged]\n"{hid}" = "user: accept, 2026-12-01"\n', encoding="utf-8")
        p = self.run_live("--last-success", "none")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("1 hits, 1 acknowledged, 0 new", p.stdout)

    def test_a_commit_older_than_the_window_is_not_read(self):
        self.commit("clean\n", f"see the {TRACKER} row", "2026-11-01T00:00:00Z")
        p = self.run_live("--last-success", "none")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0 commits read", p.stdout)

    def test_without_the_key_the_run_refuses(self):
        p = self.run_live("--last-success", "none", key=False)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("the name key is not set", p.stderr)

    def test_the_gap_guard_and_the_first_run(self):
        self.commit("clean\n", "clean change", "2026-11-29T00:00:00Z")
        p = self.run_live("--last-success", "2026-11-22")  # 9 days before, the window is 7
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("were not read", p.stderr)
        p = self.run_live("--last-success", "2026-11-28")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("first run", p.stdout)
        p = self.run_live("--last-success", "none")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("first run of this pass: an explicit look-back of 7 days (from 2026-11-24)", p.stdout)
        p = self.run_live("--last-success", "2026-11-10")  # before the pass existed: a first run, not a gap
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("first run of this pass", p.stdout)

    def test_zero_commits_read_while_pushed_is_a_problem(self):
        start = datetime.date(2026, 11, 24)
        pushed = [{"name": "x", "pushed_at": "2026-11-30T00:00:00Z"}]
        quiet = [{"name": "x", "pushed_at": "2026-11-01T00:00:00Z"}]
        self.assertIn("0 commits read", pcl.zero_read_problem(0, pushed, start))
        self.assertIsNone(pcl.zero_read_problem(0, quiet, start))
        self.assertIsNone(pcl.zero_read_problem(3, pushed, start))

    def test_resolve_maps_an_id_back_off_ci_and_is_refused_in_ci(self):
        sha = self.commit("clean\n", f"see the {TRACKER} row", "2026-11-29T00:00:00Z")
        hid = pcl.hit_id(bytes.fromhex(KEY), NAME, sha[:12], "internal tracker")
        p = self.run_live("--resolve", hid[:16])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"{hid[:16]} {NAME} {sha[:12]}", p.stdout)
        p = self.run_live("--resolve", hid[:16], ci=True)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertNotIn(NAME, p.stdout)

    def test_check_ack_reads_the_file_without_the_key(self):
        hid = "c" * 64
        self.ack.write_text(f'[acknowledged]\n"{hid}" = "user: accept, 2026-12-01"\n', encoding="utf-8")
        p = self.run_live("--check-ack", key=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("1 acknowledged hit(s)", p.stdout)
        self.ack.write_text('[acknowledged]\n"abc" = "x"\n', encoding="utf-8")
        p = self.run_live("--check-ack", key=False)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        # the committed file itself parses
        p = subprocess.run([sys.executable, str(SCRIPT), "--check-ack"], capture_output=True, text=True,
                           encoding="utf-8", env={"PATH": os.environ["PATH"], "HOME": str(self.home)})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_the_ack_file_rejects_a_malformed_row(self):
        with self.assertRaises(pcl.Unreadable):
            pcl.load_ack('[acknowledged]\n"abc" = "x"\n')
        with self.assertRaises(pcl.Unreadable):
            pcl.load_ack(f'[acknowledged]\n"{"a" * 64}" = ""\n')


if __name__ == "__main__":
    unittest.main()
