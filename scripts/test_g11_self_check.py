#!/usr/bin/env python3
"""tests of scripts/g11_self_check.py: a stub ALICE-Law checkout (a git repository whose four check scripts record
their arguments and exit with a chosen status) and a crate directory with its pin

    python3 scripts/test_g11_self_check.py
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import importlib.util

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("g11sc", HERE / "g11_self_check.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

NAMES = ("g11_table_gen.py", "g11_table_check.py", "shared_files_check.py", "conformance_roster_check.py")
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def h(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class SelfCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.law, self.crate, self.log = root / "law", root / "crate", root / "argv.jsonl"
        (self.law / "scripts").mkdir(parents=True)
        (self.law / "conformance").mkdir()
        (self.law / "conformance" / "shared-files.toml").write_text("# table\n", encoding="utf-8")
        for n in NAMES:
            (self.law / "scripts" / n).write_text(
                "import json, os, sys\n"
                f"open({str(self.log)!r}, 'a').write(json.dumps([{n!r}] + sys.argv[1:]) + '\\n')\n"
                + ("print(\"shared_files_check: own copies not at a pending row's target: \" + os.environ.get('OFF_N', '0'))\n"
                   if n == "shared_files_check.py" else "") +
                f"print('summary of {n}')\n"
                f"sys.exit(int(os.environ.get('RC_' + {n.split('.')[0]!r}, '0')))\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.law)], check=True, env=ENV)
        subprocess.run(["git", "-C", str(self.law), "add", "-A"], check=True, env=ENV)
        subprocess.run(["git", "-C", str(self.law), "commit", "-q", "-m", "c"], check=True, env=ENV)
        self.rev = subprocess.run(["git", "-C", str(self.law), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()
        (self.crate / ".github").mkdir(parents=True)
        self.pin()
        self.saved_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)
        self.tmp.cleanup()

    def pin(self, rev=None, table=None, checker=None):
        (self.crate / ".github" / "shared-files-pin").write_text(
            f"name ALICE-X\nrev {rev or self.rev}\n"
            f"table-sha256 {table or h(self.law / 'conformance' / 'shared-files.toml')}\n"
            f"checker-sha256 {checker or h(self.law / 'scripts' / 'shared_files_check.py')}\n", encoding="utf-8")

    def run_check(self, **rcs):
        for k, v in rcs.items():
            os.environ[f"RC_{k}"] = str(v)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = sc.main(["--repo", str(self.crate), "--name", "ALICE-X"], law=self.law)
        calls = [json.loads(ln) for ln in self.log.read_text().splitlines()] if self.log.exists() else []
        return code, out.getvalue(), calls

    def test_all_four_run_against_the_pinned_checkout_and_pass(self):
        code, out, calls = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertEqual([c[0] for c in calls], list(NAMES))
        self.assertEqual(out.count("g11 self-check: "), 6)
        law = f"{self.rev}={self.law}"
        self.assertEqual(calls[0][-3:], ["--check", "--law", law])
        self.assertEqual(calls[1][-2:], ["--law", law])
        self.assertIn(str(self.law / "conformance" / "shared-files.toml"), calls[2])
        self.assertEqual(calls[3][1:3], ["--check-manifest", str(self.crate.resolve())])
        self.assertIn("summary of g11_table_check.py", out)

    def test_a_finding_in_any_check_is_exit_1_and_every_check_still_runs(self):
        for n in ("g11_table_gen", "g11_table_check", "shared_files_check", "conformance_roster_check"):
            self.log.unlink(missing_ok=True)
            code, out, calls = self.run_check(**{n: 1})
            os.environ.pop(f"RC_{n}")
            self.assertEqual(code, 1, (n, out))
            self.assertEqual(len(calls), 4, n)

    def test_compared_nothing_is_exit_2_and_a_finding_wins(self):
        code, out, _ = self.run_check(shared_files_check=2)
        self.assertEqual(code, 2, out)
        code, out, _ = self.run_check(shared_files_check=2, g11_table_check=1)
        self.assertEqual(code, 1, out)

    def test_a_checkout_at_another_revision_runs_nothing(self):
        self.pin(rev="0" * 40)
        code, out, calls = self.run_check()
        self.assertEqual(code, 2, out)
        self.assertEqual(calls, [])
        self.assertIn("checkout --detach", out)

    def test_a_table_or_checker_other_than_the_pinned_bytes_runs_nothing(self):
        for kw in ({"table": "a" * 64}, {"checker": "b" * 64}):
            self.pin(**kw)
            code, out, calls = self.run_check()
            self.assertEqual(code, 2, (kw, out))
            self.assertEqual(calls, [])

    def test_an_edited_or_extra_file_in_the_checkout_runs_nothing(self):
        # the scripts run from the working tree: a checkout that differs from its HEAD is not the pinned revision
        f = self.law / "scripts" / "conformance_roster_check.py"
        orig = f.read_text(encoding="utf-8")
        f.write_text("import sys\nprint('manifest check: ok')\nsys.exit(0)\n", encoding="utf-8")
        code, out, calls = self.run_check()
        self.assertEqual((code, calls), (2, []), out)
        self.assertIn("conformance_roster_check.py", out)
        f.write_text(orig, encoding="utf-8")
        (self.law / "scripts" / "helper.py").write_text("x = 1\n", encoding="utf-8")  # an untracked module
        code, out, calls = self.run_check()
        self.assertEqual((code, calls), (2, []), out)
        (self.law / "scripts" / "helper.py").unlink()
        (self.law / "conformance" / "extra.toml").write_text("\n", encoding="utf-8")
        self.assertEqual(self.run_check()[0], 2)

    def test_the_self_check_itself_may_be_an_untracked_copy_for_an_older_pin(self):
        (self.law / "scripts" / "g11_self_check.py").write_text("# a copy\n", encoding="utf-8")
        code, out, calls = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 4)

    def manifest(self, status):
        (self.crate / "alice-law-conformance.toml").write_text(f'[G11]\nstatus = "{status}"\n', encoding="utf-8")

    def test_a_g11_pass_with_an_own_copy_off_target_is_a_finding(self):
        self.manifest("pass")
        os.environ["OFF_N"] = "1"
        code, out, calls = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertEqual(len(calls), 4)
        self.assertIn("declares G11 pass, and 1 own copy is not at a pending row's target", out)

    def test_own_copies_off_target_do_not_fail_a_crate_that_does_not_declare_pass(self):
        for status in ("fail-known", "unverified"):
            self.manifest(status)
            os.environ["OFF_N"] = "3"
            code, out, _ = self.run_check()
            self.assertEqual(code, 0, (status, out))
            self.assertIn("3 own copies off target", out)

    def test_a_g11_pass_with_every_own_copy_at_target_passes(self):
        # rows still pending on other mains do not hold the crate back
        self.manifest("pass")
        os.environ["OFF_N"] = "0"
        code, out, _ = self.run_check()
        self.assertEqual(code, 0, out)

    def test_a_copies_check_without_the_count_is_not_checked(self):
        self.manifest("pass")
        f = self.law / "scripts" / "shared_files_check.py"
        f.write_text(f.read_text(encoding="utf-8").replace("own copies not at", "own copies off"), encoding="utf-8")
        subprocess.run(["git", "-C", str(self.law), "commit", "-qam", "older checker"], check=True, env=ENV)
        self.rev = subprocess.run(["git", "-C", str(self.law), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()
        self.pin()
        code, out, _ = self.run_check()
        self.assertEqual(code, 2, out)
        self.assertIn("printed no count of own copies off target", out)

    def g11_pin(self, rev=None, checker=None):
        (self.crate / ".github" / "g11-table-pin").write_text(
            f"name ALICE-X\nrev {rev or self.rev}\n"
            f"checker-sha256 {checker or h(self.law / 'scripts' / 'g11_table_check.py')}\n", encoding="utf-8")

    def test_a_g11_table_pin_must_name_the_same_revision_and_checker(self):
        self.g11_pin()
        self.assertEqual(self.run_check()[0], 0)
        self.log.unlink(missing_ok=True)
        for kw in ({"rev": "1" * 40}, {"checker": "c" * 64}):
            self.g11_pin(**kw)
            code, out, calls = self.run_check()
            self.assertEqual((code, calls), (2, []), (kw, out))
            self.assertIn("g11-table-pin", out)
        (self.crate / ".github" / "g11-table-pin").write_text("rev x\n", encoding="utf-8")
        self.assertEqual(self.run_check()[0], 2)

    def test_a_missing_or_malformed_pin_runs_nothing(self):
        (self.crate / ".github" / "shared-files-pin").write_text("rev nothex\n", encoding="utf-8")
        self.assertEqual(self.run_check()[0], 2)
        (self.crate / ".github" / "shared-files-pin").unlink()
        code, out, calls = self.run_check()
        self.assertEqual((code, calls), (2, []), out)


if __name__ == "__main__":
    unittest.main()
