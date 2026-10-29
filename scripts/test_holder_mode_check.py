#!/usr/bin/env python3
"""Tests of scripts/holder_mode_check.py (python3 scripts/test_holder_mode_check.py)

The holder's script is the one the holders take (the planned bytes of scripts/shared_files.sh in conformance/planned/,
unchanged); the ALICE-Law it fetches is a local repository whose checker is a stand-in printing what $FAKE_COMPARED
and $FAKE_EXIT say, so what is tested is the redirected fetch, the re-pointed pin (the holder's script refuses a
SHA-256 that is not the one at the revision) and how the holder's result is read."""
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
import holder_mode_check as hm  # noqa: E402
import shared_files_check as sfc  # noqa: E402

CHECKER = '''import os, sys
n = os.environ.get("FAKE_COMPARED", "3")
print(f"shared_files_check: compared: files {n}, repos 1, copies {n}, pending 0, 0 finding(s)")
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def holder_script() -> bytes:
    """the scripts/shared_files.sh the holders move to: its planned bytes in this repository's table"""
    table = sfc.load((HERE.parent / "conformance" / "shared-files.toml").read_text(encoding="utf-8"))
    row = next(e for e in table if e["path"] == hm.SCRIPT)
    blob = row["pending"]["planned"] if sfc.is_planned(row) else None
    if blob is None:
        raise unittest.SkipTest(f"{hm.SCRIPT} is not planned now: no bytes beside the table")
    return (HERE.parent / "conformance" / sfc.PLANNED_DIR / blob).read_bytes()


class HolderMode(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.d)])
        self.law = self.d / "law"
        (self.law / "conformance").mkdir(parents=True)
        (self.law / "scripts").mkdir()
        (self.law / "conformance" / "shared-files.toml").write_text("# a table\n", encoding="utf-8")
        (self.law / "scripts" / "shared_files_check.py").write_text(CHECKER, encoding="utf-8")
        env = dict(os.environ, **ENV)
        for c in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "law"]):
            subprocess.run(["git", *c], cwd=self.law, env=env, check=True)
        self.holder = self.d / "holder"
        (self.holder / "scripts").mkdir(parents=True)
        (self.holder / ".github").mkdir()
        (self.holder / "scripts" / "shared_files.sh").write_bytes(holder_script())
        # a pin at another revision with other sums: the check must re-point it, or the holder's script refuses
        (self.holder / ".github" / "shared-files-pin").write_text(
            "name ALICE-Analytics\nrev " + "0" * 40 + "\ntable-sha256 " + "1" * 64 + "\nchecker-sha256 " + "2" * 64
            + "\n", encoding="utf-8")
        self.saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.saved)))

    def go(self, **env) -> tuple[int, str]:
        os.environ.update(env)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = hm.main(["--holder-tree", str(self.holder), "--law-repo", str(self.law)])
        return code, buf.getvalue()

    def test_the_holder_s_own_script_passes_against_this_revision(self):
        code, out = self.go()
        self.assertEqual(code, 0, out)
        self.assertIn("own check passes", out)
        self.assertIn("3 files compared", out)

    def test_a_holder_red_against_this_revision_is_red(self):
        code, out = self.go(FAKE_EXIT="1")
        self.assertEqual(code, 1, out)
        self.assertIn("the change breaks that holder", out)

    def test_a_check_that_compared_nothing_is_not_a_pass(self):
        code, out = self.go(FAKE_COMPARED="0")
        self.assertEqual(code, 2, out)
        self.assertIn("compared nothing", out)

    def test_a_holder_script_that_cannot_run_is_not_a_pass(self):
        code, out = self.go(FAKE_EXIT="2")
        self.assertEqual(code, 2, out)
        self.assertIn("failed to run", out)

    def test_a_holder_without_the_script_compares_nothing(self):
        (self.holder / "scripts" / "shared_files.sh").unlink()
        code, out = self.go()
        self.assertEqual(code, 2, out)
        self.assertIn("compared nothing", out)

    def test_a_pin_without_a_line_to_re_point_is_refused(self):
        (self.holder / ".github" / "shared-files-pin").write_text("name ALICE-Analytics\n", encoding="utf-8")
        code, out = self.go()
        self.assertEqual(code, 2, out)
        self.assertIn("no line for", out)


if __name__ == "__main__":
    unittest.main()
