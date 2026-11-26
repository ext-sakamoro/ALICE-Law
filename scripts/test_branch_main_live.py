#!/usr/bin/env python3
"""Tests of the exemption table of scripts/branch_main_live.py (no network)."""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import branch_main_live as live  # noqa: E402

ROW = '[[exempt]]\nrepo = "ALICE-A"\npath = "compat/Cargo.toml"\nreason = "compat reader of the 0.1 format"\n'


def table(text: str, now=None) -> dict:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.toml"
        p.write_text(text)
        return live.load_exempt(p, now)


REV = "4" * 40
OTHER = "5" * 40
NOW = datetime(2026, 11, 27, 2, 0, tzinfo=timezone(timedelta(hours=9)))
WINDOW = ('[[exempt]]\nrepo = "ALICE-C"\npath = "Cargo.toml"\ndependency = "alice-x"\nrev = "' + REV + '"\n'
          'expires = "2026-11-29T01:30:00+09:00"\nreason = "x 4.0 wave"\n')
WINDOW_LOCK = WINDOW.replace('path = "Cargo.toml"', 'path = "Cargo.lock"')
MANIFEST_FINDING = f"ALICE-C: Cargo.toml: [dependencies] alice-x: rev = {REV}, not branch = \"main\""
LOCK_FINDING = f"ALICE-C: Cargo.lock: alice-x 4.0.0: git+https://github.com/o/ALICE-X?rev={REV}#{REV}"


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

    def test_two_rows_for_one_file_are_refused(self):
        with self.assertRaises(ValueError):
            table(ROW + ROW.replace("compat reader of the 0.1 format", "another reason"))

    def test_a_window_row_exempts_its_dependency_at_its_commit_only(self):
        rows = table(WINDOW + WINDOW_LOCK, NOW)
        other_rev = MANIFEST_FINDING.replace(REV, OTHER)
        other_dep = f"ALICE-C: Cargo.toml: [dependencies] alice-y: rev = {REV}, not branch = \"main\""
        prefix_dep = f"ALICE-C: Cargo.toml: [dependencies] alice-xy: rev = {REV}, not branch = \"main\""
        left, unused, n = live.apply_exempt([MANIFEST_FINDING, LOCK_FINDING, other_rev, other_dep, prefix_dep], rows)
        self.assertEqual(left, [other_rev, other_dep, prefix_dep])
        self.assertEqual((unused, n), ([], 2))

    def test_an_unused_window_row_is_a_finding(self):
        left, unused, n = live.apply_exempt([MANIFEST_FINDING], table(WINDOW + WINDOW_LOCK, NOW))
        self.assertEqual((left, n), ([], 1))
        self.assertEqual(len(unused), 1)
        self.assertIn("window row for alice-x " + REV, unused[0])

    def test_an_expired_window_row_fails_the_table(self):
        table(WINDOW, NOW)
        with self.assertRaisesRegex(ValueError, "expired"):
            table(WINDOW, datetime(2026, 11, 29, 1, 31, tzinfo=timezone(timedelta(hours=9))))

    def test_a_window_row_needs_all_three_fields_a_full_commit_and_an_offset(self):
        for bad in (WINDOW.replace('expires = "2026-11-29T01:30:00+09:00"\n', ""),
                    WINDOW.replace(REV, "4" * 12),
                    WINDOW.replace("+09:00", ""),
                    WINDOW.replace('dependency = "alice-x"', 'dependency = "serde"')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                table(bad, NOW)

    def test_a_file_row_and_a_window_row_for_one_file_are_refused(self):
        file_row = '[[exempt]]\nrepo = "ALICE-C"\npath = "Cargo.toml"\nreason = "y"\n'
        with self.assertRaises(ValueError):
            table(file_row + WINDOW, NOW)

    def test_check_table_reads_only_the_table(self):
        self.assertEqual(live.main(["--check-table"]), 0)
        orig = live.EXEMPT
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.toml"
            p.write_text(ROW + ROW)
            live.EXEMPT = p
            try:
                self.assertEqual(live.main(["--check-table"]), 2)
            finally:
                live.EXEMPT = orig

    def test_the_repository_table_reads(self):
        rows = live.load_exempt()
        self.assertTrue(rows)
        self.assertTrue(all(r.strip() for r in rows.values()))


if __name__ == "__main__":
    unittest.main()
