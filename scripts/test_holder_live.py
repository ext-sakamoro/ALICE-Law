#!/usr/bin/env python3
"""Tests for holder_live.py: the rights-holder rule over the public repositories, against the baseline.

Usage: python3 scripts/test_holder_live.py
"""
from __future__ import annotations

import atexit
import shutil
import subprocess
import sys
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import holder_live as hl  # noqa: E402
import licence_files_check as lfc  # noqa: E402


def scratch() -> Path:
    """a temporary directory removed when the test process ends (a fixture repository is read after the call
    that made it returns, so it cannot be a with-block)"""
    d = Path(tempfile.mkdtemp(prefix="law-test-"))
    atexit.register(shutil.rmtree, d, ignore_errors=True)
    return d

OTHER = "Ex" + "toria"  # split, so this file's own text is not an occurrence


def upstream(files: dict[str, str]) -> Path:
    d = scratch()
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    g = ["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "x"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "uploadpack.allowFilter", "true"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "uploadpack.allowAnySHA1InWant", "true"], check=True)
    return d


class ScanGit(unittest.TestCase):
    def test_a_blobless_clone_is_read_through_the_rule(self):
        up = upstream({"LICENSE": f"(c) {OTHER}\n", "Cargo.toml": '[package]\nname = "x"\nauthors = ["you"]\n',
                       "vendor/x/LICENSE": f"(c) {OTHER}\n", "src/lib.rs": f"// {OTHER}\n"})
        base = scratch()
        with mock.patch.object(hl, "GITHUB", "file://"):
            name, occ, n, err = hl.read_repo(base, {"name": "r", "full_name": str(up), "default_branch": "main", "size": 1})
        self.assertIsNone(err)
        self.assertEqual(occ, {("holder", "LICENSE"), ("authors", "Cargo.toml")})
        self.assertEqual(n, 2)

    def test_an_unreachable_repository_is_an_error_not_a_pass(self):
        base = scratch()
        with mock.patch.object(hl, "GITHUB", "file:///nonexistent/"):
            _, occ, _, err = hl.read_repo(base, {"name": "r", "full_name": "r", "default_branch": "main", "size": 1})
        self.assertIsNone(occ)
        self.assertTrue(err)

    def test_an_empty_repository_has_nothing_to_read(self):
        self.assertEqual(hl.read_repo(Path("."), {"name": "e", "size": 0}), ("e", None, 0, None))


class CompareAll(unittest.TestCase):
    def test_new_stale_and_vanished_entries(self):
        b = {lfc.repo_key("a"): {("holder", "LICENSE")}, lfc.repo_key("gone"): {("authors", "Cargo.toml")}}
        f = hl.compare_all({"a": {("authors", "Cargo.toml")}}, b)
        self.assertEqual(len(f), 3, f)
        self.assertTrue(any("not in the baseline" in x for x in f))
        self.assertTrue(any(x.startswith("a: stale") for x in f))
        self.assertTrue(any("is not public" in x for x in f))

    def test_equal_is_clean(self):
        b = {lfc.repo_key("a"): {("holder", "LICENSE")}}
        self.assertEqual(hl.compare_all({"a": {("holder", "LICENSE")}, "b": set()}, b), [])


class GrowthOnly(unittest.TestCase):
    """the branch path: growth fails, a fixed (stale) entry is a note that the daily run turns red"""

    def test_stale_and_vanished_entries_are_notes_and_growth_still_fails(self):
        b = {lfc.repo_key("a"): {("holder", "LICENSE")}, lfc.repo_key("gone"): {("authors", "Cargo.toml")}}
        findings, notes = hl.compare_split({"a": {("authors", "Cargo.toml")}}, b)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("not in the baseline", findings[0])
        self.assertEqual(len(notes), 2, notes)
        self.assertTrue(all("stale" in n for n in notes))

    def run_main(self, results, baseline_rows, *flags):
        repos = [{"name": n} for n in results]
        with mock.patch.object(hl, "public_repos", return_value=repos), \
                mock.patch.object(hl, "read_repo", side_effect=lambda base, r: results[r["name"]]):
            b = scratch() / "b.toml"
            b.write_text(lfc.dump_baseline(baseline_rows), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = hl.main(["--baseline", str(b), *flags])
            return code, out.getvalue()

    def test_a_stale_entry_fails_the_daily_run_and_is_a_note_on_growth_only(self):
        base = {lfc.repo_key("a"): {("holder", "LICENSE")}}
        fixed = {"a": ("a", set(), 2, None)}
        self.assertEqual(self.run_main(fixed, base)[0], 1)
        code, out = self.run_main(fixed, base, "--growth-only")
        self.assertEqual(code, 0, out)
        self.assertIn("stale", out)

    def test_growth_fails_on_growth_only(self):
        grown = {"a": ("a", {("holder", "LICENSE"), ("holder", "NOTICE")}, 2, None)}
        code, out = self.run_main(grown, {lfc.repo_key("a"): {("holder", "LICENSE")}}, "--growth-only")
        self.assertEqual(code, 1, out)

    def test_growth_only_still_needs_something_read(self):
        code, out = self.run_main({"a": ("a", set(), 0, None)}, {}, "--growth-only")
        self.assertEqual(code, 2, out)


class Shrink(unittest.TestCase):
    def test_only_removes_and_never_adds(self):
        b = {lfc.repo_key("a"): {("holder", "LICENSE"), ("authors", "Cargo.toml")}, lfc.repo_key("gone"): {("holder", "NOTICE")}}
        read = {"a": {("holder", "LICENSE"), ("holder", "NEW")}}
        self.assertEqual(hl.shrink(b, read, []), {lfc.repo_key("a"): {("holder", "LICENSE")}})

    def test_a_named_repository_leaves_the_others(self):
        b = {lfc.repo_key("a"): {("holder", "LICENSE")}, lfc.repo_key("b"): {("holder", "LICENSE")}}
        read = {"a": set(), "b": set()}
        self.assertEqual(hl.shrink(b, read, ["a"]), {lfc.repo_key("b"): {("holder", "LICENSE")}})


class Exit(unittest.TestCase):
    def run_main(self, repos, results):
        with mock.patch.object(hl, "public_repos", return_value=repos), \
                mock.patch.object(hl, "read_repo", side_effect=lambda base, r: results[r["name"]]):
            b = scratch() / "b.toml"
            b.write_text("", encoding="utf-8")
            return hl.main(["--baseline", str(b)])

    def test_a_partial_read_is_exit_2(self):
        repos = [{"name": "a"}, {"name": "b"}]
        res = {"a": ("a", set(), 3, None), "b": ("b", None, 0, "b: fetch failed")}
        self.assertEqual(self.run_main(repos, res), 2)

    def test_nothing_listed_or_read_is_exit_2(self):
        self.assertEqual(self.run_main([], {}), 2)
        self.assertEqual(self.run_main([{"name": "a"}], {"a": ("a", set(), 0, None)}), 2)

    def test_a_new_occurrence_is_exit_1_and_clean_is_0(self):
        self.assertEqual(self.run_main([{"name": "a"}], {"a": ("a", {("holder", "LICENSE")}, 1, None)}), 1)
        self.assertEqual(self.run_main([{"name": "a"}], {"a": ("a", set(), 1, None)}), 0)


class UnderTest(unittest.TestCase):
    """--local NAME=DIR: the repository under test (the change CI runs on) is read from its checkout at HEAD, every
    other one at its live default branch, so a change that removes a rights statement and its baseline row together
    is judged on its own tree, not on a main that still has the file"""

    def checkout(self, files: dict[str, str]) -> Path:
        return upstream(files)

    def run_main(self, local: Path | None, baseline: str, main_result):
        repos = [{"name": "law"}, {"name": "other"}]

        def remote(base, r):
            if r["name"] == "law":
                if local is not None:
                    raise AssertionError("the repository under test was fetched from its main")
                return main_result
            return ("other", set(), 1, None)

        with mock.patch.object(hl, "public_repos", return_value=repos), \
                mock.patch.object(hl, "read_repo", side_effect=remote):
            b = scratch() / "b.toml"
            b.write_text(baseline, encoding="utf-8")
            args = ["--baseline", str(b)] + (["--local", f"law={local}"] if local is not None else [])
            return hl.main(args)

    def test_a_statement_removed_with_its_row_passes_on_the_change(self):
        # main still has the statement (and so does not match a baseline without its row); the change removed both
        on_main = ("law", {("holder", "LICENSE-COMMERCIAL.md")}, 1, None)
        tree = self.checkout({"LICENSE": "(c) the author\n", "Cargo.toml": '[package]\nname = "x"\n'})
        self.assertEqual(self.run_main(tree, "", on_main), 0)
        self.assertEqual(self.run_main(None, "", on_main), 1)  # read at main, the same change is red

    def test_the_statement_still_present_on_the_change_is_a_finding(self):
        on_main = ("law", {("holder", "LICENSE-COMMERCIAL.md")}, 1, None)
        tree = self.checkout({"LICENSE-COMMERCIAL.md": f"(c) {OTHER}\n", "Cargo.toml": '[package]\nname = "x"\n'})
        self.assertEqual(self.run_main(tree, "", on_main), 1)

    def test_the_checkout_is_read_at_head_not_at_its_working_files(self):
        tree = self.checkout({"LICENSE": f"(c) {OTHER}\n"})
        (tree / "LICENSE").write_text("(c) the author\n", encoding="utf-8")  # uncommitted: not what CI tests
        occ, n = hl.read_local(tree)
        self.assertEqual((occ, n), ({("holder", "LICENSE")}, 1))

    def test_a_local_name_that_is_not_a_listed_repository_is_exit_2(self):
        tree = self.checkout({"LICENSE": "(c) the author\n"})
        with mock.patch.object(hl, "public_repos", return_value=[{"name": "other"}]), \
                mock.patch.object(hl, "read_repo", return_value=("other", set(), 1, None)):
            b = scratch() / "b.toml"
            b.write_text("", encoding="utf-8")
            self.assertEqual(hl.main(["--baseline", str(b), "--local", f"law={tree}"]), 2)
            # a listed name without a checkout is malformed, not a read of something else
            for bad in ("other", "other=", f"={tree}"):
                self.assertEqual(hl.main(["--baseline", str(b), "--local", bad]), 2, bad)


if __name__ == "__main__":
    sys.exit(unittest.main())
