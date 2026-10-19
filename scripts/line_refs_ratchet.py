#!/usr/bin/env python3
"""The line-number reference baseline only goes down (conformance/line-refs-baseline.toml).

Each crate's manifest may hold exactly its baseline's line-number references (the roster check compares them
with every main). This check compares a change's baseline file with main's: a baseline raised above main's, or
a crate added with a baseline above 0, is a finding (a new line reference is never allowed back in), and so is
removing the file: with every baseline at 0 it bans every reference (a crate it does not list counts as 0).

  python3 scripts/line_refs_ratchet.py --baseline conformance/line-refs-baseline.toml --base <main's copy>

Prints `line-refs ratchet: compared N, M finding(s)`. Exit 0 when nothing rose, 1 on a finding, 2 when the
input cannot be read or nothing was compared (a base with no entry).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import conformance_roster_check as crc  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline", type=Path, required=True, help="the change's baseline file (may be absent)")
    ap.add_argument("--base", type=Path, required=True, help="main's baseline file (may be absent)")
    a = ap.parse_args(argv)
    try:
        new = crc.load_line_baseline(a.baseline)
        old = crc.load_line_baseline(a.base)
    except crc.InputError as e:
        print(f"line-refs ratchet: {e}", file=sys.stderr)
        return 2
    if old is None:
        # main has no baseline file: this change introduces it (the file is never removed afterwards)
        if new is None:
            print("line-refs ratchet: no baseline file on main or here (compared nothing)", file=sys.stderr)
            return 2
        print(f"line-refs ratchet: the baseline file is introduced ({len(new)} entries)")
        return 0
    if not old:
        print("line-refs ratchet: main's baseline file has no entry (compared nothing)", file=sys.stderr)
        return 2
    findings = []
    if new is None:
        findings.append("the baseline file is removed: keep it (with every baseline at 0 it bans every manifest's "
                        "line-number references, and the roster reads it)")
        compared = len(old)
    else:
        compared = 0
        for repo in sorted(set(old) | set(new)):
            a_, b_ = old.get(repo), new.get(repo)
            if b_ is None:
                continue  # a crate leaving the roster takes its line out
            compared += 1
            if a_ is None and b_ > 0:
                findings.append(f"{repo}: a new baseline entry must start at 0, not {b_}")
            elif a_ is not None and b_ > a_:
                findings.append(f"{repo}: the baseline rose from {a_} to {b_} (it only goes down)")
    for f in findings:
        print(f"line-refs ratchet: {f}", file=sys.stderr)
    print(f"line-refs ratchet: compared {compared}, {len(findings)} finding(s)")
    if compared == 0:
        print("line-refs ratchet: compared nothing", file=sys.stderr)
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
