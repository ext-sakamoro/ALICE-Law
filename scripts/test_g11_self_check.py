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
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import importlib.util

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("g11sc", HERE / "g11_self_check.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

NAMES = ("g11_table_gen.py", "g11_table_check.py", "shared_files_check.py", "conformance_roster_check.py",
         "non_pass_ratchet.py", "no_panic_ratchet.py")
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


PASSING_TEST = "print('ok')\n"
# fetches ALICE-Law by its public url: passes only when the url is sent to the pinned checkout (EXPECTED: its HEAD)
LAW_READING_TEST = ("import subprocess, sys\n"
                    "r = subprocess.run(['git', 'ls-remote', 'https://github.com/ext-sakamoro/ALICE-Law', 'HEAD'], "
                    "capture_output=True, text=True, timeout=60)\n"
                    "sys.exit(0 if r.returncode == 0 and r.stdout.split()[:1] == ['EXPECTED'] else 3)\n")


def with_origin(crate: Path) -> Path:
    """a bare origin for the crate (its main is what the no-panic check fetches); returns the origin"""
    origin = crate.parent / (crate.name + "-origin.git")
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, env=ENV)
    subprocess.run(["git", "-C", str(crate), "remote", "add", "origin", str(origin)], check=True, env=ENV)
    return origin


def update_main(crate: Path) -> None:
    """the crate's origin main set to its HEAD"""
    subprocess.run(["git", "-C", str(crate), "push", "-q", "origin", "HEAD:refs/heads/main"], check=True,
                   env=ENV, capture_output=True)


