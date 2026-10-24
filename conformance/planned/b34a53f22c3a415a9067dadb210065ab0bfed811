#!/usr/bin/env python3
"""Tests of scripts/check_pipe_grep_q.py: the forms it must catch and the ones it must leave"""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

spec = importlib.util.spec_from_file_location(
    "check_pipe_grep_q", pathlib.Path(__file__).resolve().parent / "check_pipe_grep_q.py"
)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class Caught(unittest.TestCase):
    def test_pipe_into_grep_q(self):
        for line in [
            "rustup target list --installed | grep -q '^wasm32$' || rustup target add wasm32",
            'printf \'%s\\n\' "$list" | grep -qx "$f" || exit 1',
            "cmd | grep -xq pat",
            "cmd |grep -Eq 'a|b'",
            "cmd | grep --quiet pat",
            "cmd | grep -E --silent pat",
            "has() { rustup toolchain list | grep -q \"^$1\"; }",
        ]:
            with self.subTest(line=line):
                self.assertEqual(len(check.offending(line)), 1)


class Left(unittest.TestCase):
    def test_other_forms(self):
        for line in [
            'grep -q "^$1" <<<"$(rustup toolchain list)"',
            'grep -qx "$f" <<<"$list"',
            "cmd | grep -c pat",
            "cmd | grep pat > out",
            "grep -q pat file",
            "# a comment: cmd | grep -q pat",
            "echo done  # cmd | grep -q pat",
        ]:
            with self.subTest(line=line):
                self.assertEqual(check.offending(line), [])

    def test_line_numbers(self):
        self.assertEqual(check.offending("ok\ncmd | grep -q x\n")[0][0], 2)


if __name__ == "__main__":
    unittest.main()
