#!/usr/bin/env python3
"""Point the end to end check at the main branches of the core repositories

Main mode runs the same test as the published mode, with every core crate replaced
by a checkout of its repository's main branch. The checkouts sit side by side in
one directory (as the repositories expect: alice-lol depends on ../../ALICE-SDF and
others by path), and this script writes a Cargo `[patch.crates-io]` table that
points each crate at its checkout.

    python3 scripts/main_mode.py patch  <checkouts>   # writes e2e/.cargo/config.toml
    python3 scripts/main_mode.py verify               # after `cargo metadata` can resolve

`patch` also raises e2e/Cargo.toml's own direct requirements on these crates to the
version on main where they do not accept it, and turns on the e2e crate's `main-mode`
feature, in the CI checkout only (the committed manifest keeps the published
requirements the published mode resolves). The e2e crate is the test, not a consumer
whose lag needs a warning: a consumer that does not accept a new major is reported
before the push by the pre-push wiring check and before publishing by
`scripts/release_train.py publish-gate`.

`verify` reads the resolved graph of e2e/ and fails unless every crate below came
from its checkout, each at least once. Cargo leaves a patch unused, and takes the
crate from crates.io instead, when the version on main does not match a requirement
on it: after the raise that can only be a requirement of one core crate on another
(for example a new major version on main that another crate's main does not accept
yet). That is the early warning this mode exists for, so it is a failure, not a
note. A run that found none of the crates in the graph fails too.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
E2E = ROOT / "e2e"
CONFIG = E2E / ".cargo" / "config.toml"

# crate -> (repository, directory of the crate inside it)
CRATES = {
    "alice-det-math": ("ALICE-DetMath", "."),
    "alice-crypto": ("ALICE-Crypto", "."),
    "alice-zip": ("ALICE-Zip", "."),
    "alice-analytics": ("ALICE-Analytics", "."),
    "alice-db": ("ALICE-DB", "."),
    "alice-physics": ("ALICE-Physics", "."),
    "alice-sdf": ("ALICE-SDF", "."),
    "alice-lol": ("ALICE-LOL", "alice-lol"),
    "alice-lol-macro": ("ALICE-LOL", "alice-lol-macro"),
    "alice-edge": ("ALICE-Edge", "."),
}

# checked out next to the core repositories because alice-lol names them by path
# (optional dependencies still have to be found for Cargo to resolve)
EXTRA_REPOSITORIES = ["ALICE-LLM"]


def _version(text: str) -> tuple[int, ...]:
    parts = re.match(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
    if not parts:
        raise ValueError(f"not a version: {text!r}")
    return tuple(int(x) if x is not None else -1 for x in parts.groups())


def accepts(requirement: str, version: str) -> bool:
    """Cargo's rule for the comparators e2e/Cargo.toml uses: a bare or caret version
    (`"0.5"`, `"^3"`), `=`, `>=`, `>`, `<`, `<=`, comma-separated"""
    v = tuple(int(x) for x in version.split("-")[0].split("."))
    for comparator in (c.strip() for c in requirement.split(",") if c.strip()):
        m = re.match(r"^(\^|=|>=|<=|>|<)?\s*([0-9].*)$", comparator)
        if not m:
            raise ValueError(f"requirement {requirement!r}: {comparator!r} is not a comparator this reads")
        op, bound = m.group(1) or "^", m.group(2)
        b = _version(bound)
        given = [x for x in b if x >= 0]
        full = tuple((given + [0, 0, 0])[:3])
        if op == "^":
            if v < full:
                return False
            # the first non-zero given part is fixed (0.x fixes x, 0.0.x fixes x)
            fixed = next((i for i, x in enumerate(given) if x != 0), len(given) - 1)
            if list(v[: fixed + 1]) != given[: fixed + 1]:
                return False
        elif op == "=":
            if list(v[: len(given)]) != given:
                return False
        elif op == ">=" and v < full:
            return False
        elif op == ">" and v <= full:
            return False
        elif op == "<" and v >= full:
            return False
        elif op == "<=" and v > full:
            return False
    return True


DEP_RE = re.compile(r'^(?P<name>alice-[\w-]+)\s*=\s*(?P<rest>.*)$', re.M)
REQ_RE = re.compile(r'^(?P<pre>\{.*?\bversion\s*=\s*|)"(?P<req>[^"]*)"')


def raise_requirements(manifest: str, versions: dict[str, str]) -> tuple[str, list[str]]:
    """e2e/Cargo.toml with each direct requirement on a core crate that does not accept
    the version on main replaced by that version (caret), and the lines it changed"""
    changed: list[str] = []

    def one(m: re.Match) -> str:
        name, rest = m["name"], m["rest"]
        if name not in versions:
            return m.group(0)
        r = REQ_RE.match(rest)
        if not r:
            raise ValueError(f"e2e/Cargo.toml: {name} has no version requirement to compare")
        if accepts(r["req"], versions[name]):
            return m.group(0)
        changed.append(f'{name}: "{r["req"]}" -> "{versions[name]}"')
        return f'{name} = {r["pre"]}"{versions[name]}"{rest[r.end():]}'

    out = DEP_RE.sub(one, manifest)
    if not re.search(r"^\[features\]\s*$", out, re.M) or "main-mode = []" not in out:
        raise ValueError("e2e/Cargo.toml has no `main-mode = []` feature to turn on")
    out = re.sub(r"^\[features\]\s*$", '[features]\ndefault = ["main-mode"]', out, count=1, flags=re.M)
    return out, changed


def patch(checkouts: Path) -> int:
    lines = [
        "# Written by scripts/main_mode.py: the core crates from their main branches",
        "[patch.crates-io]",
    ]
    versions: dict[str, str] = {}
    for crate, (repo, sub) in CRATES.items():
        path = (checkouts / repo / sub).resolve()
        manifest = path / "Cargo.toml"
        if not manifest.is_file():
            print(f"main mode: no {manifest}", file=sys.stderr)
            return 2
        name = tomllib.loads(manifest.read_text(encoding="utf-8"))["package"]["name"]
        if name != crate:
            print(f"main mode: {manifest} is {name}, not {crate}", file=sys.stderr)
            return 2
        lines.append(f'{crate} = {{ path = "{path.as_posix()}" }}')
        versions[crate] = tomllib.loads(manifest.read_text(encoding="utf-8"))["package"]["version"]
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"main mode: patched {len(CRATES)} crates in {CONFIG.relative_to(ROOT)}")
    manifest = E2E / "Cargo.toml"
    raised, changed = raise_requirements(manifest.read_text(encoding="utf-8"), versions)
    manifest.write_text(raised, encoding="utf-8")
    for line in changed:
        print(f"main mode: e2e requirement raised to main: {line}")
    print(f"main mode: {len(changed)} e2e requirement(s) raised, feature main-mode on")
    return 0


def problems_of(packages: list[dict]) -> tuple[dict[str, list[tuple[str, str]]], list[str], int]:
    """(each core crate's resolved (version, source) list, the problems, crates compared):
    a core crate missing from the graph, or present from anywhere but its checkout, is one"""
    seen: dict[str, list[tuple[str, str]]] = {}
    for p in packages:
        if p["name"] in CRATES:
            seen.setdefault(p["name"], []).append((p["version"], p["source"] or "path"))
    problems = []
    for crate in CRATES:
        found = seen.get(crate, [])
        if not any(source == "path" for _, source in found):
            problems.append(f"{crate}: not resolved from main (0 times)")
        for version, source in found:
            if source != "path":
                problems.append(f"{crate} {version}: from {source}, not from main")
    return seen, problems, sum(len(v) for v in seen.values())


def verify() -> int:
    if not CONFIG.is_file():
        # the patch step failed or was skipped: every crate would read as "from crates.io",
        # which looks like a wiring failure when it is not one
        print(f"main mode: the patch did not run (no {CONFIG.relative_to(ROOT)}), nothing to verify",
              file=sys.stderr)
        return 2
    out = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--manifest-path", str(E2E / "Cargo.toml")],
        check=True,
        capture_output=True,
        text=True,
        cwd=E2E,  # Cargo reads e2e/.cargo/config.toml from the working directory
    ).stdout
    packages = json.loads(out)["packages"]
    seen, problems, compared = problems_of(packages)
    for crate in CRATES:
        for version, source in seen.get(crate, []):
            print(f"| {crate} | {version} | {'main' if source == 'path' else source} |")
    print(f"main mode: compared {compared} resolved crates, {len(problems)} problem(s)")
    for p in problems:
        print(f"main mode: {p}")
    if compared == 0:
        return 2
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "patch":
        return patch(Path(argv[2]))
    if len(argv) == 2 and argv[1] == "verify":
        return verify()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
