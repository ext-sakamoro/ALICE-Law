#!/usr/bin/env python3
"""Workflow jobs that run on ubuntu-latest while depending on what ubuntu 26.04 changed.

    runner_pin_check.py --repo DIR      the workflows of a checkout (.github/workflows/*.yml, *.yaml)

The patterns (conformance/runner-pin.toml) name what changed: a job whose text matches one of them must name a
fixed image, not ubuntu-latest. A job runs on ubuntu-latest when its `runs-on` says so, or when `runs-on` reads
the matrix and the job's matrix lists ubuntu-latest. The workflow text is read line by line (no YAML library):
a job is the block under `jobs:` that starts at two spaces of indent, up to the next such key; comments are
not read (a comment before a job key sits in the block of the job above it).

Prints one line per offending job ("found: <workflow>: <job>: <pattern>") and a summary. Exit 0 when none, 1 on
any, 2 when no workflow was read (an empty read compares nothing).
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

PATTERNS = Path(__file__).resolve().parent.parent / "conformance" / "runner-pin.toml"
JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):\s*(#.*)?$")
TOP_KEY = re.compile(r"^[A-Za-z0-9_-]+:")


def patterns(path: Path = PATTERNS) -> dict[str, re.Pattern]:
    table = tomllib.loads(path.read_text(encoding="utf-8"))["pattern"]
    if not table:
        raise ValueError(f"{path}: no pattern")
    return {name: re.compile(rx, re.I) for name, rx in table.items()}


def jobs(text: str) -> list[tuple[str, str]]:
    """(name, text) of each job under the top-level `jobs:` key"""
    lines = text.splitlines()
    out: list[tuple[str, str]] = []
    in_jobs, name, body = False, None, []
    for line in lines:
        if TOP_KEY.match(line):
            if name is not None:
                out.append((name, "\n".join(body)))
                name, body = None, []
            in_jobs = line.split(":", 1)[0] == "jobs"
            continue
        if not in_jobs:
            continue
        m = JOB_KEY.match(line)
        if m:
            if name is not None:
                out.append((name, "\n".join(body)))
            name, body = m.group(1), []
        elif name is not None:
            body.append(line)
    if name is not None:
        out.append((name, "\n".join(body)))
    return out


def code(body: str) -> str:
    """the job's text without comments: a comment (often the next job's preamble) is not what the job runs"""
    return "\n".join(line.split("#", 1)[0] for line in body.splitlines())


def runs_on_latest(body: str) -> bool:
    """whether the job's runner resolves to ubuntu-latest"""
    code_ = code(body)
    m = re.search(r"^\s*runs-on:\s*(.*)$", code_, re.M)
    if not m:
        return False
    value = m.group(1)
    if "ubuntu-latest" in value:
        return True
    return "matrix." in value and "ubuntu-latest" in code_


def offending(text: str, pats: dict[str, re.Pattern]) -> list[tuple[str, str]]:
    """(job, pattern name) of each job that runs on ubuntu-latest and matches a pattern"""
    out = []
    for name, body in jobs(text):
        if not runs_on_latest(body):
            continue
        text = code(body)
        for pname, rx in pats.items():
            if rx.search(text):
                out.append((name, pname))
                break
    return out


def check_repo(repo: Path, pats: dict[str, re.Pattern]) -> tuple[list[str], int]:
    """(findings, workflows read) of a checkout"""
    wf = repo / ".github" / "workflows"
    files = sorted(p for p in wf.glob("*") if p.suffix in (".yml", ".yaml")) if wf.is_dir() else []
    found = []
    for p in files:
        for job, pname in offending(p.read_text(encoding="utf-8", errors="replace"), pats):
            found.append(f"{p.name}: {job}: {pname} (runs on ubuntu-latest; name ubuntu-24.04)")
    return found, len(files)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, type=Path)
    a = ap.parse_args(argv)
    found, n = check_repo(a.repo, patterns())
    for f in found:
        print(f"found: {f}")
    print(f"runner pin: {n} workflows read, {len(found)} jobs on ubuntu-latest that must name a fixed image")
    if n == 0:
        return 2
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
