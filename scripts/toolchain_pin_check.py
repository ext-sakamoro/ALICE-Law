#!/usr/bin/env python3
"""Workflow steps that pin dtolnay/rust-toolchain to a commit.

    toolchain_pin_check.py --repo DIR      the workflows of a checkout (.github/workflows/*.yml, *.yaml)

dtolnay/rust-toolchain keeps one branch per toolchain (stable, nightly, 1.85, ...) and rewrites its branches, so a
commit taken from one of them stops being reachable from any ref of the action's repository (seen 2026-11: two such
commits were used by 124 repositories). Pinning another commit only restarts that clock, and the runners already have
rustup, so a workflow installs its toolchain with a run step (`rustup toolchain install <channel> --profile minimal`)
and pins nothing. Every step that pins this action to a 40-hex commit is a finding, whatever its inputs. A step that
names a ref (`@stable`, `@v1`) is not a pin: a branch ref can not become unreachable, so it is not judged here.

The workflow text is read line by line (no YAML library; a commented-out `uses:` does not match); the commit is matched in lower-case hex
as GitHub writes it. Composite actions (.github/actions/*/action.yml) are not read.

Prints one line per finding ("found: <workflow>:<line>: ...") and a summary. Exit 0 when none, 1 on any, 2 when no
workflow was read (an empty read compares nothing).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*['\"]?dtolnay/rust-toolchain@([0-9a-f]{40})\b")


def pins(text: str) -> list[tuple[int, str]]:
    """(1-based line, commit) of each step that pins the action to a commit"""
    out = []
    for i, raw in enumerate(text.splitlines()):
        m = USES.match(raw)
        if m:
            out.append((i + 1, m.group(1)))
    return out


def findings(name: str, text: str) -> tuple[list[str], int]:
    """(findings, pinned steps compared) of one workflow"""
    ps = pins(text)
    return ([f"{name}:{ln}: dtolnay/rust-toolchain@{sha[:12]} (install the toolchain with a rustup run step)"
             for ln, sha in ps], len(ps))


def check_repo(repo: Path) -> tuple[list[str], int, int]:
    """(findings, workflows read, pinned steps) of a checkout"""
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
    found, n, _ = check_repo(a.repo)
    for f in found:
        print(f"found: {f}")
    print(f"toolchain pin: {n} workflows read, {len(found)} steps pin dtolnay/rust-toolchain to a commit")
    if n == 0:
        return 2
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
