#!/usr/bin/env python3
"""Tests for scripts/pass_anchor_ratchet.py: what counts as unanchored, the ratchet's rules both ways, the committed
baseline's form, and --only on a real git tree (a rise, a fall, at the row, and nothing read)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pass_anchor_ratchet as par  # noqa: E402

ANCHORED = '"ci runs it `.github/workflows/ci.yml#- name: Test`"'
BARE = '".github/workflows/ci.yml runs it"'


def manifest(**cells: tuple[str, str]) -> str:
    out = []
    for gate, (status, ev) in cells.items():
        out += [f"[{gate}]", f'status = "{status}"', f"evidence = [{ev}]" if ev else "evidence = []", 'mutant = "m"', ""]
    return "\n".join(out)


class Count(unittest.TestCase):
    def test_a_pass_without_an_anchor_counts_and_an_anchored_one_does_not(self):
        text = manifest(G1=("pass", BARE), G2=("pass", ANCHORED), G12=("pass", f"{BARE}, {ANCHORED}"))
        self.assertEqual(par.unanchored(text), ["G1"])

    def test_only_pass_cells_count(self):
        text = manifest(G1=("fail-known", BARE), G2=("unverified", ""), G3=("n/a", BARE), G4=("pass", ""))
        self.assertEqual(par.unanchored(text), ["G4"])

    def test_a_code_span_that_is_not_a_path_anchor_does_not_count_as_one(self):
        # `#[cfg(test)]` is code, not path#token
        text = manifest(G13=("pass", '"items under `#[cfg(test)]` are skipped"'))
        self.assertEqual(par.unanchored(text), ["G13"])


class Rules(unittest.TestCase):
    def test_equal_passes(self):
        self.assertEqual(par.compare({"A": ["G1"]}, {"A": 1}, {"A"}), [])

    def test_a_rise_and_a_fall_are_red(self):
        self.assertIn("> baseline 1", par.compare({"A": ["G1", "G2"]}, {"A": 1}, {"A"})[0])
        self.assertIn("lower the baseline", par.compare({"A": []}, {"A": 1}, {"A"})[0])

    def test_zero_refuses_any(self):
        self.assertIn("> baseline 0", par.compare({"A": ["G5"]}, {"A": 0}, {"A"})[0])

    def test_a_missing_row_and_a_row_off_the_roster_are_red(self):
        self.assertIn("no row", par.compare({"A": []}, {}, {"A"})[0])
        self.assertIn("does not list", par.compare({}, {"B": 0}, {"A"})[0])


class Baseline(unittest.TestCase):
    def test_the_committed_baseline_loads_and_covers_rows_with_counts(self):
        rows = par.load_baseline()
        self.assertTrue(rows)
        self.assertTrue(all(isinstance(v, int) and v >= 0 for v in rows.values()))

    def test_a_negative_or_non_integer_count_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.toml"
            for bad in ('unanchored = -1', 'unanchored = "1"', 'unanchored = true'):
                p.write_text(f'[repository."A"]\n{bad}\n', encoding="utf-8")
                with self.assertRaises(par.crc.InputError):
                    par.load_baseline(p)

    def test_the_render_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.toml"
            p.write_text(par.render({"A": 0, "B": 3}), encoding="utf-8")
            self.assertEqual(par.load_baseline(p), {"A": 0, "B": 3})


class CheckBaseline(unittest.TestCase):
    def run_check(self, rows: dict[str, int]) -> int:
        with tempfile.TemporaryDirectory() as t:
            b = Path(t) / "b.toml"
            b.write_text(par.render(rows), encoding="utf-8")
            return par.main(["--check-baseline", "--baseline", str(b)])

    def test_the_committed_baseline_passes_and_reads_no_repository(self):
        self.assertEqual(par.main(["--check-baseline"]), 0)

    def test_a_row_off_the_roster_is_red_and_a_bad_count_cannot_be_read(self):
        self.assertEqual(self.run_check({"ALICE-Not-In-The-Roster": 0}), 1)
        with tempfile.TemporaryDirectory() as t:
            b = Path(t) / "b.toml"
            b.write_text('[repository."A"]\nunanchored = -1\n', encoding="utf-8")
            self.assertEqual(par.main(["--check-baseline", "--baseline", str(b)]), 2)


class Only(unittest.TestCase):
    def repo(self, text: str | None) -> Path:
        d = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        if text is not None:
            (d / "alice-law-conformance.toml").write_text(text, encoding="utf-8")
            subprocess.run(["git", "-C", str(d), "add", "."], check=True)
        subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "t"], check=True)
        return d

    def run_only(self, d: Path, n: int) -> int:
        with tempfile.TemporaryDirectory() as t:
            b = Path(t) / "b.toml"
            b.write_text(par.render({"ALICE-T": n}), encoding="utf-8")
            return par.main(["--only", f"ALICE-T={d}", "--baseline", str(b)])

    def test_at_the_row_above_it_below_it_and_nothing_read(self):
        d = self.repo(manifest(G1=("pass", BARE), G2=("pass", ANCHORED)))
        self.assertEqual(self.run_only(d, 1), 0)
        self.assertEqual(self.run_only(d, 0), 1)
        self.assertEqual(self.run_only(d, 2), 1)
        self.assertEqual(self.run_only(self.repo(None), 0), 2)


if __name__ == "__main__":
    unittest.main(buffer=True)
