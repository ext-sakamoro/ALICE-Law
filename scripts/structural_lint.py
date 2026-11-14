#!/usr/bin/env python3
"""The structural lint ratchet: ast-grep rules over every repository's production Rust code, counts that may only go down.

    structural_lint.py [--owner NAME] [--local NAME=DIR] [--jobs N]
    structural_lint.py --only NAME=DIR [--baseline FILE]
    structural_lint.py --write-baseline
    structural_lint.py --write-own NAME=DIR --baseline FILE

The rules are lint/ast-grep/rules/*.yml (syntax tree, no build of the repository), each with the cases of
lint/ast-grep/rule-tests/*-test.yml that it must and must not match; the rule tests run before any scan, and a
failing test stops the run. Per repository the run counts, in its production code (`**/src/**/*.rs`, not under
tests / benches / examples / scripts, not in a `#[cfg(test)]` / `#[cfg(all(test, ..))]` module or a `#[test]` function; `cfg(any(test, ..))` counts):

  print     production-print: println! / eprintln! / print! / eprint! / dbg! outside src/bin and src/main.rs
  hashmap   hashmap-iteration-order: a HashMap / HashSet made in a function and iterated there (the order is not
            fixed between runs)
  ignores   `ast-grep-ignore` comments: a suppression is a site too, so it may only go down like the hits; each
            one must read `// ast-grep-ignore: <rule-id>[, <rule-id>] -- <reason of 12 or more characters>`, any
            other form is a finding (a bare marker suppresses every rule with no reason)

libm-transcendental (`x.sin()`, `f64::exp(x)` and the like) is counted and printed but is not part of the
baseline: without types it also matches deterministic methods of the same name (fixed-point types, a generic
Real trait), so the gate for it is clippy's disallowed_methods in each crate, which sees the types.

conformance/structural-lint-baseline.toml holds the counts of the public repositories only, keyed by name. A private
repository's name does not appear here in any form (a hash of a short name is recovered by trying names): a private
repository keeps its own row in its own tree (conformance-own/structural-lint.toml, written with --write-own) and its
own CI runs `--only NAME=. --baseline conformance-own/structural-lint.toml` with this checker at a pinned commit.
A row keyed `sha256:...` is refused. The rules, on every repository read:
  * a count above the baseline is red (a new site);
  * a count below the baseline is red until the baseline is lowered in the same commit;
  * a repository with production Rust code and no row is red; a malformed marker is red;
  * 0 production files read over all the repositories is red (a check that compared nothing passes nothing).

Modes:
  (default)      every ALICE-* repository the GitHub API lists as public now, each default branch fetched at
                 depth 1; --local NAME=DIR reads repository NAME from the checkout DIR instead. A public row whose
                 repository is not listed as public is red.
  --only N=DIR   one repository from DIR against its row (the form a repository's own CI runs)
  --only N=DIR --baseline FILE
                 the same against another baseline file (a private repository's own row)
  --write-baseline
                 rewrite the baseline from the live read of the public repositories, after a deliberate change
  --write-own N=DIR --baseline FILE
                 write the one row of repository N, read from DIR, to FILE (a private repository's own file)

The scanner is `ast-grep` (AST_GREP, else on PATH) at AST_GREP_VERSION, the version CI installs; another version stops
the run (exit 2) with the version to install. Exit 0 when nothing was found, 1 on a finding, 2 when
something could not be read, a rule test failed, or nothing was compared.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

BASELINE = ROOT / "conformance" / "structural-lint-baseline.toml"
CONFIG = ROOT / "lint" / "ast-grep" / "sgconfig.yml"
# the scanner version the rules are written against: the one the workflows install (a test keeps them equal)
AST_GREP_VERSION = "0.51.0"
GITHUB = "https://github.com/"
# rule id -> baseline field; the gated rules
GATED = {"production-print": "print", "hashmap-iteration-order": "hashmap"}
REPORTED = ("libm-transcendental",)
FIELDS = ("print", "hashmap", "ignores")
SKIP_DIRS = {"tests", "benches", "examples", "scripts", "target", ".git"}
MARKER_RE = re.compile(r"ast-grep-ignore")
GOOD_MARKER_RE = re.compile(r"//\s*ast-grep-ignore:\s*([a-z0-9-]+(?:\s*,\s*[a-z0-9-]+)*)\s+--\s+(\S.{11,})$")


def scanner() -> str:
    """the ast-grep to run, at AST_GREP_VERSION: another version can match the rules differently, so its verdict is
    not this lint's (0.51.2 reports nothing on a case of production-print that 0.51.0 matches)"""
    exe = os.environ.get("AST_GREP") or shutil.which("ast-grep")
    if not exe:
        raise hl.Unreadable(f"ast-grep not found (install ast-grep {AST_GREP_VERSION}; set AST_GREP or put it on PATH)")
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        raise hl.Unreadable(f"{exe} --version did not answer ({type(e).__name__})") from e
    m = re.search(r"\bast-grep\s+(\d+\.\d+\.\d+)\b", out)
    if not m:
        raise hl.Unreadable(f"{exe} --version gave no version: {out.strip()[:80]!r}")
    if m.group(1) != AST_GREP_VERSION:
        raise hl.Unreadable(f"{exe} is ast-grep {m.group(1)}; the rules and their tests are written against "
                            f"{AST_GREP_VERSION}, the version CI installs: install ast-grep {AST_GREP_VERSION} "
                            f"(pip install ast-grep-cli=={AST_GREP_VERSION}) and put it on PATH or in AST_GREP")
    return exe


