#!/usr/bin/env python3
"""tests for scripts/shared_files_strict.sh: how the exit codes of its two checks combine (a finding in either is a
finding, a check that could not run is never a pass), with the two checks replaced by stubs

    python3 scripts/test_shared_files_strict.py
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Strict(unittest.TestCase):
    def run_with(self, first: tuple[int, str], second: tuple[int, str]) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").mkdir()
            shutil.copy(HERE / "shared_files_strict.sh", root / "scripts" / "shared_files_strict.sh")
            for name, (rc, err) in (("shared_files_check.py", first), ("gen_shared_files.py", second)):
                (root / "scripts" / name).write_text(f"import sys\nsys.stderr.write({err!r})\nsys.exit({rc})\n")
            return subprocess.run(["bash", str(root / "scripts" / "shared_files_strict.sh")], capture_output=True,
                                  text=True, timeout=60)

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

    def test_the_checks_stderr_is_shown(self):
        p = self.run_with((1, "shared_files_check: a finding line\n"), (0, ""))
        self.assertIn("a finding line", p.stderr)


if __name__ == "__main__":
    unittest.main()
