#!/usr/bin/env python3
"""Tests of scripts/crates_conformance_ci.py (python3 scripts/test_crates_conformance_ci.py)

The repositories and the checkers are stand-ins (fetch and run replaced): what is tested is the accounting, the
red / ok decisions and the nothing-compared failures, not the checkers themselves (they have their own tests)."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import crates_conformance_ci as c  # noqa: E402


class Crates(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.saved = (c.table_repos, c.baseline_repos, c.fetch, c.run)

    def tearDown(self):
        c.table_repos, c.baseline_repos, c.fetch, c.run = self.saved

    def repo(self, name: str, g11: bool = True) -> Path:
        d = self.td / name
        (d / "conformance").mkdir(parents=True, exist_ok=True)
        if g11:
            (d / "conformance" / "g11.toml").write_text("table\n")
        return d

    def go(self, kind: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = c.g11(None) if kind == "g11" else c.g15(None)
        return code, buf.getvalue()

    def test_g11_every_crate_ok(self):
        repos = {"ALICE-A": self.repo("ALICE-A"), "ALICE-B": self.repo("ALICE-B")}
        c.table_repos = lambda: sorted(repos)
        c.fetch = lambda r, into, src: repos[r]
        c.run = lambda argv: (0, "g11_table_gen: 3 rows, 0 gaps")
        code, out = self.go("g11")
        self.assertEqual(code, 0, out)
        self.assertIn("g11: checked 2 repositories, 0 red", out)

    def test_g11_a_gap_is_red_and_named(self):
        repos = {"ALICE-A": self.repo("ALICE-A")}
        c.table_repos = lambda: sorted(repos)
        c.fetch = lambda r, into, src: repos[r]
        c.run = lambda argv: (1, "error: G13 is pass and has no row")
        code, out = self.go("g11")
        self.assertEqual(code, 1, out)
        self.assertIn("g11: ALICE-A: red (exit 1): error: G13 is pass and has no row", out)

    def test_g11_a_table_that_moves_is_reported_not_red(self):
        a = self.repo("ALICE-A")
        c.table_repos = lambda: ["ALICE-A"]
        c.fetch = lambda r, into, src: a

        def regenerate(argv):
            (a / "conformance" / "g11.toml").write_text("another table\n")
            return 0, "g11_table_gen: 3 rows, 0 gaps"
        c.run = regenerate
        code, out = self.go("g11")
        self.assertEqual(code, 0, out)
        self.assertIn("its table changes with this catalog", out)

    def test_g11_an_unreadable_repository_is_red(self):
        c.table_repos = lambda: ["ALICE-A", "ALICE-B"]
        b = self.repo("ALICE-B")
        c.fetch = lambda r, into, src: None if r == "ALICE-A" else b
        c.run = lambda argv: (0, "ok")
        code, out = self.go("g11")
        self.assertEqual(code, 1, out)
        self.assertIn("g11: ALICE-A: could not be read", out)

    def test_g11_no_crate_with_a_table_compares_nothing(self):
        a = self.repo("ALICE-A", g11=False)
        c.table_repos = lambda: ["ALICE-A"]
        c.fetch = lambda r, into, src: a
        c.run = lambda argv: self.fail("nothing to run")
        code, out = self.go("g11")
        self.assertEqual(code, 2, out)
        self.assertIn("g11: checked 0 repositories", out)

    def test_g15_counts_and_red(self):
        repos = {"ALICE-A": self.repo("ALICE-A"), "ALICE-B": self.repo("ALICE-B")}
        c.baseline_repos = lambda: sorted(repos)
        c.fetch = lambda r, into, src: repos[r]
        c.run = lambda argv: (1, "against the baseline 1") if "ALICE-B" in argv else (0, "against the baseline 0")
        code, out = self.go("g15")
        self.assertEqual(code, 1, out)
        self.assertIn("g15: ALICE-B: red (exit 1)", out)
        self.assertIn("g15: checked 2 repositories, 1 red", out)

    def test_g15_this_repository_is_read_in_its_own_tree(self):
        c.baseline_repos = lambda: [c.SELF]
        c.fetch = lambda r, into, src: self.fail("ALICE-Law is not fetched")
        seen = []
        c.run = lambda argv: (seen.append(argv), (0, "ok"))[1]
        code, out = self.go("g15")
        self.assertEqual(code, 0, out)
        self.assertIn(str(c.ROOT), seen[0])

    def test_g15_an_empty_baseline_compares_nothing(self):
        c.baseline_repos = lambda: []
        code, out = self.go("g15")
        self.assertEqual(code, 2, out)
        self.assertIn("g15: checked 0 repositories", out)


if __name__ == "__main__":
    unittest.main()
