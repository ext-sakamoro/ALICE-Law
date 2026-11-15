#!/usr/bin/env python3
"""On a push run of Conformance (live), say which jobs were skipped, so a green push run is not read as counted.

The jobs that read every public repository carry `if: github.event_name != 'push'` and run daily and by hand only:
a push to main runs the rest. GitHub shows a skipped job as grey and the run as green, so on a push run this writes
to the job summary (and the log) the name of every such job, read from the workflow file itself, and where the last
count of each is (the latest scheduled or dispatched run). On other events it writes nothing.

  live_scope_note.py --event EVENT [--workflow PATH] [--summary PATH]

A workflow with no job skipped on push, or one this reader can not read, is exit 2 (the reader is then wrong, not
the workflow quiet): 0 compared is not a pass.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKFLOW = HERE.parent / ".github" / "workflows" / "conformance-live.yml"
SKIP_ON_PUSH = re.compile(r"""^\s{4}if:\s*github\.event_name\s*!=\s*['"]push['"]\s*$""")
JOB = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
NAME = re.compile(r"^\s{4}name:\s*(.+?)\s*$")


def skipped_on_push(text: str) -> list[str]:
    """the display name (or id) of every job whose job-level `if` is `github.event_name != 'push'`"""
    out: list[str] = []
    in_jobs = False
    job = name = None
    skip = False

    def close():
        if job is not None and skip:
            out.append(name or job)

    for line in text.splitlines():
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if line and not line.startswith(" ") and not line.startswith("#"):
            break  # a later top-level key ends the jobs map
        m = JOB.match(line)
        if m:
            close()
            job, name, skip = m.group(1), None, False
            continue
        if job is None:
            continue
        m = NAME.match(line)
        if m and name is None:
            name = m.group(1).strip("'\"")
        elif SKIP_ON_PUSH.match(line):
            skip = True
    close()
    return out


def note(names: list[str]) -> str:
    rows = "\n".join(f"- {n}" for n in names)
    return ("### A push run: these jobs did not run\n\n"
            "A push to main runs only the jobs about this repository. The jobs below read every public repository "
            "and run daily and by hand only, so this run's green says nothing about them; their last count is the "
            "latest scheduled or dispatched run of this workflow.\n\n" + rows + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--event", required=True)
    ap.add_argument("--workflow", default=str(WORKFLOW))
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    a = ap.parse_args(argv)
    try:
        text = Path(a.workflow).read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: {a.workflow}: {e}", file=sys.stderr)
        return 2
    names = skipped_on_push(text)
    if not names:
        print(f"error: {a.workflow}: no job is skipped on push (0 read): the reader, not the workflow, is wrong",
              file=sys.stderr)
        return 2
    if a.event != "push":
        print(f"live_scope_note: event {a.event}: all {len(names)} repository-wide jobs run")
        return 0
    for n in names:
        print(f"live_scope_note: skipped on push (not counted in this run): {n}")
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(note(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
