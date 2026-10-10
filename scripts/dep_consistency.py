#!/usr/bin/env python3
"""Dependency consistency of the core ALICE crates, from their crates.io releases.

Three checks, each of which compares something and fails when it compared nothing:

1. single version: every `alice-*` package appears in `Cargo.lock` at exactly one
   version (two versions of one crate are two copies of its types and its arithmetic;
   a value saved by one cannot be read back as the same type by the other)
2. presence: every core crate is in the lock file
3. requirement vs latest: each `alice-*` requirement on a core crate, both those of
   this repository's `Cargo.toml` and those of every core crate at the version the
   lock file resolved, accepts the latest release of that crate (a requirement that
   does not is how a second version gets into a consumer's lock file)

usage: dep_consistency.py [--lock Cargo.lock] [--manifest Cargo.toml]
Prints a Markdown report. Exit 1 when a check finds a problem, 2 when a check
compared nothing or crates.io could not be read.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Callable

CORE = (
    "alice-lol", "alice-det-math", "alice-sdf", "alice-physics",
    "alice-db", "alice-zip", "alice-crypto", "alice-analytics",
)
API = "https://crates.io/api/v1/crates"
USER_AGENT = "alice-law-integration (https://github.com/ext-sakamoro/ALICE-Law)"

# ---------------------------------------------------------------- versions (Cargo's semver rules)

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


class Version:
    def __init__(self, text: str):
        m = VERSION_RE.match(text.strip())
        if not m:
            raise ValueError(f"not a version: {text!r}")
        self.text = text.strip()
        self.core = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        self.pre = tuple(m.group(4).split(".")) if m.group(4) else ()

    def key(self):
        # a release sorts after its pre-releases; numeric identifiers sort below alphanumeric ones
        pre = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in self.pre)
        return (self.core, 0 if self.pre else 1, pre)

    def __lt__(self, other):
        return self.key() < other.key()

    def __le__(self, other):
        return self.key() <= other.key()

    def __eq__(self, other):
        return self.key() == other.key()

    def __repr__(self):
        return self.text


COMPARATOR_RE = re.compile(r"^(\^|~|=|>=|<=|>|<)?\s*(\d+|\*)(?:\.(\d+|\*))?(?:\.(\d+|\*))?(?:-([0-9A-Za-z.-]+))?$")


def _bounds(op: str, parts: list[int | None], pre: tuple) -> tuple[Version | None, bool, Version | None]:
    """(low, low_inclusive, high_exclusive) of one comparator; None = unbounded"""
    major, minor, patch = parts

    def v(a, b, c, p=()):
        s = f"{a}.{b}.{c}" + ("-" + ".".join(p) if p else "")
        return Version(s)

    if major is None:  # *
        return None, True, None
    low = v(major, minor or 0, patch or 0, pre)
    if op in ("", "^"):
        if major > 0 or minor is None:
            high = v(major + 1, 0, 0)
        elif minor > 0 or patch is None:
            high = v(0, minor + 1, 0)
        else:
            high = v(0, 0, patch + 1)
        return low, True, high
    if op == "~":
        high = v(major + 1, 0, 0) if minor is None else v(major, minor + 1, 0)
        return low, True, high
    if op == "=":
        if minor is None:
            return low, True, v(major + 1, 0, 0)
        return low, True, v(major, minor + 1, 0)
    if op == ">=":
        return low, True, None
    if op == ">":
        if minor is None:
            return v(major + 1, 0, 0), True, None
        return v(major, minor + 1, 0), True, None
    if op == "<":
        return None, True, low
    if op == "<=":
        if minor is None:
            return None, True, v(major + 1, 0, 0)
        return None, True, v(major, minor + 1, 0)
    raise ValueError(op)


def matches(req: str, version: str) -> bool:
    """Whether `version` satisfies the Cargo requirement `req` (comma = and)

    A pre-release version satisfies a requirement only when one of its comparators
    names the same major.minor.patch with a pre-release (Cargo's rule)
    """
    ver = Version(version)
    comparators = [c.strip() for c in req.split(",") if c.strip()]
    if not comparators:
        raise ValueError(f"empty requirement: {req!r}")
    pre_allowed = not ver.pre
    for c in comparators:
        m = COMPARATOR_RE.match(c)
        if not m:
            raise ValueError(f"cannot read requirement {req!r}")
        op = m.group(1) or ""
        nums = [None if g in (None, "*") else int(g) for g in m.group(2, 3, 4)]
        pre = tuple(m.group(5).split(".")) if m.group(5) else ()
        if pre and nums[0] is not None and nums[1] is not None and nums[2] is not None \
                and (nums[0], nums[1], nums[2]) == ver.core:
            pre_allowed = True
        if None not in nums and op in ("=", ">=", ">", "<", "<="):
            # a full version: compare with it directly (pre-release included)
            t = Version(f"{nums[0]}.{nums[1]}.{nums[2]}" + ("-" + ".".join(pre) if pre else ""))
            if not {"=": ver == t, ">=": t <= ver, ">": t < ver, "<": ver < t, "<=": ver <= t}[op]:
                return False
            continue
        low, low_inc, high = _bounds(op, nums, pre)
        if low is not None and (ver < low if low_inc else ver <= low):
            return False
        if high is not None and not ver < high:
            return False
    return pre_allowed


# ---------------------------------------------------------------- lock file and manifest

PACKAGE_RE = re.compile(r'^\[\[package\]\]\s*\nname = "([^"]+)"\s*\nversion = "([^"]+)"', re.M)


def lock_packages(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for name, version in PACKAGE_RE.findall(text):
        out[name].append(version)
    return out


def manifest_requirements(text: str) -> dict[str, str]:
    """`alice-*` requirements of the `[dependencies]` table (`name = "req"` or `{ version = "req" }`)"""
    out = {}
    section = None
    for line in text.splitlines():
        s = line.split("#", 1)[0].strip()
        if s.startswith("["):
            section = s
            continue
        if section != "[dependencies]":
            continue
        m = re.match(r'^(alice-[a-z0-9-]+)\s*=\s*(?:"([^"]+)"|\{.*?\bversion\s*=\s*"([^"]+)")', s)
        if m:
            out[m.group(1)] = m.group(2) or m.group(3)
    return out


# ---------------------------------------------------------------- crates.io

Fetch = Callable[[str], dict]


def http_fetch(path: str) -> dict:
    req = urllib.request.Request(f"{API}/{path}", headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            time.sleep(1)  # crates.io asks for at most one request per second
            return data
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    raise AssertionError("unreachable")


def latest_release(fetch: Fetch, crate: str) -> str:
    c = fetch(crate)["crate"]
    return c.get("max_stable_version") or c["max_version"]


def published_requirements(fetch: Fetch, crate: str, version: str) -> list[tuple[str, str, bool]]:
    """(dependency, requirement, optional) of the normal `alice-*` dependencies of one release"""
    deps = fetch(f"{crate}/{version}/dependencies")["dependencies"]
    return sorted({(d["crate_id"], d["req"], bool(d["optional"])) for d in deps
                   if d["kind"] == "normal" and d["crate_id"].startswith("alice-")})


# ---------------------------------------------------------------- the checks

def check(lock_text: str, manifest_text: str, fetch: Fetch) -> tuple[list[str], list[str], int]:
    """(report lines, problems, compared count)"""
    lines: list[str] = []
    problems: list[str] = []
    compared = 0

    pkgs = lock_packages(lock_text)
    alice = {n: sorted(vs, key=Version) for n, vs in pkgs.items() if n.startswith("alice-")}
    lines += ["## 1. One version per crate", "", "| crate | versions in Cargo.lock | |", "|---|---|---|"]
    for name, vs in sorted(alice.items()):
        compared += 1
        bad = len(vs) > 1
        lines.append(f"| {name} | {', '.join(vs)} | {'**2 or more versions**' if bad else 'ok'} |")
        if bad:
            problems.append(f"{name} is linked at {len(vs)} versions: {', '.join(vs)}")
    lines.append("")

    lines += ["## 2. Every core crate is present", ""]
    missing = [c for c in CORE if c not in alice]
    compared += len(CORE)
    lines.append(f"{len(CORE) - len(missing)} of {len(CORE)} core crates in Cargo.lock"
                 + (f"; missing: {', '.join(missing)}" if missing else ""))
    lines.append("")
    problems += [f"{c} is not in Cargo.lock" for c in missing]

    lines += ["## 3. Requirements accept the latest release", "",
              "| crate (version) | requires | latest | |", "|---|---|---|---|"]
    latest = {c: latest_release(fetch, c) for c in CORE}
    sources: list[tuple[str, str, str, bool]] = [
        ("this repository", dep, req, False) for dep, req in sorted(manifest_requirements(manifest_text).items())
    ]
    for crate in CORE:
        for version in alice.get(crate, []):
            for dep, req, optional in published_requirements(fetch, crate, version):
                sources.append((f"{crate} {version}", dep, req, optional))
    for who, dep, req, optional in sources:
        if dep not in CORE:
            continue
        compared += 1
        ok = matches(req, latest[dep])
        opt = " (optional)" if optional else ""
        lines.append(f"| {who} | {dep} {req}{opt} | {latest[dep]} | {'ok' if ok else '**does not accept the latest**'} |")
        if not ok:
            problems.append(f"{who} requires {dep} {req}{opt}, which does not accept the latest release {latest[dep]}")
    lines.append("")
    return lines, problems, compared


def main(argv: list[str] | None = None, fetch: Fetch = http_fetch) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lock", default="Cargo.lock")
    ap.add_argument("--manifest", default="Cargo.toml")
    a = ap.parse_args(argv)
    lock, manifest = Path(a.lock), Path(a.manifest)
    if not lock.is_file() or not manifest.is_file():
        print(f"error: {lock} or {manifest} is missing", file=sys.stderr)
        return 2
    try:
        lines, problems, compared = check(lock.read_text(encoding="utf-8"),
                                          manifest.read_text(encoding="utf-8"), fetch)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print("# Dependency consistency of the core ALICE crates\n")
    print("\n".join(lines))
    if not lock_packages(lock.read_text(encoding="utf-8")) or compared == 0:
        print("error: nothing was compared", file=sys.stderr)
        return 2
    print(f"{compared} comparisons, {len(problems)} problems\n")
    for p in problems:
        print(f"- {p}")
        print(f"error: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
