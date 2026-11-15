#!/usr/bin/env python3
"""The keyed public text check over the new commits of every public repository (ALICE-Law's daily run).

    public_text_commits_live.py [--read-out FILE] [--owner NAME] [--days N] [--jobs N] [--today YYYY-MM-DD]
                                [--last-success YYYY-MM-DD|none] [--since YYYY-MM-DD] [--previous FILE|none]
                                [--source DIR]
    public_text_commits_live.py --judge FILE [--ack FILE]   (the hits a read wrote, against the acknowledged ones)
    public_text_commits_live.py --check-ack [--ack FILE]   (only parse the acknowledgement file)
    public_text_commits_live.py --resolve ID [ID ...]      (on a machine with the name key; refused in CI)

A public repository's own CI runs the public text check without the name key (the unkeyed half only); the keyed
half runs at push, on a machine whose push hook has the key, and here. The tree of every default branch is read
daily by public_text_live.py; this reads the commits: every branch of every public repository the GitHub API
lists, each commit whose committer date is inside the window (see below), with
public_text_check.py --commits under the key (message, added lines, added paths).

The output of CI is public, and repository names and commit ids are public and enumerable, so a hit is printed
as its kind and a keyed id only: HMAC-SHA256 under the name key of "<repository> NUL <commit, 12 hex> NUL <kind>".
Only a holder of the key can map an id back (--resolve, run on that machine). A hit in a commit can not be
removed by a later commit (it is in the history; rewriting public history is the owner's decision), so a hit is
acknowledged in conformance/public-text-commit-ack.toml by its keyed id, citing that decision with its date. A hit
that is not acknowledged is a finding.

The window: the last --days days (7), widened back to the day before the previous complete read. The previous
complete read is the newest scheduled or dispatched run of the Conformance (live) workflow on main whose read step
(READ_STEP, in job READ_JOB) succeeded; the read step succeeds when every repository was read, whatever it found,
so a red judgement or another job's red does not widen the window and a missed day is read by the next run. A
previous complete read more than --days less one day ago is a gap: the read still reads it (the window reaches
back to it), and the judgement is red once so the gap is seen; the next run's previous complete read is this one,
so the red does not repeat. More than MAX_DAYS days is red (2) before any read: read it by hand with --since. The
first run after this pass was added (FIRST_RUN) reads --days days and says so.

A hit that is not acknowledged stays a finding after its commit leaves the window: the read carries forward the
unacknowledged hits of the previous complete read (its artifact HITS_ARTIFACT, kind and keyed id only), adds the
hits found now, drops the acknowledged ones, and writes the result for the next run. Once a complete read exists,
a previous complete read that can not be found, or its hit file, is red (2): the window and the carried hits would
otherwise start over unseen.

A read (--read-out) writes one line per hit, "<kind>\t<keyed id>" (and a first line "#gap\t<previous read>\t<days>"
when there was a gap), and exits 0 when every repository was read; the
judgement (--judge) compares that file with the acknowledgements.

Known limits (the push hook is the only reader of these): a commit on a branch deleted or force-pushed away
inside the window before the daily run; a commit pushed inside the window whose committer date is older than the
window (an old local branch pushed late).

Exit 0 when every listed repository was read and no hit is new, 1 on a new hit, 2 when the key is missing, a
repository could not be read, no repository was listed, no commit was read although a repository was pushed to
inside the window, or the previous complete read is too old. With --read-out, hits do not change the exit code
(the judgement does).
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
import io
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import holder_live as hl  # noqa: E402
import public_text_check as ptc  # noqa: E402

CHECK = HERE / "public_text_check.py"
ACK = HERE.parent / "conformance" / "public-text-commit-ack.toml"
GITHUB = "https://github.com/"
WORKFLOW = "conformance-live.yml"
READ_JOB = "Every public repository's new commits, keyed"
READ_STEP = "Read the public repositories' new commits, keyed"
MAX_DAYS = 30
HITS_ARTIFACT = "public-text-commit-hits"
HITS_FILE = "commit-hits.txt"
# a finding of the checker that names no commit (a settings entry) carries a path or a class that can point at
# the repository: such a finding is reported under this fixed kind
OUTSIDE = "a finding outside the commits"
# the day this pass was added: a previous successful run before it read no commits, so the next run is a first run
FIRST_RUN = datetime.date(2026, 11, 16)
SUMMARY_RE = re.compile(r"^public text: (\d+) findings in (\d+) commits", re.M)
FOUND_RE = re.compile(r"^found: (.*)$", re.M)
COMMIT_RE = re.compile(r"^commit ([0-9a-f]{12}) (.*)$")
ACK_RE = re.compile(r'^"([0-9a-f]{64})"\s*=\s*"([^"]*\d{4}-\d{2}-\d{2}[^"]*)"\s*$')
HIT_RE = re.compile(r"^([^\t#][^\t]*)\t([0-9a-f]{64})$")
GAP_RE = re.compile(r"^#gap\t(\d{4}-\d{2}-\d{2})\t(\d+)$")
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
        return "-", OUTSIDE, ""
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
            raise Unreadable(f"ack line {n}: needs one '\"<64 hex keyed id>\" = \"<the decision it cites, with its "
                             f"date YYYY-MM-DD>\"' per hit")
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


def runs_since(owner: str, event: str, oldest: datetime.date) -> list[dict]:
    """the completed runs of the live workflow on main for one event, newest first, back to `oldest` (paged)"""
    out: list[dict] = []
    page = 1
    while True:
        data = hl.api_get(f"{hl.API}/repos/{owner}/ALICE-Law/actions/workflows/{WORKFLOW}/runs"
                          f"?branch=main&status=completed&event={event}&per_page=100&page={page}")
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            raise hl.Unreadable(f"the {event} run list is not readable")
        for run in runs:
            if run["created_at"][:10] < oldest.isoformat():
                return out
            out.append(run)
        if len(runs) < 100:
            return out
        page += 1


def find_complete_read(runs: list[dict], jobs_of) -> tuple[datetime.date, int] | None:
    """the newest of `runs` (schedule and dispatch runs on main) whose read step succeeded, as (date, run id)"""
    for run in sorted(runs, key=lambda r: r["created_at"], reverse=True):
        if read_step_succeeded(jobs_of(run["id"])):
            return datetime.date.fromisoformat(run["created_at"][:10]), run["id"]
    return None


def last_complete_read(owner: str, today: datetime.date) -> tuple[datetime.date, int] | None:
    """(date, run id) of the newest scheduled or dispatched run of the live workflow on main whose read step
    succeeded (the read step succeeds when every repository was read, whatever it found), searched back to
    MAX_DAYS + 1 days; None when there is none in that span"""
    oldest = today - datetime.timedelta(days=MAX_DAYS + 1)
    runs = runs_since(owner, "schedule", oldest) + runs_since(owner, "workflow_dispatch", oldest)

    def jobs_of(run_id: int) -> list[dict]:
        jobs = hl.api_get(f"{hl.API}/repos/{owner}/ALICE-Law/actions/runs/{run_id}/jobs?per_page=100")
        listed = jobs.get("jobs") if isinstance(jobs, dict) else None
        if not isinstance(listed, list):
            raise hl.Unreadable(f"the jobs of run {run_id} are not readable")
        return listed

    return find_complete_read(runs, jobs_of)


class _NoAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """the artifact zip redirects to storage that refuses the API token: drop it on the redirect"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


