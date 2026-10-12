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
        self.assertIn("not (only) as expected-failure.lol-0-4 says", out)
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

    def test_the_expected_error_plus_an_unrelated_one_is_red(self):
        out_text = FAILS + "error: failed to download `serde v1.0.0` (HTTP 503)\n"
        rc, out, gout, _ = self.run_step(ROSTER + ENTRY, 101, out_text)
        self.assertEqual(rc, 1)
        self.assertIn("not explained by `expect`: error: failed to download", out)
        self.assertNotIn("expected=true", gout)

    def test_a_log_line_quoting_the_text_does_not_count(self):
        out_text = ("INFO previous run said: " + FAILS +
                    "thread 'main' panicked at src/main.rs:3:5:\nboom\n")
        rc, out, gout, _ = self.run_step(ROSTER + ENTRY, 101, out_text)
        self.assertEqual(rc, 1)
        self.assertIn("not explained by `expect`: thread 'main' panicked", out)
        self.assertIn("`expect` text in no error line", out)

    def test_a_failure_with_no_error_line_is_red(self):
        rc, out, _, _ = self.run_step(ROSTER + ENTRY, 1, "    " + FAILS)  # indented: not an error line
        self.assertEqual(rc, 1)
        self.assertIn("no error line", out)

    def test_cargo_details_under_the_error_are_not_error_lines(self):
        cargo = (FAILS + "candidate versions found which didn't match: 0.3.0\n"
                 "location searched: crates.io index\nrequired by package `x v0.0.0`\n")
        self.assertEqual(self.run_step(ROSTER + ENTRY, 101, cargo)[0], 0)

    def test_every_run_lists_the_entries_in_the_summary(self):
        for code, output in ((0, "ok\n"), (101, FAILS)):
            _, _, _, summ = self.run_step(ROSTER + ENTRY, code, output, step="other")
            self.assertIn("| resolve | alice-lol 0.4.0 | alice-lol 0.4 is not on crates.io yet |", summ)

    def check(self, roster, workflows):
        with tempfile.TemporaryDirectory() as d:
            p, w = Path(d, "r.toml"), Path(d, "wf")
            p.write_text(roster, encoding="utf-8")
            w.mkdir()
            for name, text in workflows.items():
                (w / name).write_text(text, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = ef.main(["--check", "--roster", str(p), "--workflows", str(w)])
            return rc, out.getvalue()

    def test_an_entry_no_workflow_runs_is_an_orphan(self):
        wrapped = {"a.yml": "run: python3 ../scripts/expected_failure.py resolve -- cargo generate-lockfile\n"}
        self.assertEqual(self.check(ROSTER + ENTRY + "exit = 101\n", wrapped)[0], 0)
        rc, out = self.check(ROSTER + ENTRY, {"a.yml": "run: python3 scripts/test_expected_failure.py resolve\n- name: x\n"})
        self.assertEqual(rc, 1)
        self.assertIn("orphan: expected-failure.lol-0-4", out)
        self.assertEqual(self.check(ROSTER + ENTRY, {})[0], 2)

    def test_a_cargo_step_without_exit_is_red(self):
        wrapped = {"a.yml": "run: python3 ../scripts/expected_failure.py resolve -- cargo generate-lockfile\n"}
        rc, out = self.check(ROSTER + ENTRY, wrapped)
        self.assertEqual(rc, 1)
        self.assertIn("no exit: expected-failure.lol-0-4 wraps a cargo command", out)
        self.assertEqual(self.check(ROSTER + ENTRY + "exit = 101\n", wrapped)[0], 0)
        other = {"a.yml": "run: python3 ../scripts/expected_failure.py resolve -- make build\n"}
        self.assertEqual(self.check(ROSTER + ENTRY, other)[0], 0)

    def test_the_repository_entries_name_cargo_exit_101(self):
        entries = ef.load_entries((HERE.parent / "release-train.toml").read_text(encoding="utf-8"))
        self.assertTrue(entries)
        for step, e in entries.items():
            self.assertEqual(e["exit"], 101, step)

    def test_an_expected_line_then_a_crash_is_red(self):
        rc, out, _, _ = self.run_step(ROSTER + ENTRY + "exit = 101\n", 139, FAILS)
        self.assertEqual(rc, 1)
        self.assertIn("exit status 139, the entry expects 101", out)

    def test_the_repository_has_no_orphan(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ef.main(["--check"]), 0)

    def test_error_lines_in_any_case_and_from_anyhow_count(self):
        for extra in ("Error: config not found\n", "ERROR something broke\n", "ERROR\n", "error[E0425]: x\n",
                      "Error: Caused by: y\n"):
            rc, out, _, _ = self.run_step(ROSTER + ENTRY, 101, FAILS + extra)
            self.assertEqual(rc, 1, extra)
            self.assertIn("not explained by `expect`", out)
        # words that only start like it are not error lines
        self.assertEqual(self.run_step(ROSTER + ENTRY, 101, FAILS + "errors were fixed\nErroneous\n")[0], 0)

    def test_colour_codes_are_removed_before_matching(self):
        coloured = "\x1b[1m\x1b[31merror\x1b[0m\x1b[1m: failed to select a version for the requirement `alice-lol = \"^0.4\"`\x1b[0m\n"
        self.assertEqual(self.run_step(ROSTER + ENTRY, 101, coloured)[0], 0)
        rc, out, _, _ = self.run_step(ROSTER + ENTRY, 101, FAILS + "\x1b[31merror\x1b[0m: something else\n")
        self.assertEqual(rc, 1)
        self.assertIn("error: something else", out)

    def test_the_exit_status_must_match_when_the_entry_names_it(self):
        entry = ENTRY + "exit = 101\n"
        self.assertEqual(self.run_step(ROSTER + entry, 101, FAILS)[0], 0)
        rc, out, _, _ = self.run_step(ROSTER + entry, 1, FAILS)
        self.assertEqual(rc, 1)
        self.assertIn("exit status 1, the entry expects 101", out)
        for bad in ("exit = 0\n", 'exit = "101"\n', "exit = true\n"):
            self.assertEqual(self.run_step(ROSTER + ENTRY + bad, 101, FAILS)[0], 2, bad)

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
