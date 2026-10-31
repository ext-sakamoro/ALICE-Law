#!/usr/bin/env python3
"""Run the tests of the tools that read today's date with the clock moved ahead.

A test that leaves the date to the wall clock (a call without `--today` / `today=`) passes on
the day it was written and fails some days later, when its fixture dates have aged. This
runner moves `datetime.datetime.now()` and `datetime.date.today()` CLOCK_SHIFT_DAYS days
ahead (default 10) before any test or tool is imported, then runs TESTS in-process: every
test must still pass. The tools read the clock as `datetime.datetime.now(...)` at call time,
so the shifted classes reach them whether a test imports a tool by name or by path. A tool
run as a subprocess is not shifted (none of TESTS does that for a tool that reads the date).

usage: scripts/run_tests_shifted_clock.py [MODULE ...]   (default: TESTS)
Exit 1 when a test fails or errors, 2 when no test ran.
"""
from __future__ import annotations

import datetime
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHIFT = datetime.timedelta(days=int(os.environ.get("CLOCK_SHIFT_DAYS", "10")))
# the tests of the tools that read today's date (conformance_roster_check for the gates' adoption windows, and
# land_law_state through it; gen_shared_files and shared_files_check for the pending rows) and of the tools that call
# them (release_train reads the clock only to refuse a date in the future, which a clock
# moved ahead cannot turn red, so its tests are not run here)
TESTS = ("test_land_law_state", "test_conformance_roster_check", "test_gen_shared_files",
         "test_shared_files_check", "test_line_refs_ratchet", "test_line_refs_propose")

REAL_DATETIME = datetime.datetime
REAL_DATE = datetime.date


class ShiftedDateTime(REAL_DATETIME):
    @classmethod
    def now(cls, tz=None):
        return REAL_DATETIME.now(tz) + SHIFT

    @classmethod
    def utcnow(cls):
        return REAL_DATETIME.now(datetime.timezone.utc).replace(tzinfo=None) + SHIFT

    @classmethod
    def today(cls):
        return REAL_DATETIME.now() + SHIFT


class ShiftedDate(REAL_DATE):
    @classmethod
    def today(cls):
        return REAL_DATE.today() + SHIFT


def main(argv: list[str] | None = None) -> int:
    names = list(TESTS if not argv else argv)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    datetime.datetime, datetime.date = ShiftedDateTime, ShiftedDate
    try:
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
        if suite.countTestCases() == 0:
            print("run_tests_shifted_clock: no test ran", file=sys.stderr)
            return 2
        print(f"run_tests_shifted_clock: clock {SHIFT.days} days ahead "
              f"({ShiftedDate.today().isoformat()}), {suite.countTestCases()} tests")
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.wasSuccessful() else 1
    finally:
        datetime.datetime, datetime.date = REAL_DATETIME, REAL_DATE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
