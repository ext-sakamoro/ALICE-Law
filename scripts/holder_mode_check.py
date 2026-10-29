#!/usr/bin/env python3
"""A holder's own shared-file check, run against this checkout's table and checker.

Every crate that holds shared files runs its scripts/shared_files.sh: it fetches ALICE-Law at the revision its
.github/shared-files-pin names, copies the table and the checker alone into a temporary directory (no planned/ beside
the table) and runs the checker in its crate mode (--repo, --name) on the crate's committed tree. A change to the
table or the checker that this shape breaks reaches every crate that pins it, while the checks in this repository,
which run beside the whole conformance/ directory, stay green (2026-10-30: a rule asking for planned/ copies gave 23
findings in every crate and none here).

This reads a real holder's main (default ALICE-Analytics), keeps its own scripts/shared_files.sh unchanged, points
its pin at this checkout's HEAD (the table's and the checker's SHA-256 at HEAD) and runs that script, with the
fetch of ALICE-Law redirected to this repository, so the revision under test need not be on GitHub yet.

usage: holder_mode_check.py [--holder ALICE-X] [--holder-tree DIR] [--law-repo DIR]

--holder-tree: the holder's tree from a directory instead of its main (the tests)
--law-repo: the repository the fetch is redirected to (default: this checkout's)

Exit 0: the holder's check passed and compared at least one file. 1: it failed (the change breaks that holder).
2: it compared nothing, or the holder's tree, its script or its pin could not be read.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LAW_URL = "https://github.com/ext-sakamoro/ALICE-Law"
GITHUB = "https://github.com/ext-sakamoro/"
COMPARED = re.compile(r"compared: files (\d+)")
SCRIPT = "scripts/shared_files.sh"
PIN = ".github/shared-files-pin"


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout


def head_file(repo: Path, path: str) -> bytes:
    return subprocess.run(["git", "show", f"HEAD:{path}"], cwd=repo, capture_output=True, check=True).stdout


def holder_tree(name: str, into: Path) -> Path:
    """the holder's main as a committed tree (git archive, no line-ending conversion)"""
    g = into / "holder.git"
    git("init", "-q", "--bare", str(g))
    git("fetch", "-q", "--depth", "1", GITHUB + name, "main", cwd=g)
    tree = into / "holder"
    tree.mkdir()
    archive = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "archive", "FETCH_HEAD"],
                             cwd=g, capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
    return tree


def repin(tree: Path, law: Path, rev: str) -> None:
    """the holder's pin, pointed at `rev` of `law`: name and visibility kept, rev and the two SHA-256 replaced"""
    lines = (tree / PIN).read_text(encoding="utf-8").splitlines()
    want = {"rev": rev,
            "table-sha256": hashlib.sha256(head_file(law, "conformance/shared-files.toml")).hexdigest(),
            "checker-sha256": hashlib.sha256(head_file(law, "scripts/shared_files_check.py")).hexdigest()}
    out = []
    for ln in lines:
        key = ln.split(maxsplit=1)[0] if ln.strip() else ""
        out.append(f"{key} {want.pop(key)}" if key in want else ln)
    if want:
        raise ValueError(f"{PIN}: no line for {', '.join(sorted(want))}")
    (tree / PIN).write_text("\n".join(out) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--holder", default="ALICE-Analytics")
    ap.add_argument("--holder-tree", type=Path)
    ap.add_argument("--law-repo", type=Path, default=Path("."))
    a = ap.parse_args(argv)
    law = a.law_repo.resolve()
    with tempfile.TemporaryDirectory() as td:
        try:
            rev = git("rev-parse", "HEAD", cwd=law).strip()
            common = (law / git("rev-parse", "--git-common-dir", cwd=law).strip()).resolve()
            if a.holder_tree is not None:
                tree = Path(td) / "holder"
                shutil.copytree(a.holder_tree, tree)
            else:
                tree = holder_tree(a.holder, Path(td))
            if not (tree / SCRIPT).is_file() or not (tree / PIN).is_file():
                print(f"holder_mode_check: {a.holder} has no {SCRIPT} or {PIN} (compared nothing)")
                return 2
            repin(tree, law, rev)
        except (subprocess.CalledProcessError, OSError, ValueError) as e:
            print(f"holder_mode_check: cannot prepare {a.holder}'s check: {e}")
            return 2
        env = dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"url.file://{common}.insteadOf",
                   GIT_CONFIG_VALUE_0=LAW_URL)
        r = subprocess.run(["bash", str(tree / SCRIPT), str(tree)], cwd=tree, env=env, capture_output=True, text=True,
                           encoding="utf-8")
    out = (r.stdout + r.stderr).strip()
    print(out)
    m = COMPARED.search(out)
    if r.returncode not in (0, 1):
        print(f"holder_mode_check: {a.holder}'s check failed to run (exit {r.returncode}) at {rev[:12]}")
        return 2
    if m is None or int(m.group(1)) == 0:
        print(f"holder_mode_check: {a.holder}'s check compared nothing at {rev[:12]}")
        return 2
    if r.returncode != 0:
        print(f"holder_mode_check: {a.holder}'s own check is red against {rev[:12]} (the change breaks that holder)")
        return 1
    print(f"holder_mode_check: {a.holder}'s own check passes against {rev[:12]} ({m.group(1)} files compared)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
