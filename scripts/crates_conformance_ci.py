#!/usr/bin/env python3
"""The crates' conformance tables against this branch's catalog and baseline (a ci/ branch that changes them).

Conformance (live) reads the crates on main only, so a ci/ change to conformance/g11-shared-rows.toml or to
conformance/surface-claims-baseline.toml reached main before anything read the crates with it. This reads each
crate at its default branch (a shallow clone of its public repository, or --source DIR/<repo> for a local copy)
and runs, with this branch's files:

- g11: scripts/g11_table_gen.py --write into the copy, for every crate whose default branch carries
  conformance/g11.toml (the repositories of conformance/shared-files.toml). A gap or an unresolved row is red;
  a table that differs from the crate's own is reported (the crate regenerates at its next pin), not red.
- g15: scripts/surface_claims_check.py --baseline conformance/surface-claims-baseline.toml for every repository
  the baseline lists (this repository is checked in its own tree). A count above or below the baseline is red.

usage: crates_conformance_ci.py (g11 | g15) [--source DIR]

Each prints "<kind>: checked N repositories, K red" and fails when N is 0 (nothing compared).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER = "ext-sakamoro"
SELF = "ALICE-Law"


def table_repos() -> list[str]:
    text = (ROOT / "conformance" / "shared-files.toml").read_text(encoding="utf-8")
    repos: set[str] = set()
    for e in tomllib.loads(text)["file"]:
        repos |= set(e["repos"])
    return sorted(repos - {SELF})


def baseline_repos() -> list[str]:
    text = (ROOT / "conformance" / "surface-claims-baseline.toml").read_text(encoding="utf-8")
    return sorted(tomllib.loads(text).get("repository", {}))


LOCAL_COPIES: list[tuple[Path, Path]] = []


def drop_local_copies() -> None:
    while LOCAL_COPIES:
        src, dest = LOCAL_COPIES.pop()
        subprocess.run(["git", "-C", str(src), "worktree", "remove", "--force", str(dest)], capture_output=True)


def fetch(repo: str, into: Path, source: Path | None) -> Path | None:
    """a work tree of `repo`'s default branch under `into`, or None when it cannot be read"""
    dest = into / repo
    if source is not None:
        # a local copy is read at its origin/main (its checked-out branch may be anything)
        src = source / repo
        if not (src / ".git").exists():
            return None
        r = subprocess.run(["git", "-C", str(src), "worktree", "add", "-q", "--detach", str(dest), "origin/main"],
                           capture_output=True)
        if r.returncode == 0:
            LOCAL_COPIES.append((src, dest))
    else:
        r = subprocess.run(["git", "clone", "-q", "--depth", "1", f"https://github.com/{OWNER}/{repo}", str(dest)],
                           capture_output=True)
    return dest if r.returncode == 0 else None


def run(argv: list[str]) -> tuple[int, str]:
    r = subprocess.run([sys.executable, *argv], capture_output=True, text=True, encoding="utf-8")
    return r.returncode, (r.stdout + r.stderr).strip()


def g11(source: Path | None) -> int:
    checked = red = 0
    with tempfile.TemporaryDirectory() as td:
        for repo in table_repos():
            tree = fetch(repo, Path(td), source)
            if tree is None:
                print(f"g11: {repo}: could not be read")
                red += 1
                continue
            if not (tree / "conformance" / "g11.toml").exists():
                continue
            checked += 1
            before = (tree / "conformance" / "g11.toml").read_bytes()
            code, out = run([str(ROOT / "scripts" / "g11_table_gen.py"), "--repo", str(tree), "--name", repo,
                             "--catalog", str(ROOT / "conformance" / "g11-shared-rows.toml"), "--write"])
            last = out.splitlines()[-1] if out else ""
            if code != 0:
                red += 1
                print(f"g11: {repo}: red (exit {code}): {last}")
                continue
            moved = (tree / "conformance" / "g11.toml").read_bytes() != before
            print(f"g11: {repo}: ok{' (its table changes with this catalog: regenerate at the next pin)' if moved else ''}: {last}")
    print(f"g11: checked {checked} repositories, {red} red")
    if checked == 0:
        print("g11: no crate carries conformance/g11.toml (compared nothing)")
        return 2
    return 1 if red else 0


def g15(source: Path | None) -> int:
    checked = red = 0
    baseline = ROOT / "conformance" / "surface-claims-baseline.toml"
    with tempfile.TemporaryDirectory() as td:
        for repo in baseline_repos():
            tree = ROOT if repo == SELF else fetch(repo, Path(td), source)
            if tree is None:
                print(f"g15: {repo}: could not be read")
                red += 1
                continue
            checked += 1
            code, out = run([str(ROOT / "scripts" / "surface_claims_check.py"), "--repo", str(tree),
                             "--baseline", str(baseline), "--name", repo])
            last = out.splitlines()[-1] if out else ""
            if code != 0:
                red += 1
                print(f"g15: {repo}: red (exit {code}): {last}")
            else:
                print(f"g15: {repo}: ok: {last}")
    print(f"g15: checked {checked} repositories, {red} red")
    if checked == 0:
        print("g15: the baseline lists no repository (compared nothing)")
        return 2
    return 1 if red else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("kind", choices=["g11", "g15"])
    ap.add_argument("--source", type=Path, help="read each repository from DIR/<repo> instead of GitHub")
    a = ap.parse_args(argv)
    try:
        return g11(a.source) if a.kind == "g11" else g15(a.source)
    finally:
        drop_local_copies()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
