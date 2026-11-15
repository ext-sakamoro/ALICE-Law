#!/usr/bin/env python3
"""Tests of scripts/public_text_commits_live.py on local repositories (--source, no network), under a made-up key.

  - a commit inside the window with a finding: printed as its kind and keyed id only; the log names neither the
    repository nor the commit; exit 1
  - the same hit acknowledged by its keyed id: exit 0
  - a commit older than the window: not read
  - no key: exit 2
  - the window: a previous complete read inside it changes nothing; an older one widens the window back to it and
    the judgement is red once (the gap is read, and seen); one more than MAX_DAYS ago is exit 2 before any read; none
    (or one before the pass existed) is a first run with an explicit look-back
  - the previous complete read is the newest run whose read step succeeded, whatever the other jobs did
  - --read-out writes kind and keyed id only and exits 0 on hits; --judge exits 1 on a new hit or a gap
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
        self.assertIn("1 commits read since 2026-11-24, 1 hits", p.stdout)
        self.assertIn("1 hits, 0 acknowledged, 1 new", p.stdout)
        # the public log names neither the repository nor the commit
        self.assertNotIn(NAME, p.stdout + p.stderr)
        self.assertNotIn(sha[:12], p.stdout + p.stderr)
        self.assertNotIn(sha[:7], p.stdout + p.stderr)

    def test_an_acknowledged_hit_passes(self):
        sha = self.commit("clean\n", f"see the {TRACKER} row", "2026-11-29T00:00:00Z")
        hid = pcl.hit_id(bytes.fromhex(KEY), NAME, sha[:12], "internal tracker")
        self.ack.write_text(f'[acknowledged]\n"{hid}" = "accepted by the owner on 2026-12-01"\n', encoding="utf-8")
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

    def test_the_gap_is_read_and_red_once(self):
        # a commit 8 days back is outside the 7-day window: read only because the window reaches back to the gap
        self.commit("clean\n", f"see the {TRACKER} row", "2026-11-23T00:00:00Z")
        p = self.run_live("--last-success", "2026-11-22", "--previous", "none")  # 9 days before
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("from 2026-11-21", p.stdout)
        self.assertIn("1 commits read since 2026-11-21", p.stdout)
        self.assertIn("this run is red once so the gap is seen", p.stderr)
        # the boundary: 6 days back is no gap (window 7), 7 days back is one
        self.assertEqual(pcl.gap_of(datetime.date(2026, 12, 1), 7, datetime.date(2026, 11, 25)), 0)
        self.assertEqual(pcl.gap_of(datetime.date(2026, 12, 1), 7, datetime.date(2026, 11, 24)), 7)
        p = self.run_live("--last-success", "2026-11-28", "--previous", "none")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("first run", p.stdout)
        self.assertNotIn("1 commits read", p.stdout)  # the 8-day-old commit is outside the plain window
        p = self.run_live("--last-success", "2026-12-01", "--today", "2027-01-15", "--previous", "none")  # more than MAX_DAYS
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("--since 2026-12-01", p.stderr)
        self.assertNotIn("commits read", p.stdout)
        p = self.run_live("--since", "2026-10-30", "--previous", "none")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("(--since)", p.stdout)
        p = self.run_live("--last-success", "none")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("first run of this pass: an explicit look-back of 7 days (from 2026-11-24)", p.stdout)
        p = self.run_live("--last-success", "2026-11-10")  # before the pass existed: a first run, not a gap
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("first run of this pass", p.stdout)

    def test_the_previous_complete_read_is_the_read_step_alone(self):
        def jobs(read, other="failure"):
            return [{"name": "Every public ALICE-* repository's production panic sites only go down",
                     "conclusion": other, "steps": [{"name": "x", "conclusion": other}]},
                    {"name": pcl.READ_JOB, "conclusion": "failure",
                     "steps": [{"name": "Test the new-commit reader", "conclusion": "success"},
                               {"name": pcl.READ_STEP, "conclusion": read},
                               {"name": "The hits against the acknowledged ones", "conclusion": "failure"}]}]
        # another job red and the judgement red (an unacknowledged hit): the read still counts
        self.assertTrue(pcl.read_step_succeeded(jobs("success")))
        self.assertFalse(pcl.read_step_succeeded(jobs("failure", other="success")))
        self.assertFalse(pcl.read_step_succeeded(jobs("skipped", other="success")))
        self.assertFalse(pcl.read_step_succeeded([]))

    def test_read_out_writes_kind_and_id_and_judge_decides(self):
        self.commit("clean\n", f"see the {TRACKER} row", "2026-11-29T00:00:00Z")
        out = Path(self.tmp.name, "hits.txt")
        p = self.run_live("--last-success", "none", "--read-out", str(out))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        text = out.read_text(encoding="utf-8")
        self.assertRegex(text, r"^# public text commit hits, read 2026-12-01 from 2026-11-24\ninternal tracker\t[0-9a-f]{64}\n$")
        self.assertNotIn(NAME, text)
        p = self.run_live("--judge", str(out), key=False)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        hid = text.splitlines()[1].split("\t")[1]
        self.ack.write_text(f'[acknowledged]\n"{hid}" = "accepted by the owner on 2026-12-01"\n', encoding="utf-8")
        p = self.run_live("--judge", str(out), key=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out.write_text(f"#gap\t2026-11-20\t11\n{text}", encoding="utf-8")
        p = self.run_live("--judge", str(out), key=False)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        out.write_text("internal tracker\tabc\n", encoding="utf-8")
        p = self.run_live("--judge", str(out), key=False)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

    def test_an_unacknowledged_hit_is_carried_after_it_leaves_the_window(self):
        prev = Path(self.tmp.name, "prev.txt")
        hid = "d" * 64
        prev.write_text(f"# public text commit hits, read 2026-11-30 from 2026-11-23\ninternal tracker\t{hid}\n",
                        encoding="utf-8")
        out = Path(self.tmp.name, "hits.txt")
        # nothing new today: the carried hit is still red
        p = self.run_live("--last-success", "2026-11-30", "--previous", str(prev), "--read-out", str(out))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("1 unacknowledged hits carried", p.stdout)
        self.assertIn(hid, out.read_text(encoding="utf-8"))
        p = self.run_live("--judge", str(out), key=False)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        # acknowledged: no longer carried, and green
        self.ack.write_text(f'[acknowledged]\n"{hid}" = "accepted by the owner on 2026-12-01"\n', encoding="utf-8")
        p = self.run_live("--last-success", "2026-11-30", "--previous", str(prev), "--read-out", str(out))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn(hid, out.read_text(encoding="utf-8"))
        p = self.run_live("--judge", str(out), key=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # a previous complete read whose hits are not given is not a fresh start
        p = self.run_live("--last-success", "2026-11-30", "--read-out", str(out))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

    def test_no_complete_read_after_the_first_days_is_not_a_first_run(self):
        # only push runs on main (or none at all) after this pass has run for more than a window: exit 2, not a
        # first run
        jobs = {}
        runs = [{"id": 1, "created_at": "2026-11-29T00:00:00Z"}]
        self.assertIsNone(pcl.find_complete_read(runs, lambda rid: jobs.get(rid, [])))
        with self.assertRaises(pcl.Unreadable):
            pcl.window_start(datetime.date(2026, 12, 1), 7, None, None)
        # inside the first window after FIRST_RUN it is a first run
        start, what = pcl.window_start(datetime.date(2026, 11, 20), 7, None, None)
        self.assertIn("first run", what)
        # the newest run whose read step succeeded is the one found, older ones are not asked
        ok = [{"name": pcl.READ_JOB, "steps": [{"name": pcl.READ_STEP, "conclusion": "success"}]}]
        asked = []
        runs = [{"id": 1, "created_at": "2026-11-20T00:00:00Z"}, {"id": 2, "created_at": "2026-11-29T00:00:00Z"},
                {"id": 3, "created_at": "2026-11-30T00:00:00Z"}]
        found = pcl.find_complete_read(runs, lambda rid: (asked.append(rid), ok if rid in (1, 2) else [])[1])
        self.assertEqual(found, (datetime.date(2026, 11, 29), 2))
        self.assertEqual(asked, [3, 2])

    def test_the_run_search_pages_back_to_the_oldest_day_only(self):
        pages = {1: [{"id": i, "created_at": "2026-11-30T00:00:00Z"} for i in range(100)],
                 2: [{"id": 100, "created_at": "2026-11-29T00:00:00Z"}, {"id": 101, "created_at": "2026-10-01T00:00:00Z"},
                     {"id": 102, "created_at": "2026-09-01T00:00:00Z"}]}
        asked = []

        def fake(url):
            page = int(url.rsplit("page=", 1)[1])
            asked.append(page)
            return {"workflow_runs": pages.get(page, [])}

        real = pcl.hl.api_get
        pcl.hl.api_get = fake
        try:
            runs = pcl.runs_since("o", "schedule", datetime.date(2026, 10, 30))
        finally:
            pcl.hl.api_get = real
        self.assertEqual(len(runs), 101)
        self.assertEqual(asked, [1, 2])

    def test_the_kind_is_part_of_the_id(self):
        k = bytes.fromhex(KEY)
        self.assertNotEqual(pcl.hit_id(k, NAME, "a" * 12, "internal tracker"), pcl.hit_id(k, NAME, "a" * 12, "device"))

    def test_main_reports_zero_read_while_pushed(self):
        self.commit("clean\n", "clean change", "2026-11-29T00:00:00Z")
        real = pcl.window_commits
        pcl.window_commits = lambda g, start: []
        try:
            env = {"ALICE_PRIVATE_NAME_HMAC_KEY": KEY, "HOME": str(self.home)}
            old = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                rc = pcl.main(["--source", str(self.src), "--today", TODAY, "--ack", str(self.ack),
                               "--last-success", "none"])
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        finally:
            pcl.window_commits = real
        self.assertEqual(rc, 2)

    def test_an_unreadable_repository_is_not_named_in_the_public_log(self):
        broken = self.src / "ALICE-Broken"
        broken.mkdir()
        (broken / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        p = self.run_live("--last-success", "none")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("couldn't read:", p.stdout)
        self.assertNotIn("ALICE-Broken", p.stdout + p.stderr)

    def test_a_finding_outside_the_commits_gets_a_fixed_kind(self):
        self.assertEqual(pcl.parse_hit(".public-text.toml: allow line names a path"), ("-", pcl.OUTSIDE, ""))

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
        self.assertIn(f"{hid} {NAME} {sha[:12]}", p.stdout)
        p = self.run_live("--resolve", hid[:16], ci=True)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertNotIn(NAME, p.stdout)

    def test_check_ack_reads_the_file_without_the_key(self):
        hid = "c" * 64
        self.ack.write_text(f'[acknowledged]\n"{hid}" = "accepted by the owner on 2026-12-01"\n', encoding="utf-8")
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
        with self.assertRaises(pcl.Unreadable):  # a value without the date of the decision
            pcl.load_ack(f'[acknowledged]\n"{"a" * 64}" = "accepted"\n')


if __name__ == "__main__":
    unittest.main()
