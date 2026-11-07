#!/usr/bin/env python3
"""Check that `cargo test` output ran at least N tests in each named test binary.

usage: cargo test ... 2>&1 | python3 scripts/min_passed.py [--no-ignored] lib=1044 physics_hub_analytic=23 ...

A binary is named by its `Running` line: `unittests src/lib.rs` is `lib`, `tests/foo.rs` is `foo`.
Fails when a named binary did not run, ran fewer tests than its minimum, or a test failed.
With --no-ignored it also fails when a named binary ignored a test (for a run with
--include-ignored, where an ignored test means one the run was meant to execute did not).
"""

import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# cargo's line names the source and then the binary in brackets; a test runner may print its own
# "Running ..." line after it, as the browser runner of wasm-bindgen does
RUNNING = re.compile(r"^\s*Running (?:unittests )?(\S+) \(")
RESULT = re.compile(r"^test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored")


def binary_name(path: str) -> str:
    # cargo prints the path with the platform's separator (tests\foo.rs on Windows)
    path = path.replace("\\", "/")
    if path == "src/lib.rs":
        return "lib"
    return path.rsplit("/", 1)[-1].removesuffix(".rs")


def main() -> int:
    want = {}
    args = sys.argv[1:]
    no_ignored = "--no-ignored" in args
    for arg in (a for a in args if a != "--no-ignored"):
        name, _, n = arg.partition("=")
        want[name] = int(n)
    if not want:
        print("min_passed: no binary named", file=sys.stderr)
        return 2

    got = {}
    ignored = {}
    current = None
    failed = 0
    for raw in sys.stdin:
        sys.stdout.write(raw)
        line = ANSI.sub("", raw)
        m = RUNNING.match(line)
        if m:
            current = binary_name(m.group(1))
            continue
        m = RESULT.match(line)
        if m and current is not None:
            got[current] = int(m.group(1))
            ignored[current] = int(m.group(3))
            failed += int(m.group(2))
            current = None

    bad = []
    for name, n in want.items():
        if name not in got:
            bad.append(f"{name}: did not run")
        elif got[name] < n:
            bad.append(f"{name}: {got[name]} passed, expected at least {n}")
        if no_ignored and ignored.get(name, 0):
            bad.append(f"{name}: {ignored[name]} ignored")
    if failed:
        bad.append(f"{failed} test(s) failed")
    compared = sum(1 for name in want if name in got)
    print(f"min_passed: {compared}/{len(want)} binaries compared", file=sys.stderr)
    for b in bad:
        print(f"min_passed: {b}", file=sys.stderr)
    return 1 if bad or compared == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
