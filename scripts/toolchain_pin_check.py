#!/usr/bin/env python3
"""Workflow steps that pin dtolnay/rust-toolchain to a commit without naming the toolchain.

    toolchain_pin_check.py --repo DIR      the workflows of a checkout (.github/workflows/*.yml, *.yaml)

dtolnay/rust-toolchain keeps one branch per toolchain (stable, nightly, 1.85, ...) and rewrites those branches, so a
commit taken from one of them stops being reachable from any ref of the action's repository (seen 2026-11: two such
commits were used by 124 repositories). A commit of the master branch stays reachable, and with it the toolchain is
the step's `with: toolchain:` input. So a step that pins the action to a 40-hex commit must also carry
`with: toolchain:`; a step without it is a finding. A step that names a ref (`@stable`, `@v1`) is not a pin and is
not judged here.

The workflow text is read line by line (no YAML library): a step starts at a `- ` list item and runs to the next item
at the same or a lower indent; comments are not read. `toolchain:` counts only as a direct child of the step's `with:`
and with a non-empty value. Limits, all on the safe side or out of scope: a flow map (`with: { toolchain: stable }`)
is not parsed and reads as missing (a false finding, none in use); the commit is matched in lower-case hex as GitHub
writes it; composite actions (.github/actions/*/action.yml) are not read; whether the pinned commit is still reachable
is not judged here (a reachability check over the upstream refs does that); this check only asks for the form
that stays reachable.

Prints one line per finding ("found: <workflow>:<line>: ...") and a summary. Exit 0 when none, 1 on any, 2 when no
workflow was read (an empty read compares nothing).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

USES = re.compile(r"^(\s*)(?:-\s+)?uses:\s*['\"]?dtolnay/rust-toolchain@([0-9a-f]{40})\b")
ITEM = re.compile(r"^(\s*)-\s")
TOOLCHAIN = re.compile(r"^\s*toolchain:(.*)$")
WITH = re.compile(r"^\s*with:\s*$")


def strip(line: str) -> str:
    """the line without its comment (a `#` inside a quoted value is not used by these workflows)"""
    return line.split("#", 1)[0].rstrip()


def step_bounds(lines: list[str], at: int) -> tuple[int, int, int]:
    """(first line, end line, item indent) of the step that holds line `at`"""
    start = at
    while start >= 0 and not ITEM.match(lines[start]):
        start -= 1
    if start < 0:
        return at, at + 1, len(lines[at]) - len(lines[at].lstrip())
    indent = len(ITEM.match(lines[start]).group(1))
    end = start + 1
    while end < len(lines):
        s = lines[end]
        if s.strip():
            m = ITEM.match(s)
            lead = len(s) - len(s.lstrip())
            if (m and len(m.group(1)) <= indent) or lead < indent or (lead == indent and not m):
                break
        end += 1
    return start, end, indent


def pins(text: str) -> list[tuple[int, str, bool]]:
    """(1-based line, commit, carries `with: toolchain:`) of each step that pins the action to a commit"""
    lines = [strip(x) for x in text.splitlines()]
    out = []
    for i, line in enumerate(lines):
        m = USES.match(line)
        if not m:
            continue
        start, end, _ = step_bounds(lines, i)
        out.append((i + 1, m.group(2), with_toolchain(lines[start:end])))
    return out


def with_toolchain(body: list[str]) -> bool:
    """whether the step's `with:` has a direct child `toolchain:` with a non-empty value (a `toolchain:` under `env:`
    or elsewhere in the step is not an input of the action)"""
    for k, line in enumerate(body):
        if not WITH.match(line):
            continue
        w = len(line) - len(line.lstrip())
        child = None
        for x in body[k + 1:]:
            if not x.strip():
                continue
            lead = len(x) - len(x.lstrip())
            if lead <= w:
                break
            if child is None:
                child = lead
            if lead != child:
                continue
            m = TOOLCHAIN.match(x)
            if m and m.group(1).strip().strip("'\"").strip():
                return True
    return False


def findings(name: str, text: str) -> tuple[list[str], int]:
    """(findings, pinned steps compared) of one workflow"""
    ps = pins(text)
    return ([f"{name}:{ln}: dtolnay/rust-toolchain@{sha[:12]} without `with: toolchain:`" for ln, sha, ok in ps if not ok],
            len(ps))


def check_repo(repo: Path) -> tuple[list[str], int, int]:
    """(findings, workflows read, pinned steps compared) of a checkout"""
    wf = repo / ".github" / "workflows"
    files = sorted(p for p in wf.glob("*") if p.suffix in (".yml", ".yaml")) if wf.is_dir() else []
    found, compared = [], 0
    for p in files:
        f, n = findings(p.name, p.read_text(encoding="utf-8", errors="replace"))
        found += f
        compared += n
    return found, len(files), compared


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, type=Path)
    a = ap.parse_args(argv)
    found, n, compared = check_repo(a.repo)
    for f in found:
        print(f"found: {f}")
    print(f"toolchain pin: {n} workflows read, {compared} pinned dtolnay/rust-toolchain steps, "
          f"{len(found)} without `with: toolchain:`")
    if n == 0:
        return 2
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
