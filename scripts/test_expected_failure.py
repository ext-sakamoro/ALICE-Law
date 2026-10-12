#!/usr/bin/env python3
"""Tests of scripts/expected_failure.py (python3 scripts/test_expected_failure.py)"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ef", HERE / "expected_failure.py")
ef = importlib.util.module_from_spec(spec)
sys.modules["ef"] = ef
spec.loader.exec_module(ef)

ROSTER = """
[crate.alice-lol]
version = "0.4.0"
state = "planned"
order = 0
"""
ENTRY = """
[expected-failure.lol-0-4]
step = "resolve"
until = "alice-lol 0.4.0"
expect = ["failed to select a version", "alice-lol = \\"^0.4\\""]
why = "alice-lol 0.4 is not on crates.io yet"
"""
FAILS = 'error: failed to select a version for the requirement `alice-lol = "^0.4"`\n'


class ExpectedFailure(unittest.TestCase):
    def run_step(self, roster, code, output, step="resolve"):
        with tempfile.TemporaryDirectory() as d:
            p, gout, summ = Path(d, "r.toml"), Path(d, "out"), Path(d, "summary")
            p.write_text(roster, encoding="utf-8")
            old = {k: os.environ.get(k) for k in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")}
            os.environ["GITHUB_OUTPUT"], os.environ["GITHUB_STEP_SUMMARY"] = str(gout), str(summ)
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    rc = ef.main([step, "--roster", str(p), "--", "cmd"], runner=lambda cmd: (code, output))
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            read = lambda f: f.read_text(encoding="utf-8") if f.exists() else ""
            return rc, out.getvalue(), read(gout), read(summ)

    def test_a_failure_as_listed_is_expected_and_said(self):
        rc, out, gout, summ = self.run_step(ROSTER + ENTRY, 101, FAILS)
        self.assertEqual(rc, 0, out)
        self.assertIn("expected=true", gout)
        self.assertIn("Expected failure until alice-lol 0.4.0", summ)
        self.assertIn("did not run", summ)

    def test_a_failure_without_an_entry_keeps_its_status(self):
        rc, _, gout, _ = self.run_step(ROSTER, 101, FAILS)
        self.assertEqual(rc, 101)
        self.assertNotIn("expected=true", gout)

    def test_a_failure_for_another_reason_is_red(self):
        rc, out, gout, _ = self.run_step(ROSTER + ENTRY, 101, "error: could not compile\n")
        self.assertEqual(rc, 1)
        self.assertIn("not as expected-failure.lol-0-4 says", out)
        self.assertNotIn("expected=true", gout)

    def test_every_expected_text_must_be_there(self):
        rc, _, _, _ = self.run_step(ROSTER + ENTRY, 101, "error: failed to select a version for something else\n")
        self.assertEqual(rc, 1)

    def test_a_pass_with_an_entry_is_red_until_the_entry_goes(self):
        rc, out, gout, _ = self.run_step(ROSTER + ENTRY, 0, "ok\n")
        self.assertEqual(rc, 1)
        self.assertIn("out of date", out)
        self.assertIn("expected=false", gout)

    def test_a_pass_without_an_entry_goes(self):
        rc, _, gout, _ = self.run_step(ROSTER, 0, "ok\n")
        self.assertEqual((rc, "expected=false" in gout), (0, True))

    def test_after_the_release_the_entry_is_stale(self):
        rc, out, gout, _ = self.run_step(ROSTER.replace('"planned"', '"published"') + ENTRY, 101, FAILS)
        self.assertEqual(rc, 1)
        self.assertIn("is stale", out)
        self.assertNotIn("expected=true", gout)

    def test_another_step_is_not_covered(self):
        self.assertEqual(self.run_step(ROSTER + ENTRY, 101, FAILS, step="test")[0], 101)

    def test_malformed_entries_exit_2(self):
        for bad in (ENTRY.replace('until = "alice-lol 0.4.0"', 'until = "alice-lol 9.9.9"'),
                    ENTRY.replace('until = "alice-lol 0.4.0"', 'until = "alice-lol"'),
                    ENTRY.replace('expect = ["failed to select a version", "alice-lol = \\"^0.4\\""]', "expect = []"),
                    ENTRY.replace('why = "alice-lol 0.4 is not on crates.io yet"', ""),
                    ENTRY.replace('step = "resolve"', 'stepp = "resolve"'),
                    ENTRY + ENTRY.replace("lol-0-4", "lol-0-4b")):
            self.assertEqual(self.run_step(ROSTER + bad, 101, FAILS)[0], 2, bad)

    def test_the_command_runs_and_its_output_is_read(self):
        code, output = ef.run([sys.executable, "-c", "import sys; print('to out'); print('to err', file=sys.stderr); sys.exit(3)"])
        self.assertEqual(code, 3)
        self.assertIn("to out", output)
        self.assertIn("to err", output)

    def test_the_repository_entries_are_well_formed(self):
        ef.load_entries((HERE.parent / "release-train.toml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
