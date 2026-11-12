#!/usr/bin/env python3
"""The branch-main check (branch_main_check.py) over every public repository of the account, at its default branch.

    branch_main_live.py [--owner NAME] [--local NAME=DIR] [--jobs N]

The repositories are the ones the GitHub API lists as public at run time (holder_live.public_repos). Each default
branch is fetched at depth 1 without blobs, and only its Cargo.toml / Cargo.lock files are read. --local NAME=DIR
reads repository NAME (the one a CI run tests) from the checkout DIR instead; NAME must be one of the listed public
repositories. There is no baseline: every alice-* dependency not from its repository's main branch is red, except
in a file conformance/branch-main-exempt.toml names (repo, path, reason: a published version pinned on purpose,
such as a compat reader of an old format). A row whose file has no finding fails, so the table cannot go stale.

Exit 0 when every repository was read and nothing was found, 1 on a finding, 2 when a repository could not be read,
no repository was listed, or no manifest or lock was read.
"""
from __future__ import annotations

import argparse
import tomllib
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import branch_main_check as bmc  # noqa: E402
import holder_live as hl  # noqa: E402

GITHUB = "https://github.com/"
EXEMPT = HERE.parent / "conformance" / "branch-main-exempt.toml"


def load_exempt(path: Path = EXEMPT) -> dict[tuple[str, str], str]:
    """{(repo, path): reason} of the exemption table; a row without a reason is refused"""
    rows = tomllib.loads(path.read_text(encoding="utf-8")).get("exempt", [])
    out = {}
    for r in rows:
        if not (isinstance(r.get("repo"), str) and isinstance(r.get("path"), str) and str(r.get("reason", "")).strip()):
            raise ValueError(f"{path.name}: a row needs repo, path and a reason: {r!r}")
        if (r["repo"], r["path"]) in out:
            raise ValueError(f"{path.name}: two rows for {r['repo']}: {r['path']}")
        out[(r["repo"], r["path"])] = r["reason"]
    return out


def apply_exempt(found: list[str], exempt: dict[tuple[str, str], str]) -> tuple[list[str], list[str], int]:
    """(findings left, unused rows, findings exempted); a finding reads `<repo>: <path>: <what>`"""
    left, used, n = [], set(), 0
    for f in found:
        repo, path, _ = f.split(": ", 2)
        if (repo, path) in exempt:
            used.add((repo, path))
            n += 1
        else:
            left.append(f)
    unused = [f"{repo}: {path}: exemption row with no finding (remove it)" for repo, path in sorted(set(exempt) - used)]
    return left, unused, n


def read_repo(base: Path, repo: dict) -> tuple[str, list[str] | None, int, str | None]:
    """(name, findings or None when the repository is empty, files read, error)"""
    name = repo["name"]
    if repo.get("size", 1) == 0:
        return name, None, 0, None
    g = base / name
    try:
        subprocess.run(["git", "init", "-q", str(g)], check=True, capture_output=True)
        hl.git(g, "remote", "add", "origin", GITHUB + repo["full_name"])
        hl.git(g, "fetch", "-q", "--depth", "1", "--no-tags", "--filter=blob:none", "origin",
               f"+refs/heads/{repo['default_branch']}:refs/remotes/origin/main")
        paths = [p for p in hl.git(g, "ls-tree", "-r", "-z", "--name-only", "origin/main").split("\0")
                 if p.rsplit("/", 1)[-1] in ("Cargo.toml", "Cargo.lock")]
        files = {p: hl.git(g, "show", f"origin/main:{p}") for p in paths}
        found, n = bmc.check_files(files)
        return name, [f"{name}: {f}" for f in found], n, None
    except (hl.Unreadable, subprocess.CalledProcessError, ValueError) as e:
        return name, None, 0, str(e)
    finally:
        shutil.rmtree(g, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--local", help="NAME=DIR: read repository NAME from the checkout DIR")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--check-table", action="store_true",
                    help="only read the exemption table (each row with repo, path and a reason, no two rows for a file)")
    a = ap.parse_args(argv)
    if a.check_table:
        try:
            rows = load_exempt(EXEMPT)
        except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
            print(f"branch main live: {e}")
            return 2
        print(f"branch main live: {EXEMPT.name}: {len(rows)} row(s), each with a reason")
        return 0
    try:
        repos = hl.public_repos(a.owner)
    except hl.Unreadable as e:
        print(f"branch main live: {e}")
        return 2
    if not repos:
        print("branch main live: no public repository listed (compared nothing)")
        return 2
    local_name, local_dir = (a.local.split("=", 1) if a.local else (None, None))
    if local_name is not None and local_name not in {r["name"] for r in repos}:
        print(f"branch main live: --local {local_name} is not a listed public repository")
        return 2
    with tempfile.TemporaryDirectory() as base:
        todo = [r for r in repos if r["name"] != local_name]
        with ThreadPoolExecutor(a.jobs) as ex:
            results = list(ex.map(lambda r: read_repo(Path(base), r), todo))
    if local_name is not None:
        found, n = bmc.check_repo(Path(local_dir))
        results.append((local_name, [f"{local_name}: {f}" for f in found], n, None))
    found = [f for _, fs, _, _ in results if fs for f in fs]
    try:
        exempt = load_exempt(EXEMPT)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        print(f"branch main live: {e}")
        return 2
    found, unused, exempted = apply_exempt(found, exempt)
    found += unused
    errors = [(n, e) for n, _, _, e in results if e]
    empty = sum(1 for _, fs, _, e in results if fs is None and e is None)
    files = sum(n for _, _, n, _ in results)
    for f in sorted(found):
        print(f"found: {f}")
    for n, e in errors:
        print(f"unread: {n}: {e[:200]}")
    print(f"branch main live: {len(repos)} public repositories, {len(results) - len(errors) - empty} read, {empty} empty, "
          f"{len(errors)} unread, {files} manifest / lock file(s) read, {exempted} exempted by "
          f"{len(exempt)} row(s), {len(found)} finding(s) (alice-* not from a main branch, or an unused exemption)")
    if errors or files == 0:
        return 2
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
