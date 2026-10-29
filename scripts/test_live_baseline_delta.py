#!/usr/bin/env python3
"""Tests of scripts/live_baseline_delta.py (python3 scripts/test_live_baseline_delta.py)

The checker is a stand-in: it prints one finding per line of the baseline it is given that starts with "find ",
plus the lines of $FAKE_ALWAYS (another repository's findings, the same with either baseline), and a summary;
$FAKE_EXIT and $FAKE_UNREAD change its exit code and its unread count."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_baseline_delta as d  # noqa: E402

FAKE = '''import argparse, os, sys
ap = argparse.ArgumentParser(); ap.add_argument("--baseline"); ap.add_argument("--local"); ap.add_argument("rest", nargs="*")
a = ap.parse_args()
EMBEDDED = []  # findings the checker allows by itself (a baseline kept in the checker's own text)
found = [ln[5:] for ln in open(a.baseline, encoding="utf-8").read().splitlines() if ln.startswith("find ")]
found += EMBEDDED
if a.local:
    name, tree = a.local.split("=", 1)
    probe = os.path.join(tree, "docs", "zz-probe.md")
    if os.path.exists(probe):
        found += [f"{name}: docs/zz-probe.md: private path" for ln in open(probe) if ln.strip()]
found += [x for x in os.environ.get("FAKE_ALWAYS", "").split("|") if x]
for f in found:
    print("found: " + f)
read = int(os.environ.get("FAKE_READ", "3"))
print(f"fake live: 3 public repositories, {read} read, 0 empty, {os.environ.get('FAKE_UNREAD', '0')} unread")
sys.exit(int(os.environ.get("FAKE_EXIT", "1" if found else "0")))
'''


class Delta(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.d)])
        (self.d / "fake.py").write_text(FAKE, encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.d)], check=True)
        self.env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))

    def baselines(self, base: str, change: str):
        b = self.d / "baseline.toml"
        b.write_text(base, encoding="utf-8")
        g = ["git", "-C", str(self.d), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-q", "-m", "base"], check=True)
        b.write_text(change, encoding="utf-8")

    def go(self, *extra: str, **env) -> tuple[int, str]:
        os.environ.update(env)
        buf = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            with contextlib.redirect_stdout(buf):
                code = d.main(["--checker", str(self.d / "fake.py"), "--baseline", "baseline.toml", "--base-ref", "HEAD",
                               *extra])
        finally:
            os.chdir(cwd)
        return code, buf.getvalue()

    def test_another_repositorys_growth_is_reported_not_red(self):
        self.baselines("", "")
        code, out = self.go(FAKE_ALWAYS="ALICE-X: a.yml: private repository name")
        self.assertEqual(code, 0, out)
        self.assertIn("elsewhere (not this change's): ALICE-X: a.yml: private repository name", out)
        self.assertIn("0 finding(s) this change makes worse, 1 elsewhere", out)

    def test_a_finding_only_the_change_has_is_red(self):
        self.baselines("", "find ALICE-Y: b.toml: an entry removed while its occurrence is there\n")
        code, out = self.go()
        self.assertEqual(code, 1, out)
        self.assertIn("worse with this change: ALICE-Y: b.toml: an entry removed while its occurrence is there", out)

    def test_a_finding_the_change_fixes_is_reported(self):
        self.baselines("find ALICE-Z: c: stale\n", "")
        code, out = self.go()
        self.assertEqual(code, 0, out)
        self.assertIn("fixed by this change: ALICE-Z: c: stale", out)

    def test_a_line_the_change_adds_to_this_repository_is_this_changes(self):
        # ba's probe: the base run reads this repository at the base, the change's run at the checkout, so a private
        # path the change adds here is worse with this change, not "elsewhere"
        self.baselines("", "")
        (self.d / "docs").mkdir()
        (self.d / "docs" / "zz-probe.md").write_text("a private line\n")
        code, out = self.go("--self", "ALICE-Law")
        self.assertEqual(code, 1, out)
        self.assertIn("worse with this change: ALICE-Law: docs/zz-probe.md: private path", out)

    def test_a_line_this_repository_already_had_is_not_the_changes(self):
        self.baselines("", "")
        (self.d / "docs").mkdir()
        (self.d / "docs" / "zz-probe.md").write_text("a private line\n")
        g = ["git", "-C", str(self.d), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-q", "-m", "had it"], check=True)
        code, out = self.go("--self", "ALICE-Law")
        self.assertEqual(code, 0, out)
        self.assertIn("elsewhere (not this change's): ALICE-Law: docs/zz-probe.md: private path", out)

    def test_a_change_to_the_checker_s_own_baseline_is_this_changes(self):
        # 2026-10-30: lowering the baseline kept inside public_text_check.py was reported "elsewhere", because both runs
        # used the change's checker; the base run runs the base's checker, so what the change's checker adds is worse
        self.baselines("", "")
        fake = self.d / "fake.py"
        fake.write_text(fake.read_text(encoding="utf-8").replace(
            "EMBEDDED = []", 'EMBEDDED = ["ALICE-Y: c.py: allowance lowered"]'), encoding="utf-8")
        code, out = self.go()
        self.assertEqual(code, 1, out)
        self.assertIn("worse with this change: ALICE-Y: c.py: allowance lowered", out)

    def new_checker(self, embedded: str = "[]") -> tuple[int, str]:
        # a change that adds a checker (and its baseline) the base does not have
        self.baselines("", "")
        (self.d / "new_checker.py").write_text((self.d / "fake.py").read_text(encoding="utf-8").replace(
            "EMBEDDED = []", f"EMBEDDED = {embedded}"), encoding="utf-8")
        buf = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            with contextlib.redirect_stdout(buf):
                code = d.main(["--checker", "new_checker.py", "--baseline", "baseline.toml", "--base-ref", "HEAD"])
        finally:
            os.chdir(cwd)
        return code, buf.getvalue()

    def test_a_new_checker_with_no_findings_passes(self):
        # the base has no findings of a checker it does not have: a new checker is not "nothing to compare"
        code, out = self.new_checker()
        self.assertEqual(code, 0, out)
        self.assertIn("a new checker, every finding is this change's", out)

    def test_a_new_checker_s_findings_are_all_this_changes(self):
        code, out = self.new_checker('["ALICE-Z: d.py: a new finding"]')
        self.assertEqual(code, 1, out)
        self.assertIn("worse with this change: ALICE-Z: d.py: a new finding", out)

    def test_an_unread_repository_is_red(self):
        self.baselines("", "")
        code, out = self.go(FAKE_UNREAD="1")
        self.assertEqual(code, 1, out)
        self.assertIn("could not read 1 repositories", out)

    def test_a_failed_checker_is_red(self):
        self.baselines("", "")
        code, out = self.go(FAKE_EXIT="2")
        self.assertEqual(code, 1, out)
        self.assertIn("run failed (exit 2)", out)

    def test_a_run_that_read_nothing_is_red(self):
        self.baselines("", "")
        code, out = self.go(FAKE_READ="0")
        self.assertEqual(code, 1, out)
        self.assertIn("read no repository (compared nothing)", out)

    def test_a_base_without_the_baseline_compares_nothing(self):
        (self.d / "other").write_text("x")
        g = ["git", "-C", str(self.d), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-q", "-m", "base"], check=True)
        (self.d / "baseline.toml").write_text("", encoding="utf-8")
        code, out = self.go()
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
