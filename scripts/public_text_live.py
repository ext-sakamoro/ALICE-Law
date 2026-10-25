#!/usr/bin/env python3
"""The public text check (public_text_check.py --tree) over every public repository of the account, at its default branch.

    public_text_live.py [--owner NAME] [--baseline FILE] [--local NAME=DIR] [--write | --shrink] [--jobs N]

A repository that has no public-text workflow of its own is read here too, so the internal vocabulary
cannot come back through a repository that never adopted the check. The repositories are the ones the
GitHub API lists as public at run time (holder_live.public_repos): a repository that turns public is
read on the next run, a private one never. Each default branch is fetched at depth 1 and checked with
public_text_check.py --tree in a process of its own (its allow-list and settings are that repository's).

The findings are compared with the baseline (conformance/public-text-baseline.toml) by count: each key is
sha256(<repository> NUL <finding without its line number>), so the baseline names neither a repository nor
a path. More findings of a key than the baseline has is a finding, fewer is a finding (stale: lower it with
--shrink), so the list only goes down. --write rewrites the baseline from what was read (used when the list
is first made). --local NAME=DIR reads repository NAME (the one a CI run tests) from the checkout DIR at its
HEAD instead of its default branch; NAME must be one of the listed public repositories.
The output names the repository and the place and kind of each new finding, never the word (the output of
CI is public too).
Exit 0 when every repository was read and nothing differs, 1 on a finding, 2 when a repository could not be
read, no repository was listed, or no file was compared.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import holder_live as hl  # noqa: E402

CHECK = HERE / "public_text_check.py"
BASELINE = HERE.parent / "conformance" / "public-text-baseline.toml"
GITHUB = "https://github.com/"
LINE_RE = re.compile(r":\d+:")
COMPARED_RE = re.compile(r"^public text: (\d+) findings in (\d+) files at ", re.M)
ENTRY_RE = re.compile(r'^"([0-9a-f]{64})"\s*=\s*([1-9][0-9]*)\s*$')


class Unreadable(Exception):
    pass


def place(finding: str) -> str:
    """a finding without its line numbers: the same word moving within a file is the same entry"""
    return LINE_RE.sub(":", finding)


def key(repo: str, finding: str) -> str:
    return hashlib.sha256(f"{repo}\0{place(finding)}".encode("utf-8")).hexdigest()


def check(g: Path, name: str, ref: str) -> tuple[list[str], int]:
    """(findings, files compared) of public_text_check.py --tree on the checkout g at ref"""
    r = subprocess.run([sys.executable, str(CHECK), "--repo", str(g), "--repo-name", name, "--tree", ref],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = COMPARED_RE.search(r.stdout)
    if r.returncode not in (0, 1) or m is None:
        tail = (r.stderr.strip().splitlines() or [""])[-1]
        raise Unreadable(f"{name}: public_text_check exited {r.returncode}: {tail[:200]}")
    found = [line[len("found: "):] for line in r.stdout.splitlines() if line.startswith("found: ")]
    if len(found) != int(m.group(1)):
        raise Unreadable(f"{name}: {len(found)} findings printed, the summary says {m.group(1)}")
    return found, int(m.group(2))


def read_repo(base: Path, repo: dict) -> tuple[str, list[str] | None, int, str | None]:
    """(name, findings or None when the repository is empty, files compared, error)"""
    name = repo["name"]
    if repo.get("size", 1) == 0:
        return name, None, 0, None
    g = base / name
    try:
        subprocess.run(["git", "init", "-q", str(g)], check=True, capture_output=True)
        hl.git(g, "remote", "add", "origin", GITHUB + repo["full_name"])
        hl.git(g, "fetch", "-q", "--depth", "1", "--no-tags", "origin",
               f"+refs/heads/{repo['default_branch']}:refs/remotes/origin/main")
        found, n = check(g, name, "origin/main")
        return name, found, n, None
    except (Unreadable, hl.Unreadable, subprocess.CalledProcessError) as e:
        return name, None, 0, str(e)
    finally:
        # the findings are read: the clone goes now, so the run holds one clone per job, not one per repository
        shutil.rmtree(g, ignore_errors=True)


def counts(read: dict[str, list[str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for name, found in read.items():
        for f in found:
            k = key(name, f)
            out[k] = out.get(k, 0) + 1
    return out


def load_baseline(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#") or s == "[findings]":
            continue
        m = ENTRY_RE.match(s)
        if m is None or m.group(1) in out:
            raise Unreadable(f"baseline line {n}: needs one '\"<sha256>\" = <count ≥ 1>' per key")
        out[m.group(1)] = int(m.group(2))
    return out


def dump_baseline(b: dict[str, int]) -> str:
    head = ("# Generated by scripts/public_text_live.py --write / --shrink; edit by --shrink only.\n"
            "# sha256(<repository> NUL <finding without its line number>) = the number of findings at that place.\n"
            "[findings]\n")
    return head + "".join(f'"{k}" = {v}\n' for k, v in sorted(b.items()))


def compare(read: dict[str, list[str]], baseline: dict[str, int]) -> list[str]:
    """findings: more at a place than the baseline has (new), fewer (stale)"""
    out = []
    got = counts(read)
    seen: set[str] = set()
    for name, found in sorted(read.items()):
        for f in dict.fromkeys(found):
            k = key(name, f)
            if k in seen:
                continue
            seen.add(k)
            if got[k] > baseline.get(k, 0):
                out.append(f"{name}: {place(f)} ({got[k]}, the baseline has {baseline.get(k, 0)})")
    for k in sorted(baseline):
        if got.get(k, 0) < baseline[k]:
            out.append(f"stale baseline entry {k[:12]}: {got.get(k, 0)} found, the baseline has {baseline[k]} "
                       "(fixed: lower it with --shrink)")
    return out


def shrink(baseline: dict[str, int], read: dict[str, list[str]]) -> dict[str, int]:
    """each entry lowered to what was found; never one raised or added"""
    got = counts(read)
    return {k: min(v, got.get(k, 0)) for k, v in baseline.items() if got.get(k, 0) > 0}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--baseline", default=str(BASELINE))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true")
    g.add_argument("--shrink", action="store_true")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--local", help="NAME=DIR: read repository NAME from the checkout DIR at HEAD")
    a = ap.parse_args(argv)
    local = None
    if a.local is not None:
        name, sep, d = a.local.partition("=")
        if not name or not sep or not d:
            print(f"error: --local {a.local!r}: needs NAME=DIR", file=sys.stderr)
            return 2
        local = (name, Path(d))
    try:
        repos = hl.public_repos(a.owner)
    except hl.Unreadable as e:
        print(f"error: couldn't read the repository list: {e}", file=sys.stderr)
        return 2
    if not repos:
        print("error: no public repository was listed", file=sys.stderr)
        return 2
    if local is not None and local[0] not in {r["name"] for r in repos}:
        print(f"error: --local names {local[0]}, which is not a listed public repository", file=sys.stderr)
        return 2
    read: dict[str, list[str]] = {}
    errors: list[str] = []
    empty = files = 0
    if local is not None:
        try:
            found, n = check(local[1], local[0], "HEAD")
        except Unreadable as e:
            print(f"error: couldn't read the checkout of {local[0]}: {e}", file=sys.stderr)
            return 2
        print(f"public text live: {local[0]} read from its checkout at HEAD ({n} files)")
        read[local[0]] = found
        files += n
    others = [r for r in repos if local is None or r["name"] != local[0]]
    with tempfile.TemporaryDirectory() as tmp, concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for name, found, n, err in ex.map(lambda r: read_repo(Path(tmp), r), others):
            if err:
                errors.append(err)
            elif found is None:
                empty += 1
            else:
                read[name] = found
                files += n
    for e in errors:
        print(f"couldn't read: {e}")
    print(f"public text live: {len(repos)} public repositories, {len(read)} read, {empty} empty, {len(errors)} unread, "
          f"{files} files compared, {sum(len(v) for v in read.values())} findings")
    if errors or not read or files == 0:
        print("error: not every public repository was read (no result is given for a partial read)", file=sys.stderr)
        return 2
    if a.write:
        Path(a.baseline).write_text(dump_baseline(counts(read)), encoding="utf-8")
        print(f"wrote {a.baseline}")
        return 0
    try:
        baseline = load_baseline(Path(a.baseline).read_text(encoding="utf-8"))
    except (OSError, Unreadable) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.shrink:
        shrunk = shrink(baseline, read)
        removed = sum(baseline.values()) - sum(shrunk.values())
        Path(a.baseline).write_text(dump_baseline(shrunk), encoding="utf-8")
        print(f"shrank {a.baseline}: {removed} findings removed")
        return 0
    findings = compare(read, baseline)
    for f in findings:
        print(f"found: {f}")
    print(f"public text live: {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
