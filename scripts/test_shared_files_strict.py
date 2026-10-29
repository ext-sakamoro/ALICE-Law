#!/usr/bin/env python3
"""tests for scripts/shared_files_strict.sh: how the exit codes of its four checks combine (a finding in any is a
finding, a check that could not run is never a pass), with the checks replaced by stubs

    python3 scripts/test_shared_files_strict.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Strict(unittest.TestCase):
    CHECKS = ("shared_files_check.py", "gen_shared_files.py", "conformance_roster_check.py", "gen_law_status.py")

    def run_with(self, first: tuple[int, str] = (0, ""), second: tuple[int, str] = (0, ""),
                 state: tuple[int, str] = (0, ""), readme: tuple[int, str] = (0, ""),
                 *args: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").mkdir()
            shutil.copy(HERE / "shared_files_strict.sh", root / "scripts" / "shared_files_strict.sh")
            shutil.copy(HERE / "ci_base.py", root / "scripts" / "ci_base.py")
            for name, (rc, err) in zip(self.CHECKS, (first, second, state, readme)):
                (root / "scripts" / name).write_text(
                    f"import sys\nsys.stderr.write({err!r})\nprint('argv', {name!r}, *sys.argv[1:])\nsys.exit({rc})\n")
            env = dict(os.environ)
            for name in ("LAW_BASE_TABLE", "LAW_BASE_STATE"):
                f = root / f"{name.lower()}.toml"
                f.write_text("# base\n", encoding="utf-8")
                env.setdefault(name, str(f))
            env.update(self.env)
            return subprocess.run(["bash", str(root / "scripts" / "shared_files_strict.sh"), *args],
                                  capture_output=True, text=True, timeout=60, env=env, encoding="utf-8")

    env: dict[str, str] = {}

    def argv(self, p: subprocess.CompletedProcess) -> dict[str, list[str]]:
        return {ln.split()[1]: ln.split()[2:] for ln in p.stdout.splitlines() if ln.startswith("argv ")}

    def test_the_landing_mode_leaves_drift_to_the_daily_run(self):
        # a change landing on main: an adoption with the same bytes and a state behind the crates' mains are notes
        a = self.argv(self.run_with())
        self.assertEqual(len(a), 4, a)
        self.assertNotIn("--strict", a["shared_files_check.py"])
        self.assertNotIn("--strict", a["gen_shared_files.py"])
        self.assertIn("--base-table", a["shared_files_check.py"])
        self.assertIn("--base-table", a["gen_shared_files.py"])
        self.assertIn("--live", a["shared_files_check.py"])
        self.assertIn("--check", a["gen_shared_files.py"])
        self.assertIn("--lock-file", a["gen_shared_files.py"])
        self.assertIn("--state-drift-note", a["conformance_roster_check.py"])
        self.assertEqual(len(a["conformance_roster_check.py"]), 2, a)  # the flag and its base state file
        self.assertIn("--check", a["gen_law_status.py"])

    def test_the_daily_mode_is_strict(self):
        a = self.argv(self.run_with((0, ""), (0, ""), (0, ""), (0, ""), "--daily"))
        self.assertEqual(len(a), 4, a)
        self.assertIn("--strict", a["shared_files_check.py"])
        self.assertIn("--strict", a["gen_shared_files.py"])
        self.assertIn("--strict", a["conformance_roster_check.py"])
        self.assertNotIn("--state-drift-note", a["conformance_roster_check.py"])

    def in_repo(self, build) -> subprocess.CompletedProcess:
        """Run the landing mode (no base file given) in a git repository that `build(git, commit)` makes; the table
        check prints the first line of the base table it was given"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").mkdir()
            (root / "conformance").mkdir()
            shutil.copy(HERE / "shared_files_strict.sh", root / "scripts" / "shared_files_strict.sh")
            shutil.copy(HERE / "ci_base.py", root / "scripts" / "ci_base.py")
            (root / "scripts" / "shared_files_check.py").write_text(
                "import sys\na = sys.argv\nprint('BASE', open(a[a.index('--base-table') + 1]).readline().strip())\n")
            for name in self.CHECKS[1:]:
                (root / "scripts" / name).write_text("import sys\nsys.exit(0)\n")
            env = {k: v for k, v in os.environ.items() if not k.startswith(("LAW_BASE_", "GIT_"))}
            env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

            def git(*a: str) -> str:
                return subprocess.run(["git", *a], cwd=root, env=env, check=True, capture_output=True, text=True,
                                      encoding="utf-8").stdout.strip()

            def commit(mark: str, files=("shared-files.toml", "manifest-state.toml")) -> str:
                for f in files:
                    (root / "conformance" / f).write_text(f"# {mark}\n", encoding="utf-8")
                git("add", "-A")
                git("commit", "-q", "-m", mark)
                return git("rev-parse", "HEAD")

            git("init", "-q", "-b", "work")
            build(git, commit)
            return subprocess.run(["bash", str(root / "scripts" / "shared_files_strict.sh")], cwd=root, env=env,
                                  capture_output=True, text=True, timeout=60, encoding="utf-8")

    def test_no_base_commit_is_not_a_pass(self):
        # no origin/main to take a merge-base with: ci_base.py cannot name the base, and nothing is compared with HEAD
        p = self.in_repo(lambda git, commit: commit("head"))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("the base commit could not be read (scripts/ci_base.py)", p.stderr)
        self.assertNotIn("BASE", p.stdout)

    def test_a_file_missing_at_the_base_commit_is_not_a_pass(self):
        # the base commit is named, but it has no manifest-state.toml: not compared with an empty or a later file
        def build(git, commit):
            commit("main", files=("shared-files.toml",))
            git("update-ref", "refs/remotes/origin/main", "HEAD")
            commit("head")
        p = self.in_repo(build)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn(":conformance/manifest-state.toml could not be read: not a pass", p.stderr)

    def test_the_landing_base_is_the_commit_ci_base_names(self):
        # no base file given: the landing mode reads the table and the state at the base scripts/ci_base.py names
        # (the merge-base with origin/main), not at HEAD and not at a fallback ref
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").mkdir()
            (root / "conformance").mkdir()
            shutil.copy(HERE / "shared_files_strict.sh", root / "scripts" / "shared_files_strict.sh")
            shutil.copy(HERE / "ci_base.py", root / "scripts" / "ci_base.py")
            # the table check prints the first line of the base table it was given
            (root / "scripts" / "shared_files_check.py").write_text(
                "import sys\na = sys.argv\nprint('BASE', open(a[a.index('--base-table') + 1]).readline().strip())\n")
            for name in self.CHECKS[1:]:
                (root / "scripts" / name).write_text("import sys\nsys.exit(0)\n")
            env = {k: v for k, v in os.environ.items() if not k.startswith(("LAW_BASE_", "GIT_"))}
            env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
            git = lambda *a: subprocess.run(["git", *a], cwd=root, env=env, check=True, capture_output=True,
                                            text=True, encoding="utf-8").stdout.strip()
            git("init", "-q", "-b", "work")

            def commit(mark: str) -> str:
                for f in ("shared-files.toml", "manifest-state.toml"):
                    (root / "conformance" / f).write_text(f"# {mark}\n", encoding="utf-8")
                git("add", "-A")
                git("commit", "-q", "-m", mark)
                return git("rev-parse", "HEAD")

            fork = commit("main")
            # main moved on after this change forked from it: the base is the fork point, not main's tip
            git("checkout", "-q", "-b", "later")
            git("update-ref", "refs/remotes/origin/main", commit("later main"))
            git("checkout", "-q", "work")
            git("reset", "-q", "--hard", fork)
            commit("head")
            p = subprocess.run(["bash", str(root / "scripts" / "shared_files_strict.sh")], cwd=root, env=env,
                               capture_output=True, text=True, timeout=60, encoding="utf-8")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("BASE # main\n", p.stdout)
        self.assertNotIn("BASE # head", p.stdout)
        self.assertNotIn("BASE # later main", p.stdout)

    def test_an_unreadable_base_is_not_a_pass(self):
        # no base file and no git repository to read origin/main from: the landing mode cannot compare
        self.env = {"LAW_BASE_TABLE": "/nonexistent/base.toml", "LAW_BASE_STATE": "/nonexistent/state.toml",
                    "GIT_DIR": "/nonexistent"}
        try:
            p = self.run_with()
        finally:
            self.env = {}
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("could not be read", p.stderr)
        self.assertEqual(self.argv(p), {})

    def test_an_unknown_argument_is_not_a_pass(self):
        p = self.run_with((0, ""), (0, ""), (0, ""), (0, ""), "--dialy")
        self.assertEqual(p.returncode, 2, p.stderr)

    def test_both_pass(self):
        p = self.run_with((0, ""), (0, ""))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("shared-files strict: ok", p.stdout)

    def test_a_finding_in_either_check_is_a_finding_even_when_the_other_could_not_run(self):
        for first, second in (((1, "x\n"), (2, "fetching main failed\n")), ((2, "fetching main failed\n"), (1, "x\n")),
                              ((1, "x\n"), (0, "")), ((0, ""), (1, "x\n"))):
            p = self.run_with(first, second)
            self.assertEqual(p.returncode, 1, (first, second, p.stderr))
            self.assertIn("finding", p.stderr)
            self.assertNotIn("shared-files strict: ok", p.stdout)

    def test_a_check_that_could_not_run_is_not_a_pass(self):
        p = self.run_with((2, "a: fetching main failed\n"), (0, ""))
        self.assertEqual(p.returncode, 2)
        self.assertIn("infra", p.stderr)
        p = self.run_with((0, ""), (2, "shared_files_check: compared nothing\n"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("nothing compared", p.stderr)
        self.assertNotIn("infra", p.stderr)
        p = self.run_with((3, ""), (0, ""))
        self.assertEqual(p.returncode, 2)
        self.assertIn("infra", p.stderr)

    def test_a_stale_law_state_is_a_finding(self):
        # the roster check finds manifest-state.toml behind the crates' mains: no receipt for a stale state
        p = self.run_with(state=(1, ""))
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("finding (conformance_roster_check.py", p.stderr)
        p = self.run_with(readme=(1, ""))
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("finding (gen_law_status.py --check)", p.stderr)

    def test_the_state_checks_run_and_are_not_a_pass_when_they_compare_nothing(self):
        p = self.run_with(state=(2, "error: nothing was compared (0 manifests, 0 gates)\n"))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("nothing compared (conformance_roster_check.py", p.stderr)
        p = self.run_with(readme=(2, "error: no README was compared\n"))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("nothing compared (gen_law_status.py --check)", p.stderr)
        p = self.run_with(state=(2, "error: gh api repos/x: HTTP 502\n"))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("infra", p.stderr)

    def test_the_checks_stderr_is_shown(self):
        p = self.run_with((1, "shared_files_check: a finding line\n"), (0, ""))
        self.assertIn("a finding line", p.stderr)


if __name__ == "__main__":
    unittest.main()
