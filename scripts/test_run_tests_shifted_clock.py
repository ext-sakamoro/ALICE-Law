#!/usr/bin/env python3
"""Tests of scripts/run_tests_shifted_clock.py: a test that leaves the date to the wall clock is
red under the moved clock, the same test with the date given is green, and the moved clock
reaches a tool loaded by path."""
from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_tests_shifted_clock as rts  # noqa: E402

# a tool that ages an entry against today (the default when no date is given), and a test of it
TOOL = textwrap.dedent('''\
    import datetime

    def stale(since, today=None):
        today = today or datetime.datetime.now(datetime.timezone.utc).date()
        return (today - since).days > 3
    ''')
TESTS = textwrap.dedent('''\
    import datetime, unittest
    import clockfix_tool as tool

    class Wall(unittest.TestCase):
        def test_fresh(self):
            # the fixture is dated today: fresh on the day it is written, stale days later
            self.assertFalse(tool.stale(datetime.date.fromisoformat({since!r}){today}))
    ''')


class ShiftedClock(unittest.TestCase):
    def run_with(self, today_arg: str) -> int:
        with tempfile.TemporaryDirectory() as d:
            since = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
            (Path(d) / "clockfix_tool.py").write_text(TOOL, encoding="utf-8")
            (Path(d) / "clockfix_test.py").write_text(TESTS.format(since=since, today=today_arg), encoding="utf-8")
            sys.path.insert(0, d)
            try:
                for m in ("clockfix_tool", "clockfix_test"):
                    sys.modules.pop(m, None)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    return rts.main(["clockfix_test"])
            finally:
                sys.path.remove(d)
                for m in ("clockfix_tool", "clockfix_test"):
                    sys.modules.pop(m, None)

    def test_a_test_on_the_wall_clock_is_red(self):
        self.assertEqual(self.run_with(""), 1)
        # and the runner puts the real clock back when it returns
        self.assertIs(datetime.datetime, rts.REAL_DATETIME)
        self.assertIs(datetime.date, rts.REAL_DATE)

    def test_the_same_test_with_the_date_given_is_green(self):
        given = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        self.assertEqual(self.run_with(f", today=datetime.date.fromisoformat({given!r})"), 0)

    def test_the_clock_is_restored_and_reaches_a_tool_loaded_by_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "by_path.py"
            p.write_text(TOOL, encoding="utf-8")
            spec = importlib.util.spec_from_file_location("some_alias", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            datetime.datetime, datetime.date = rts.ShiftedDateTime, rts.ShiftedDate
            try:
                moved = datetime.datetime.now(datetime.timezone.utc).date()
            finally:
                datetime.datetime, datetime.date = rts.REAL_DATETIME, rts.REAL_DATE
            real = datetime.datetime.now(datetime.timezone.utc).date()
            self.assertEqual((moved - real).days, rts.SHIFT.days)
            self.assertIs(datetime.datetime, rts.REAL_DATETIME)
            self.assertFalse(mod.stale(real))

    def test_no_test_is_exit_2(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            # a module with no test in it
            self.assertEqual(rts.main(["run_tests_shifted_clock"]), 2)


if __name__ == "__main__":
    unittest.main()
