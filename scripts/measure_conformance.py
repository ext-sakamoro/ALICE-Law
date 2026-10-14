#!/usr/bin/env python3
"""Measure what is verifiably true of the release-train crates for the machine-checkable parts
of the gates in docs/CONFORMANCE.md, from each repository's committed tree (git only).

A cell is a measured fact with where it was found (a file and line, a workflow job), or
"not found": it is not a manifest status. A crate's manifest decides `pass` with the evidence
and the mutant; this table says which evidence exists today, so a manifest row can be checked
against it and a reader can tell what is claimed from what is there.

    measure_conformance.py --repos DIR [--ref origin/main] [--law .] [--crates A,B,...] [--json]

DIR holds one checkout per repository (ALICE-Zip, ALICE-DB, ...). Exit 2 when a repository
cannot be read or nothing was measured.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

CHAIN = ["ALICE-DetMath", "ALICE-Zip", "ALICE-Analytics", "ALICE-DB", "ALICE-Physics", "ALICE-SDF",
         "ALICE-Edge", "ALICE-LOL"]
# run-varying sources, matched as text (a lexical list: a source spelled another way is not seen,
# so "no use" means none of these forms, not none at all)
CLOCK_RNG = ("SystemTime::now", "Instant::now", "Utc::now", "Local::now", "Uuid::new_v4", "Uuid::now_v7",
             "thread_rng", "rand::rng()", "rand::random", "from_entropy", "from_os_rng", "OsRng", "getrandom",
             "fastrand::", "RandomState")


class Repo:
    def __init__(self, path: Path, ref: str):
        self.path, self.ref = path, ref
        r = subprocess.run(["git", "-C", str(path), "ls-tree", "-r", "--name-only", ref], capture_output=True,
                           text=True)
        if r.returncode != 0:
            raise OSError(f"{path}: cannot list {ref}")
        self.files = r.stdout.split()

    def show(self, name: str) -> str:
        r = subprocess.run(["git", "-C", str(self.path), "show", f"{self.ref}:{name}"], capture_output=True)
        return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else ""

    def grep_re(self, pattern: str, paths: list[str]) -> list[tuple[str, str]]:
        """(file:line, matched text) of each match of an extended regular expression at ref"""
        if not paths:
            return []
        r = subprocess.run(["git", "-C", str(self.path), "grep", "-n", "-o", "-E", "-e", pattern, self.ref, "--",
                            *paths], capture_output=True, text=True)
        out = []
        for line in r.stdout.splitlines():
            parts = line.split(":", 3)
            out.append((f"{parts[1]}:{parts[2]}", parts[3].split()[-1] if len(parts) > 3 else ""))
        return out

    def grep(self, pattern: str, paths: list[str]) -> list[str]:
        """file:line of each match of a fixed string in the given paths at ref"""
        if not paths:
            return []
        r = subprocess.run(["git", "-C", str(self.path), "grep", "-n", "-F", "-e", pattern, self.ref, "--", *paths],
                           capture_output=True, text=True)
        # each line is <ref>:<path>:<line>:<text>
        return [":".join(line.split(":")[1:3]) for line in r.stdout.splitlines()]


def clippy_lists(repo: Repo) -> dict[str, dict]:
    """per clippy configuration file: the disallowed methods (by type) and paths"""
    out = {}
    for f in repo.files:
        if re.search(r"(^|/)\.?clippy\.toml$", f) and not f.startswith(("tests/", "fuzz/")):
            try:
                data = tomllib.loads(repo.show(f))
            except tomllib.TOMLDecodeError:
                out[f] = {"error": "does not parse"}
                continue
            methods = {"f32": set(), "f64": set()}
            paths = set()
            for e in data.get("disallowed-methods", []):
                p = e["path"] if isinstance(e, dict) else e
                paths.add(p)
                m = re.fullmatch(r"(?:(?:core|std)::)?(f32|f64)::([a-z_0-9]+)", p)
                if m:
                    methods[m.group(1)].add(m.group(2))
            out[f] = {"methods": methods, "paths": paths}
    return out


def workflows(repo: Repo) -> dict[str, str]:
    return {f: repo.show(f) for f in repo.files if f.startswith(".github/workflows/") and f.endswith((".yml", ".yaml"))}


def measure(repo: Repo, banned: set[str]) -> dict[str, str]:
    cells: dict[str, str] = {}
    lists = clippy_lists(repo)
    wf = workflows(repo)
    wf_text = "\n".join(wf.values())
    src = [f for f in repo.files if f.endswith(".rs") and "/src/" in f"/{f}" and not f.startswith(("tests/", "fuzz/"))]

    # G1: every banned method in a clippy list (both widths), and a CI clippy run that denies it
    if not lists:
        cells["G1"] = "not found: no clippy configuration"
    else:
        parts = []
        for f, d in sorted(lists.items()):
            if "error" in d:
                parts.append(f"{f}: {d['error']}")
                continue
            miss = sorted((banned - d["methods"]["f32"]) | (banned - d["methods"]["f64"]))
            parts.append(f"{f}: " + ("all banned listed" if not miss else f"missing {', '.join(miss)}"))
        ci = "clippy" in wf_text and ("-D warnings" in wf_text or "disallowed_methods" in wf_text)
        cells["G1"] = "; ".join(parts) + ("; CI clippy -D found" if ci else "; CI clippy -D not found")

    # G2: f32/f64 max and min banned in a clippy list; a raw_* manifest
    mm = [f for f, d in lists.items() if "methods" in d and {"max", "min"} <= d["methods"]["f32"] & d["methods"]["f64"]]
    raw = [f for f in repo.files if re.search(r"raw_minmax.*manifest", f)]
    cells["G2"] = (f"max/min banned in {', '.join(sorted(mm))}" if mm else "max/min ban not found") + \
                  (f"; raw manifest {', '.join(raw)}" if raw else "; raw manifest not found")

    # G3: a checker for outer fma negation wired in CI
    g3 = [f for f in repo.files if "fneg" in f and f.endswith((".py", ".rs"))]
    cells["G3"] = (f"checker {', '.join(g3[:2])}" + ("; in CI" if any(Path(f).stem in wf_text for f in g3) else
                                                       "; not referenced by a workflow")) if g3 else "not found"

    # G4 / G5: the release profile on an aarch64 runner
    arm = re.search(r"(macos-latest|macos-1[45]\b|ubuntu-24\.04-arm|ubuntu-22\.04-arm)", wf_text)
    rel = "--release" in wf_text
    cells["G5"] = ("release test found" if rel else "release test not found") + \
                  (f", aarch64 runner {arm.group(1)}" if arm else ", aarch64 runner not found")
    cells["G4"] = "needs the test names (audit): " + cells["G5"]

    # G6: an exported identifier constant and a test that pins its hex
    ids = [f"{hit} ({name})" for hit, name in repo.grep_re(r"pub (const|static|use) [A-Za-z_:]*SEMANTICS_ID", src)]
    hexpin = re.compile(r'"[0-9a-f]{64}"')
    tests = [f for f in repo.files if f.endswith(".rs") and (f.startswith("tests/") or "/tests/" in f)]
    pinned = [f for f in tests if "SEMANTICS_ID" in (t := repo.show(f)) and hexpin.search(t)]
    own = [i for i in ids if "::" not in i.split("(", 1)[1]]
    reexport = [i for i in ids if "::" in i.split("(", 1)[1]]
    if own:
        g6 = "own identifier " + ", ".join(own)
    elif reexport:
        g6 = "re-export only " + ", ".join(reexport) + " (not a fold of the crate's own pins)"
    else:
        g6 = "identifier not found"
    cells["G6"] = g6 + (f"; a test names SEMANTICS_ID next to a 64-hex literal: {pinned[0]} (audit that it pins it)"
                        if pinned else "; hex pin test not found")

    # G10: clock / RNG banned in a clippy list, or present in library code
    banned_paths = set().union(*[d.get("paths", set()) for d in lists.values()]) if lists else set()
    lint = sorted(p for p in banned_paths if any(c.split("::")[-1].rstrip("()") in p for c in CLOCK_RNG if c.split("::")[-1]))
    lib = [f for f in src if "/bin/" not in f"/{f}" and not f.endswith("/main.rs")]
    uses = [hit for c in CLOCK_RNG for hit in repo.grep(c, lib)]
    scan = f"lexical scan of {len(CLOCK_RNG)} forms"
    cells["G10"] = (f"lint bans {', '.join(lint)}" if lint else "no lint") + \
                   (f"; {len(uses)} uses in library sources ({scan}, cfg(test) included, audit; first {uses[0]})"
                    if uses else f"; none of the forms in library sources ({scan})")

    # G12: CI runs ALICE-Law's public_text_check
    cells["G12"] = "public_text_check in CI" if "public_text_check" in wf_text else "not found"

    # G14: flags that change float semantics, and the tests that assert the environment
    flag_re = re.compile(r"target-cpu=native|fast-math")
    cfg = [f for f in repo.files if re.search(r"(^|/)\.cargo/config(\.toml)?$", f) or f.endswith("build.rs")]
    in_cfg = [f for f in cfg if flag_re.search(repo.show(f))]          # applies to every build: a finding
    in_wf = [f for f in wf if flag_re.search(wf[f])]                  # a job: a test leg or a shipped build, audit
    ftz = [f for f in tests if re.search(r"subnormal|denormal|flush.to.zero", repo.show(f), re.I)]
    cells["G14"] = (f"flag in build configuration {', '.join(in_cfg)}" if in_cfg else "no flag in build configuration") + \
                   (f"; flag in workflow {', '.join(in_wf)} (test leg or shipped build: audit)" if in_wf else "") + \
                   (f"; a test mentions subnormals: {ftz[0]} (audit that it asserts no flush)" if ftz
                    else "; environment test not found")
    for g in ("G7", "G8", "G9", "G11", "G13"):
        cells[g] = "audit"
    return cells


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repos", required=True)
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--law", default=".")
    ap.add_argument("--crates", default=",".join(CHAIN))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    gates = tomllib.loads(Path(a.law, "conformance/gates.toml").read_text(encoding="utf-8"))["gate"]
    g1 = next(g for g in gates if g["id"] == "G1")
    banned = {m["name"] for m in g1.get("methods", []) if m["verdict"] == "banned"}
    if not banned:
        print("error: conformance/gates.toml has no banned G1 method", file=sys.stderr)
        return 2
    table = {}
    for name in a.crates.split(","):
        try:
            table[name] = measure(Repo(Path(a.repos, name), a.ref), banned)
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    if not table:
        print("error: nothing was measured", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(table, indent=2, sort_keys=True))
        return 0
    ids = [g["id"] for g in gates]
    for name, cells in table.items():
        print(f"## {name}")
        for g in ids:
            print(f"- {g}: {cells.get(g, 'audit')}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