def previous_hits(owner: str, run_id: int) -> str:
    """the hit file of run `run_id`'s artifact HITS_ARTIFACT (Unreadable when it is missing)"""
    data = hl.api_get(f"{hl.API}/repos/{owner}/ALICE-Law/actions/runs/{run_id}/artifacts?name={HITS_ARTIFACT}")
    arts = data.get("artifacts") if isinstance(data, dict) else None
    if not isinstance(arts, list) or not arts:
        raise Unreadable(f"run {run_id} has no {HITS_ARTIFACT} artifact: the hits it carried can not be read")
    if arts[0].get("expired"):
        raise Unreadable(f"the {HITS_ARTIFACT} artifact of run {run_id} has expired")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    req = urllib.request.Request(arts[0]["archive_download_url"],
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "public-text-commits",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.build_opener(_NoAuthOnRedirect).open(req, timeout=60) as r:
            blob = r.read()
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            return z.read(HITS_FILE).decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile) as e:
        raise Unreadable(f"the {HITS_ARTIFACT} artifact of run {run_id} is not readable: {e}") from e


def read_step_succeeded(jobs: list[dict]) -> bool:
    for job in jobs:
        if job.get("name") != READ_JOB:
            continue
        for step in job.get("steps") or []:
            if step.get("name") == READ_STEP:
                return step.get("conclusion") == "success"
    return False


