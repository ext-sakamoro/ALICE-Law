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
BASH_BROKEN = 'set -u\necho "demo: tests/a.rs: no such file"\nnot_a_command_here\nexit 1\n'
BASH_REPORTS = 'echo "demo: tests/a.rs: no such file" >&2\nexit 1\n'


class RunCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def py(self, body: str) -> list[str]:
        p = self.dir / "checker.py"
        p.write_text(body, encoding="utf-8")
        return [sys.executable, str(p)]

    def sh(self, body: str) -> list[str]:
        p = self.dir / "checker.sh"
        p.write_text(body, encoding="utf-8")
        return ["bash", str(p)]

    def test_a_reported_problem_passes(self):
        self.assertIsNone(cc.run_check(self.py(REPORTS), expect_exit=1, expect_text="tests/a.rs: no such file"))

    def test_a_clean_run_passes(self):
        self.assertIsNone(cc.run_check(self.py(CLEAN), expect_exit=0))

    def test_several_texts_must_all_appear(self):
        self.assertIsNone(cc.run_check(self.py(REPORTS), expect_exit=1, expect_text=["tests/a.rs", "no such file"]))
        why = cc.run_check(self.py(REPORTS), expect_exit=1, expect_text=["tests/a.rs", "is stale"])
        self.assertIn("did not report 'is stale'", why)

    def test_a_crash_is_a_crash_even_with_the_expected_exit_and_text(self):
        why = cc.run_check(self.py(CRASHES_ON_TEXT), expect_exit=1, expect_text="tests/a.rs: no such file")
        self.assertIsNotNone(why)
        self.assertTrue(why.startswith("crashed"), why)

    def test_a_crash_with_exit_0_is_a_crash(self):
        why = cc.run_check(self.py(CRASH_EXIT_0), expect_exit=0)
        self.assertTrue(why and why.startswith("crashed"), why)

    def test_a_crash_is_named_a_crash_before_its_exit_code(self):
        # the crash exits 1 where 0 was expected: the reason is the crash, not the exit code
        why = cc.run_check(self.py(CRASHES_ON_TEXT), expect_exit=0)
        self.assertTrue(why and why.startswith("crashed"), why)

    def test_a_wrong_exit_code_is_refused(self):
        why = cc.run_check(self.py(EXITS_2), expect_exit=1, expect_text="tests/a.rs: no such file")
        self.assertIn("exit 2, expected 1", why)
        why = cc.run_check(self.py(REPORTS), expect_exit=0)
        self.assertIn("exit 1, expected 0", why)

    def test_a_missing_text_is_refused(self):
        why = cc.run_check(self.py(REPORTS), expect_exit=1, expect_text="is stale")
        self.assertIn("did not report 'is stale'", why)

    def test_a_bash_error_is_a_crash_with_the_bash_markers(self):
        why = cc.run_check(self.sh(BASH_BROKEN), expect_exit=1, expect_text="tests/a.rs: no such file",
                           crash_markers=cc.BASH_CRASH)
        self.assertTrue(why and why.startswith("crashed"), why)
        self.assertIsNone(cc.run_check(self.sh(BASH_REPORTS), expect_exit=1, expect_text="tests/a.rs: no such file",
                                       crash_markers=cc.BASH_CRASH))

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
