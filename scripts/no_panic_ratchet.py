#!/usr/bin/env python3
"""The no-panic ratchet: every repository's production panic sites may only go down.

    no_panic_ratchet.py [--owner NAME] [--local NAME=DIR] [--jobs N]
    no_panic_ratchet.py --only NAME=DIR
    no_panic_ratchet.py --write-baseline --roster-json FILE --clone NAME=DIR ...

The count comes from tools/no-panic-scan (syntax tree, no build of the repository): per repository, the sites of
`.unwrap()`, `.expect(..)`, `panic!`, `todo!`, `unimplemented!`, `unreachable!` in its production code, and every
`allow(..)` / `expect(..)` attribute naming the clippy lint of one of them (a deny of the lint is void there, so it
is a site). conformance/no-panic-baseline.toml holds each repository's counts, keyed by its name when it is public
and by `sha256:` and the SHA-256 of its lower-case name when it is private (no private name is written here).

The rules, on every repository read:
  * a count above the baseline is red (a new site);
  * a count below the baseline is red until the baseline is lowered in the same commit (a stale baseline would let
    the count rise back unnoticed);
  * a repository with production Rust code and no row is red; a repository whose code does not parse is red;
  * 0 files read over all the repositories is red (a check that compared nothing passes nothing).
When a repository reaches 0 for a lint, its row keeps the 0: from then on the repository carries
`#![deny(clippy::<lint>)]` at its crate roots and the ratchet holds it at 0 even if the deny is removed.

Modes:
  (default)      every ALICE-* repository (--prefix) the GitHub API lists as public now (holder_live.public_repos), each default branch
                 fetched at depth 1; --local NAME=DIR reads repository NAME from the checkout DIR instead. A public
                 row whose repository is no longer listed as public is red (it was removed or made private: move or
                 drop its row). Private rows are not read here (a CI token sees only public repositories).
  --only N=DIR   one repository from DIR against its row (the form a repository's own CI runs, private ones by the
                 hash of N)
  --write-baseline
                 rewrite the baseline: the public rows from the live read above (every public ALICE-* repository
                 at its default branch), the private rows from local clones (--clone NAME=DIR, each read at
                 origin/main through `git archive`; a clone of a public repository is not used, the live read is),
                 with each repository's visibility from --roster-json (`gh repo list <owner> --json
                 name,visibility`); run it locally with a token that sees private repositories, after a
                 deliberate change of counts.

Exit 0 when nothing was found, 1 on a finding, 2 when something could not be read or nothing was compared.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import holder_live as hl  # noqa: E402

BASELINE = ROOT / "conformance" / "no-panic-baseline.toml"
SCANNER_DIR = ROOT / "tools" / "no-panic-scan"
LINTS = ("unwrap", "expect", "panic", "todo", "unimplemented", "unreachable")
GITHUB = "https://github.com/"


def key(name: str, public: bool) -> str:
    """the row key: the name of a public repository, `sha256:<hex>` of a private one's lower-case name"""
    return name if public else "sha256:" + hashlib.sha256(name.lower().encode()).hexdigest()


def scanner() -> Path:
    """the scanner binary: NO_PANIC_SCAN if set, else built from tools/no-panic-scan (release)"""
    env = os.environ.get("NO_PANIC_SCAN")
    if env:
        return Path(env)
    target = Path(os.environ.get("CARGO_TARGET_DIR", SCANNER_DIR / "target"))
    subprocess.run(["cargo", "build", "-q", "--release", "--manifest-path", str(SCANNER_DIR / "Cargo.toml")],
                   check=True, env={**os.environ, "CARGO_TARGET_DIR": str(target)})
    return target / "release" / "no-panic-scan"


def parse_scan(out: str) -> dict:
    """the scanner's tab-separated output → {"files", "unparsed", <lint>: n, "unparsed_files": [...]}"""
    r: dict = {"unparsed_files": []}
    for line in out.splitlines():
        if not line.strip() or line.startswith("site "):
            continue
        k, _, v = line.partition("\t")
        if k == "unparsed-file":
            r["unparsed_files"].append(v)
        else:
            r[k] = int(v)
    missing = [k for k in ("files", "unparsed", *LINTS) if k not in r]
    if missing:
        raise hl.Unreadable(f"scanner output lacks {missing}")
    return r


