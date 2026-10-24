#!/usr/bin/env python3
"""The rights-holder rule of licence_files_check.py over every public repository of the account, at its default branch.

    holder_live.py [--owner NAME] [--baseline FILE] [--write] [--jobs N]

Lists the owner's public repositories (GitHub REST, with GITHUB_TOKEN / GH_TOKEN when set, retrying on rate limits
and server errors), fetches each default branch without blobs, reads only the rights statements and manifests, and
compares the occurrences with the baseline (conformance/holder-baseline.toml): an occurrence not listed is a finding,
a listed one that is gone (or whose repository is no longer public) is a finding (stale), so the list only goes down.
--write rewrites the baseline from what was read (used when the list is first made or when entries are removed).
Exit 0 when every repository was read and nothing differs, 1 on a finding, 2 when a repository could not be read,
no repository was listed, or no file was read.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import licence_files_check as lfc  # noqa: E402

API = "https://api.github.com"
GITHUB = "https://github.com/"


class Unreadable(Exception):
    pass


def api_get(url: str, tries: int = 6) -> object:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "holder-live"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    delay = 5.0
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            retry = e.code in (403, 429) or e.code >= 500
            if not retry or attempt == tries - 1:
                raise Unreadable(f"{url}: HTTP {e.code}") from e
            reset = e.headers.get("Retry-After") or ""
            time.sleep(float(reset) if reset.isdigit() else delay)
            delay *= 2
        except urllib.error.URLError as e:
            if attempt == tries - 1:
                raise Unreadable(f"{url}: {e.reason}") from e
            time.sleep(delay)
            delay *= 2
    raise Unreadable(url)


def public_repos(owner: str) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        batch = api_get(f"{API}/users/{owner}/repos?type=owner&per_page=100&page={page}")
        if not isinstance(batch, list):
            raise Unreadable(f"listing {owner}: not a list")
        out += [r for r in batch if not r.get("private")]
        if len(batch) < 100:
            return out
        page += 1


def git(g: Path, *args: str, binary: bool = False):
    r = subprocess.run(["git", "-C", str(g), *args], capture_output=True, text=not binary)
    if r.returncode != 0:
        raise Unreadable(f"{g.name}: git {' '.join(args[:2])} failed: {(r.stderr or b'')!s:.200}")
    return r.stdout


def scan_git(g: Path, ref: str) -> tuple[set[tuple[str, str]], int]:
    """(occurrences, files read) of a blob-less clone at ref"""
    paths = [p for p in git(g, "ls-tree", "-r", "-z", "--name-only", ref).split("\0") if p]
    wanted = [p for p in paths if lfc.read_paths(p) and (lfc.RIGHTS_FILE_RE.match(p.rsplit("/", 1)[-1])
                                                           or p.rsplit("/", 1)[-1] in ("Cargo.toml", "pyproject.toml"))]
    if wanted:
        oids = [git(g, "rev-parse", f"{ref}:{p}").strip() for p in wanted]
        for i in range(0, len(oids), 200):
            git(g, "-c", "fetch.negotiationAlgorithm=noop", "fetch", "-q", "--no-tags", "--no-write-fetch-head",
                "origin", *oids[i:i + 200])
    occ = lfc.holder_occurrences(wanted, lambda p: git(g, "show", f"{ref}:{p}", binary=True))
    return occ, len(wanted)


def read_repo(base: Path, repo: dict) -> tuple[str, set[tuple[str, str]] | None, int, str | None]:
    """(name, occurrences or None when the repository is empty, files read, error)"""
    name = repo["name"]
    if repo.get("size", 1) == 0:
        return name, None, 0, None
    g = base / name
    try:
        subprocess.run(["git", "init", "-q", str(g)], check=True, capture_output=True)
        git(g, "remote", "add", "origin", GITHUB + repo["full_name"])
        branch = repo["default_branch"]
        git(g, "fetch", "-q", "--filter=blob:none", "--depth", "1", "origin", f"+refs/heads/{branch}:refs/remotes/origin/main")
        occ, n = scan_git(g, "origin/main")
        return name, occ, n, None
    except Unreadable as e:
        return name, None, 0, str(e)


def compare_all(read: dict[str, set[tuple[str, str]]],
                baseline: dict[str, set[tuple[str, str]]]) -> list[str]:
    """findings: a new occurrence, a listed one that is gone, an entry for a repository that was not listed"""
    findings = []
    keys = {}
    for name, occ in sorted(read.items()):
        keys[lfc.repo_key(name)] = name
        new, stale = lfc.compare_baseline(name, occ, baseline)
        findings += [f"{name}: {kind} {path} (not in the baseline)" for kind, path in sorted(new)]
        findings += [f"{name}: stale baseline entry {kind} {path} (fixed: remove it)" for kind, path in sorted(stale)]
    for key in sorted(set(baseline) - set(keys)):
        findings += [f"repository {key[:12]}: stale baseline entry {kind} {path} (the repository is not public or "
                     "has no content)" for kind, path in sorted(baseline[key])]
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--baseline", default=str(lfc.HOLDER_BASELINE))
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args(argv)
    try:
        repos = public_repos(a.owner)
    except Unreadable as e:
        print(f"error: couldn't read the repository list: {e}", file=sys.stderr)
        return 2
    if not repos:
        print("error: no public repository was listed", file=sys.stderr)
        return 2
    read: dict[str, set[tuple[str, str]]] = {}
    errors: list[str] = []
    empty = files = 0
    with tempfile.TemporaryDirectory() as tmp, concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for name, occ, n, err in ex.map(lambda r: read_repo(Path(tmp), r), repos):
            if err:
                errors.append(err)
            elif occ is None:
                empty += 1
            else:
                read[name] = occ
                files += n
    for e in errors:
        print(f"couldn't read: {e}")
    print(f"holder live: {len(repos)} public repositories, {len(read)} read, {empty} empty, {len(errors)} unread, "
          f"{files} files read, {sum(len(v) for v in read.values())} occurrences")
    if errors or not read or files == 0:
        print("error: not every public repository was read (no result is given for a partial read)", file=sys.stderr)
        return 2
    if a.write:
        Path(a.baseline).write_text(lfc.dump_baseline({lfc.repo_key(n): o for n, o in read.items() if o}), encoding="utf-8")
        print(f"wrote {a.baseline}")
        return 0
    baseline = lfc.load_baseline(Path(a.baseline).read_text(encoding="utf-8"))
    findings = compare_all(read, baseline)
    for f in findings:
        print(f"found: {f}")
    print(f"holder live: {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
