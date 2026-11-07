#!/usr/bin/env python3
"""Tests of scripts/min_passed.py on hand-written `cargo test` output."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("min_passed.py")


def out(*binaries, sep="/"):
    lines = []
    for name, passed, failed, ignored in binaries:
        lines.append(f"     Running tests{sep}{name}.rs (target{sep}debug{sep}deps{sep}{name}-0123.exe)")
        lines.append(
            f"test result: ok. {passed} passed; {failed} failed; {ignored} ignored; "
            "0 measured; 0 filtered out; finished in 0.01s"
        )
    return "\n".join(lines) + "\n"


def run(stdin, *args):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True
    )
    return r.returncode


CASES = [
    # (description, cargo output, args, expected exit code)
    ("enough passed", out(("a", 5, 0, 0)), ["a=5"], 0),
    ("fewer passed", out(("a", 4, 0, 0)), ["a=5"], 1),
    ("a binary did not run", out(("a", 5, 0, 0)), ["a=5", "b=1"], 1),
    ("a failure", out(("a", 5, 1, 0)), ["a=5"], 1),
    ("ignored is allowed without the flag", out(("a", 4, 0, 1)), ["a=4"], 0),
    ("ignored fails with --no-ignored", out(("a", 4, 0, 1)), ["--no-ignored", "a=4"], 1),
    ("--no-ignored with none ignored", out(("a", 5, 0, 0)), ["--no-ignored", "a=5"], 0),
    ("nothing compared", "no test output\n", ["a=1"], 1),
    ("no binary named", out(("a", 5, 0, 0)), ["--no-ignored"], 2),
    # Windows prints the paths with backslashes
    ("a Windows path", out(("a", 5, 0, 0), sep="\\"), ["a=5"], 0),
    # the browser runner prints a Running line of its own between cargo's and the result
    ("a browser runner", "     Running tests/w.rs (w.wasm)\nRunning headless tests in Chrome on `http://127.0.0.1:1/`\ntest result: ok. 6 passed; 0 failed; 0 ignored; 0 filtered out\n", ["w=6"], 0),
    ("a browser runner below the floor", "     Running tests/w.rs (w.wasm)\nRunning headless tests in Chrome on `http://127.0.0.1:1/`\ntest result: ok. 5 passed; 0 failed; 0 ignored; 0 filtered out\n", ["w=6"], 1),
    ("a Windows library", "     Running unittests src\\lib.rs (x.exe)\ntest result: ok. 3 passed; 0 failed; 0 ignored; 0 measured\n", ["lib=3"], 0),
]


def main() -> int:
    bad = 0
    for what, stdin, args, want in CASES:
        got = run(stdin, *args)
        if got != want:
            print(f"test_min_passed: {what}: exit {got}, expected {want}", file=sys.stderr)
            bad += 1
    print(f"test_min_passed: {len(CASES) - bad}/{len(CASES)} cases", file=sys.stderr)
    return 1 if bad or not CASES else 0


if __name__ == "__main__":
    sys.exit(main())