def rule_tests(exe: str) -> None:
    """every rule's cases (lint/ast-grep/rule-tests) pass, or the run stops"""
    p = subprocess.run([exe, "test", "-c", str(CONFIG), "--skip-snapshot-tests"], capture_output=True, text=True,
                       encoding="utf-8", cwd=CONFIG.parent)
    if p.returncode != 0:
        raise hl.Unreadable(f"the rule tests failed:\n{p.stdout[-2000:]}{p.stderr[-500:]}")


def production_files(d: Path) -> list[Path]:
    """the files the rules read: **/src/**/*.rs not under tests / benches / examples / scripts"""
    out = []
    for p in d.rglob("*.rs"):
        rel = p.relative_to(d).parts
        if "src" not in rel[:-1] or SKIP_DIRS.intersection(rel[:-1]):
            continue
        out.append(p)
    return out


def markers(files: list[Path], base: Path) -> tuple[int, list[str]]:
    """(the ast-grep-ignore comments, the malformed ones as `<file>:<line>`)"""
    n, bad = 0, []
    for f in files:
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if MARKER_RE.search(line):
                n += 1
                m = GOOD_MARKER_RE.search(line.strip())
                if not m or any(r not in GATED and r not in REPORTED for r in re.split(r"\s*,\s*", m.group(1))):
                    bad.append(f"{f.relative_to(base)}:{i}")
    return n, bad


def scan_dir(exe: str, d: Path) -> dict:
    files = production_files(d)
    r: dict = {"files": len(files), "print": 0, "hashmap": 0, "libm-transcendental": 0, "bad_markers": []}
    if not files:
        r["ignores"] = 0
        return r
    p = subprocess.run([exe, "scan", "-c", str(CONFIG), "--json=compact"], capture_output=True, text=True,
                       encoding="utf-8", cwd=d)
    if p.returncode not in (0, 1):
        raise hl.Unreadable(f"{d}: ast-grep scan exit {p.returncode}: {p.stderr.strip()[:200]}")
    try:
        hits = json.loads(p.stdout or "[]")
    except json.JSONDecodeError as e:
        raise hl.Unreadable(f"{d}: ast-grep output is not JSON: {e}") from e
    for h in hits:
        rid = h.get("ruleId")
        if rid in GATED:
            r[GATED[rid]] += 1
        elif rid in REPORTED:
            r[rid] += 1
    r["ignores"], r["bad_markers"] = markers(files, d)
    return r


def load_baseline(path: Path = BASELINE) -> dict[str, dict[str, int]]:
    rows = tomllib.loads(path.read_text(encoding="utf-8")).get("repo", {})
    for k, row in rows.items():
        if k.startswith("sha256:"):
            raise ValueError(f"{path.name}: row {k[:16]}...: a hashed name; a private repository keeps its row in its own tree")
        bad = [f for f in FIELDS if not isinstance(row.get(f), int) or row[f] < 0]
        extra = [f for f in row if f not in FIELDS]
        if bad or extra:
            raise ValueError(f"{path.name}: row {k!r}: bad {bad} / unknown {extra}")
    return rows


def compare(read: dict[str, dict], baseline: dict[str, dict[str, int]]) -> list[str]:
    """findings for the repositories read (key → scan); a repository with no production Rust code is not compared"""
    out = []
    for k in sorted(read):
        s = read[k]
        for b in s["bad_markers"]:
            out.append(f"{k}: {b}: an ast-grep-ignore needs `: <rule-id> -- <reason of 12 or more characters>`")
        if s["files"] == 0:
            if k in baseline:
                out.append(f"{k}: no production Rust code read but it has a row (drop the row)")
            continue
        row = baseline.get(k)
        if row is None:
            out.append(f"{k}: production Rust code and no row in {BASELINE.name}")
            continue
        for f in FIELDS:
            have, base = s[f], row[f]
            if have > base:
                out.append(f"{k}: {f} {have} > baseline {base} (a new site)")
            elif have < base:
                out.append(f"{k}: {f} {have} < baseline {base} (lower the baseline in this commit)")
    return out


