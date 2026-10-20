#!/usr/bin/env python3
"""A workflow step that runs Cargo with `--locked` after the job generated the lock file runs
only when that generation succeeded.

`--locked` refuses to create a missing Cargo.lock. A job that writes the lock file in an
earlier step (`cargo generate-lockfile`, in a step with an `id`) and runs a later `--locked`
step under `always()`, `!cancelled()` or `failure()` runs that step even when the generating
step failed or was skipped: it then fails with "cannot create the lock file because --locked
was passed", a second failure that hides the first (the e2e-main run of 2026-10-19 showed it
under a failed feature-coverage step). Such a step must also require
`steps.<id>.outcome == 'success'` for the generating step's id. A step without those
functions in its `if:` runs only when every earlier step succeeded, which already holds.

Fails on every step that breaks the rule, and when no `--locked` step after a generated lock
file was read (nothing compared).

usage: locked_lockfile_check.py [workflows dir]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

STEP = re.compile(r"^(\s*)- ")
OVERRIDES = ("always()", "!cancelled()", "failure()")


def steps_of(text: str) -> list[list[str]]:
    """the lines of each step of each job's `steps:` list, in order (a step starts at a `- ` item
    of a `steps:` key and runs to the next item at its indentation or a shallower line)"""
    out: list[list[str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if re.match(r"^\s+steps:\s*$", lines[i]):
            base = len(lines[i]) - len(lines[i].lstrip())
            i += 1
            item = None
            while i < len(lines):
                line = lines[i]
                indent = len(line) - len(line.lstrip())
                if line.strip() and indent <= base:
                    break
                m = STEP.match(line)
                if m and (item is None or len(m.group(1)) <= item):
                    item = len(m.group(1))
                    out.append([line])
                elif out and line.strip() and not line.lstrip().startswith("#"):
                    out[-1].append(line)  # (a comment says nothing the runner reads)
                i += 1
            continue
        i += 1
    return out


def generating_ids(text: str) -> set[str]:
    """the `id` of every step whose lines run `generate-lockfile` (a step written once with an anchor,
    `- &patch`, is read where it is written; its aliases `- *patch` reuse that id)"""
    ids = set()
    for step in steps_of(text):
        body = "\n".join(step)
        if "generate-lockfile" in body:
            ids.update(re.findall(r"^\s*-?\s*id:\s*([\w-]+)\s*$", body, re.M))
    return ids


def condition(step: list[str]) -> str:
    """the step's `if:` expression: a plain value, or a block scalar (`>`, `>-`, `|`, `|-`, with an
    indentation or chomping indicator) joined from the lines indented deeper than the key"""
    for n, line in enumerate(step):
        m = re.match(r"^(\s*-?\s*)if:\s*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if not re.fullmatch(r"[>|][+-]?\d?|[>|]\d?[+-]?", value):
            return value
        key = len(m.group(1)) - (1 if m.group(1).rstrip().endswith("-") else 0)
        parts = []
        for nxt in step[n + 1:]:
            if len(nxt) - len(nxt.lstrip()) <= key:
                break
            parts.append(nxt.strip())
        return " ".join(parts)
    return ""


def problems_of(name: str, text: str) -> tuple[list[str], int]:
    """(problems, `--locked` steps compared) of one workflow"""
    ids = generating_ids(text)
    if not ids:
        return [], 0
    problems, compared = [], 0
    for step in steps_of(text):
        body = "\n".join(step)
        if "--locked" not in body or "generate-lockfile" in body:
            continue
        compared += 1
        cond = condition(step)
        if any(f in cond for f in OVERRIDES) and not any(
                re.search(rf"steps\.{re.escape(i)}\.outcome\s*==\s*'success'", cond) for i in ids):
            label = next((l.split("name:", 1)[1].strip() for l in step if "name:" in l), step[0].strip())
            problems.append(f"{name}: step {label!r} runs --locked under `{cond}` without requiring "
                            f"steps.<{'|'.join(sorted(ids))}>.outcome == 'success' (the step that writes the lock file)")
    return problems, compared


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / ".github" / "workflows"
    problems, compared = [], 0
    for wf in sorted(root.glob("*.yml")):
        p, n = problems_of(wf.name, wf.read_text(encoding="utf-8"))
        problems += p
        compared += n
    for p in problems:
        print(f"locked-lockfile: {p}", file=sys.stderr)
    print(f"locked-lockfile: {compared} --locked step(s) after a generated lock file compared, "
          f"{len(problems)} problem(s)")
    if compared == 0:
        print("locked-lockfile: nothing compared", file=sys.stderr)
        return 2
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
