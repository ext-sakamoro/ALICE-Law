#!/usr/bin/env python3
"""Tests for scripts/quiet_tests.py: a passing test file may print only the unittest summary

- a quiet passing file passes; a passing file that prints a fixture's finding (from the test itself or from a
  subprocess it does not capture) fails and names the line; a failing file keeps its exit code and its output
- the summary forms unittest writes (dots, skips, expected failures, the counts after OK) are not extra lines
- no file / a missing file: exit 2 (nothing compared)
- every workflow step that runs a routed suite goes through it (none runs it directly)

    python3 scripts/test_quiet_tests.py
"""
from __future__ import annotations

import contextlib
import io
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("qt", HERE / "quiet_tests.py")
qt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qt)

QUIET = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n" \
        "if __name__ == '__main__':\n    unittest.main(buffer=True)\n"
LOUD = QUIET.replace("        pass\n", "        print('checker: ALICE-Fixture: 3 > baseline 2 (a new site)')\n") \
    .replace("buffer=True", "")
SUBPROCESS = QUIET.replace("        pass\n", "        import subprocess, sys\n"
                           "        subprocess.run([sys.executable, '-c', 'print(\"found: ALICE-Fixture\")'])\n")
FAILING = QUIET.replace("        pass\n", "        print('evidence'); self.fail('no')\n").replace("buffer=True", "")


def run(*bodies: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as t:
        paths = []
        for i, body in enumerate(bodies):
            p = Path(t) / f"test_{i}.py"
            p.write_text(body, encoding="utf-8")
            paths.append(str(p))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = qt.main(paths)
        return code, out.getvalue()


class Runner(unittest.TestCase):
    def test_a_quiet_passing_file_passes(self):
        code, out = run(QUIET)
        self.assertEqual(code, 0, out)
        self.assertIn("passed quietly", out)

    def test_a_passing_file_that_prints_a_finding_fails_and_names_the_line(self):
        code, out = run(LOUD)
        self.assertEqual(code, 1, out)
        self.assertIn("| checker: ALICE-Fixture: 3 > baseline 2 (a new site)", out)

    def test_an_uncaptured_subprocess_line_is_extra_too(self):
        code, out = run(SUBPROCESS)
        self.assertEqual(code, 1, out)
        self.assertIn("| found: ALICE-Fixture", out)

    def test_a_failing_file_keeps_its_failure_and_shows_its_output(self):
        code, out = run(QUIET, FAILING)
        self.assertEqual(code, 1, out)
        self.assertIn("failed (exit 1)", out)
        self.assertIn("evidence", out)
        self.assertIn("2 file(s) run, 1 with a problem", out)

    def test_the_summary_forms_are_not_extra(self):
        out = ("..s.x\n" + "-" * 70 + "\nRan 5 tests in 0.012s\n\nOK (skipped=1, expected failures=1)\n"
               "Ran 1 test in 1.5s\nOK\n")
        self.assertEqual(qt.extra_lines(out), [])
        self.assertEqual(qt.extra_lines("..found: x\nOK\n"), ["..found: x"])

    def test_no_file_or_a_missing_file_compares_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(qt.main([]), 2)
            self.assertEqual(qt.main([str(HERE / "test_no_such_file.py")]), 2)


class TheWorkflowsRouteTheSuites(unittest.TestCase):
    """each workflow step runs the suite through the runner (the steps themselves then check quietness); a suite
    routed through it in one workflow is never also run directly in another (a step that bypasses the runner)"""

    def test_no_routed_suite_is_run_directly(self):
        wf = HERE.parent / ".github" / "workflows"
        texts = {p.name: p.read_text(encoding="utf-8") for p in sorted(wf.glob("*.yml"))}
        routed = {n for t in texts.values()
                  for n in re.findall(r"python3 scripts/quiet_tests\.py (scripts/test_[a-z0-9_]+\.py)", t)}
        self.assertGreater(len(routed), 0, "no workflow step runs scripts/quiet_tests.py")
        direct = sorted(f"{name}: {n}" for name, t in texts.items() for n in routed
                        if re.search(rf"python3 {re.escape(n)}\b", t))
        self.assertEqual(direct, [])


if __name__ == "__main__":
    unittest.main(buffer=True)