def render(rows: dict[str, dict[str, int]]) -> str:
    head = (
        "# The structural lint ratchet's baseline (scripts/structural_lint.py): a repository's production sites of the\n"
        "# ast-grep rules in lint/ast-grep/rules and its ast-grep-ignore comments, keyed by repository name. A count may\n"
        "# only go down; lower it in the commit that removes sites. Regenerate with `structural_lint.py --write-baseline`\n"
        "# (public repositories) or `--write-own` (one repository's own file).\n"
    )
    body = []
    for k in sorted(rows):
        body.append(f'\n[repo."{k}"]')
        body += [f"{f} = {rows[k][f]}" for f in FIELDS]
    return head + "\n".join(body) + "\n"


def read_live(owner: str, local: tuple[str, Path] | None, jobs: int, exe: str, prefix: str) -> tuple[dict, list[str]]:
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
            return name, scan_dir(exe, local[1]), None
        if repo.get("size", 1) == 0:
            return name, None, None
        g = base / name
        try:
            subprocess.run(["git", "init", "-q", str(g)], check=True, capture_output=True)
            hl.git(g, "remote", "add", "origin", GITHUB + repo["full_name"])
            hl.git(g, "fetch", "-q", "--depth", "1", "--no-tags", "origin",
                   f"+refs/heads/{repo['default_branch']}:refs/remotes/origin/main")
            hl.git(g, "checkout", "-q", "--detach", "origin/main")
            return name, scan_dir(exe, g), None
        except (hl.Unreadable, subprocess.CalledProcessError) as e:
            return name, None, str(e)
        finally:
            shutil.rmtree(g, ignore_errors=True)

    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=jobs) as ex:
        for name, s, err in ex.map(lambda r: one(Path(tmp), r), repos):
            if err:
                errors.append(f"{name}: {err}")
            elif s is not None:
                read[name] = s
    return read, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--local", help="NAME=DIR: read public repository NAME from the checkout DIR")
    ap.add_argument("--only", help="NAME=DIR: compare only repository NAME, read from DIR")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--prefix", default="ALICE-", help="only repositories whose name starts with this")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--write-own", help="NAME=DIR: write the one row of NAME to --baseline")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    a = ap.parse_args(argv)
    try:
        exe = scanner()
        rule_tests(exe)
    except hl.Unreadable as e:
        print(f"structural_lint: {e}", file=sys.stderr)
        return 2

    if a.write_own:
        if a.baseline == BASELINE:
            print("structural_lint: --write-own needs --baseline (the repository's own file)", file=sys.stderr)
            return 2
        name, _, d = a.write_own.partition("=")
        try:
            sc = scan_dir(exe, Path(d))
        except hl.Unreadable as e:
            print(f"structural_lint: {e}", file=sys.stderr)
            return 2
        if not sc["files"] or sc["bad_markers"]:
            print(f"structural_lint: {name}: {sc['files']} production files, malformed markers {sc['bad_markers']}",
                  file=sys.stderr)
            return 2
        a.baseline.parent.mkdir(parents=True, exist_ok=True)
        a.baseline.write_text(render({name: {f: sc[f] for f in FIELDS}}), encoding="utf-8")
        print(f"structural_lint: wrote the row of {name} to {a.baseline}")
        return 0

    if a.write_baseline:
        try:
            live, errors = read_live(a.owner, None, a.jobs, exe, a.prefix)
        except hl.Unreadable as e:
            print(f"structural_lint: {e}", file=sys.stderr)
            return 2
        if errors:
            for e in errors:
                print(f"structural_lint: unreadable: {e}", file=sys.stderr)
            return 2
        rows = {k: {f: sc[f] for f in FIELDS} for k, sc in live.items() if sc["files"]}
        if not rows:
            print("structural_lint: no repository with production Rust code read", file=sys.stderr)
            return 2
        a.baseline.write_text(render(rows), encoding="utf-8")
        print(f"structural_lint: wrote {len(rows)} rows")
        return 0

    try:
        baseline = load_baseline(a.baseline)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        print(f"structural_lint: {e}", file=sys.stderr)
        return 2
    errors: list[str] = []
    try:
        if a.only:
            name, _, d = a.only.partition("=")
            read = {name: scan_dir(exe, Path(d))}
        else:
            local = None
            if a.local:
                n, _, d = a.local.partition("=")
                local = (n, Path(d))
            read, errors = read_live(a.owner, local, a.jobs, exe, a.prefix)
    except hl.Unreadable as e:
        print(f"structural_lint: {e}", file=sys.stderr)
        return 2
    files = sum(s["files"] for s in read.values())
    findings = compare(read, baseline)
    if not a.only:
        for k in sorted(baseline):
            if k not in read:
                findings.append(f"{k}: has a row but is not a listed public repository read here (move or drop it)")
    for e in errors:
        print(f"structural_lint: unreadable: {e}", file=sys.stderr)
    for f in findings:
        print(f"structural_lint: {f}")
    totals = {f: sum(s[f] for s in read.values()) for f in (*FIELDS, *REPORTED)}
    print(f"structural_lint: {len(read)} repositories read, {files} production files, "
          + ", ".join(f"{k} {v}" for k, v in totals.items()) + f" (libm reported, not gated), {len(findings)} finding(s)")
    if errors or files == 0:
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
