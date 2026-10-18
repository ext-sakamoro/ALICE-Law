#!/usr/bin/env python3
"""tests for scripts/shared_files_strict.sh: how the exit codes of its four checks combine (a finding in any is a
finding, a check that could not run is never a pass), with the checks replaced by stubs

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
    CHECKS = ("shared_files_check.py", "gen_shared_files.py", "conformance_roster_check.py", "gen_law_status.py")

    def run_with(self, first: tuple[int, str] = (0, ""), second: tuple[int, str] = (0, ""),
                 state: tuple[int, str] = (0, ""), readme: tuple[int, str] = (0, "")) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").mkdir()
            shutil.copy(HERE / "shared_files_strict.sh", root / "scripts" / "shared_files_strict.sh")
            for name, (rc, err) in zip(self.CHECKS, (first, second, state, readme)):
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

    def test_a_stale_law_state_is_a_finding(self):
        # the roster check finds manifest-state.toml behind the crates' mains: no receipt for a stale state
        p = self.run_with(state=(1, ""))
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("finding (conformance_roster_check.py --strict)", p.stderr)
        p = self.run_with(readme=(1, ""))
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("finding (gen_law_status.py --check)", p.stderr)

    def test_the_state_checks_run_and_are_not_a_pass_when_they_compare_nothing(self):
        p = self.run_with(state=(2, "error: nothing was compared (0 manifests, 0 gates)\n"))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("nothing compared (conformance_roster_check.py --strict)", p.stderr)
        p = self.run_with(readme=(2, "error: no README was compared\n"))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("nothing compared (gen_law_status.py --check)", p.stderr)
        p = self.run_with(state=(2, "error: gh api repos/x: HTTP 502\n"))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("infra", p.stderr)

    def test_the_checks_stderr_is_shown(self):
        p = self.run_with((1, "shared_files_check: a finding line\n"), (0, ""))
        self.assertIn("a finding line", p.stderr)


if __name__ == "__main__":
    unittest.main()
