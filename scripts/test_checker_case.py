#!/usr/bin/env python3
"""scripts/checker_case.py の試験

Each case writes a small checker (Python or bash) in a temp directory and runs it through
run_check. oracle (by construction of each checker): a checker that reports the problem and
exits 1 is a pass; a checker that crashes is reported as a crash, also when the crash exits
with the expected code or its traceback prints the expected text; a wrong exit code, a missing
text, a weak text or a failing case without text are refused; no case at all is a failure.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checker_case as cc  # noqa: E402

REPORTS = 'import sys\nprint("demo: tests/a.rs: no such file", file=sys.stderr)\nsys.exit(1)\n'
CLEAN = 'print("demo: 3 files compared")\n'
# a crash whose traceback prints the source line, and so the expected text
CRASHES_ON_TEXT = 'raise KeyError("demo: tests/a.rs: no such file")\n'
CRASH_EXIT_0 = 'import sys\ntry:\n    {}["k"]\nexcept KeyError:\n    import traceback; traceback.print_exc()\nsys.exit(0)\n'
EXITS_2 = 'import sys\nprint("demo: tests/a.rs: no such file")\nsys.exit(2)\n'
BASH_BROKEN = 'echo "demo: tests/a.rs: no such file"\nnot_a_command_here\nexit 1\n'
BASH_SYNTAX = 'echo "demo: tests/a.rs: no such file"\nif then\nfi\nexit 1\n'
BASH_UNBOUND = 'set -u\necho "demo: tests/a.rs: no such file"\necho "$NOT_SET_ANYWHERE"\nexit 1\n'
BASH_REPORTS = 'echo "demo: tests/a.rs: no such file" >&2\nexit 1\n'
# a checker that legitimately reports a syntax error (or a missing command) in its input
BASH_REPORTS_SYNTAX = 'echo "demo: config.toml: line 3: syntax error in the value" >&2\nexit 1\n'
BASH_REPORTS_MISSING = 'echo "demo: line 2: tool rustfmt: command not found on PATH" >&2\nexit 1\n'


class RunCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def assertRefused(self, why, text):
        # a refusal is a reason (not None) that names what went wrong
        self.assertIsNotNone(why, f"expected a refusal naming {text!r}")
        self.assertIn(text, why)

    def assertCrashed(self, why):
        self.assertIsNotNone(why, "expected the run to be reported as a crash")
        self.assertTrue(why.startswith("crashed"), why)

    def py(self, body: str) -> list[str]:
        p = self.dir / "checker.py"
        p.write_text(body, encoding="utf-8")
        return [sys.executable, str(p)]

    def sh(self, body: str) -> list[str]:
        p = self.dir / "checker.sh"
        p.write_text(body, encoding="utf-8")
        return ["bash", str(p)]

    def test_a_program_not_on_path_is_refused_before_it_runs(self):
        with self.assertRaises(ValueError) as e:
            cc.run_check(["no-such-checker-9f3c", "x"], expect_exit=0, env={"PATH": str(self.dir)})
        self.assertIn("'no-such-checker-9f3c' is not on PATH", str(e.exception))

    def test_a_bare_name_is_found_on_the_given_path(self):
        exe = Path(sys.executable)
        self.assertEqual(cc.program(exe.stem, {"PATH": str(exe.parent)}).lower(),
                         str(Path(shutil.which(exe.stem, path=str(exe.parent)))).lower())
        self.assertEqual(cc.program(str(exe)), str(exe))

    def test_a_reported_problem_passes(self):
        self.assertIsNone(cc.run_check(self.py(REPORTS), expect_exit=1, expect_text="tests/a.rs: no such file"))

    def test_a_clean_run_passes(self):
        self.assertIsNone(cc.run_check(self.py(CLEAN), expect_exit=0))

    def test_several_texts_must_all_appear(self):
        self.assertIsNone(cc.run_check(self.py(REPORTS), expect_exit=1, expect_text=["tests/a.rs", "no such file"]))
        why = cc.run_check(self.py(REPORTS), expect_exit=1, expect_text=["tests/a.rs", "is stale"])
        self.assertRefused(why, "did not report 'is stale'")

    def test_a_crash_is_a_crash_even_with_the_expected_exit_and_text(self):
        why = cc.run_check(self.py(CRASHES_ON_TEXT), expect_exit=1, expect_text="tests/a.rs: no such file")
        self.assertCrashed(why)

    def test_a_crash_with_exit_0_is_a_crash(self):
        why = cc.run_check(self.py(CRASH_EXIT_0), expect_exit=0)
        self.assertCrashed(why)

    def test_a_crash_is_named_a_crash_before_its_exit_code(self):
        # the crash exits 1 where 0 was expected: the reason is the crash, not the exit code
        why = cc.run_check(self.py(CRASHES_ON_TEXT), expect_exit=0)
        self.assertCrashed(why)

    def test_a_wrong_exit_code_is_refused(self):
        why = cc.run_check(self.py(EXITS_2), expect_exit=1, expect_text="tests/a.rs: no such file")
        self.assertRefused(why, "exit 2, expected 1")
        why = cc.run_check(self.py(REPORTS), expect_exit=0)
        self.assertRefused(why, "exit 1, expected 0")

    def test_a_missing_text_is_refused(self):
        why = cc.run_check(self.py(REPORTS), expect_exit=1, expect_text="is stale")
        self.assertRefused(why, "did not report 'is stale'")

    def test_bash_errors_of_the_script_are_crashes(self):
        for body in (BASH_BROKEN, BASH_SYNTAX, BASH_UNBOUND):
            with self.subTest(body=body):
                why = cc.run_check(self.sh(body), expect_exit=1, expect_text="tests/a.rs: no such file", shell="bash")
                self.assertCrashed(why)

    def test_a_bash_error_is_not_a_crash_without_shell_bash(self):
        # a Python checker's output is not read for bash's forms
        why = cc.run_check(self.sh(BASH_BROKEN), expect_exit=1, expect_text="tests/a.rs: no such file")
        self.assertIsNone(why)

    def test_a_bash_checker_that_reports_passes(self):
        self.assertIsNone(cc.run_check(self.sh(BASH_REPORTS), expect_exit=1,
                                       expect_text="tests/a.rs: no such file", shell="bash"))

    def test_a_reported_syntax_error_or_missing_command_is_not_a_crash(self):
        # the checker reports them about its input; bash did not raise them about the script
        self.assertIsNone(cc.run_check(self.sh(BASH_REPORTS_SYNTAX), expect_exit=1,
                                       expect_text="config.toml: line 3: syntax error in the value", shell="bash"))
        self.assertIsNone(cc.run_check(self.sh(BASH_REPORTS_MISSING), expect_exit=1,
                                       expect_text="tool rustfmt: command not found on PATH", shell="bash"))

    def test_an_error_in_a_sourced_file_is_a_crash(self):
        lib = self.dir / "lib.sh"
        lib.write_text("helper() {\n  not_a_command_in_lib\n}\n", encoding="utf-8")
        body = f'. "{lib}"\necho "demo: tests/a.rs: no such file"\nhelper\nexit 1\n'
        why = cc.run_check(self.sh(body), expect_exit=1, expect_text="tests/a.rs: no such file", shell="bash")
        self.assertCrashed(why)

    def test_an_unknown_shell_is_refused_before_the_command_runs(self):
        ran = self.dir / "ran"
        with self.assertRaises(ValueError):
            cc.run_check(self.sh(f'touch "{ran}"\n' + BASH_REPORTS), expect_exit=1,
                         expect_text="tests/a.rs: no such file", shell="zsh")
        self.assertFalse(ran.exists(), "the command ran before the shell was refused")

    def test_a_failing_case_needs_a_text(self):
        with self.assertRaises(ValueError):
            cc.run_check(self.py(REPORTS), expect_exit=1)
        with self.assertRaises(ValueError):
            cc.run_check(self.py(REPORTS), expect_exit=1, expect_text=[])

    def test_a_weak_text_is_refused(self):
        # each occurs, ignoring case, in text a crash prints
        for weak in ["error:", "Error", "line 1", "not found", "command", "Traceback", "", "  ", "\t"]:
            with self.subTest(weak=weak), self.assertRaises(ValueError):
                cc.run_check(self.py(REPORTS), expect_exit=1, expect_text=weak)

    def test_texts_no_crash_prints_are_accepted(self):
        for strong in ["finding", "no such file", "exit 1", "compared nothing"]:
            with self.subTest(strong=strong):
                self.assertIsNone(cc.weak_text(strong))

    def test_the_crash_corpus_is_not_empty(self):
        # a corpus of nothing would refuse no text at all
        self.assertGreater(len(cc.crash_corpus()), 70)
        self.assertIn("keyerror: ", cc.crash_corpus())


class RunCases(unittest.TestCase):
    def run_cases(self, cases):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cc.run_cases("demo", cases)
        return code, out.getvalue()

    def test_no_case_fails(self):
        code, out = self.run_cases([])
        self.assertEqual(code, 1)
        self.assertIn("demo: no case", out)

    def test_all_pass(self):
        code, out = self.run_cases([("a", lambda: None), ("b", lambda: None)])
        self.assertEqual(code, 0)
        self.assertIn("demo: 2/2 cases", out)

    def test_a_failing_case_is_named_with_its_reason(self):
        code, out = self.run_cases([("a", lambda: None), ("b", lambda: "crashed: KeyError")])
        self.assertEqual(code, 1)
        self.assertIn("demo: b: crashed: KeyError", out)
        self.assertIn("demo: 1/2 cases", out)

    def test_a_case_that_raises_fails(self):
        def boom():
            raise RuntimeError("boom")
        code, out = self.run_cases([("a", boom)])
        self.assertEqual(code, 1)
        self.assertIn("demo: a: raised RuntimeError: boom", out)


if __name__ == "__main__":
    unittest.main()
