#!/usr/bin/env python3
"""Generate the private-repository hash sets inside scripts/public_text_check.py from the account's repositories.

public_text_check.py holds two sets, between the markers of its generated block:
  PRIVATE_REPO_HASHES        every private repository of the owner (a home path into one is a finding)
  PRIVATE_ALICE_REPO_HASHES  the private ALICE-* repositories among them (their name as a whole token is a finding)
Each entry is the SHA-256 of the lower-case repository name; no name is written anywhere. The sets are read
from the repositories' visibility now, so a repository made public drops out on the next generation.

    gen_private_repo_hashes.py            rewrite the block from `gh repo list <owner>`
    gen_private_repo_hashes.py --check    exit 1 when the committed block differs from what would be written
    gen_private_repo_hashes.py --baseline live [--local NAME=DIR] [--check]
                                          rewrite (or check) the baseline of private repository names in the
                                          public repositories' text, over the set Conformance (live) reads:
                                          every repository the API lists as public now (holder_live.public_repos),
                                          its default branch fetched at depth 1 (as public_text_live.py does);
                                          --local reads repository NAME from the checkout DIR at HEAD
    gen_private_repo_hashes.py --baseline FILE [--check]
                                          the same over a JSON list of {"repo": name, "dir": checkout, "ref": ref}
                                          (each read by public_text_check.py --tree --repo-name-counts)
    --owner NAME                          the account (default ext-sakamoro)
    --roster-json FILE                    read [{"name": ..., "visibility": ...}] from FILE instead of gh

Run it locally, with a token that sees the account's private repositories: a CI job's own token sees
only its repository, so the check cannot run in CI (no step there runs it).

The list must come from a token that sees private repositories: a listing with no private repository, a gh
failure or an empty listing is exit 2, and nothing is written (an empty set would make the checks compare
nothing). Exit 0 written / up to date, 1 stale (--check), 2 nothing to compare.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import holder_live as hl  # noqa: E402

CHECKER = Path(__file__).resolve().parent / "public_text_check.py"
GITHUB = "https://github.com/"
BEGIN = "# >>> " + "generated private repositories >>>"
END = "# <<< " + "generated private repositories <<<"


def roster(owner: str, roster_json: str | None) -> list[dict]:
    if roster_json:
        return json.loads(Path(roster_json).read_text(encoding="utf-8"))
    r = subprocess.run(["gh", "repo", "list", owner, "--limit", "5000", "--json", "name,visibility"],
                       capture_output=True, text=True, check=False, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(f"gen_private_repo_hashes: gh repo list {owner} failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout or "[]")


def hashes(rows: list[dict]) -> tuple[list[str], list[str]]:
    """(every private repository, the private ALICE-* ones) as sorted SHA-256 of the lower-case names"""
    if not rows:
        raise SystemExit("gen_private_repo_hashes: the repository listing is empty (nothing to compare)")
    private = sorted({r["name"].lower() for r in rows if str(r.get("visibility", "")).upper() == "PRIVATE"})
    if not private:
        raise SystemExit("gen_private_repo_hashes: the listing holds no private repository (does the token "
                         "see private repositories?); nothing written")
    alice = [n for n in private if n.startswith("alice-")]
    if not alice:
        raise SystemExit("gen_private_repo_hashes: the listing holds no private ALICE-* repository; nothing written")
    h = lambda names: sorted(hashlib.sha256(n.encode("utf-8")).hexdigest() for n in names)  # noqa: E731
    return h(private), h(alice)


def block(all_private: list[str], alice: list[str]) -> str:
    def fs(name: str, items: list[str]) -> str:
        return f"{name}: frozenset[str] = frozenset({{\n" + "".join(f'    "{x}",\n' for x in items) + "})\n"
    return (f"{BEGIN}\n"
            "# written by scripts/gen_private_repo_hashes.py from the account's repositories (do not edit; "
            "--check finds a stale block)\n"
            "# The sets are held in this file: a repository's CI fetches this one file by its SHA-256, and a\n"
            "# list kept beside it would not come along (the check would then compare against nothing).\n"
            "# An empty set is refused (private_sets_problem).\n"
            + fs("PRIVATE_REPO_HASHES", all_private)
            + "# the private ALICE-* repositories among them: the name as a whole token in any text is a finding\n"
            + fs("PRIVATE_ALICE_REPO_HASHES", alice)
            + f"{END}\n")


BASELINE_BEGIN = "# >>> " + "generated private repository name baseline >>>"
BASELINE_END = "# <<< " + "generated private repository name baseline <<<"


def baseline_block(per_repo: dict[str, dict[str, int]]) -> str:
    lines = [f"{BASELINE_BEGIN}\n", "PRIVATE_REPO_NAME_BASELINE: dict[str, dict[str, int]] = {\n"]
    for repo_hash in sorted(per_repo):
        entries = per_repo[repo_hash]
        if not entries:
            continue
        lines.append(f'    "{repo_hash}": {{\n')
        lines += [f'        "{k}": {n},\n' for k, n in sorted(entries.items())]
        lines.append("    },\n")
    lines += ["}\n", f"{BASELINE_END}\n"]
    return "".join(lines)


def read_counts(checker: Path, repo: str, path: str, ref: str) -> dict[str, int]:
    """the private repository name counts of one repository's tree, as the checker keys them"""
    r = subprocess.run([sys.executable, str(checker), "--repo", path, "--repo-name", repo, "--tree", ref,
                        "--repo-name-counts"], capture_output=True, text=True, check=False, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(f"gen_private_repo_hashes: {repo}: the checker could not read the tree "
                         f"(exit {r.returncode}): {r.stderr.strip()[:200]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def baseline(checker: Path, listing: str) -> tuple[dict[str, dict[str, int]], int]:
    """{sha256(repo): counts} of the repositories in `listing`, and how many were read"""
    repos = json.loads(Path(listing).read_text(encoding="utf-8"))
    if not repos:
        raise SystemExit("gen_private_repo_hashes: the baseline listing is empty (nothing to compare)")
    out = {}
    for r in repos:
        out[hashlib.sha256(r["repo"].lower().encode("utf-8")).hexdigest()] = read_counts(
            checker, r["repo"], r["dir"], r.get("ref", "HEAD"))
    return out, len(repos)


def read_live_repo(checker: Path, base: Path, repo: dict) -> tuple[str, dict[str, int] | None]:
    """(name, counts) of one listed public repository at its default branch, None when it is empty"""
    name = repo["name"]
    if repo.get("size", 1) == 0:
        return name, None
    g = base / name
    try:
        subprocess.run(["git", "init", "-q", str(g)], check=True, capture_output=True)
        hl.git(g, "remote", "add", "origin", GITHUB + repo["full_name"])
        hl.git(g, "fetch", "-q", "--depth", "1", "--no-tags", "origin",
               f"+refs/heads/{repo['default_branch']}:refs/remotes/origin/main")
        return name, read_counts(checker, name, str(g), "origin/main")
    except (hl.Unreadable, subprocess.CalledProcessError) as e:
        raise SystemExit(f"gen_private_repo_hashes: {name}: could not be read: {e}") from None
    finally:
        shutil.rmtree(g, ignore_errors=True)


def baseline_live(checker: Path, owner: str, local: tuple[str, str] | None,
                  jobs: int = 8) -> tuple[dict[str, dict[str, int]], int]:
    """{sha256(repo): counts} over every public repository the API lists now, and how many were read"""
    try:
        repos = hl.public_repos(owner)
    except hl.Unreadable as e:
        raise SystemExit(f"gen_private_repo_hashes: couldn't read the repository list: {e}") from None
    if not repos:
        raise SystemExit("gen_private_repo_hashes: no public repository was listed (nothing to compare)")
    if local is not None and local[0] not in {r["name"] for r in repos}:
        raise SystemExit(f"gen_private_repo_hashes: --local names {local[0]}, which is not a listed public repository")
    out: dict[str, dict[str, int]] = {}
    key = lambda n: hashlib.sha256(n.lower().encode("utf-8")).hexdigest()  # noqa: E731
    if local is not None:
        out[key(local[0])] = read_counts(checker, local[0], local[1], "HEAD")
    others = [r for r in repos if local is None or r["name"] != local[0]]
    with tempfile.TemporaryDirectory() as tmp, concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        for name, counts in ex.map(lambda r: read_live_repo(checker, Path(tmp), r), others):
            if counts is not None:
                out[key(name)] = counts
    return out, len(out)


def replace_between(text: str, begin: str, end: str, new: str) -> str:
    if text.count(begin) != 1 or text.count(end) != 1:
        raise SystemExit(f"gen_private_repo_hashes: {CHECKER.name} must hold the generated block's markers once each")
    return text[:text.index(begin)] + new + text[text.index(end) + len(end) + 1:]


def replace_block(text: str, new: str) -> str:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise SystemExit(f"gen_private_repo_hashes: {CHECKER.name} must hold the generated block's markers once each")
    start = text.index(BEGIN)
    end = text.index(END) + len(END) + 1
    return text[:start] + new + text[end:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--roster-json")
    ap.add_argument("--checker", default=str(CHECKER))
    ap.add_argument("--baseline", metavar="live|FILE")
    ap.add_argument("--local", metavar="NAME=DIR")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args(argv)
    if a.baseline:
        try:
            path = Path(a.checker)
            text = path.read_text(encoding="utf-8")
            local = None
            if a.local is not None:
                name, sep, d = a.local.partition("=")
                if not name or not sep or not d:
                    raise SystemExit(f"gen_private_repo_hashes: --local {a.local!r}: needs NAME=DIR")
                local = (name, d)
            if a.baseline == "live":
                per_repo, read = baseline_live(path, a.owner, local, a.jobs)
            else:
                per_repo, read = baseline(path, a.baseline)
            new = replace_between(text, BASELINE_BEGIN, BASELINE_END, baseline_block(per_repo))
        except SystemExit as e:
            print(e, file=sys.stderr)
            return 2
        lines = sum(sum(v.values()) for v in per_repo.values())
        with_hits = sum(1 for v in per_repo.values() if v)
        if a.check:
            if new != text:
                print(f"gen_private_repo_hashes: the baseline is stale ({read} repositories read, {with_hits} with "
                      f"names, {lines} lines)", file=sys.stderr)
                return 1
            print(f"gen_private_repo_hashes: baseline up to date ({read} repositories, {lines} lines)")
            return 0
        path.write_text(new, encoding="utf-8")
        print(f"gen_private_repo_hashes: baseline of {read} repositories written ({with_hits} with names, {lines} lines)")
        return 0
    try:
        all_private, alice = hashes(roster(a.owner, a.roster_json))
        path = Path(a.checker)
        text = path.read_text(encoding="utf-8")
        new = replace_block(text, block(all_private, alice))
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    if a.check:
        if new != text:
            print(f"gen_private_repo_hashes: {path.name}'s generated block is stale ({len(all_private)} private "
                  f"repositories, {len(alice)} ALICE-*, as listed now); run scripts/gen_private_repo_hashes.py",
                  file=sys.stderr)
            return 1
        print(f"gen_private_repo_hashes: up to date ({len(all_private)} private, {len(alice)} ALICE-*)")
        return 0
    path.write_text(new, encoding="utf-8")
    print(f"gen_private_repo_hashes: wrote {len(all_private)} private repositories, {len(alice)} ALICE-*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
