#!/usr/bin/env python3
"""Compare dep_consistency.matches with the semver crate on generated requirements.

The requirement check of dep_consistency.py is only as good as its reading of
Cargo's rules, so this generates requirement / version pairs (carets, tildes,
exact, ranges, wildcards, partial versions, pre-releases, two-comparator
requirements) plus a fixed grid of every operator and requirement shape against the
versions one step away, asks tools/semver-oracle (the semver crate) for each answer and
fails on any disagreement, including a requirement that one of the two can read and
the other cannot. Fewer than MIN_COMPARED compared pairs is a failure.

usage: semver_differential.py [--pairs N] [--seed S] [--oracle path/to/semver-oracle]
"""
from __future__ import annotations

import argparse
import importlib.util
import random
import re
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
WILD = ["*", "x", "X"]
PRE = ["-alpha", "-alpha.1", "-beta.1", "-beta.2", "-beta.11", "-rc.1", "-preview.4"]


def pairs(n: int, seed: int) -> list[tuple[str, str]]:
    r = random.Random(seed)
    part = lambda: str(r.choice([0, 0, 1, 2, 3, 14]))  # noqa: E731

    def version():
        return ".".join(part() for _ in range(3)) + (r.choice(PRE) if r.random() < 0.3 else "")

    def comparator():
        k = r.choice([1, 2, 3])
        nums = [part() for _ in range(k)]
        if r.random() < 0.15:
            # wildcards (`*`, `x`, `X`) in the last one or two places, including 3-part forms
            # such as `1.2.*` and `1.*.*`
            for i in range(r.choice([1, 2]) if k > 1 else 1):
                nums[k - 1 - i] = r.choice(WILD)
        pre = r.choice(PRE) if k == 3 and not set(nums) & set(WILD) and r.random() < 0.3 else ""
        return r.choice(OPS) + ".".join(nums) + pre

    def near(req):
        # a version next to the requirement's own numbers (same major, a neighbouring minor
        # or patch), where the rules differ most: `1.2.*` against 1.3.0, `^0.2` against 0.3.0
        nums = [int(x) for x in re.findall(r"\d+", req.split(",")[0].split("-")[0])][:3]
        nums += [r.choice([0, 1, 2]) for _ in range(3 - len(nums))]
        i = r.choice([0, 1, 2])
        nums[i] = max(0, nums[i] + r.choice([-1, 0, 1]))
        return ".".join(map(str, nums)) + (r.choice(PRE) if r.random() < 0.2 else "")

    out = set()
    while len(out) < n:
        req = comparator()
        if r.random() < 0.2:
            req += ", " + comparator()
        out.add((req, near(req) if r.random() < 0.5 else version()))
    return sorted(out)


def grid() -> list[tuple[str, str]]:
    """Every operator with every requirement shape (1, 2 or 3 parts, wildcards in each
    trailing place, a pre-release), against the versions one step away in each part:
    the places where the rules differ (`1.2.*` against 1.3.0 and 1.13.0, `^0.2` against
    0.3.0, `<=1.2.3-beta.1` against 1.2.3) are all in it, whatever the random pairs hit"""
    out = []
    for M, m, p in ((0, 0, 3), (0, 2, 3), (1, 2, 3)):
        shapes = [f"{M}", f"{M}.{m}", f"{M}.{m}.{p}", f"{M}.{m}.{p}-beta.2"]
        shapes += [f"{M}.{w}" for w in WILD] + [f"{M}.{m}.{w}" for w in WILD] + [f"{M}.*.*", "*", "x"]
        versions = {f"{a}.{b}.{c}" for a in (M, M + 1) if a >= 0 for b in (m - 1, m, m + 1, 13) if b >= 0
                    for c in (p - 1, p, p + 1) if c >= 0}
        versions |= {f"{M}.{m}.{p}-beta.1", f"{M}.{m}.{p}-beta.2", f"{M}.{m}.{p}-beta.11",
                     f"{M}.{m + 1}.0-alpha"}
        if M > 0:
            versions |= {f"{M - 1}.{m}.{p}", f"{M - 1}.99.0"}
        for shape in shapes:
            for op in OPS:
                for v in sorted(versions):
                    out.append((op + shape, v))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pairs", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261011)
    ap.add_argument("--oracle", help="a built semver-oracle binary (default: cargo run the tool)")
    a = ap.parse_args(argv)
    todo = sorted(set(pairs(a.pairs, a.seed)) | set(grid()))
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
    compared, unreadable, wrong = 0, 0, []
    for (req, ver), want in zip(todo, answers):
        # a requirement the semver crate cannot read must be unreadable here too (E), so
        # that dep_consistency.py stops with exit 2 instead of guessing
        compared += 1
        unreadable += want == "E"
        try:
            got = "1" if dc.matches(req, ver) else "0"
        except ValueError:
            got = "E"
        if got != want:
            wrong.append(f"{req!r} vs {ver}: semver crate {want}, dep_consistency {got}")
    print(f"semver differential: {compared} compared ({unreadable} unreadable to the semver crate), "
          f"{len(wrong)} disagreements")
    for w in wrong[:20]:
        print(f"error: {w}", file=sys.stderr)
    if compared < MIN_COMPARED:
        print(f"error: only {compared} pairs compared (at least {MIN_COMPARED})", file=sys.stderr)
        return 2
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