def window_start(today: datetime.date, days: int, last: datetime.date | None, since: datetime.date | None,
                 first: bool = False) -> tuple[datetime.date, str]:
    """(the first day read, what the window is), or Unreadable when the previous complete read is too old"""
    start = today - datetime.timedelta(days=days)
    if since is not None:
        return min(start, since), f"from {min(start, since).isoformat()} (--since)"
    if last is None and not first and (today - FIRST_RUN).days > days:
        raise Unreadable(f"no complete read since {(today - datetime.timedelta(days=MAX_DAYS + 1)).isoformat()} "
                         f"was found, and this pass has run since {FIRST_RUN.isoformat()}: read the commits by hand "
                         f"with --since")
    if last is None or last < FIRST_RUN:
        return start, (f"first run of this pass: an explicit look-back of {days} days (from {start.isoformat()}); "
                       f"older commits are read only by the tree pass and the push hook")
    if (today - last).days > MAX_DAYS:
        raise Unreadable(f"the previous complete read was {last.isoformat()}, more than {MAX_DAYS} days ago: read "
                         f"the commits since then by hand with --since {last.isoformat()}")
    widened = min(start, last - datetime.timedelta(days=1))
    if widened < start:
        return widened, (f"from {widened.isoformat()}: the previous complete read was {last.isoformat()}, so the "
                         f"window reaches back to the day before it")
    return start, f"from {start.isoformat()} ({days} days; the previous complete read was {last.isoformat()})"


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


def gap_of(today: datetime.date, days: int, last: datetime.date | None) -> int:
    """the days since the previous complete read when they exceed --days less one (a missed read), else 0"""
    if last is None or last < FIRST_RUN:
        return 0
    n = (today - last).days
    return n if n > days - 1 else 0


def load_hits(text: str) -> tuple[list[tuple[str, str]], tuple[str, int] | None]:
    out = []
    gap = None
    for n, line in enumerate(text.splitlines(), 1):
        g = GAP_RE.match(line)
        if g and gap is None:
            gap = (g.group(1), int(g.group(2)))
            continue
        if line.startswith("# "):
            continue
        m = HIT_RE.match(line)
        if m is None:
            raise Unreadable(f"hit line {n}: needs '<kind>\\t<64 hex keyed id>'")
        out.append((m.group(1), m.group(2)))
    return out, gap


def judge(hits: list[tuple[str, str]], ack: dict[str, str], gap: tuple[str, int] | None = None, dropped: int = 0
          ) -> int:
    """red on a hit not in `ack` or on a gap; `dropped` = acknowledged hits already left out of `hits`"""
    if gap:
        print(f"error: the previous complete read was {gap[0]}, {gap[1]} days before this one: the commits of the gap "
              f"were read now (the window reached back), and this run is red once so the gap is seen",
              file=sys.stderr)
    new = [(kind, hid) for kind, hid in hits if hid not in ack]
    print(f"public text commits: {len(hits) + dropped} hits, {len(hits) - len(new) + dropped} acknowledged, "
          f"{len(new)} new")
    for kind, hid in new:
        print(f"found: {kind} {hid[:16]}")
    return 1 if new or gap else 0