def h(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Fixture(unittest.TestCase):
    """a stub ALICE-Law checkout and a crate pinned to it (no tests of its own)"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.law, self.crate, self.log = root / "law", root / "crate", root / "argv.jsonl"
        (self.law / "scripts").mkdir(parents=True)
        (self.law / "conformance").mkdir()
        # one shared row whose path is a test file (the shared-tests check runs the crate's copy of it)
        (self.law / "conformance" / "shared-files.toml").write_text(
            '# table\n[[file]]\npath = "scripts/test_shared_probe.py"\nsha256 = "' + "0" * 64
            + '"\nrepos = ["ALICE-X"]\n', encoding="utf-8")
        for n in NAMES:
            (self.law / "scripts" / n).write_text(
                "import json, os, sys\n"
                f"open({str(self.log)!r}, 'a').write(json.dumps([{n!r}] + sys.argv[1:]) + '\\n')\n"
                + ("print(\"shared_files_check: own copies not at a pending row's target: \" + os.environ.get('OFF_N', '0')) "
                   "if '--private' not in sys.argv else None\n"
                   "[print(f'shared_files_check: pending: {p}: per-repository data (no repository holds the row), not compared') "
                   "for p in os.environ.get('PRIV_DATA', '').split(',') if p and '--private' in sys.argv]\n"
                   "import pathlib\n"
                   "probe = pathlib.Path(sys.argv[sys.argv.index('--repo') + 1]) / 'probe.txt'\n"
                   "sys.exit(1) if probe.exists() and b'\\r' in probe.read_bytes() else None\n"
                   "sys.exit(1) if (probe.parent / 'link.txt').exists() else None\n"
                   if n == "shared_files_check.py" else "") +
                f"print('summary of {n}')\n"
                f"sys.exit(int(os.environ.get('RC_' + {n.split('.')[0]!r}, '0')))\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.law)], check=True, env=ENV)
        subprocess.run(["git", "-C", str(self.law), "add", "-A"], check=True, env=ENV)
        subprocess.run(["git", "-C", str(self.law), "commit", "-q", "-m", "c"], check=True, env=ENV)
        self.rev = subprocess.run(["git", "-C", str(self.law), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True, encoding="utf-8").stdout.strip()
        (self.crate / ".github").mkdir(parents=True)
        (self.crate / "scripts").mkdir()
        self.shared_test(PASSING_TEST)
        subprocess.run(["git", "init", "-q", str(self.crate)], check=True, env=ENV)
        self.origin = with_origin(self.crate)
        # the private crates' own no-panic row (a public crate's check reads ALICE-Law's baseline instead)
        (self.crate / "conformance-own").mkdir()
        (self.crate / "conformance-own" / "no-panic.toml").write_text("# own row\n", encoding="utf-8")
        self.pin()
        self.saved_env = dict(os.environ)
        # a private crate's non-pass baseline is handed in (kept outside ALICE-Law); the stub only records it
        self.npb = Path(self.tmp.name) / "non-pass-private.toml"
        self.npb.write_text('[repository."ALICE-X"]\nnon_pass = 0\n', encoding="utf-8")
        self.extra = ["--non-pass-baseline", str(self.npb), "--no-panic-cache", str(root / "npcache")]
        self.follow_main = True  # the crate's origin main follows each commit (False: main stays where it was)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)
        self.tmp.cleanup()

    def pin(self, rev=None, table=None, checker=None, extra=""):
        (self.crate / ".github" / "shared-files-pin").write_text(
            f"name ALICE-X\nrev {rev or self.rev}\n"
            f"table-sha256 {table or h(self.law / 'conformance' / 'shared-files.toml')}\n"
            f"checker-sha256 {checker or h(self.law / 'scripts' / 'shared_files_check.py')}\n{extra}", encoding="utf-8")

    def shared_test(self, body: str) -> None:
        """the crate's copy of the shared test file the stub table lists"""
        (self.crate / "scripts" / "test_shared_probe.py").write_text(body, encoding="utf-8")

    def own(self, text):
        (self.crate / "conformance").mkdir(exist_ok=True)
        (self.crate / "conformance" / "g11-own.toml").write_text(text, encoding="utf-8")

    def commit_crate(self):
        # the checks read the crate's committed tree (git archive HEAD): commit what the test wrote
        subprocess.run(["git", "-C", str(self.crate), "add", "-A"], check=True, env=ENV)
        subprocess.run(["git", "-C", str(self.crate), "commit", "-q", "--allow-empty", "-m", "c"], check=True, env=ENV)
        if self.follow_main:
            update_main(self.crate)

    def run_check(self, **rcs):
        self.commit_crate()
        for k, v in rcs.items():
            os.environ[f"RC_{k}"] = str(v)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = sc.main(["--repo", str(self.crate), "--name", "ALICE-X"] + self.extra, law=self.law)
        calls = [json.loads(ln) for ln in self.log.read_text().splitlines()] if self.log.exists() else []
        return code, out.getvalue(), calls

    def run_check_uncommitted(self):
        """the self-check on what HEAD holds, the working copy left as the test wrote it"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = sc.main(["--repo", str(self.crate), "--name", "ALICE-X"] + self.extra, law=self.law)
        return code, out.getvalue()


class SelfCheck(Fixture):
    def test_all_four_run_against_the_pinned_checkout_and_pass(self):
        code, out, calls = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertEqual([c[0] for c in calls], list(NAMES))
        self.assertEqual(out.count("g11 self-check: "), 9)
        law = f"{self.rev}={self.law}"
        self.assertEqual(calls[0][-3:], ["--check", "--law", law])
        self.assertEqual(calls[1][-2:], ["--law", law])
        self.assertIn(str(self.law / "conformance" / "shared-files.toml"), calls[2])
        self.assertEqual(calls[3][1:3], ["--check-manifest", str(self.crate.resolve())])
        self.assertIn("summary of g11_table_check.py", out)

    def test_a_failing_shared_test_is_a_finding_and_every_check_still_runs(self):
        self.shared_test("import sys\nprint('a fixture finding')\nsys.exit(1)\n")
        code, out, calls = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("g11 self-check: shared-tests: exit 1: 1 of 1 shared test file(s) failed: "
                      "scripts/test_shared_probe.py", out)
        self.assertIn("  scripts/test_shared_probe.py: exit 1", out)
        self.assertEqual([c[0] for c in calls], list(NAMES))

    def test_a_passing_shared_test_is_named(self):
        code, out, _ = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertIn("g11 self-check: shared-tests: exit 0: 1 shared test file(s) passed: "
                      "scripts/test_shared_probe.py", out)

    def test_a_crate_holding_no_shared_test_is_not_checked(self):
        (self.crate / "scripts" / "test_shared_probe.py").unlink()
        code, out, _ = self.run_check()
        self.assertEqual(code, 2, out)
        self.assertIn("shared-tests: exit 2: the committed tree holds no shared test file (nothing compared)", out)

    def test_the_shared_test_runs_the_committed_file_not_the_working_copy(self):
        self.shared_test("import sys\nsys.exit(1)\n")
        self.commit_crate()
        self.shared_test(PASSING_TEST)  # fixed in the working copy only: the committed test still fails
        code, out = self.run_check_uncommitted()
        self.assertEqual(code, 1, out)
        self.assertIn("1 of 1 shared test file(s) failed", out)

    def test_the_shared_test_reads_alice_law_at_the_pinned_checkout(self):
        # a test that fetches ALICE-Law by its public url gets the pinned checkout's revision (no network)
        self.shared_test(LAW_READING_TEST.replace("EXPECTED", self.rev))
        code, out, _ = self.run_check()
        self.assertEqual(code, 0, out)

    def test_a_shared_test_that_does_not_finish_is_a_finding(self):
        self.shared_test("import time\ntime.sleep(30)\n")
        self.commit_crate()
        code, lines = sc.run_shared_tests(self.crate, self.law, ["scripts/test_shared_probe.py"], timeout=1)
        self.assertEqual(code, 1, lines)
        self.assertIn("scripts/test_shared_probe.py: did not finish in 1 s", lines)

    def test_the_non_pass_check_reads_law_s_baseline_for_a_public_crate(self):
        code, out, calls = self.run_check()
        self.assertEqual(code, 0, out)
        np_call = calls[4]
        self.assertEqual(np_call[0], "non_pass_ratchet.py")
        self.assertEqual(np_call[1:3], ["--only", f"ALICE-X={self.crate.resolve()}"])
        self.assertIn(str(self.law / "conformance" / "non-pass-baseline.toml"), np_call)
        self.assertNotIn(str(self.npb), np_call)

    def test_a_private_crate_s_non_pass_check_reads_the_handed_baseline(self):
        self.pin(extra="visibility private\n")
        code, out, calls = self.run_check()
        self.assertEqual(code, 0, out)
        np_call = calls[4]
        self.assertEqual(np_call[1:3], ["--private", f"ALICE-X={self.crate.resolve()}"])
        self.assertIn(str(self.npb), np_call)

    def test_a_private_crate_without_a_handed_baseline_is_not_checked(self):
        self.pin(extra="visibility private\n")
        self.extra = []
        code, out, calls = self.run_check()
        self.assertEqual(code, 2, out)
        self.assertIn("non-pass: a private crate's baseline is not handed in", out)
        self.assertNotIn("non_pass_ratchet.py", [c[0] for c in calls])

    def test_the_no_panic_check_is_a_landing_against_the_fetched_main(self):
        code, out, calls = self.run_check()
        self.assertEqual(code, 0, out)
        np = calls[5]
        self.assertEqual(np[0], "no_panic_ratchet.py")
        self.assertEqual(np[1], "--only")
        self.assertTrue(np[2].startswith("ALICE-X="), np)
        self.assertEqual(Path(np[2].split("=", 1)[1]).name, "head", np)  # the committed tree
        self.assertIn("--landing", np)
        self.assertEqual(Path(np[np.index("--base") + 1]).name, "main", np)
        self.assertEqual(np[np.index("--baseline") + 1], str(self.law / "conformance" / "no-panic-baseline.toml"))
        self.assertEqual(np[np.index("--scanner-cache") + 1], self.extra[-1])

    def test_a_private_crate_s_no_panic_row_is_its_own_committed_file(self):
        self.pin(extra="visibility private\n")
        code, out, calls = self.run_check()
        self.assertEqual(code, 0, out)
        np = calls[5]
        self.assertEqual(Path(np[np.index("--baseline") + 1]).parts[-3:], ("head", "conformance-own", "no-panic.toml"),
                         np)

    def test_a_private_crate_without_its_no_panic_row_is_not_checked(self):
        self.pin(extra="visibility private\n")
        (self.crate / "conformance-own" / "no-panic.toml").unlink()
        code, out, calls = self.run_check()
        self.assertEqual(code, 2, out)
        self.assertIn("no-panic: a private crate keeps its row in conformance-own/no-panic.toml", out)
        self.assertNotIn("no_panic_ratchet.py", [c[0] for c in calls])

    def test_a_main_that_cannot_be_fetched_is_not_checked_never_a_skip(self):
        subprocess.run(["git", "-C", str(self.crate), "remote", "remove", "origin"], check=True, env=ENV)
        self.follow_main = False
        code, out, calls = self.run_check()
        self.assertEqual(code, 2, out)
        self.assertIn("no-panic: the crate's main could not be read (git fetch origin main failed", out)
        self.assertNotIn("no_panic_ratchet.py", [c[0] for c in calls])
        self.assertIn("not checked", out.splitlines()[-1])

    def test_the_main_is_fetched_at_check_time_not_a_stale_local_ref(self):
        # the local refs/remotes/origin/main stays at the first commit while origin's main moves on
        (self.crate / "a.txt").write_text("first\n", encoding="utf-8")
        self.commit_crate()
        other = Path(self.tmp.name) / "other"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.origin), str(other)], check=True, env=ENV,
                       capture_output=True)
        (other / "a.txt").write_text("second\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(other), "commit", "-qam", "m"], check=True, env=ENV)
        subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "HEAD:refs/heads/main"], check=True,
                       env=ENV, capture_output=True)
        dest = Path(self.tmp.name) / "fetched"
        dest.mkdir()
        sc.fetched_main(self.crate, dest)
        self.assertEqual((dest / "a.txt").read_text(encoding="utf-8"), "second\n")

    def test_a_finding_in_any_check_is_exit_1_and_every_check_still_runs(self):
        for n in ("g11_table_gen", "g11_table_check", "shared_files_check", "conformance_roster_check", "non_pass_ratchet",
                  "no_panic_ratchet"):
            self.log.unlink(missing_ok=True)
            code, out, calls = self.run_check(**{n: 1})
            os.environ.pop(f"RC_{n}")
            self.assertEqual(code, 1, (n, out))
            self.assertEqual(len(calls), 6, n)

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
        self.assertEqual(len(calls), 6)

    def manifest(self, status):
        (self.crate / "alice-law-conformance.toml").write_text(f'[G11]\nstatus = "{status}"\n', encoding="utf-8")

    def test_a_g11_pass_with_an_own_copy_off_target_is_a_finding(self):
        self.manifest("pass")
        os.environ["OFF_N"] = "1"
        code, out, calls = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertEqual(len(calls), 6)
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
                                  text=True, check=True, encoding="utf-8").stdout.strip()
        self.pin()
        code, out, _ = self.run_check()
        self.assertEqual(code, 2, out)
        self.assertIn("printed no count of own copies off target", out)

    def test_a_law_checkout_that_converted_line_endings_still_matches_its_pins(self):
        # Windows with core.autocrlf: git sees a clean checkout, but the files hold CRLF; the pins are of the blobs
        self.g11_pin()  # pinned from the committed bytes, as a crate pins them
        subprocess.run(["git", "-C", str(self.law), "config", "core.autocrlf", "true"], check=True, env=ENV)
        rels = ("conformance/shared-files.toml", "scripts/shared_files_check.py", "scripts/g11_table_check.py")
        for rel in rels:
            (self.law / rel).unlink()
        subprocess.run(["git", "-C", str(self.law), "checkout", "--", *rels], check=True, env=ENV)  # git converts
        self.assertIn(b"\r\n", (self.law / rels[0]).read_bytes())
        st = subprocess.run(["git", "-C", str(self.law), "status", "--porcelain"], capture_output=True, text=True, env=ENV, encoding="utf-8")
        self.assertEqual(st.stdout, "")  # the checkout is the pinned revision as git reads it
        code, out, calls = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 6)

    def test_the_checks_read_the_crate_s_committed_tree_not_its_converted_checkout(self):
        subprocess.run(["git", "-C", str(self.crate), "config", "core.autocrlf", "true"], check=True, env=ENV)
        (self.crate / "probe.txt").write_bytes(b"a shared line\n")
        self.commit_crate()
        (self.crate / "probe.txt").unlink()
        subprocess.run(["git", "-C", str(self.crate), "checkout", "--", "probe.txt"], check=True, env=ENV)
        self.assertIn(b"\r\n", (self.crate / "probe.txt").read_bytes())  # the checkout converted it
        code, out, calls = self.run_check()
        self.assertEqual(code, 0, out)
        copies = next(c for c in calls if c[0] == "shared_files_check.py")
        self.assertNotEqual(copies[copies.index("--repo") + 1], str(self.crate))

    def test_a_committed_symbolic_link_is_left_out_of_the_tree_the_checks_read(self):
        # a shared copy is a file: a link (here to a real file) is not written as one, nor followed
        (self.crate / "real.txt").write_bytes(b"x\n")
        os.symlink("real.txt", self.crate / "link.txt")
        code, out, calls = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 6)

    def test_a_crate_whose_head_has_no_file_is_not_checked(self):
        # an empty HEAD (the pin only in the checkout): nothing to compare, never a pass
        empty = Path(self.tmp.name) / "empty"
        subprocess.run(["git", "init", "-q", str(empty)], check=True, env=ENV)
        subprocess.run(["git", "-C", str(empty), "commit", "-q", "--allow-empty", "-m", "c"], check=True, env=ENV)
        (empty / ".github").mkdir()
        (empty / ".github" / "shared-files-pin").write_text((self.crate / ".github" / "shared-files-pin").read_text())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = sc.main(["--repo", str(empty), "--name", "ALICE-X"], law=self.law)
        self.assertEqual(code, 2, out.getvalue())
        self.assertIn("HEAD has no file", out.getvalue())

    def test_a_crate_that_is_no_git_repository_is_not_checked(self):
        import shutil
        shutil.rmtree(self.crate / ".git")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = sc.main(["--repo", str(self.crate), "--name", "ALICE-X"], law=self.law)
        self.assertEqual(code, 2, out.getvalue())
        self.assertIn("git ls-tree HEAD failed", out.getvalue())

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


    # a private repository (pin line `visibility private`): the table never names it, so the copies check runs in
    # its private mode (each table path present compared with its row) and every per-repository data file it
    # reports has to be declared in conformance/g11-own.toml [private-data] with a reason

    def test_a_private_pin_runs_the_copies_check_in_private_mode(self):
        self.pin(extra="visibility private\n")
        code, out, calls = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertIn("--private", calls[2])
        self.assertNotIn("--private", calls[0] + calls[1] + calls[3])
        self.assertIn("own-target: private mode", out)

    def test_a_public_pin_does_not_run_the_private_mode(self):
        for extra in ("", "visibility public\n"):
            self.log.unlink(missing_ok=True)
            self.pin(extra=extra)
            code, out, calls = self.run_check()
            self.assertEqual(code, 0, (extra, out))
            self.assertNotIn("--private", calls[2])

    def test_a_private_data_file_not_declared_is_a_finding(self):
        self.pin(extra="visibility private\n")
        os.environ["PRIV_DATA"] = "scripts/data.tsv"
        code, out, _ = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/data.tsv", out)
        self.assertIn("[private-data]", out)

    def test_a_private_data_file_declared_with_a_reason_passes(self):
        self.pin(extra="visibility private\n")
        self.own('[private-data]\n"scripts/data.tsv" = "generated from clippy.toml by the shared generator"\n')
        os.environ["PRIV_DATA"] = "scripts/data.tsv"
        code, out, _ = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertIn("1 per-repository data file(s) declared", out)

    def test_a_private_data_reason_too_short_is_a_finding(self):
        self.pin(extra="visibility private\n")
        self.own('[private-data]\n"scripts/data.tsv" = "data"\n')
        os.environ["PRIV_DATA"] = "scripts/data.tsv"
        code, out, _ = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("reason", out)

    def test_a_declared_private_data_file_the_check_does_not_report_is_a_finding(self):
        self.pin(extra="visibility private\n")
        self.own('[private-data]\n"scripts/gone.tsv" = "generated from clippy.toml by the shared generator"\n')
        code, out, _ = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/gone.tsv", out)

    def test_private_data_declared_by_a_public_crate_is_a_finding(self):
        self.own('[private-data]\n"scripts/data.tsv" = "generated from clippy.toml by the shared generator"\n')
        code, out, _ = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("[private-data]", out)

    def test_an_unreadable_own_table_in_private_mode_is_not_checked(self):
        self.pin(extra="visibility private\n")
        self.own("[private-data\n")
        code, out, _ = self.run_check()
        self.assertEqual(code, 2, out)

    def test_a_visibility_other_than_public_or_private_or_twice_runs_nothing(self):
        for extra in ("visibility secret\n", "visibility private\nvisibility private\n"):
            self.log.unlink(missing_ok=True)
            self.pin(extra=extra)
            code, out, calls = self.run_check()
            self.assertEqual((code, calls), (2, []), (extra, out))


# a stand-in for rustup: `target list --installed` prints the targets in STATE, `target add` adds to it, FAIL_ADD
# makes the add fail; each call is appended to CALLS (the test reads what the self-check asked for)
STUB_RUSTUP = """import json, pathlib, sys
state, calls = pathlib.Path(STATE), pathlib.Path(CALLS)
have = json.loads(state.read_text(encoding="utf-8")) if state.exists() else []
args = sys.argv[1:]
with calls.open("a", encoding="utf-8") as f:
    f.write(" ".join(args) + "\\n")
if args[:3] == ["target", "list", "--installed"]:
    print("\\n".join(have))
    sys.exit(0)
if args[:2] == ["target", "add"] and not FAIL_ADD:
    state.write_text(json.dumps(sorted(set(have) | set(args[2:]))), encoding="utf-8")
    sys.exit(0)
sys.exit(4)
"""
# the shared test of the fixture: needs aarch64-unknown-linux-gnu installed (as the fneg test needs its targets) and
# leaves RAN behind, so a test can tell it was not run
NEEDS_TARGET_TEST = """import json, pathlib, sys
pathlib.Path(RAN).write_text("ran", encoding="utf-8")
state = pathlib.Path(STATE)
have = json.loads(state.read_text(encoding="utf-8")) if state.exists() else []
sys.exit(0 if "aarch64-unknown-linux-gnu" in have else 5)
"""
SETUP_ROW = ('[[test]]\npath = "scripts/test_shared_probe.py"\nrustc-targets = ["aarch64-unknown-linux-gnu"]\n'
             'why = "the probe compiles for aarch64"\n')


class SharedTestSetup(Fixture):
    """the rustc targets a shared test declares in conformance/shared-test-setup.toml are installed before it runs"""

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.state, self.calls, self.ran = root / "rustup-state.json", root / "rustup-calls.txt", root / "ran.txt"
        self.shared_test(self.fill(NEEDS_TARGET_TEST))
        self.stub(fail_add=False)

    def fill(self, text: str) -> str:
        return (text.replace("STATE", repr(str(self.state))).replace("CALLS", repr(str(self.calls)))
                .replace("RAN", repr(str(self.ran))))

    def stub(self, fail_add: bool) -> None:
        self.rustup_py = Path(self.tmp.name) / "rustup_stub.py"
        self.rustup_py.write_text(self.fill(STUB_RUSTUP).replace("FAIL_ADD", repr(fail_add)), encoding="utf-8")
        self.rustup = [sys.executable, str(self.rustup_py)]

    def setup_file(self, text: str) -> None:
        (self.law / "conformance" / "shared-test-setup.toml").write_text(text, encoding="utf-8")

    def commit_law(self) -> None:
        """ALICE-Law's checkout committed and the crate pinned to the new revision (the self-check refuses a dirty one)"""
        subprocess.run(["git", "-C", str(self.law), "add", "-A"], check=True, env=ENV)
        subprocess.run(["git", "-C", str(self.law), "commit", "-q", "-m", "setup"], check=True, env=ENV)
        self.rev = subprocess.run(["git", "-C", str(self.law), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True, encoding="utf-8").stdout.strip()
        self.pin()

    def load(self):
        return sc.shared_test_setup(self.law, self.law / "conformance" / "shared-files.toml")

    def run_tests(self, setup):
        self.commit_crate()
        return sc.run_shared_tests(self.crate, self.law, ["scripts/test_shared_probe.py"], setup=setup,
                                   rustup=self.rustup)

    def asked(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    # the declaration
    def test_no_setup_file_declares_nothing(self):
        self.assertEqual(self.load(), {})

    def test_a_row_declares_the_targets_of_its_test(self):
        self.setup_file(SETUP_ROW)
        self.assertEqual(self.load(), {"scripts/test_shared_probe.py": ["aarch64-unknown-linux-gnu"]})

    def test_a_malformed_declaration_is_refused(self):
        bad = {
            "unknown key": SETUP_ROW + 'skip = true\n',
            "missing why": SETUP_ROW.replace('why = "the probe compiles for aarch64"\n', ""),
            "empty why": SETUP_ROW.replace("the probe compiles for aarch64", " "),
            "no shared test row": SETUP_ROW.replace("test_shared_probe", "test_elsewhere"),
            "not a triple": SETUP_ROW.replace("aarch64-unknown-linux-gnu", "aarch64; rm -rf x"),
            "empty targets": SETUP_ROW.replace('["aarch64-unknown-linux-gnu"]', "[]"),
            "declared twice": SETUP_ROW + SETUP_ROW,
            "unknown top-level": SETUP_ROW + '[other]\nx = 1\n',
        }
        for what, text in bad.items():
            with self.subTest(what):
                self.setup_file(text)
                with self.assertRaises(ValueError):
                    self.load()

    def test_law_s_own_declaration_reads_against_law_s_table(self):
        law = HERE.parent
        setup = sc.shared_test_setup(law, law / "conformance" / "shared-files.toml")
        self.assertEqual(setup["scripts/test_fneg_fma_ir_check.py"],
                         ["aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu"])

    def test_platforms_alone_declare_where_the_test_runs(self):
        self.setup_file('[[test]]\npath = "scripts/test_shared_probe.py"\nplatforms = ["macos", "linux"]\n'
                        'why = "the tool runs on Linux and macOS only"\n')
        self.assertEqual(self.load(), {})
        self.assertEqual(sc.shared_test_platforms(self.law, self.law / "conformance" / "shared-files.toml"),
                         {"scripts/test_shared_probe.py": ["linux", "macos"]})

    def test_a_malformed_platform_declaration_is_refused(self):
        row = '[[test]]\npath = "scripts/test_shared_probe.py"\nplatforms = PLATS\nwhy = "the tool runs here"\n'
        bad = {"unknown platform": row.replace("PLATS", '["linux", "solaris"]'),
               "empty platforms": row.replace("PLATS", "[]"),
               "repeated platform": row.replace("PLATS", '["linux", "linux"]'),
               "neither targets nor platforms": row.replace("platforms = PLATS\n", "")}
        for what, text in bad.items():
            with self.subTest(what):
                self.setup_file(text)
                with self.assertRaises(ValueError):
                    sc.setup_rows(self.law, self.law / "conformance" / "shared-files.toml")

    def test_the_platform_name_of_each_os(self):
        self.assertEqual([sc.this_platform(n) for n in ("win32", "darwin", "linux", "cygwin")],
                         ["windows", "macos", "linux", "windows"])

    # the platforms
    def test_a_test_declared_for_other_platforms_is_not_run_here_and_is_named(self):
        self.shared_test(self.fill("import pathlib, sys\npathlib.Path(RAN).write_text('ran')\nsys.exit(1)\n"))
        self.commit_crate()
        code, lines = sc.run_shared_tests(self.crate, self.law, ["scripts/test_shared_probe.py"],
                                          platforms={"scripts/test_shared_probe.py": ["linux", "macos"]},
                                          platform="windows", setup={"scripts/test_shared_probe.py":
                                                                     ["aarch64-unknown-linux-gnu"]},
                                          rustup=self.rustup)
        self.assertEqual(code, 0, lines)
        self.assertIn("not run on windows (declared for other platforms): scripts/test_shared_probe.py (linux/macos)",
                      lines)
        self.assertFalse(self.ran.exists())
        self.assertEqual(self.asked(), [], "the setup of a test not run here is not installed")

    def test_the_same_test_runs_on_a_declared_platform(self):
        # the mutant of the test above: the platform declared, so the failing test runs and fails
        self.shared_test(self.fill("import pathlib, sys\npathlib.Path(RAN).write_text('ran')\nsys.exit(1)\n"))
        self.commit_crate()
        code, lines = sc.run_shared_tests(self.crate, self.law, ["scripts/test_shared_probe.py"],
                                          platforms={"scripts/test_shared_probe.py": ["linux", "macos"]},
                                          platform="linux")
        self.assertEqual(code, 1, lines)
        self.assertTrue(self.ran.exists())

    def test_an_undeclared_test_runs_on_every_platform(self):
        self.shared_test(self.fill("import pathlib\npathlib.Path(RAN).write_text('ran')\n"))
        self.commit_crate()
        code, lines = sc.run_shared_tests(self.crate, self.law, ["scripts/test_shared_probe.py"], platforms={},
                                          platform="windows")
        self.assertEqual(code, 0, lines)
        self.assertTrue(self.ran.exists())

    def test_the_self_check_reads_law_s_platforms(self):
        self.shared_test(self.fill("import pathlib, sys\npathlib.Path(RAN).write_text('ran')\nsys.exit(1)\n"))
        self.setup_file('[[test]]\npath = "scripts/test_shared_probe.py"\nplatforms = ["linux", "macos"]\n'
                        'why = "the tool runs on Linux and macOS only"\n')
        self.commit_law()
        with mock.patch.object(sc, "this_platform", lambda name=None: "windows"):
            code, out, _ = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertIn("g11 self-check: shared-tests: not run on windows (declared for other platforms): "
                      "scripts/test_shared_probe.py (linux/macos)", out)
        self.assertFalse(self.ran.exists())

    # the install
    def test_a_missing_target_is_added_and_the_test_then_passes(self):
        code, lines = self.run_tests({"scripts/test_shared_probe.py": ["aarch64-unknown-linux-gnu"]})
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.asked(), ["target list --installed", "target add aarch64-unknown-linux-gnu"])
        self.assertIn("setup: rustc targets added: aarch64-unknown-linux-gnu", lines)

    def test_without_the_setup_the_same_test_fails(self):
        # the mutant: no declaration, so nothing is installed and the strict test fails (it is not skipped)
        code, lines = self.run_tests({})
        self.assertEqual(code, 1, lines)
        self.assertEqual(self.asked(), [])
        self.assertIn("1 of 1 shared test file(s) failed: scripts/test_shared_probe.py", lines)

    def test_an_installed_target_is_not_added_again(self):
        self.state.write_text(json.dumps(["aarch64-unknown-linux-gnu"]), encoding="utf-8")
        code, lines = self.run_tests({"scripts/test_shared_probe.py": ["aarch64-unknown-linux-gnu"]})
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.asked(), ["target list --installed"])

    def test_a_setup_that_cannot_be_installed_is_not_checked_and_runs_no_test(self):
        self.stub(fail_add=True)
        code, lines = self.run_tests({"scripts/test_shared_probe.py": ["aarch64-unknown-linux-gnu"]})
        self.assertEqual(code, 2, lines)
        self.assertTrue(lines[-1].startswith("setup: rustup target add aarch64-unknown-linux-gnu failed"), lines)
        self.assertFalse(self.ran.exists())

    def test_no_rustup_is_not_checked(self):
        self.rustup = [str(Path(self.tmp.name) / "no-such-rustup")]
        code, lines = self.run_tests({"scripts/test_shared_probe.py": ["aarch64-unknown-linux-gnu"]})
        self.assertEqual(code, 2, lines)
        self.assertTrue(lines[-1].startswith("setup: rustup target list --installed failed"), lines)
        self.assertFalse(self.ran.exists())

    # through the self-check
    def test_the_self_check_installs_law_s_declared_setup(self):
        self.setup_file(SETUP_ROW)
        self.commit_law()
        with mock.patch.object(sc, "RUSTUP", self.rustup):
            code, out, _ = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertIn("g11 self-check: shared-tests: setup: rustc targets added: aarch64-unknown-linux-gnu", out)
        self.assertIn("shared-tests: exit 0: 1 shared test file(s) passed", out)

    def test_the_self_check_without_law_s_declaration_fails_the_test(self):
        with mock.patch.object(sc, "RUSTUP", self.rustup):
            code, out, _ = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("shared-tests: exit 1: 1 of 1 shared test file(s) failed", out)

    def test_a_setup_that_cannot_be_installed_leaves_the_whole_self_check_not_checked(self):
        # every caller (the holders' CI step, the prepass receipt, the board through CI) reads a non-zero exit as red:
        # an unchecked shared test is exit 2 with the "not checked" verdict, never "pass"
        self.setup_file(SETUP_ROW)
        self.commit_law()
        self.stub(fail_add=True)
        with mock.patch.object(sc, "RUSTUP", self.rustup):
            code, out, _ = self.run_check()
        self.assertEqual(code, 2, out)
        self.assertIn("g11 self-check: shared-tests: exit 2: setup: rustup target add aarch64-unknown-linux-gnu failed", out)
        self.assertIn("ALICE-X at ALICE-Law", out)
        self.assertIn(": not checked (an input could not be read or nothing was compared)", out)
        self.assertNotIn(": pass\n", out)
        self.assertFalse(self.ran.exists())

    def test_an_unreadable_declaration_is_not_checked(self):
        self.setup_file(SETUP_ROW + "skip = true\n")
        self.commit_law()
        with mock.patch.object(sc, "RUSTUP", self.rustup):
            code, out, _ = self.run_check()
        self.assertEqual(code, 2, out)
        self.assertIn("shared-tests: the table or conformance/shared-test-setup.toml could not be read", out)
        self.assertFalse(self.ran.exists())


class PrivateCopiesReal(unittest.TestCase):
    """the private mode with the real scripts/shared_files_check.py: a private repository holding a stale copy of a
    shared file fails, the row's bytes pass (the other three checks are stubs)"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.law, self.crate = root / "law", root / "crate"
        (self.law / "scripts").mkdir(parents=True)
        (self.law / "conformance").mkdir()
        self.good = b"print('shared v2')\n"
        self.test_body = b"print('a shared test passes')\n"
        (self.law / "scripts" / "shared_files_check.py").write_bytes((HERE / "shared_files_check.py").read_bytes())
        for n in ("g11_table_gen.py", "g11_table_check.py", "conformance_roster_check.py", "non_pass_ratchet.py",
                  "no_panic_ratchet.py"):
            (self.law / "scripts" / n).write_text("print('ok')\n", encoding="utf-8")
        (self.law / "conformance" / "shared-files.toml").write_text(
            '[[file]]\npath = "scripts/shared_tool.py"\n'
            f'sha256 = "{hashlib.sha256(self.good).hexdigest()}"\n'
            f'source = {{ repo = "ALICE-Zip", rev = "{"a" * 40}" }}\n'
            'repos = ["ALICE-DB", "ALICE-Zip"]\n'
            '[[file]]\npath = "scripts/test_shared_tool.py"\n'
            f'sha256 = "{hashlib.sha256(self.test_body).hexdigest()}"\n'
            f'source = {{ repo = "ALICE-Zip", rev = "{"a" * 40}" }}\n'
            'repos = ["ALICE-DB", "ALICE-Zip"]\n', encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.law)], check=True, env=ENV)
        subprocess.run(["git", "-C", str(self.law), "add", "-A"], check=True, env=ENV)
        subprocess.run(["git", "-C", str(self.law), "commit", "-q", "-m", "c"], check=True, env=ENV)
        rev = subprocess.run(["git", "-C", str(self.law), "rev-parse", "HEAD"], capture_output=True, text=True,
                             check=True, encoding="utf-8").stdout.strip()
        (self.crate / ".github").mkdir(parents=True)
        (self.crate / "scripts").mkdir()
        subprocess.run(["git", "init", "-q", str(self.crate)], check=True, env=ENV)
        with_origin(self.crate)
        # a private crate keeps its no-panic row in its own tree (the stub checker only needs the file there)
        (self.crate / "conformance-own").mkdir()
        (self.crate / "conformance-own" / "no-panic.toml").write_text("# own row\n", encoding="utf-8")
        (self.crate / ".github" / "shared-files-pin").write_text(
            f"name ALICE-Private\nrev {rev}\n"
            f"table-sha256 {h(self.law / 'conformance' / 'shared-files.toml')}\n"
            f"checker-sha256 {h(self.law / 'scripts' / 'shared_files_check.py')}\nvisibility private\n",
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, data: bytes):
        (self.crate / "scripts" / "shared_tool.py").write_bytes(data)
        (self.crate / "scripts" / "test_shared_tool.py").write_bytes(self.test_body)
        subprocess.run(["git", "-C", str(self.crate), "add", "-A"], check=True, env=ENV)
        subprocess.run(["git", "-C", str(self.crate), "commit", "-q", "-m", "c"], check=True, env=ENV)
        update_main(self.crate)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = sc.main(["--repo", str(self.crate), "--name", "ALICE-Private", "--non-pass-baseline",
                            str(self.crate.parent / "npb.toml")], law=self.law)
        return code, out.getvalue()

    def test_a_private_repository_holding_a_stale_copy_fails(self):
        code, out = self.run_with(b"print('shared v1')\n")
        self.assertEqual(code, 1, out)
        self.assertIn("the private copy differs", out)

    def test_a_private_repository_holding_the_row_s_bytes_passes(self):
        code, out = self.run_with(self.good)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
