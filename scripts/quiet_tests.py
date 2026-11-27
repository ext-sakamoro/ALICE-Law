#!/usr/bin/env python3
"""Run test files so that a passing run prints only the unittest summary

A test that feeds a checker a fixture prints the checker's findings about that fixture (made-up
repository names, counts against a made-up baseline). In a CI log those lines sit next to the
real findings of the next step and read as them. This runner runs each test file and, when it
passes, requires its output (stdout and stderr together) to hold nothing but the unittest summary:
the progress dots, the dashed rule, "Ran N tests in ...", "OK" (with its skipped / expected-failure
counts) and blank lines. Any other line is reported and the run fails: capture the output in the
test (unittest.main(buffer=True) for prints, capture_output for subprocesses). A failing test file
keeps its exit code and its whole output is shown (the lines are then evidence, not noise).

    python3 scripts/quiet_tests.py scripts/test_a.py [scripts/test_b.py ...]

Exit 0 when every file passed quietly, 1 when a file failed or printed more than the summary, 2
when no file was given or one does not exist (nothing compared).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SUMMARY = re.compile(r"^(?:[.sxEFu]*|-{20,}|Ran \d+ tests? in [0-9.]+s|OK(?: \([a-z ]+=\d+(?:, [a-z ]+=\d+)*\))?)$")


def extra_lines(output: str) -> list[str]:
    """the lines of a passing run's output that are not the unittest summary"""
    return [ln for ln in output.splitlines() if not SUMMARY.match(ln.strip())]


def run(path: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, path], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout + p.stderr


def main(argv: list[str]) -> int:
    if not argv:
        print("quiet_tests: no test file given (nothing compared)", file=sys.stderr)
        return 2
    missing = [a for a in argv if not Path(a).is_file()]
    if missing:
        print(f"quiet_tests: no such test file: {', '.join(missing)}", file=sys.stderr)
        return 2
    bad = 0
    for path in argv:
        code, out = run(path)
        if code != 0:
            sys.stdout.write(out)
            print(f"quiet_tests: {path}: failed (exit {code})")
            bad += 1
            continue
        extra = extra_lines(out)
        if extra:
            print(f"quiet_tests: {path}: passed but printed {len(extra)} line(s) besides the unittest summary "
                  f"(a fixture's findings would read as real ones in the log; capture them in the test):")
            for ln in extra[:10]:
                print(f"  | {ln}")
            bad += 1
            continue
        print(f"quiet_tests: {path}: passed quietly ({out.count(chr(10))} summary line(s))")
    print(f"quiet_tests: {len(argv)} file(s) run, {bad} with a problem")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
