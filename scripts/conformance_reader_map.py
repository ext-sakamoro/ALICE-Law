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


RUN_KEY = re.compile(r"^(\s*)(?:- )?run:\s*(.*)$")


def run_texts(text: str) -> list[str]:
    """the text of every `run:` step of a workflow: a single-line value, or a block scalar (`|` / `>`) and the
    lines indented under it (a script named elsewhere, as in `paths:`, is not run)"""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = RUN_KEY.match(lines[i])
        i += 1
        if not m:
            continue
        indent = len(m.group(1)) + (2 if lines[i - 1].lstrip().startswith("- ") else 0)
        value = m.group(2).strip()
        if value and value[0] not in "|>":
            out.append(value)
            continue
        block = []
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
            block.append(lines[i])
            i += 1
        out.append("\n".join(block))
    return out


def ci_scripts(root: Path) -> tuple[set[str], int]:
    """the scripts a step of a ci/** workflow runs, and how many ci/** workflows there are"""
    scripts: set[str] = set()
    n = 0
    for wf in sorted((root / ".github" / "workflows").glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        if on_ci_branches(text):
            n += 1
            for run in run_texts(text):
                scripts |= set(RUN_SCRIPT.findall(run))
    return scripts, n


def names(file: str) -> re.Pattern[str]:
    """how a script names `file`: its whole repository-relative path as one token, or a path joined from its quoted
    parts (`"conformance" / "sub" / "a.toml"`); for planned/, the directory the checker reads next to the table"""
    if file == PLANNED:
        return re.compile(r"""PLANNED_DIR\s*=\s*["']planned["']|(?<![\w./-])conformance/planned(?![\w.-])""")
    parts = file.split("/")
    whole = r"(?<![\w./-])" + re.escape(file) + r"(?![\w.-])"
    joined = r"""["']""" + r"""["']\s*/\s*["']""".join(map(re.escape, parts)) + r"""["']"""
    return re.compile(whole + "|" + joined)


def readers(root: Path, file: str) -> list[str]:
    """the non-test scripts that name `file`"""
    pattern = names(file)
    found = []
    for s in sorted((root / "scripts").iterdir()):
        if s.suffix not in (".py", ".sh") or s.name.startswith("test_") or s.name == Path(__file__).name:
            continue  # the tests, and this check (it spells the paths it looks for), are not readers
        if pattern.search(s.read_text(encoding="utf-8", errors="replace")):
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
