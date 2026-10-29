#!/usr/bin/env python3
"""What a change to a live baseline makes worse: the live check with the base's baseline and with the change's.

A live check (holder_live.py, public_text_live.py) reads every public repository at its default branch, so on a
ci/ branch it fails on growth in any repository, which the change cannot fix. This runs the checker twice in one
job: the base's checker (from a read-only checkout of the base, origin/main) with the base's baseline, and this
checkout's checker with the change's, so a baseline kept in the checker's own text is compared too. It fails only on
a finding the change's run has and the base's does not (the change made it worse, e.g. an entry removed while its
occurrence is still there, or an allowance lowered). A finding both runs have is another repository's and is
reported, not red; the daily run on main is the full check.

usage: live_baseline_delta.py --checker SCRIPT --baseline FILE [--base-ref REF] [--self NAME] [--] [checker args ...]

--self NAME: this repository is NAME among the public ones. The base's run reads it at REF (a read-only checkout of
that commit) and the change's run at the checkout (`--local NAME=<tree>` to the checker), so a line the change adds
to this repository is the change's, not another repository's.

Red as well: either run reading a repository it could not read (its "N unread" is above 0), a run that ends with
neither 0 nor 1 (the checker failed), and a run that prints no summary or reads no repository (compared nothing). A
checker the base does not have is new: the base has no findings, so every finding of the change's run is the change's.
A base that has the checker but not the baseline compares nothing (exit 2).
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
    # named by the caller (scripts/ci_base.py in CI): main's tip is not the base of a change that forked earlier
    ap.add_argument("--base-ref", required=True, help="the commit whose baseline the change is compared with")
    ap.add_argument("--self", dest="self_name", metavar="NAME")
    ap.add_argument("args", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    extra = a.args[1:] if a.args[:1] == ["--"] else a.args
    top = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                              encoding="utf-8").stdout.strip() or ".").resolve()
    checker_rel = a.checker.resolve().relative_to(top) if a.checker.is_absolute() else a.checker
    with tempfile.TemporaryDirectory() as td:
        # the base run runs the base's checker (a baseline kept in the checker's own text, and the modules beside it,
        # are the base's), from a read-only checkout of base-ref; the change's run runs this checkout's
        tree = Path(td) / "base-tree"
        r = subprocess.run(["git", "worktree", "add", "-q", "--detach", str(tree), a.base_ref], capture_output=True)
        if r.returncode != 0:
            print(f"live_baseline_delta: cannot check out {a.base_ref} to run its checker")
            return 2
        try:
            # a checker the base does not have is new with this change: the base has no findings, so every finding
            # the change's run has is the change's (zero, or baselined in the same change)
            new_checker = not (tree / checker_rel).is_file()
            base = None if new_checker else base_baseline(a.baseline, a.base_ref, Path(td))
            if not new_checker and base is None:
                print(f"live_baseline_delta: {a.base_ref} has no {a.baseline} (nothing to compare with)")
                return 2
            base_extra, change_extra = list(extra), list(extra)
            if a.self_name:
                base_extra += ["--local", f"{a.self_name}={tree}"]
                change_extra += ["--local", f"{a.self_name}=."]
            runs = {"change": run(a.checker, Path(a.baseline), change_extra)}
            if new_checker:
                print(f"live_baseline_delta: {a.base_ref} has no {checker_rel}: a new checker, every finding is this "
                      "change's")
            else:
                runs["base"] = run(tree / checker_rel, base, base_extra)
        finally:
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
    base_found, change_found = set(runs["base"][1]) if "base" in runs else set(), set(runs["change"][1])
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
