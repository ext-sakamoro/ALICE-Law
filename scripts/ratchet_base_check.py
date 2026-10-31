#!/usr/bin/env python3
"""Every comparison with a committed conformance/ file reads it at the base scripts/ci_base.py names.

A ratchet (a baseline that only goes down, pending rows that do not rise, a state-drift note) compares
this tree's conformance/ file with the same file at an earlier commit. That commit must be the one
scripts/ci_base.py names: on a push to main the commit main stood at before the push, elsewhere the
merge-base with origin/main. Reading `FETCH_HEAD` after `git fetch origin main`, or `origin/main`,
compares a push to main with itself (the ratchet then passes whatever the push did) and a branch with
a main it may not be based on.

Each `git show REV:conformance/...` and `git cat-file -e REV:conformance/...` in the workflows and
scripts/preflight.sh must read REV from the shell variable `base`, and a file that has one must assign
`base=$(python3 scripts/ci_base.py ...)`. A finding is printed per line; no such read in any file is
a failure too (exit 1 either way).

usage: ratchet_base_check.py [repository root]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

READ = re.compile(r"git\s+(?:show|cat-file\s+-e)\s+(\"?)([^\s:\"]+)\1:conformance/")
ASSIGN = re.compile(r"""\bbase=\$\(python3?\s+scripts/ci_base\.py\b""")
BASE = {"$base", "${base}"}


def files(root: Path) -> list[Path]:
    return sorted((root / ".github" / "workflows").glob("*.yml")) + [root / "scripts" / "preflight.sh"]


def check(root: Path) -> tuple[list[str], int]:
    """(findings, reads compared)"""
    findings, reads = [], 0
    for path in files(root):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        hits = 0
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for m in READ.finditer(line):
                hits += 1
                if m.group(2) not in BASE:
                    findings.append(f"{rel}:{n}: reads conformance/ at {m.group(2)}, not at the ci_base.py base ($base)")
        if hits and not ASSIGN.search(text):
            findings.append(f"{rel}: compares with conformance/ at $base but never sets base=$(python3 scripts/ci_base.py ...)")
        reads += hits
    if reads == 0:
        findings.append("no `git show`/`git cat-file -e` of a conformance/ file was found (nothing compared)")
    return findings, reads


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    findings, reads = check(root)
    for f in findings:
        print(f"error: {f}")
    if findings:
        return 1
    print(f"ratchet base: {reads} reads of conformance/ files, each at the ci_base.py base")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