def scan_dir(bin_: Path, d: Path) -> dict:
    p = subprocess.run([str(bin_), str(d)], capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise hl.Unreadable(f"{d}: no-panic-scan exit {p.returncode}: {p.stderr.strip()[:200]}")
    return parse_scan(p.stdout)


def load_baseline(path: Path = BASELINE) -> dict[str, dict[str, int]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rows = data.get("repo", {})
    for k, row in rows.items():
        bad = [lint for lint in LINTS if not isinstance(row.get(lint), int) or row[lint] < 0]
        extra = [f for f in row if f not in LINTS]
        if bad or extra:
            raise ValueError(f"{path.name}: row {k!r}: bad {bad} / unknown {extra}")
    return rows


def compare(read: dict[str, dict], baseline: dict[str, dict[str, int]]) -> list[str]:
    """findings for the repositories read (key → scan); a repository with no production Rust code is not compared"""
    out = []
    for k in sorted(read):
        s = read[k]
        if s["unparsed"]:
            out.append(f"{k}: {s['unparsed']} file(s) do not parse ({', '.join(s['unparsed_files'][:5])})")
        if s["files"] == 0:
            if k in baseline:
                out.append(f"{k}: no production Rust code read but it has a row (drop the row)")
            continue
        row = baseline.get(k)
        if row is None:
            out.append(f"{k}: production Rust code and no row in {BASELINE.name}")
            continue
        for lint in LINTS:
            have, base = s[lint], row[lint]
            if have > base:
                out.append(f"{k}: {lint} {have} > baseline {base} (a new site)")
            elif have < base:
                out.append(f"{k}: {lint} {have} < baseline {base} (lower the baseline in this commit)")
    return out


def render(rows: dict[str, dict[str, int]]) -> str:
    head = (
        "# The no-panic ratchet's baseline (scripts/no_panic_ratchet.py): each repository's production panic sites\n"
        "# counted by tools/no-panic-scan, keyed by the name of a public repository and by sha256:<SHA-256 of the\n"
        "# lower-case name> of a private one. A count may only go down; lower it in the commit that removes sites.\n"
        "# Regenerate with `no_panic_ratchet.py --write-baseline` (it needs a token that sees private repositories).\n"
    )
    body = []
    for k in sorted(rows):
        body.append(f'\n[repo."{k}"]')
        body += [f"{lint} = {rows[k][lint]}" for lint in LINTS]
    return head + "\n".join(body) + "\n"


def read_live(owner: str, local: tuple[str, Path] | None, jobs: int, bin_: Path,
              prefix: str) -> tuple[dict, list[str]]:
    repos = [r for r in hl.public_repos(owner) if r["name"].startswith(prefix)]
    if not repos:
        raise hl.Unreadable("no public repository listed")
    names = {r["name"] for r in repos}
    if local and local[0] not in names:
        raise hl.Unreadable(f"--local {local[0]} is not a listed public repository")
    errors: list[str] = []
    read: dict[str, dict] = {}

    def one(base: Path, repo: dict):
        name = repo["name"]
        if local and name == local[0]:
            return name, scan_dir(bin_, local[1]), None
        if repo.get("size", 1) == 0:
            return name, None, None
        g = base / name
        try:
            subprocess.run(["git", "init", "-q", str(g)], check=True, capture_output=True)
            hl.git(g, "remote", "add", "origin", GITHUB + repo["full_name"])
            hl.git(g, "fetch", "-q", "--depth", "1", "--no-tags", "origin",
                   f"+refs/heads/{repo['default_branch']}:refs/remotes/origin/main")
            hl.git(g, "checkout", "-q", "--detach", "origin/main")
            return name, scan_dir(bin_, g), None
        except (hl.Unreadable, subprocess.CalledProcessError) as e:
            return name, None, str(e)
        finally:
            shutil.rmtree(g, ignore_errors=True)

    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=jobs) as ex:
        for name, s, err in ex.map(lambda r: one(Path(tmp), r), repos):
            if err:
                errors.append(f"{name}: {err}")
            elif s is not None:
                read[key(name, True)] = s
    return read, errors


def archive_scan(bin_: Path, name: str, clone: Path) -> dict:
    """origin/main of a local clone, extracted with git archive into a temporary directory and scanned"""
    with tempfile.TemporaryDirectory() as tmp:
        arch = subprocess.run(["git", "-C", str(clone), "archive", "origin/main"], capture_output=True)
        if arch.returncode != 0:
            raise hl.Unreadable(f"{name}: git archive origin/main failed")
        subprocess.run(["tar", "-x", "-C", tmp], input=arch.stdout, check=True)
        return scan_dir(bin_, Path(tmp))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--local", help="NAME=DIR: read public repository NAME from the checkout DIR")
    ap.add_argument("--only", help="NAME=DIR: compare only repository NAME, read from DIR")
    ap.add_argument("--private", action="store_true", help="with --only: NAME is private (its row is the hash)")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--prefix", default="ALICE-", help="only repositories whose name starts with this")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--roster-json", type=Path)
    ap.add_argument("--clone", action="append", default=[], help="NAME=DIR (with --write-baseline)")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    a = ap.parse_args(argv)
    bin_ = scanner()

    if a.write_baseline:
        if not a.roster_json or not a.clone:
            print("no_panic_ratchet: --write-baseline needs --roster-json and --clone", file=sys.stderr)
            return 2
        vis = {r["name"]: r["visibility"].upper() == "PUBLIC" for r in json.loads(a.roster_json.read_text())}
        if not any(not v for v in vis.values()):
            print("no_panic_ratchet: the roster lists no private repository (a token that sees them is needed)",
                  file=sys.stderr)
            return 2
        rows: dict[str, dict[str, int]] = {}
        try:
            live, errors = read_live(a.owner, None, a.jobs, bin_, a.prefix)
        except hl.Unreadable as e:
            print(f"no_panic_ratchet: {e}", file=sys.stderr)
            return 2
        if errors:
            for e in errors:
                print(f"no_panic_ratchet: unreadable: {e}", file=sys.stderr)
            return 2
        for k, s in live.items():
            if s["unparsed"]:
                print(f"no_panic_ratchet: {k}: {s['unparsed']} file(s) do not parse", file=sys.stderr)
                return 2
            if s["files"]:
                rows[k] = {lint: s[lint] for lint in LINTS}
        for spec in a.clone:
            name, _, d = spec.partition("=")
            if name not in vis:
                # a local clone that is not one of the account's repositories (a scratch copy)
                print(f"no_panic_ratchet: skipped {name}: not in the roster", file=sys.stderr)
                continue
            if vis[name]:
                continue  # public: the live read above is the row
            s = archive_scan(bin_, name, Path(d))
            if s["unparsed"]:
                print(f"no_panic_ratchet: {name}: {s['unparsed']} file(s) do not parse", file=sys.stderr)
                return 2
            if s["files"]:
                rows[key(name, vis[name])] = {lint: s[lint] for lint in LINTS}
        if not rows:
            print("no_panic_ratchet: no repository with production Rust code read", file=sys.stderr)
            return 2
        a.baseline.write_text(render(rows), encoding="utf-8")
        print(f"no_panic_ratchet: wrote {len(rows)} rows")
        return 0

    baseline = load_baseline(a.baseline)
    if a.only:
        name, _, d = a.only.partition("=")
        k = key(name, not a.private)
        read = {k: scan_dir(bin_, Path(d))}
        errors: list[str] = []
    else:
        local = None
        if a.local:
            n, _, d = a.local.partition("=")
            local = (n, Path(d))
        try:
            read, errors = read_live(a.owner, local, a.jobs, bin_, a.prefix)
        except hl.Unreadable as e:
            print(f"no_panic_ratchet: {e}", file=sys.stderr)
            return 2
    files = sum(s["files"] for s in read.values())
    findings = compare(read, baseline)
    if not a.only:
        listed = set(read)
        for k in sorted(baseline):
            if not k.startswith("sha256:") and k not in listed:
                findings.append(f"{k}: has a row but is not a listed public repository read here (move or drop it)")
    for e in errors:
        print(f"no_panic_ratchet: unreadable: {e}", file=sys.stderr)
    for f in findings:
        print(f"no_panic_ratchet: {f}")
    sites = sum(s[lint] for s in read.values() for lint in LINTS)
    print(f"no_panic_ratchet: {len(read)} repositories read, {files} production files, {sites} sites, "
          f"{len(findings)} finding(s)")
    if errors or files == 0:
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