def read_ack(path: str) -> dict[str, str]:
    return load_ack(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--ack", default=str(ACK))
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--today", help="the date of the run (YYYY-MM-DD, default today in UTC)")
    ap.add_argument("--last-success", help="the date of the previous complete read (YYYY-MM-DD; default: asked of "
                                           "the API; 'none' for no previous read)")
    ap.add_argument("--since", help="read from this day (YYYY-MM-DD) at least: a gap read by hand")
    ap.add_argument("--previous", help="the hit file of the previous complete read ('none': no previous read; "
                                       "default: its artifact, asked of the API)")
    ap.add_argument("--source", help="read repositories from this directory of git repositories (the tests)")
    ap.add_argument("--read-out", help="write the hits (kind and keyed id) to this file; hits do not change the exit")
    ap.add_argument("--judge", metavar="FILE", help="judge the hits a read wrote against the acknowledgements")
    ap.add_argument("--resolve", nargs="+", metavar="ID",
                    help="print the full keyed id, repository, commit and place of each id (on a machine with the "
                         "key)")
    ap.add_argument("--check-ack", action="store_true",
                    help="only parse the acknowledged-hit file (no key, no network; the ci/ branch check)")
    a = ap.parse_args(argv)
    if a.check_ack:
        try:
            rows = read_ack(a.ack)
        except (Unreadable, OSError) as e:
            print(f"error: {a.ack}: {e}", file=sys.stderr)
            return 2
        print(f"public text commits: {Path(a.ack).name}: {len(rows)} acknowledged hit(s), every row well formed")
        return 0
    if a.judge:
        try:
            hits, gap = load_hits(Path(a.judge).read_text(encoding="utf-8"))
            return judge(hits, read_ack(a.ack), gap)
        except (Unreadable, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    key = ptc.load_name_key()
    if key is None:
        print(f"error: the name key is not set ({ptc.NAME_KEY_ENV}): the keyed check can not run", file=sys.stderr)
        return 2
    in_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    if a.resolve and in_ci:
        print("error: --resolve prints repositories and commits: it runs on a key holder's machine, not in CI",
              file=sys.stderr)
        return 2
    if a.days < 2:
        print("error: --days must be 2 or more (the window must overlap the daily runs)", file=sys.stderr)
        return 2
    today = (datetime.date.fromisoformat(a.today) if a.today
             else datetime.datetime.now(datetime.timezone.utc).date())
    try:
        since = datetime.date.fromisoformat(a.since) if a.since else None
        if a.resolve and since is None:
            since = today - datetime.timedelta(days=a.days)
        run_id = None
        if a.resolve or since is not None or a.last_success == "none":
            last = None
        elif a.last_success:
            last = datetime.date.fromisoformat(a.last_success)
        else:
            found = last_complete_read(a.owner, today)
            last, run_id = found if found else (None, None)
        start, what = window_start(today, a.days, last, since, first=a.last_success == "none")
        # the hits the previous complete read carried: from --previous, else from its artifact
        if a.resolve:
            carried_text = ""
        elif a.previous:
            carried_text = "" if a.previous == "none" else Path(a.previous).read_text(encoding="utf-8")
        elif run_id is not None:
            carried_text = previous_hits(a.owner, run_id)
        elif (last is not None and last >= FIRST_RUN) or since is not None:
            raise Unreadable("the previous complete read's hits can not be read without its run: pass --previous")
        else:
            carried_text = ""
        carried, _ = load_hits(carried_text)
    except (hl.Unreadable, ValueError, KeyError) as e:
        print(f"error: couldn't read the previous run: {e}", file=sys.stderr)
        return 2
    except (Unreadable, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"public text commits: {what}")
    if carried:
        print(f"public text commits: {len(carried)} unacknowledged hits carried from the previous complete read")
    source = Path(a.source) if a.source else None
    try:
        repos = source_repos(source) if source else hl.public_repos(a.owner)
    except (hl.Unreadable, OSError) as e:
        print(f"error: couldn't read the repository list: {e}", file=sys.stderr)
        return 2
    if not repos:
        print("error: no public repository was listed", file=sys.stderr)
        return 2
    read, commits, errors, empty = scan(repos, start, source, a.jobs)
    for e in errors:
        # the full error names the repository: only its class is printed where the output is public
        print(f"couldn't read: {e}" if a.resolve else f"couldn't read: {e.split(':', 1)[-1].split(':', 1)[0].strip()}")
    hits = []
    for name, found in read.items():
        for f in found:
            sha, kind, where = parse_hit(f)
            hits.append((hit_id(key, name, sha, kind), kind, name, sha, where))
    if a.resolve:
        want = set(a.resolve)
        for hid, kind, name, sha, where in hits:
            if any(hid.startswith(w) for w in want):
                print(f"{hid} {name} {sha} {where}: {kind}")
        return 0
    print(f"public text commits: {len(repos)} public repositories, {len(read)} read, {len(errors)} unread, "
          f"{commits} commits read since {start.isoformat()}, {len(hits)} hits")
    print("public text commits: not read here: commits on branches deleted or force-pushed away inside the window "
          "before this run, and commits with a committer date older than the window (the push hook reads those)")
    if errors or not read:
        print("error: not every public repository was read (no result is given for a partial read)", file=sys.stderr)
        return 2
    problem = zero_read_problem(commits, repos, start)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    lines = [(kind, hid) for hid, kind, *_ in hits]
    try:
        ack = read_ack(a.ack)
    except (OSError, Unreadable) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    # carried and new, each once, the acknowledged dropped (an acknowledged hit is never carried again)
    seen: set[str] = set()
    merged = []
    for kind, hid in carried + lines:
        if hid not in seen and hid not in ack:
            seen.add(hid)
            merged.append((kind, hid))
    dropped = len({h for _, h in carried + lines} & set(ack))
    lines = merged
    gap_days = gap_of(today, a.days, last)
    gap = (last.isoformat(), gap_days) if gap_days and last else None
    if a.read_out:
        head = f"# public text commit hits, read {today.isoformat()} from {start.isoformat()}\n"
        head += f"#gap\t{gap[0]}\t{gap[1]}\n" if gap else ""
        Path(a.read_out).write_text(head + "".join(f"{k}\t{h}\n" for k, h in lines), encoding="utf-8")
        print(f"public text commits: {len(lines)} hits written to the judgement")
        return 0
    try:
        return judge(lines, ack, gap, dropped)
    except (OSError, Unreadable) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
