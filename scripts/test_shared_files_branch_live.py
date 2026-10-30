#!/usr/bin/env python3
"""Tests of scripts/shared_files_branch_live.py (python3 scripts/test_shared_files_branch_live.py)

The mains are the fixture repositories of scripts/test_shared_files_check.py; the base is the table committed in a
repository around the table under check, read at HEAD."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import shared_files_branch_live as bl  # noqa: E402
from test_shared_files_check import SHARED, Base, run  # noqa: E402

ENV = ["-c", "user.name=t", "-c", "user.email=t@t"]


class BranchLive(Base):
    def base_at_head(self, base_row: dict) -> None:
        """commit a table with base_row as the base (HEAD) beside the table under check"""
        d = self.t.parent
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        self.t.write_text(self.w.table(base_row), encoding="utf-8")
        subprocess.run(["git", *ENV, "-C", str(d), "add", self.t.name], check=True)
        subprocess.run(["git", *ENV, "-C", str(d), "commit", "-q", "-m", "base"], check=True)

    def branch_live(self) -> tuple[int, str]:
        self.carry_planned()  # the planned rows' copies next to the table, as check() writes them
        return run(bl.main, ["--table", str(self.t), "--base-ref", "HEAD", "--today", "2026-10-17"])

    def converge(self) -> None:
        if (self.w.work / "ALICE-C" / "scripts/s.sh").read_bytes() != SHARED:
            self.w.commit("ALICE-C", {"scripts/s.sh": SHARED})

    def test_a_row_that_converged_on_the_mains_while_the_change_is_checked_is_a_note(self):
        # the race of 2026-10-30: a rollout landed its last copy while a change to this repository was checked
        self.base_at_head(self.w.planned())
        self.t.write_text(self.w.table(self.w.planned()), encoding="utf-8")
        self.converge()
        code, out = self.branch_live()
        self.assertEqual(code, 0, out)
        self.assertIn("scripts/s.sh: converged since the base", out)

    def test_a_change_that_edits_a_converged_row_is_still_a_finding(self):
        self.base_at_head(self.w.planned())
        edited = self.w.planned()
        edited["pending"]["exit"] = "an exit the change rewrote"
        self.t.write_text(self.w.table(edited), encoding="utf-8")
        self.converge()
        code, out = self.branch_live()
        self.assertEqual(code, 1, out)
        self.assertIn("it has converged (regenerate the table)", out)

    def test_a_base_without_the_table_compares_nothing(self):
        d = self.t.parent
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        (d / "other").write_text("x", encoding="utf-8")
        subprocess.run(["git", *ENV, "-C", str(d), "add", "other"], check=True)
        subprocess.run(["git", *ENV, "-C", str(d), "commit", "-q", "-m", "base"], check=True)
        self.t.write_text(self.w.table(self.w.planned()), encoding="utf-8")
        code, out = self.branch_live()
        self.assertEqual(code, 2, out)
        self.assertIn("no base to compare with", out)


if __name__ == "__main__":
    unittest.main()
