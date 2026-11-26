#!/usr/bin/env python3
"""The branch-main check (branch_main_check.py) over every public repository of the account, at its default branch.

    branch_main_live.py [--owner NAME] [--local NAME=DIR] [--jobs N]

The repositories are the ones the GitHub API lists as public at run time (holder_live.public_repos). Each default
branch is fetched at depth 1 without blobs, and only its Cargo.toml / Cargo.lock files are read. --local NAME=DIR
reads repository NAME (the one a CI run tests) from the checkout DIR instead; NAME must be one of the listed public
repositories. There is no baseline: every alice-* dependency not from its repository's main branch is red, except
in a file conformance/branch-main-exempt.toml names (repo, path, reason: a published version pinned on purpose,
such as a compat reader of an old format), and, for a coordinated move that pins a candidate commit for a short
while, the findings a window row names (repo, path, dependency, rev, expires). A row whose file has no finding
fails, and a window row past its expiry fails, so the table cannot go stale.

Exit 0 when every repository was read and nothing was found, 1 on a finding, 2 when a repository could not be read,
no repository was listed, or no manifest or lock was read.
"""
from __future__ import annotations

import argparse
import re
import tomllib
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import branch_main_check as bmc  # noqa: E402
import holder_live as hl  # noqa: E402

GITHUB = "https://github.com/"
EXEMPT = HERE.parent / "conformance" / "branch-main-exempt.toml"


REV_RE = re.compile(r"[0-9a-f]{40}")


def load_exempt(path: Path = EXEMPT, now: datetime | None = None) -> dict[tuple[str, ...], str]:
    """{key: reason} of the exemption table; a row without a reason, or a window row that has expired, is refused

    A row names a file (repo, path) and exempts its findings. A window row also names `dependency`, `rev` (the full
    commit) and `expires` (an ISO 8601 time with its offset): it exempts only the findings of that file that name that
    dependency at that commit, for a coordinated move that pins a candidate commit for a short while, and it fails the
    check once `expires` has passed. Its key is (repo, path, dependency, rev); a plain row's key is (repo, path).
    """
    rows = tomllib.loads(path.read_text(encoding="utf-8")).get("exempt", [])
    now = now or datetime.now(timezone.utc)
    out: dict[tuple[str, ...], str] = {}
    for r in rows:
        if not (isinstance(r.get("repo"), str) and isinstance(r.get("path"), str) and str(r.get("reason", "")).strip()):
            raise ValueError(f"{path.name}: a row needs repo, path and a reason: {r!r}")
        window = [k for k in ("dependency", "rev", "expires") if k in r]
        if window and len(window) != 3:
            raise ValueError(f"{path.name}: a window row needs dependency, rev and expires together: {r!r}")
        if window:
            if not (isinstance(r["dependency"], str) and r["dependency"].startswith("alice-")):
                raise ValueError(f"{path.name}: a window row's dependency is an alice-* crate: {r!r}")
            if not (isinstance(r["rev"], str) and REV_RE.fullmatch(r["rev"])):
                raise ValueError(f"{path.name}: a window row's rev is a full 40-hex commit: {r!r}")
            try:
                expires = datetime.fromisoformat(str(r["expires"]))
            except ValueError as e:
                raise ValueError(f"{path.name}: a window row's expires is not an ISO 8601 time: {r!r}") from e
            if expires.tzinfo is None:
                raise ValueError(f"{path.name}: a window row's expires needs its offset: {r!r}")
            if now > expires:
                raise ValueError(f"{path.name}: window row expired at {r['expires']} (remove it): "
                                 f"{r['repo']}: {r['path']}: {r['dependency']} {r['rev']}")
            key: tuple[str, ...] = (r["repo"], r["path"], r["dependency"], r["rev"])
        else:
            key = (r["repo"], r["path"])
        if key in out:
            raise ValueError(f"{path.name}: two rows for {' / '.join(key)}")
        out[key] = r["reason"]
    plain = {k for k in out if len(k) == 2}
    for k in out:
        if len(k) == 4 and k[:2] in plain:
            raise ValueError(f"{path.name}: {k[0]}: {k[1]} has a file row and a window row (the file row covers it)")
    return out


def _names(what: str, dependency: str, rev: str) -> bool:
    """whether a finding's text names `dependency` at commit `rev` (a manifest row or a lock package)"""
    return rev in what and re.search(rf"(^|[\s\]]){re.escape(dependency)}[\s:]", what) is not None


def apply_exempt(found: list[str], exempt: dict[tuple[str, ...], str]) -> tuple[list[str], list[str], int]:
    """(findings left, unused rows, findings exempted); a finding reads `<repo>: <path>: <what>`"""
    left, used, n = [], set(), 0
    windows = [k for k in exempt if len(k) == 4]
    for f in found:
        repo, path, what = f.split(": ", 2)
        hit = (repo, path) if (repo, path) in exempt else next(
            (k for k in windows if k[:2] == (repo, path) and _names(what, k[2], k[3])), None)
        if hit is not None:
            used.add(hit)
            n += 1
        else:
            left.append(f)
    unused = [f"{': '.join(k[:2])}: exemption row with no finding (remove it)" if len(k) == 2
              else f"{': '.join(k[:2])}: window row for {k[2]} {k[3]} with no finding (remove it)"
              for k in sorted(set(exempt) - used)]
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
