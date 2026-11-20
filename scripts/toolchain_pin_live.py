#!/usr/bin/env python3
"""The toolchain pin check (toolchain_pin_check.py) over every public repository of the account, at its default branch.

    toolchain_pin_live.py [--owner NAME] [--baseline FILE] [--local NAME=DIR] [--write] [--jobs N]

The repositories are the ones the GitHub API lists as public at run time (holder_live.public_repos). Each default
branch is fetched at depth 1 without blobs, and only its .github/workflows/*.yml / *.yaml are read. --local
NAME=DIR reads repository NAME (the one a CI run tests) from the checkout DIR instead.

The findings are counted per "<repository>/<workflow>" and compared with the baseline
(conformance/toolchain-pin-baseline.toml, written when the check was added): more than the baseline is a finding
(a new commit pin), fewer is a finding too (stale: lower the row, or drop it at 0, with --write),
so the baseline only goes down. --write rewrites the baseline from what was read (only after the read was whole).

Exit 0 when every repository was read and the counts equal the baseline, 1 on a difference, 2 when a repository
could not be read, no repository was listed, or no pinned step was compared across all repositories.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import holder_live as hl  # noqa: E402
import toolchain_pin_check as tpc  # noqa: E402

GITHUB = "https://github.com/"
BASELINE = HERE.parent / "conformance" / "toolchain-pin-baseline.toml"
HEADER = """# Workflows of public repositories that pin dtolnay/rust-toolchain to a commit (scripts/toolchain_pin_live.py).
# Each row is "<repository>/<workflow>" = the number of such steps when the check was added; the daily run is red on
# any count above its row (a new one) and on any count below it (stale: lower or drop the row with --write). Fix a
# row by installing the toolchain with a rustup run step (`rustup toolchain install <channel> --profile minimal`).
"""


def read_repo(base: Path, repo: dict) -> tuple[str, Counter | None, int, str | None]:
    """(name, findings per workflow or None when the repository is empty, pinned steps compared, error)"""
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
        counts, compared = Counter(), 0
        for p in names:
            found, n = tpc.findings(p.rsplit("/", 1)[-1], hl.git(g, "show", f"origin/main:{p}"))
            compared += n
            if found:
                counts[f"{name}/{p.rsplit('/', 1)[-1]}"] += len(found)
        return name, counts, compared, None
    except (hl.Unreadable, subprocess.CalledProcessError) as e:
        return name, None, 0, str(e)
    finally:
        shutil.rmtree(g, ignore_errors=True)


def read_local(name: str, d: Path) -> tuple[str, Counter, int, None]:
    wf = d / ".github" / "workflows"
    files = sorted(p for p in wf.glob("*") if p.suffix in (".yml", ".yaml")) if wf.is_dir() else []
    counts, compared = Counter(), 0
    for p in files:
        found, n = tpc.findings(p.name, p.read_text(encoding="utf-8", errors="replace"))
        compared += n
        if found:
            counts[f"{name}/{p.name}"] += len(found)
    return name, counts, compared, None


def load_baseline(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    return {k: int(v) for k, v in tomllib.loads(path.read_text(encoding="utf-8")).get("pins", {}).items()}


ROW = __import__("re").compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.ya?ml$")


def lint_baseline(path: Path) -> list[str]:
    """problems of the baseline's form, read without the network: no [pins] rows, a key not <repository>/<workflow>,
    a count not above 0 (a fixed row is dropped, not kept at 0)"""
    try:
        rows = tomllib.loads(path.read_text(encoding="utf-8")).get("pins", {})
    except (OSError, tomllib.TOMLDecodeError) as e:
        return [f"{path}: unreadable: {e}"]
    if not rows:
        return [f"{path}: no [pins] row (an empty baseline compares nothing)"]
    out = [f"{path}: {k}: not <repository>/<workflow>" for k in rows if not ROW.match(k)]
    out += [f"{path}: {k}: count {v} (must be above 0)" for k, v in rows.items() if not isinstance(v, int) or v <= 0]
    return out


def write_baseline(path: Path, counts: Counter) -> None:
    rows = "".join(f'"{k}" = {v}\n' for k, v in sorted(counts.items()) if v > 0)
    path.write_text(HEADER + "[pins]\n" + rows, encoding="utf-8")


def compare(counts: Counter, base: dict[str, int]) -> list[str]:
    out = []
    for k in sorted(set(counts) | set(base)):
        have, want = counts.get(k, 0), base.get(k, 0)
        if have > want:
            out.append(f"found: {k}: {have} step(s) pinning dtolnay/rust-toolchain to a commit (baseline {want})")
        elif have < want:
            out.append(f"stale: {k}: {have} now, baseline {want} (lower the row with --write)")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--local", help="NAME=DIR: read repository NAME from the checkout DIR")
    ap.add_argument("--write", action="store_true", help="rewrite the baseline from this read")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--lint-baseline", action="store_true", help="only check the baseline's form (no network)")
    a = ap.parse_args(argv)
    if a.lint_baseline:
        problems = lint_baseline(a.baseline)
        for x in problems:
            print(f"baseline: {x}")
        print(f"toolchain pin baseline: {len(load_baseline(a.baseline)) if not problems else 0} rows, {len(problems)} problem(s)")
        return 1 if problems else 0
    try:
        repos = hl.public_repos(a.owner)
    except hl.Unreadable as e:
        print(f"toolchain pin live: {e}")
        return 2
    if not repos:
        print("toolchain pin live: no public repository listed (compared nothing)")
        return 2
    local_name, local_dir = (a.local.split("=", 1) if a.local else (None, None))
    if local_name is not None and local_name not in {r["name"] for r in repos}:
        print(f"toolchain pin live: --local {local_name} is not a listed public repository")
        return 2
    with tempfile.TemporaryDirectory() as base:
        todo = [r for r in repos if r["name"] != local_name]
        with ThreadPoolExecutor(a.jobs) as ex:
            results = list(ex.map(lambda r: read_repo(Path(base), r), todo))
    if local_name is not None:
        results.append(read_local(local_name, Path(local_dir)))
    errors = [(n, e) for n, _, _, e in results if e]
    empty = sum(1 for _, c, _, e in results if c is None and e is None)
    compared = sum(n for _, _, n, _ in results)
    counts = Counter()
    for _, c, _, _ in results:
        if c:
            counts.update(c)
    for n, e in errors:
        print(f"unread: {n}: {e[:200]}")
    print(f"toolchain pin live: {len(repos)} public repositories, {len(results) - len(errors) - empty} read, {empty} empty, "
          f"{len(errors)} unread, {compared} pinned dtolnay/rust-toolchain steps, "
          f"in {len(counts)} workflow(s)")
    if errors or compared == 0:
        return 2
    if a.write:
        write_baseline(a.baseline, counts)
        print(f"toolchain pin live: wrote {a.baseline} ({len(counts)} rows)")
        return 0
    diff = compare(counts, load_baseline(a.baseline))
    for line in diff:
        print(line)
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main())
