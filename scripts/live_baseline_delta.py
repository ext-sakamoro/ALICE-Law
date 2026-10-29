#!/usr/bin/env python3
"""What a change to a live baseline makes worse: the live check with the base's baseline and with the change's.

A live check (holder_live.py, public_text_live.py) reads every public repository at its default branch, so on a
ci/ branch it fails on growth in any repository, which the change cannot fix. This runs the checker twice in one
job, once with the baseline the base (origin/main) has and once with the change's, and fails only on a finding the
change's run has and the base's does not (the change made it worse, e.g. an entry removed while its occurrence is
still there, or an allowance lowered). A finding both runs have is another repository's and is reported, not red;
the daily run on main is the full check.

usage: live_baseline_delta.py --checker SCRIPT --baseline FILE [--base-ref REF] [--self NAME] [--] [checker args ...]

--self NAME: this repository is NAME among the public ones. The base's run reads it at REF (a read-only checkout of
that commit) and the change's run at the checkout (`--local NAME=<tree>` to the checker), so a line the change adds
to this repository is the change's, not another repository's.

Red as well: either run reading a repository it could not read (its "N unread" is above 0), a run that ends with
neither 0 nor 1 (the checker failed), and a run that prints no summary or reads no repository (compared nothing).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SUMMARY = re.compile(r"(\d+) public repositories, (\d+) read, (\d+) empty, (\d+) unread")


def run(checker: Path, baseline: Path, args: list[str]) -> tuple[int, list[str], tuple[int, ...] | None, str]:
    r = subprocess.run([sys.executable, str(checker), "--baseline", str(baseline), *args], capture_output=True,
                       text=True, encoding="utf-8")
    out = r.stdout + r.stderr
    found = sorted({ln.strip() for ln in out.splitlines() if ln.startswith("found: ")})
    m = SUMMARY.search(out)
    return r.returncode, found, tuple(int(x) for x in m.groups()) if m else None, out


def base_baseline(baseline: str, ref: str, into: Path) -> Path | None:
    r = subprocess.run(["git", "show", f"{ref}:{baseline}"], capture_output=True)
    if r.returncode != 0:
        return None
    p = into / "base-baseline.toml"
    p.write_bytes(r.stdout)
    return p


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--checker", required=True, type=Path)
    ap.add_argument("--baseline", required=True, help="the baseline file, repository-relative")
    ap.add_argument("--base-ref", default="origin/main")
    ap.add_argument("--self", dest="self_name", metavar="NAME")
    ap.add_argument("args", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    extra = a.args[1:] if a.args[:1] == ["--"] else a.args
    with tempfile.TemporaryDirectory() as td:
        base = base_baseline(a.baseline, a.base_ref, Path(td))
        if base is None:
            print(f"live_baseline_delta: {a.base_ref} has no {a.baseline} (nothing to compare with)")
            return 2
        base_extra, change_extra = list(extra), list(extra)
        tree = Path(td) / "base-tree"
        if a.self_name:
            r = subprocess.run(["git", "worktree", "add", "-q", "--detach", str(tree), a.base_ref], capture_output=True)
            if r.returncode != 0:
                print(f"live_baseline_delta: cannot check out {a.base_ref} to read this repository at the base")
                return 2
            base_extra += ["--local", f"{a.self_name}={tree}"]
            change_extra += ["--local", f"{a.self_name}=."]
        try:
            runs = {"base": run(a.checker, base, base_extra), "change": run(a.checker, Path(a.baseline), change_extra)}
        finally:
            if a.self_name:
                subprocess.run(["git", "worktree", "remove", "--force", str(tree)], capture_output=True)
    red = False
    for name, (code, found, summary, out) in runs.items():
        if code not in (0, 1):
            print(f"live_baseline_delta: the {name} run failed (exit {code}):\n{out.strip()}")
            red = True
        elif summary is None or summary[1] == 0:
            print(f"live_baseline_delta: the {name} run read no repository (compared nothing)")
            red = True
        elif summary[3] > 0:
            print(f"live_baseline_delta: the {name} run could not read {summary[3]} repositories")
            red = True
    if red:
        return 1
    base_found, change_found = set(runs["base"][1]), set(runs["change"][1])
    worse = sorted(change_found - base_found)
    for ln in sorted(change_found & base_found):
        print(f"elsewhere (not this change's): {ln[len('found: '):]}")
    for ln in sorted(base_found - change_found):
        print(f"fixed by this change: {ln[len('found: '):]}")
    for ln in worse:
        print(f"worse with this change: {ln[len('found: '):]}")
    print(f"live_baseline_delta: {runs['change'][2][1]} repositories read, {len(worse)} finding(s) this change makes "
          f"worse, {len(change_found & base_found)} elsewhere")
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
