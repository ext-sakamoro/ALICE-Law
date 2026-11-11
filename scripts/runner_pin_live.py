#!/usr/bin/env python3
"""The runner pin check (runner_pin_check.py) over every public repository of the account, at its default branch.

    runner_pin_live.py [--owner NAME] [--local NAME=DIR] [--jobs N]

The repositories are the ones the GitHub API lists as public at run time (holder_live.public_repos). Each default
branch is fetched at depth 1 without blobs, and only its .github/workflows/*.yml / *.yaml are read. --local
NAME=DIR reads repository NAME (the one a CI run tests) from the checkout DIR instead; NAME must be one of the
listed public repositories. There is no baseline: every finding is red (a job that needs ubuntu 24.04 names it).

Exit 0 when every repository was read and nothing was found, 1 on a finding, 2 when a repository could not be read,
no repository was listed, or no workflow was read.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import holder_live as hl  # noqa: E402
import runner_pin_check as rpc  # noqa: E402

GITHUB = "https://github.com/"


def read_repo(base: Path, repo: dict, pats) -> tuple[str, list[str] | None, int, str | None]:
    """(name, findings or None when the repository is empty, workflows read, error)"""
    name = repo["name"]
    if repo.get("size", 1) == 0:
        return name, None, 0, None
    g = base / name
    try:
        subprocess.run(["git", "init", "-q", str(g)], check=True, capture_output=True)
        hl.git(g, "remote", "add", "origin", GITHUB + repo["full_name"])
        hl.git(g, "fetch", "-q", "--depth", "1", "--no-tags", "--filter=blob:none", "origin",
               f"+refs/heads/{repo['default_branch']}:refs/remotes/origin/main")
        names = [p for p in hl.git(g, "ls-tree", "-z", "--name-only", "origin/main", ".github/workflows/").split("\0")
                 if p.endswith((".yml", ".yaml"))]
        found = []
        for p in names:
            text = hl.git(g, "show", f"origin/main:{p}")
            found += [f"{name}: {p.rsplit('/', 1)[-1]}: {job}: {pn}" for job, pn in rpc.offending(text, pats)]
        return name, found, len(names), None
    except (hl.Unreadable, subprocess.CalledProcessError) as e:
        return name, None, 0, str(e)
    finally:
        shutil.rmtree(g, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--local", help="NAME=DIR: read repository NAME from the checkout DIR")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args(argv)
    pats = rpc.patterns()
    try:
        repos = hl.public_repos(a.owner)
    except hl.Unreadable as e:
        print(f"runner pin live: {e}")
        return 2
    if not repos:
        print("runner pin live: no public repository listed (compared nothing)")
        return 2
    local_name, local_dir = (a.local.split("=", 1) if a.local else (None, None))
    if local_name is not None and local_name not in {r["name"] for r in repos}:
        print(f"runner pin live: --local {local_name} is not a listed public repository")
        return 2
    results = []
    with tempfile.TemporaryDirectory() as base:
        todo = [r for r in repos if r["name"] != local_name]
        with ThreadPoolExecutor(a.jobs) as ex:
            results = list(ex.map(lambda r: read_repo(Path(base), r, pats), todo))
    if local_name is not None:
        found, n = rpc.check_repo(Path(local_dir), pats)
        results.append((local_name, [f"{local_name}: {f}" for f in found], n, None))
    found = [f for _, fs, _, _ in results if fs for f in fs]
    errors = [(n, e) for n, _, _, e in results if e]
    empty = sum(1 for _, fs, _, e in results if fs is None and e is None)
    workflows = sum(n for _, _, n, _ in results)
    for f in sorted(found):
        print(f"found: {f}")
    for n, e in errors:
        print(f"unread: {n}: {e[:200]}")
    print(f"runner pin live: {len(repos)} public repositories, {len(results) - len(errors) - empty} read, {empty} empty, "
          f"{len(errors)} unread, {workflows} workflows read, {len(found)} jobs on ubuntu-latest that must name a fixed image")
    if errors or workflows == 0:
        return 2
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
