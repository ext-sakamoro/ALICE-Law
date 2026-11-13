#!/usr/bin/env python3
"""Tests of the main-commit roster in scripts/release_train.py: `check` and `record`
(python3 scripts/test_release_train_main.py)

The roster names each crate at a commit of its repository's main branch (`sha`) and the
version its manifest has there. No network: the checkouts are local fixture repositories
with an `origin/main` ref, and the CI jobs come from a fixture.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rt_main", HERE / "release_train.py")
rt = importlib.util.module_from_spec(spec)
sys.modules["rt_main"] = rt
spec.loader.exec_module(rt)

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
CI_WF = "on:\n  push:\n    branches: [main]\njobs:\n  test:\n    steps:\n      - run: cargo test\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**ENV, "HOME": str(repo)}, encoding="utf-8").stdout.strip()


def manifest(name: str, version: str, deps: str = "") -> str:
    return f'[package]\nname = "{name}"\nversion = "{version}"\nedition = "2021"\n{deps}'


def checkout(root: Path, name: str, files: dict[str, str]) -> tuple[Path, str]:
    """root/name with `files` committed and an origin/main ref at that commit; (path, commit)"""
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "gc.auto", "0")
    files = {".github/workflows/ci.yml": CI_WF, **files}
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", sha)
    return repo, sha


def commit_more(repo: Path, rel: str, text: str, move_main: bool = True) -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "y")
    sha = git(repo, "rev-parse", "HEAD")
    if move_main:
        git(repo, "update-ref", "refs/remotes/origin/main", sha)
    return sha


def roster_text(rows: dict[str, dict]) -> str:
    out = ['[train]\ngithub = "ext-sakamoro"\n']
    for name, r in rows.items():
        out.append(f"\n[crate.{name}]")
        for k, v in r.items():
            out.append(f"{k} = {v!r}".replace("'", '"') if not isinstance(v, int) else f"{k} = {v}")
    return "\n".join(out) + "\n"


class Harness(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.root = Path(self._d.name)
        self.dm, self.dm_sha = checkout(self.root, "ALICE-DetMath", {"Cargo.toml": manifest("alice-det-math", "0.6.1")})
        self.zip, self.zip_sha = checkout(self.root, "ALICE-Zip", {"Cargo.toml": manifest(
            "alice-zip", "0.11.0", '[dependencies]\nalice-det-math = { git = "https://github.com/ext-sakamoro/ALICE-DetMath", '
            'branch = "main", version = "0.6" }\n')})
        self.rows = {
            "alice-det-math": {"version": "0.6.1", "state": "main", "order": 0, "repo": "ALICE-DetMath", "sha": self.dm_sha},
            "alice-zip": {"version": "0.11.0", "state": "main", "order": 1, "repo": "ALICE-Zip", "sha": self.zip_sha},
        }
        self.provenance = ""

    def check(self, rows=None, provenance=None, runs=None):
        roster = rt.load_roster(roster_text(rows or self.rows))
        return rt.check_problems(roster, self.root, self.provenance if provenance is None else provenance,
                                 runs=runs)


class Roster(Harness):
    def test_a_main_roster_loads(self):
        rt.load_roster(roster_text(self.rows))

    def test_the_release_states_are_gone(self):
        for state in ("published", "prepared", "prepared-local", "planned"):
            rows = {**self.rows, "alice-zip": {**self.rows["alice-zip"], "state": state}}
            with self.assertRaises(rt.RosterError, msg=state):
                rt.load_roster(roster_text(rows))

    def test_a_main_row_needs_a_full_sha(self):
        for bad in ({"sha": None}, {"sha": "abc123"}, {"sha": "g" * 40}):
            row = {k: v for k, v in self.rows["alice-zip"].items() if k != "sha"}
            if bad["sha"] is not None:
                row["sha"] = bad["sha"]
            with self.assertRaises(rt.RosterError, msg=str(bad)):
                rt.load_roster(roster_text({**self.rows, "alice-zip": row}))

    def test_publication_keys_are_refused(self):
        for key, value in (("must-contain", ["a" * 40]), ("must-contain-patch", ["b" * 40]),
                           ("changelog-has", ["x"]), ("tag", "v{version}")):
            rows = {**self.rows, "alice-zip": {**self.rows["alice-zip"], key: value}}
            with self.assertRaises(rt.RosterError, msg=key):
                rt.load_roster(roster_text(rows))

    def test_an_exempt_row_needs_no_sha(self):
        rows = {**self.rows, "alice-law-integration": {"version": "0.0.0", "state": "exempt", "order": 9}}
        rt.load_roster(roster_text(rows))

    def test_the_repository_roster_loads(self):
        rt.load_roster((HERE.parent / "release-train.toml").read_text(encoding="utf-8"))


class Check(Harness):
    def test_a_roster_at_main_passes(self):
        problems, compared = self.check()
        self.assertEqual(problems, [])
        self.assertEqual(compared, 2)

    def test_a_sha_behind_main_still_passes(self):
        commit_more(self.dm, "README.md", "x\n")
        problems, _ = self.check()
        self.assertEqual(problems, [])

    def test_a_sha_not_on_main_is_refused(self):
        side = commit_more(self.dm, "README.md", "x\n", move_main=False)
        rows = {**self.rows, "alice-det-math": {**self.rows["alice-det-math"], "sha": side}}
        problems, _ = self.check(rows)
        self.assertTrue(any("not on main" in p for p in problems), problems)

    def test_an_unknown_sha_is_refused(self):
        rows = {**self.rows, "alice-det-math": {**self.rows["alice-det-math"], "sha": "0" * 40}}
        problems, _ = self.check(rows)
        self.assertTrue(any("alice-det-math" in p and "not in" in p for p in problems), problems)

    def test_a_version_other_than_the_manifest_at_sha_is_refused(self):
        rows = {**self.rows, "alice-det-math": {**self.rows["alice-det-math"], "version": "0.5.1"}}
        problems, _ = self.check(rows)
        self.assertTrue(any("0.6.1" in p and "0.5.1" in p for p in problems), problems)

    def test_another_package_at_the_manifest_is_refused(self):
        sha = commit_more(self.dm, "Cargo.toml", manifest("alice-other", "0.6.1"))
        rows = {**self.rows, "alice-det-math": {**self.rows["alice-det-math"], "sha": sha}}
        problems, _ = self.check(rows)
        self.assertTrue(any("alice-other" in p for p in problems), problems)

    def test_a_manifest_path_is_read(self):
        sha = commit_more(self.zip, "sub/Cargo.toml", manifest("alice-zip-cli", "3.2.0"))
        rows = {**self.rows, "alice-zip-cli": {"version": "3.2.0", "state": "main", "order": 2, "repo": "ALICE-Zip",
                                               "sha": sha, "manifest-path": "sub/Cargo.toml"}}
        rows["alice-zip"] = {**rows["alice-zip"], "sha": sha}
        problems, compared = self.check(rows)
        self.assertEqual(problems, [])
        self.assertEqual(compared, 3)

    def test_a_missing_checkout_is_refused(self):
        rows = {**self.rows, "alice-db": {"version": "0.6.0", "state": "main", "order": 2, "repo": "ALICE-DB",
                                          "sha": "1" * 40}}
        problems, _ = self.check(rows)
        self.assertTrue(any("ALICE-DB" in p and "checkout" in p for p in problems), problems)

    def test_the_e2e_provenance_is_the_sha_of_its_repositories(self):
        prov = f"# header\nALICE-DetMath {self.dm_sha} alice-det-math 0.6.1\n"
        self.assertEqual(self.check(provenance=prov)[0], [])
        newer = commit_more(self.dm, "README.md", "x\n")
        prov = f"ALICE-DetMath {newer} alice-det-math 0.6.1\n"
        problems, _ = self.check(provenance=prov)
        self.assertTrue(any("provenance" in p and "ALICE-DetMath" in p for p in problems), problems)

    def test_the_provenance_version_must_match(self):
        prov = f"ALICE-DetMath {self.dm_sha} alice-det-math 0.6.0\n"
        problems, _ = self.check(provenance=prov)
        self.assertTrue(any("provenance" in p and "0.6.0" in p for p in problems), problems)

    def test_an_upstream_after_its_consumer_in_order_is_refused(self):
        rows = {"alice-det-math": {**self.rows["alice-det-math"], "order": 5}, "alice-zip": self.rows["alice-zip"]}
        problems, _ = self.check(rows)
        self.assertTrue(any("order" in p and "alice-det-math" in p for p in problems), problems)

    def test_a_publishing_workflow_is_refused(self):
        wf = "on:\n  push:\n    tags: ['v*']\njobs:\n  rel:\n    steps:\n      - run: cargo publish\n"
        sha = commit_more(self.zip, ".github/workflows/release.yml", wf)
        rows = {**self.rows, "alice-zip": {**self.rows["alice-zip"], "sha": sha}}
        problems, _ = self.check(rows)
        self.assertTrue(any("publish" in p and "release.yml" in p for p in problems), problems)

    def test_a_dry_run_publish_is_not_publishing(self):
        wf = "on:\n  push:\n    branches: [main]\njobs:\n  pkg:\n    steps:\n      - run: cargo publish --dry-run\n"
        sha = commit_more(self.zip, ".github/workflows/pkg.yml", wf)
        rows = {**self.rows, "alice-zip": {**self.rows["alice-zip"], "sha": sha}}
        self.assertEqual(self.check(rows)[0], [])

    def runs_of(self, table):
        """a fixture of gh_main_runs: {(owner_repo, sha): [runs]}, every other commit with one green push run"""
        seen = []

        def runs(owner_repo, sha):
            seen.append((owner_repo, sha))
            return table.get((owner_repo, sha), [{"workflowName": "CI", "event": "push", "status": "completed",
                                                  "conclusion": "success"}])
        return runs, seen

    def test_the_main_ci_of_each_sha_is_read(self):
        runs, seen = self.runs_of({})
        problems, _ = self.check(runs=runs)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(seen), sorted([("ext-sakamoro/ALICE-DetMath", self.dm_sha),
                                               ("ext-sakamoro/ALICE-Zip", self.zip_sha)]))

    def test_a_red_push_run_is_refused(self):
        runs, _ = self.runs_of({("ext-sakamoro/ALICE-Zip", self.zip_sha): [
            {"workflowName": "CI", "event": "push", "status": "completed", "conclusion": "success"},
            {"workflowName": "Fuzz", "event": "push", "status": "completed", "conclusion": "failure"}]})
        problems, _ = self.check(runs=runs)
        self.assertTrue(any("not green" in p and "Fuzz" in p for p in problems), problems)

    def test_a_running_push_run_is_refused(self):
        runs, _ = self.runs_of({("ext-sakamoro/ALICE-Zip", self.zip_sha): [
            {"workflowName": "CI", "event": "push", "status": "in_progress", "conclusion": ""}]})
        problems, _ = self.check(runs=runs)
        self.assertTrue(any("not green" in p for p in problems), problems)

    def test_a_red_scheduled_run_is_a_note_not_a_problem(self):
        runs, _ = self.runs_of({("ext-sakamoro/ALICE-Zip", self.zip_sha): [
            {"workflowName": "CI", "event": "push", "status": "completed", "conclusion": "success"},
            {"workflowName": "Quality Deep", "event": "schedule", "status": "completed", "conclusion": "failure",
             "databaseId": 7}]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            problems, _ = self.check(runs=runs)
        self.assertEqual(problems, [])
        self.assertIn("Quality Deep 7", out.getvalue())

    def test_no_push_run_is_refused_when_a_workflow_runs_on_the_change(self):
        runs, _ = self.runs_of({("ext-sakamoro/ALICE-Zip", self.zip_sha): []})
        problems, _ = self.check(runs=runs)
        self.assertTrue(any("no workflow run" in p and "ci.yml" in p for p in problems), problems)

    def test_no_push_run_is_fine_when_every_workflow_is_filtered_out(self):
        wf = "on:\n  push:\n    branches: [main]\n    paths:\n      - 'src/**'\njobs:\n  t:\n    steps:\n      - run: x\n"
        commit_more(self.zip, ".github/workflows/ci.yml", wf)
        sha = commit_more(self.zip, "docs/notes.md", "x\n")
        rows = {**self.rows, "alice-zip": {**self.rows["alice-zip"], "sha": sha}}
        runs, _ = self.runs_of({("ext-sakamoro/ALICE-Zip", sha): []})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            problems, _ = self.check(rows, runs=runs)
        self.assertEqual(problems, [])
        self.assertIn("filtered out", out.getvalue())
        sha2 = commit_more(self.zip, "src/lib.rs", "x\n")
        rows = {**self.rows, "alice-zip": {**self.rows["alice-zip"], "sha": sha2}}
        runs, _ = self.runs_of({("ext-sakamoro/ALICE-Zip", sha2): []})
        problems, _ = self.check(rows, runs=runs)
        self.assertTrue(any("no workflow run" in p for p in problems), problems)

    def test_paths_ignore_and_negation(self):
        wf = ("on:\n  push:\n    branches: [main]\n    paths-ignore:\n      - '**.md'\njobs:\n  t:\n    steps:\n"
              "      - run: x\n")
        self.assertFalse(rt.push_runs_for(wf, ["README.md", "docs/a.md"]))
        self.assertTrue(rt.push_runs_for(wf, ["README.md", "src/a.rs"]))
        self.assertTrue(rt.filter_matches(["src/**", "!src/gen/**"], "src/a.rs"))
        self.assertFalse(rt.filter_matches(["src/**", "!src/gen/**"], "src/gen/a.rs"))
        self.assertIsNone(rt.push_runs_for("on:\n  push:\n    branches: ['ci/**']\n", ["a"]))
        self.assertIsNone(rt.push_runs_for("on:\n  pull_request:\n", ["a"]))
        self.assertTrue(rt.push_runs_for("on: [push, pull_request]\n", ["a"]))

    def test_a_private_repository_is_refused(self):
        runs, _ = self.runs_of({("ext-sakamoro/ALICE-Zip", self.zip_sha): [{"private": True}]})
        problems, _ = self.check(runs=runs)
        self.assertTrue(any("private" in p for p in problems), problems)

    def test_an_exempt_row_is_not_compared(self):
        rows = {**self.rows, "alice-law-integration": {"version": "0.0.0", "state": "exempt", "order": 9}}
        self.assertEqual(self.check(rows)[1], 2)


class Command(Harness):
    def run_cmd(self, *argv, rows=None, provenance="") -> tuple[int, str]:
        p = self.root / "release-train.toml"
        p.write_text(roster_text(rows or self.rows), encoding="utf-8")
        prov = self.root / "values.provenance"
        prov.write_text(provenance, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = rt.main(["--roster", str(p), *argv, "--repos-root", str(self.root), "--provenance", str(prov),
                            "--no-ci"])
        return code, out.getvalue()

    def test_check_exits_0_when_every_row_is_at_main(self):
        code, out = self.run_cmd("check")
        self.assertEqual(code, 0, out)
        self.assertIn("2 crate(s) compared", out)

    def test_check_exits_1_on_a_problem(self):
        rows = {**self.rows, "alice-det-math": {**self.rows["alice-det-math"], "version": "0.5.1"}}
        code, out = self.run_cmd("check", rows=rows)
        self.assertEqual(code, 1, out)

    def test_check_with_nothing_compared_exits_2(self):
        rows = {"alice-law-integration": {"version": "0.0.0", "state": "exempt", "order": 0}}
        code, out = self.run_cmd("check", rows=rows)
        self.assertEqual(code, 2, out)

    def test_record_writes_main_and_keeps_the_rest(self):
        newer = commit_more(self.dm, "Cargo.toml", manifest("alice-det-math", "0.6.2"))
        p = self.root / "release-train.toml"
        text = roster_text(self.rows).replace('[crate.alice-det-math]', '# a note kept\n[crate.alice-det-math]')
        p.write_text(text, encoding="utf-8")
        prov = self.root / "values.provenance"
        prov.write_text("", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = rt.main(["--roster", str(p), "record", "--repos-root", str(self.root), "--provenance", str(prov),
                            "--no-ci"])
        self.assertEqual(code, 0)
        after = rt.load_roster(p.read_text(encoding="utf-8"))
        self.assertEqual(after["alice-det-math"]["sha"], newer)
        self.assertEqual(after["alice-det-math"]["version"], "0.6.2")
        self.assertEqual(after["alice-zip"]["sha"], self.zip_sha)
        self.assertIn("# a note kept", p.read_text(encoding="utf-8"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rt.main(["--roster", str(p), "check", "--repos-root", str(self.root), "--provenance", str(prov),
                            "--no-ci"])
        self.assertEqual(code, 0, out.getvalue())  # what record wrote passes check as is

    def test_record_takes_the_provenance_for_e2e_repositories(self):
        newer = commit_more(self.dm, "README.md", "x\n")
        p = self.root / "release-train.toml"
        p.write_text(roster_text(self.rows), encoding="utf-8")
        prov = self.root / "values.provenance"
        prov.write_text(f"ALICE-DetMath {self.dm_sha} alice-det-math 0.6.1\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rt.main(["--roster", str(p), "record", "--repos-root", str(self.root),
                                      "--provenance", str(prov), "--no-ci"]), 0)
        after = rt.load_roster(p.read_text(encoding="utf-8"))
        self.assertEqual(after["alice-det-math"]["sha"], self.dm_sha)
        self.assertNotEqual(after["alice-det-math"]["sha"], newer)

    def test_record_refuses_a_commit_whose_main_ci_is_red(self):
        p = self.root / "release-train.toml"
        before = roster_text(self.rows)
        p.write_text(before, encoding="utf-8")
        prov = self.root / "values.provenance"
        prov.write_text("", encoding="utf-8")

        def runs(owner_repo, sha):
            ok = owner_repo.endswith("ALICE-DetMath")
            return [{"workflowName": "CI", "event": "push", "status": "completed",
                     "conclusion": "success" if ok else "failure"}]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rt.main(["--roster", str(p), "record", "--repos-root", str(self.root), "--provenance", str(prov)],
                           runs=runs)
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("ALICE-Zip", out.getvalue())
        self.assertEqual(p.read_text(encoding="utf-8"), before)

    def test_the_publication_commands_are_gone(self):
        for cmd in ("next", "publish-gate", "publish-verify", "roster-mark"):
            p = self.root / "release-train.toml"
            p.write_text(roster_text(self.rows), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit, msg=cmd):
                rt.main(["--roster", str(p), cmd])


if __name__ == "__main__":
    unittest.main()
