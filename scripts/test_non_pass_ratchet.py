#!/usr/bin/env python3
"""Tests for scripts/non_pass_ratchet.py: what counts as not met, the ratchet both ways (--states red on a fall,
--only / --private a note on a fall), --private refusing a repository with no row, the rule that a rising row names
its reason, the committed baseline's form, and the CLI on a real git tree and on a states file."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import non_pass_ratchet as npr  # noqa: E402

ROW = {"count": 2, "raised": None}


def counts(passed=0, fail=0, unv=0, na=0):
    return {"pass": passed, "fail-known": fail, "unverified": unv, "n/a": na}


def run(*args: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(HERE / "non_pass_ratchet.py"), *args], capture_output=True, text=True,
                       encoding="utf-8")
    return p.returncode, p.stdout + p.stderr


class Count(unittest.TestCase):
    def test_fail_known_and_unverified_count_and_pass_and_na_do_not(self):
        self.assertEqual(npr.non_pass(counts(passed=10, fail=2, unv=3, na=4)), 5)
        self.assertEqual(npr.non_pass(counts(passed=15)), 0)

    def test_counts_without_a_status_are_refused(self):
        with self.assertRaises(npr.crc.InputError):
            npr.non_pass({"pass": 1})


class Rules(unittest.TestCase):
    def test_equal_passes(self):
        self.assertEqual(npr.compare({"A": 2}, {"A": ROW}, {"A"}, below_is_note=False), ([], []))

    def test_a_rise_is_red_either_way(self):
        for note in (False, True):
            f, _ = npr.compare({"A": 3}, {"A": ROW}, None, below_is_note=note)
            self.assertIn("3 non-pass gate(s) > baseline 2", f[0])

    def test_a_fall_is_red_on_states_and_a_note_on_the_crate_check(self):
        f, n = npr.compare({"A": 1}, {"A": ROW}, {"A"}, below_is_note=False)
        self.assertIn("lower the row to 1) in this commit", f[0])
        self.assertEqual(n, [])
        f, n = npr.compare({"A": 1}, {"A": ROW}, None, below_is_note=True)
        self.assertEqual(f, [])
        self.assertIn("lower the row to 1", n[0])

    def test_zero_refuses_any(self):
        self.assertIn("> baseline 0", npr.compare({"A": 1}, {"A": {"count": 0, "raised": None}}, None, False)[0][0])

    def test_a_missing_row_and_a_row_off_the_roster_are_red(self):
        self.assertIn("no row", npr.compare({"A": 0}, {}, {"A"}, False)[0][0])
        self.assertIn("does not list", npr.compare({}, {"B": ROW}, {"A"}, False)[0][0])


class Raise(unittest.TestCase):
    def test_a_rise_without_a_reason_is_red_and_with_one_passes(self):
        base = {"A": {"count": 1, "raised": None}}
        self.assertIn("rises without", npr.raise_findings({"A": {"count": 2, "raised": None}}, base)[0])
        self.assertIn("rises without", npr.raise_findings({"A": {"count": 2, "raised": "aaaaaaaaaaaaa"}}, base)[0])
        self.assertEqual(npr.raise_findings({"A": {"count": 2, "raised": "G15 adopted, not yet met"}}, base), [])

    def test_a_reason_on_a_row_that_does_not_rise_is_red(self):
        base = {"A": {"count": 2, "raised": None}}
        for n in (2, 1):
            self.assertIn("does not rise", npr.raise_findings({"A": {"count": n, "raised": "G15 adopted, not met"}},
                                                              base)[0])

    def test_a_fall_and_a_new_row_need_no_reason(self):
        base = {"A": {"count": 2, "raised": None}}
        self.assertEqual(npr.raise_findings({"A": {"count": 1, "raised": None}, "B": {"count": 4, "raised": None}},
                                            base), [])


class Baseline(unittest.TestCase):
    def test_the_committed_baseline_loads_and_matches_the_roster(self):
        rows = npr.load_baseline(npr.BASELINE)
        self.assertGreater(len(rows), 0)
        code, out = run("--check-baseline")
        self.assertEqual(code, 0, out)
        self.assertIn(f"baseline {len(rows)} row(s)", out)

    def test_a_bad_row_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            for body in ('[repository."A"]\nnon_pass = -1\n', '[repository."A"]\nnon_pass = true\n',
                         '[repository."A"]\nnon_pass = 1\nwhy = "x"\n', "# nothing\n"):
                p = Path(t) / "b.toml"
                p.write_text(body, encoding="utf-8")
                self.assertEqual(run("--check-baseline", "--no-roster", "--baseline", str(p))[0], 2, body)

    def test_check_baseline_refuses_a_row_off_the_roster_unless_no_roster(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "b.toml"
            p.write_text('[repository."ALICE-NotInTheTrain"]\nnon_pass = 1\n', encoding="utf-8")
            code, out = run("--check-baseline", "--baseline", str(p))
            self.assertEqual(code, 1, out)
            self.assertIn("ALICE-NotInTheTrain: a row for a repository the roster does not list", out)
            self.assertEqual(run("--check-baseline", "--no-roster", "--baseline", str(p))[0], 0)

    def test_check_baseline_compares_with_the_base(self):
        with tempfile.TemporaryDirectory() as t:
            base, now = Path(t) / "base.toml", Path(t) / "now.toml"
            base.write_text('[repository."P"]\nnon_pass = 3\n', encoding="utf-8")
            now.write_text('[repository."P"]\nnon_pass = 4\n', encoding="utf-8")
            code, out = run("--check-baseline", "--no-roster", "--baseline", str(now), "--base-baseline", str(base))
            self.assertEqual(code, 1, out)
            self.assertIn("rises without", out)
            now.write_text('[repository."P"]\nnon_pass = 4\nraised = "G15 adopted, not yet met"\n', encoding="utf-8")
            code, out = run("--check-baseline", "--no-roster", "--baseline", str(now), "--base-baseline", str(base))
            self.assertEqual(code, 0, out)
            self.assertIn("compared with the base", out)


class States(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.states = Path(self.tmp.name) / "s.json"
        self.base = Path(self.tmp.name) / "b.toml"
        roster = sorted(npr.crc.repo_crates(npr.crc.load_toml(npr.ROSTER)))
        self.repo = roster[0]
        self.base.write_text(f'[repository."{self.repo}"]\nnon_pass = 2\n', encoding="utf-8")

    def write(self, data):
        self.states.write_text(json.dumps(data), encoding="utf-8")

    def test_states_at_the_row_pass_and_a_rise_or_fall_is_red(self):
        self.write({self.repo: counts(passed=13, unv=2)})
        self.assertEqual(run("--states", str(self.states), "--baseline", str(self.base))[0], 0)
        self.write({self.repo: counts(passed=12, unv=2, fail=1)})
        code, out = run("--states", str(self.states), "--baseline", str(self.base))
        self.assertEqual(code, 1, out)
        self.write({self.repo: counts(passed=14, unv=1)})
        code, out = run("--states", str(self.states), "--baseline", str(self.base))
        self.assertEqual(code, 1, out)
        self.assertIn("in this commit", out)

    def test_an_empty_states_file_compares_nothing(self):
        self.write({})
        code, out = run("--states", str(self.states), "--baseline", str(self.base))
        self.assertEqual(code, 2, out)
        self.assertIn("compared nothing", out)


class Tree(unittest.TestCase):
    """--only / --private on a real git tree, read the way the roster reads one manifest"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "ALICE-P"
        self.dir.mkdir()
        gates = npr.crc.gate_vocabulary(npr.crc.load_toml(npr.GATES))
        self.gates = sorted(gates, key=lambda g: int(g[1:]))
        cells = [f'[{g}]\nstatus = "unverified"\n' for g in self.gates]
        (self.dir / npr.crc.MANIFEST).write_text("\n".join(cells), encoding="utf-8")
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "m"]):
            subprocess.run(["git", "-C", str(self.dir), *args], check=True)
        self.n = len(self.gates)
        self.base = Path(self.tmp.name) / "b.toml"

    def baseline(self, n: int | None):
        self.base.write_text(f'[repository."ALICE-P"]\nnon_pass = {n}\n' if n is not None
                             else '[repository."ALICE-Q"]\nnon_pass = 0\n', encoding="utf-8")

    def test_private_at_the_row_passes_and_counts_every_unverified_gate(self):
        self.baseline(self.n)
        code, out = run("--private", f"ALICE-P={self.dir}", "--baseline", str(self.base))
        self.assertEqual(code, 0, out)
        self.assertIn(f"{self.n} non-pass gate(s) in all", out)

    def test_private_above_the_row_is_red_and_below_is_a_note(self):
        self.baseline(self.n - 1)
        self.assertEqual(run("--private", f"ALICE-P={self.dir}", "--baseline", str(self.base))[0], 1)
        self.baseline(self.n + 1)
        code, out = run("--private", f"ALICE-P={self.dir}", "--baseline", str(self.base))
        self.assertEqual(code, 0, out)
        self.assertIn(f"lower the row to {self.n}", out)

    def test_private_without_a_row_is_red_and_only_without_a_row_is_a_note(self):
        self.baseline(None)
        code, out = run("--private", f"ALICE-P={self.dir}", "--baseline", str(self.base))
        self.assertEqual(code, 1, out)
        self.assertIn("no row in the baseline", out)
        code, out = run("--only", f"ALICE-P={self.dir}", "--baseline", str(self.base))
        self.assertEqual(code, 0, out)
        self.assertIn("outside the release train", out)

    def test_a_pass_without_evidence_still_counts(self):
        # writing `pass` with no evidence does not lower the count: the roster reads it as unverified
        text = (self.dir / npr.crc.MANIFEST).read_text(encoding="utf-8")
        (self.dir / npr.crc.MANIFEST).write_text(text.replace('status = "unverified"', 'status = "pass"', 1),
                                                 encoding="utf-8")
        subprocess.run(["git", "-C", str(self.dir), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam",
                        "p"], check=True)
        self.baseline(self.n)
        code, out = run("--private", f"ALICE-P={self.dir}", "--baseline", str(self.base))
        self.assertEqual(code, 0, out)
        self.assertIn(f"{self.n} non-pass gate(s) in all", out)


if __name__ == "__main__":
    unittest.main()
