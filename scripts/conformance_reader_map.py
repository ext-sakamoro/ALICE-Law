#!/usr/bin/env python3
"""Every file under conformance/ is read by a checker that runs on a ci/ branch.

A conformance file changes on a ci/ branch before it reaches main; a checker that only runs on main reads it for
the first time after the landing. For each tracked file under conformance/ (the planned/ bytes count as one file,
`conformance/planned/*`), this finds the reader scripts that name it (scripts/*.py and scripts/*.sh, the tests
excepted) and requires one of them to be run by a workflow whose `on.push.branches` includes `ci/**`.

usage: conformance_reader_map.py [--root DIR]

Fails on an unmapped file, on no conformance file read, and on no ci/** workflow read (nothing compared is a
failure).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PLANNED = "conformance/planned/*"
RUN_SCRIPT = re.compile(r"scripts/([A-Za-z0-9_.-]+\.(?:py|sh))")


def tracked(root: Path, prefix: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "--", prefix], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout.split()
    return sorted(out)


def conformance_files(root: Path) -> list[str]:
    files = []
    for f in tracked(root, "conformance"):
        if f.startswith("conformance/planned/"):
            if PLANNED not in files:
                files.append(PLANNED)
        else:
            files.append(f)
    return files


def on_ci_branches(text: str) -> bool:
    """`ci/**` in the `branches` of the `on.push` block (the text between `on:` and `jobs:`)"""
    m = re.search(r"(?ms)^on:\s*$(.*?)^jobs:", text)
    if not m:
        return False
    block = m.group(1)
    push = re.search(r"(?ms)^  push:\s*$(.*?)(?=^  [a-z_]+:\s*$|\Z)", block)
    return bool(push and re.search(r"""branches:.*["']ci/\*\*["']""", push.group(1)))


def ci_scripts(root: Path) -> tuple[set[str], int]:
    """the scripts a ci/** workflow runs, and how many ci/** workflows there are"""
    scripts: set[str] = set()
    n = 0
    for wf in sorted((root / ".github" / "workflows").glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        if on_ci_branches(text):
            n += 1
            scripts |= set(RUN_SCRIPT.findall(text))
    return scripts, n


def readers(root: Path, file: str) -> list[str]:
    """the non-test scripts that name `file` (its path under conformance/, or for planned/ the directory)"""
    needle = "planned/" if file == PLANNED else Path(file).name
    found = []
    for s in sorted((root / "scripts").iterdir()):
        if s.suffix not in (".py", ".sh") or s.name.startswith("test_"):
            continue
        if needle in s.read_text(encoding="utf-8", errors="replace"):
            found.append(s.name)
    return found


def check(root: Path) -> tuple[int, list[str]]:
    files = conformance_files(root)
    run, workflows = ci_scripts(root)
    out = []
    if not files:
        return 2, ["conformance_reader_map: no tracked file under conformance/ (compared nothing)"]
    if workflows == 0:
        return 2, ["conformance_reader_map: no workflow runs on ci/** (compared nothing)"]
    bad = 0
    for f in files:
        rs = readers(root, f)
        on_ci = [r for r in rs if r in run]
        if on_ci:
            out.append(f"ok: {f}: read on ci/** by {', '.join(on_ci)}")
        else:
            bad += 1
            why = f"its readers ({', '.join(rs)}) run on main only" if rs else "no script reads it"
            out.append(f"unmapped: {f}: {why}")
    out.append(f"conformance_reader_map: {len(files)} file(s), {workflows} ci/** workflow(s), {bad} unmapped")
    return (1 if bad else 0), out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    a = ap.parse_args(argv)
    code, lines = check(a.root)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
