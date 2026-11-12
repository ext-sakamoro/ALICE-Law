#!/usr/bin/env python3
"""Tests of the exemption table of scripts/branch_main_live.py (no network)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import branch_main_live as live  # noqa: E402

ROW = '[[exempt]]\nrepo = "ALICE-A"\npath = "compat/Cargo.toml"\nreason = "compat reader of the 0.1 format"\n'


def table(text: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.toml"
        p.write_text(text)
        return live.load_exempt(p)


class Exempt(unittest.TestCase):
    def test_a_finding_in_an_exempted_file_is_not_counted(self):
        left, unused, n = live.apply_exempt(["ALICE-A: compat/Cargo.toml: [dependencies] alice-a: a registry release",
                                             "ALICE-A: Cargo.toml: [dependencies] alice-b: rev = x"], table(ROW))
        self.assertEqual(left, ["ALICE-A: Cargo.toml: [dependencies] alice-b: rev = x"])
        self.assertEqual((unused, n), ([], 1))

    def test_the_same_path_in_another_repository_is_not_exempted(self):
        left, _, n = live.apply_exempt(["ALICE-B: compat/Cargo.toml: x"], table(ROW))
        self.assertEqual((len(left), n), (1, 0))

    def test_an_unused_row_is_a_finding(self):
        left, unused, n = live.apply_exempt([], table(ROW))
        self.assertEqual(left, [])
        self.assertEqual(len(unused), 1)
        self.assertIn("ALICE-A: compat/Cargo.toml", unused[0])

    def test_a_row_without_a_reason_is_refused(self):
        with self.assertRaises(ValueError):
            table(ROW.replace('reason = "compat reader of the 0.1 format"\n', 'reason = " "\n'))
        with self.assertRaises(ValueError):
            table('[[exempt]]\nrepo = "ALICE-A"\nreason = "x"\n')

    def test_the_repository_table_reads(self):
        rows = live.load_exempt()
        self.assertTrue(rows)
        self.assertTrue(all(r.strip() for r in rows.values()))


if __name__ == "__main__":
    unittest.main()
