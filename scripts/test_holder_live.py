#!/usr/bin/env python3
"""Tests for holder_live.py: the rights-holder rule over the public repositories, against the baseline.

Usage: python3 scripts/test_holder_live.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import holder_live as hl  # noqa: E402
import licence_files_check as lfc  # noqa: E402

OTHER = "Ex" + "toria"  # split, so this file's own text is not an occurrence


def upstream(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp())
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
        base = Path(tempfile.mkdtemp())
        with mock.patch.object(hl, "GITHUB", "file://"):
            name, occ, n, err = hl.read_repo(base, {"name": "r", "full_name": str(up), "default_branch": "main", "size": 1})
        self.assertIsNone(err)
        self.assertEqual(occ, {("holder", "LICENSE"), ("authors", "Cargo.toml")})
        self.assertEqual(n, 2)

    def test_an_unreachable_repository_is_an_error_not_a_pass(self):
        base = Path(tempfile.mkdtemp())
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


class Exit(unittest.TestCase):
    def run_main(self, repos, results):
        with mock.patch.object(hl, "public_repos", return_value=repos), \
                mock.patch.object(hl, "read_repo", side_effect=lambda base, r: results[r["name"]]):
            b = Path(tempfile.mkdtemp()) / "b.toml"
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


if __name__ == "__main__":
    sys.exit(unittest.main())
