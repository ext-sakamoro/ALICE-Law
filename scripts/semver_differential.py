#!/usr/bin/env python3
"""Compare dep_consistency.matches with the semver crate on generated requirements.

The requirement check of dep_consistency.py is only as good as its reading of
Cargo's rules, so this generates requirement / version pairs (carets, tildes,
exact, ranges, wildcards, partial versions, pre-releases, two-comparator
requirements), asks tools/semver-oracle (the semver crate) for each answer and
fails on any disagreement. A pair the semver crate cannot read is skipped and
counted; fewer than MIN_COMPARED compared pairs is a failure.

usage: semver_differential.py [--pairs N] [--seed S] [--oracle path/to/semver-oracle]
"""
from __future__ import annotations

import argparse
import importlib.util
import random
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MIN_COMPARED = 1000

spec = importlib.util.spec_from_file_location("dep_consistency", HERE / "dep_consistency.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

OPS = ["", "^", "~", "=", ">=", ">", "<", "<="]
PRE = ["-alpha", "-alpha.1", "-beta.1", "-beta.2", "-beta.11", "-rc.1", "-preview.4"]


def pairs(n: int, seed: int) -> list[tuple[str, str]]:
    r = random.Random(seed)
    part = lambda: str(r.choice([0, 0, 1, 2, 3, 14]))  # noqa: E731

    def version():
        return ".".join(part() for _ in range(3)) + (r.choice(PRE) if r.random() < 0.3 else "")

    def comparator():
        k = r.choice([1, 2, 3])
        nums = [part() for _ in range(k)]
        if k < 3 and r.random() < 0.1:
            nums[-1] = "*"
        pre = r.choice(PRE) if k == 3 and "*" not in nums and r.random() < 0.3 else ""
        return r.choice(OPS) + ".".join(nums) + pre

    out = set()
    while len(out) < n:
        req = comparator()
        if r.random() < 0.2:
            req += ", " + comparator()
        out.add((req, version()))
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pairs", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261011)
    ap.add_argument("--oracle", help="a built semver-oracle binary (default: cargo run the tool)")
    a = ap.parse_args(argv)
    todo = pairs(a.pairs, a.seed)
    text = "".join(f"{q}\t{v}\n" for q, v in todo)
    cmd = [a.oracle] if a.oracle else [
        "cargo", "run", "--quiet", "--release", "--manifest-path",
        str(ROOT / "tools" / "semver-oracle" / "Cargo.toml")]
    proc = subprocess.run(cmd, input=text, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"error: semver-oracle failed: {proc.stderr[-500:]}", file=sys.stderr)
        return 2
    answers = proc.stdout.split()
    if len(answers) != len(todo):
        print(f"error: {len(todo)} pairs sent, {len(answers)} answers", file=sys.stderr)
        return 2
    compared, skipped, wrong = 0, 0, []
    for (req, ver), want in zip(todo, answers):
        if want == "E":
            skipped += 1
            continue
        compared += 1
        try:
            got = "1" if dc.matches(req, ver) else "0"
        except ValueError:
            got = "E"
        if got != want:
            wrong.append(f"{req!r} vs {ver}: semver crate {want}, dep_consistency {got}")
    print(f"semver differential: {compared} compared, {skipped} unreadable to the semver crate, "
          f"{len(wrong)} disagreements")
    for w in wrong[:20]:
        print(f"error: {w}", file=sys.stderr)
    if compared < MIN_COMPARED:
        print(f"error: only {compared} pairs compared (at least {MIN_COMPARED})", file=sys.stderr)
        return 2
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
