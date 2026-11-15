#!/usr/bin/env python3
"""The keyed public text check over the new commits of every public repository (ALICE-Law's daily run).

    public_text_commits_live.py [--owner NAME] [--days N] [--ack FILE] [--jobs N] [--today YYYY-MM-DD]
                                [--last-success YYYY-MM-DD] [--source DIR]
    public_text_commits_live.py --resolve ID [ID ...]      (on a machine with the name key; refused in CI)

A public repository's own CI runs the public text check without the name key (the unkeyed half only); the keyed
half runs at push, on a machine whose push hook has the key, and here. The tree of every default branch is read
daily by public_text_live.py; this reads the commits: every branch of every public repository the GitHub API
lists, each commit whose committer date is inside the window (the last --days days, 7 by default), with
public_text_check.py --commits under the key (message, added lines, added paths).

The output of CI is public, and repository names and commit ids are public and enumerable, so a hit is printed
as its kind and a keyed id only: HMAC-SHA256 under the name key of "<repository> NUL <commit, 12 hex> NUL <kind>".
Only a holder of the key can map an id back (--resolve, run on that machine). A hit in a commit can not be
removed by a later commit (it is in the history; rewriting public history is the user's decision), so a hit is
acknowledged in conformance/public-text-commit-ack.toml by its keyed id, citing that decision (the file's header
says who may add a row). A hit that is not acknowledged is a finding.

Gap guard: the run asks the API for the previous successful scheduled run of the Conformance (live) workflow. If
that run is older than the window less one day, the commits between were never read: the run is red (2). The
first run after this pass was added (FIRST_RUN) reads an explicit look-back of --days days and says so.

Known limits (the push hook is the only reader of these): a commit on a branch deleted or force-pushed away
inside the window before the daily run; a commit pushed inside the window whose committer date is older than the
window (an old local branch pushed late).

Exit 0 when every listed repository was read and no hit is new, 1 on a new hit, 2 when the key is missing, a
repository could not be read, no repository was listed, no commit was read although a repository was pushed to
inside the window, or the gap guard fires.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import hmac
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import holder_live as hl  # noqa: E402
import public_text_check as ptc  # noqa: E402

CHECK = HERE / "public_text_check.py"
ACK = HERE.parent / "conformance" / "public-text-commit-ack.toml"
GITHUB = "https://github.com/"
WORKFLOW = "conformance-live.yml"
# the day this pass was added: a previous successful run before it read no commits, so the next run is a first run
FIRST_RUN = datetime.date(2026, 11, 16)
SUMMARY_RE = re.compile(r"^public text: (\d+) findings in (\d+) commits", re.M)
FOUND_RE = re.compile(r"^found: (.*)$", re.M)
COMMIT_RE = re.compile(r"^commit ([0-9a-f]{12}) (.*)$")
ACK_RE = re.compile(r'^"([0-9a-f]{64})"\s*=\s*"([^"]+)"\s*$')
CHUNK = 200


class Unreadable(Exception):
    pass


def hit_id(key: bytes, repo: str, sha: str, kind: str) -> str:
    return hmac.new(key, f"{repo}\0{sha}\0{kind}".encode("utf-8"), hashlib.sha256).hexdigest()


def parse_hit(finding: str) -> tuple[str, str, str]:
    """(commit, kind, where) of a finding line of public_text_check.py --commits: "commit <12 hex> <where>: <kind>";
    a finding that names no commit (a settings entry) is ("-", the finding, "")"""
    m = COMMIT_RE.match(finding)
    if m is None:
        return "-", finding, ""
    rest = m.group(2)
    where, sep, kind = rest.rpartition(": ")
    return m.group(1), (kind if sep else rest), (where if sep else "")


def load_ack(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#") or s == "[acknowledged]":
            continue
        m = ACK_RE.match(s)
        if m is None or m.group(1) in out:
            raise Unreadable(f"ack line {n}: needs one '\"<64 hex keyed id>\" = \"<the decision it cites>\"' per hit")
        out[m.group(1)] = m.group(2)
    return out


def window_commits(g: Path, start: datetime.date) -> list[str]:
    out = hl.git(g, "rev-list", "--all", f"--since={start.isoformat()}T00:00:00Z")
    return [c for c in out.split() if c]


def check_commits(g: Path, name: str, commits: list[str], tip: str) -> tuple[list[str], int]:
    """(findings, commits read) of public_text_check.py --commits under the key, CHUNK commits per process"""
    found: list[str] = []
    read = 0
    for i in range(0, len(commits), CHUNK):
        r = subprocess.run([sys.executable, str(CHECK), "--repo", str(g), "--repo-name", name, "--tip", tip,
                            "--require-private-names", "--commits", *commits[i:i + CHUNK]],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = SUMMARY_RE.search(r.stdout)
        if r.returncode not in (0, 1) or m is None:
            tail = (r.stderr.strip().splitlines() or [""])[-1]
            raise Unreadable(f"{name}: public_text_check exited {r.returncode}: {tail[:200]}")
        lines = FOUND_RE.findall(r.stdout)
        if len(lines) != int(m.group(1)):
            raise Unreadable(f"{name}: {len(lines)} findings printed, the summary says {m.group(1)}")
        found += lines
        read += int(m.group(2))
    return found, read


def prefetch_blobs(g: Path, commits: list[str]) -> int:
    """fetch, in batches, the blobs the window's diffs read (both sides, every parent of a merge), so the checker
    does not fetch them one at a time; returns how many were asked for"""
    oids: set[str] = set()
    for c in commits:
        out = hl.git(g, "diff-tree", "-r", "-m", "--root", "--no-commit-id", "--no-renames", c)
        for line in out.splitlines():
            if not line.startswith(":"):
                continue
            meta = line[1:].split("\t", 1)[0].split()
            if len(meta) < 4 or "160000" in meta[:2]:
                continue  # a submodule's commit is not an object of this repository
            oids.update(o for o in meta[2:4] if o.strip("0"))
    have = set()
    if oids:
        r = subprocess.run(["git", "-C", str(g), "cat-file", "--batch-check=%(objectname)", "--buffer"],
                           input="\n".join(sorted(oids)), capture_output=True, text=True, encoding="utf-8",
                           env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
        have = {line.split()[0] for line in r.stdout.splitlines() if line and not line.endswith("missing")}
    want = sorted(oids - have)
    for i in range(0, len(want), CHUNK):
        hl.git(g, "-c", "fetch.negotiationAlgorithm=noop", "fetch", "-q", "--no-tags", "--no-write-fetch-head",
               "origin", *want[i:i + CHUNK])
    return len(want)


def read_repo(base: Path, repo: dict, start: datetime.date, source: Path | None
              ) -> tuple[str, list[str] | None, int, str | None]:
    """(name, findings or None when empty, commits read, error)"""
    name = repo["name"]
    if repo.get("size", 1) == 0:
        return name, None, 0, None
    g = base / name
    try:
        url = str(source / name) if source else GITHUB + repo["full_name"]
        # every branch, commits and trees, no blobs (no shallow edge); the commit-graph is off: with it, a partial
        # clone's on-demand fetch refuses objects the graph lists ("in the commit graph file but not in the object
        # database"), measured on 23 of 436 repositories
        r = subprocess.run(["git", "clone", "-q", "--bare", "--filter=blob:none", "--config", "core.commitGraph=false",
                            "--config", "fetch.writeCommitGraph=false", url, str(g)], capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise Unreadable(f"{name}: clone failed: {r.stderr.strip()[:200]}")
        commits = window_commits(g, start)
        if not commits:
            return name, [], 0, None
        if not source:
            prefetch_blobs(g, commits)
        tip = repo.get("default_branch") or "HEAD"
        found, read = check_commits(g, name, commits, tip)
        return name, found, read, None
    except (Unreadable, hl.Unreadable, subprocess.CalledProcessError, ValueError) as e:
        return name, None, 0, str(e)
    finally:
        shutil.rmtree(g, ignore_errors=True)


def last_success(owner: str) -> datetime.date | None:
    """the date of the previous successful scheduled run of the live workflow, None when there is none"""
    data = hl.api_get(f"{hl.API}/repos/{owner}/ALICE-Law/actions/workflows/{WORKFLOW}/runs"
                      "?event=schedule&status=success&per_page=1")
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        raise hl.Unreadable("the workflow run list is not readable")
    if not runs:
        return None
    return datetime.date.fromisoformat(runs[0]["created_at"][:10])


def source_repos(source: Path) -> list[dict]:
    """repositories from a local directory of git repositories (the tests), pushed_at = their newest commit"""
    out = []
    for d in sorted(p for p in source.iterdir() if (p / ".git").exists() or (p / "HEAD").exists()):
        when = subprocess.run(["git", "-C", str(d), "log", "-1", "--all", "--format=%cI"],
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()
        branch = subprocess.run(["git", "-C", str(d), "symbolic-ref", "--short", "HEAD"],
                                capture_output=True, text=True, encoding="utf-8").stdout.strip() or "main"
        out.append({"name": d.name, "full_name": d.name, "default_branch": branch, "size": 1, "pushed_at": when})
    return out


def scan(repos: list[dict], start: datetime.date, source: Path | None, jobs: int
         ) -> tuple[dict[str, list[str]], int, list[str], int]:
    read: dict[str, list[str]] = {}
    commits = empty = 0
    errors: list[str] = []
    with tempfile.TemporaryDirectory() as tmp, concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        for name, found, n, err in ex.map(lambda r: read_repo(Path(tmp), r, start, source), repos):
            if err:
                errors.append(err)
            elif found is None:
                empty += 1
            else:
                read[name] = found
                commits += n
    return read, commits, errors, empty


def zero_read_problem(commits: int, repos: list[dict], start: datetime.date) -> str | None:
    """the reason a run that read no commit is not a pass (a repository was pushed to inside the window), or None"""
    pushed = [r for r in repos if (r.get("pushed_at") or "")[:10] >= start.isoformat()]
    if commits == 0 and pushed:
        return f"0 commits read, but {len(pushed)} repositories were pushed to since {start.isoformat()}"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--ack", default=str(ACK))
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--today", help="the date of the run (YYYY-MM-DD, default today in UTC)")
    ap.add_argument("--last-success", help="the date of the previous successful run (YYYY-MM-DD; default: asked of "
                                           "the API; 'none' for no previous run)")
    ap.add_argument("--source", help="read repositories from this directory of git repositories (the tests)")
    ap.add_argument("--resolve", nargs="+", metavar="ID",
                    help="print the repository, commit and place of each keyed id (on a machine with the key)")
    ap.add_argument("--check-ack", action="store_true",
                    help="only parse the acknowledged-hit file (no key, no network; the ci/ branch check)")
    a = ap.parse_args(argv)
    if a.check_ack:
        try:
            rows = load_ack(Path(a.ack).read_text(encoding="utf-8"))
        except (Unreadable, OSError) as e:
            print(f"error: {a.ack}: {e}", file=sys.stderr)
            return 2
        print(f"public text commits: {Path(a.ack).name}: {len(rows)} acknowledged hit(s), every row well formed")
        return 0
    key = ptc.load_name_key()
    if key is None:
        print(f"error: the name key is not set ({ptc.NAME_KEY_ENV}): the keyed check can not run", file=sys.stderr)
        return 2
    if a.resolve and (os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS")):
        print("error: --resolve prints repositories and commits: it runs on a key holder's machine, not in CI",
              file=sys.stderr)
        return 2
    if a.days < 2:
        print("error: --days must be 2 or more (the window must overlap the daily runs)", file=sys.stderr)
        return 2
    today = (datetime.date.fromisoformat(a.today) if a.today
             else datetime.datetime.now(datetime.timezone.utc).date())
    start = today - datetime.timedelta(days=a.days)
    source = Path(a.source) if a.source else None
    try:
        repos = source_repos(source) if source else hl.public_repos(a.owner)
    except (hl.Unreadable, OSError) as e:
        print(f"error: couldn't read the repository list: {e}", file=sys.stderr)
        return 2
    if not repos:
        print("error: no public repository was listed", file=sys.stderr)
        return 2
    if not a.resolve:
        try:
            if a.last_success == "none":
                last = None
            elif a.last_success:
                last = datetime.date.fromisoformat(a.last_success)
            else:
                last = last_success(a.owner)
        except (hl.Unreadable, ValueError, KeyError) as e:
            print(f"error: couldn't read the previous run: {e}", file=sys.stderr)
            return 2
        if last is None or last < FIRST_RUN:
            print(f"public text commits: first run of this pass: an explicit look-back of {a.days} days "
                  f"(from {start.isoformat()}); older commits are read only by the tree pass and the push hook")
        elif (today - last).days > a.days - 1:
            print(f"error: the previous successful run was {last.isoformat()}, more than {a.days - 1} days ago: the "
                  f"commits between {last.isoformat()} and {start.isoformat()} were not read", file=sys.stderr)
            return 2
    read, commits, errors, empty = scan(repos, start, source, a.jobs)
    for e in errors:
        print(f"couldn't read: {e.split(':', 1)[0]}" if not a.resolve else f"couldn't read: {e}")
    hits = []
    for name, found in read.items():
        for f in found:
            sha, kind, where = parse_hit(f)
            hits.append((hit_id(key, name, sha, kind), kind, name, sha, where))
    if a.resolve:
        want = set(a.resolve)
        for hid, kind, name, sha, where in hits:
            if any(hid.startswith(w) for w in want):
                print(f"{hid[:16]} {name} {sha} {where}: {kind}")
        return 0
    try:
        ack = load_ack(Path(a.ack).read_text(encoding="utf-8"))
    except (OSError, Unreadable) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    new = [(hid, kind) for hid, kind, *_ in hits if hid not in ack]
    print(f"public text commits: {len(repos)} public repositories, {len(read)} read, {empty} empty, {len(errors)} "
          f"unread, {commits} commits read since {start.isoformat()} ({a.days} days), {len(hits)} hits, "
          f"{len(hits) - len(new)} acknowledged, {len(new)} new")
    print("public text commits: not read here: commits on branches deleted or force-pushed away inside the window "
          "before this run, and commits with a committer date older than the window (the push hook reads those)")
    if errors or not read:
        print("error: not every public repository was read (no result is given for a partial read)", file=sys.stderr)
        return 2
    problem = zero_read_problem(commits, repos, start)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    for hid, kind in new:
        print(f"found: {kind} {hid[:16]}")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
