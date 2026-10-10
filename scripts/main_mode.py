#!/usr/bin/env python3
"""Point the end to end check at the main branches of the core repositories

Main mode runs the same test as the published mode, with every core crate replaced
by a checkout of its repository's main branch. The checkouts sit side by side in
one directory (as the repositories expect: alice-lol depends on ../../ALICE-SDF and
others by path), and this script writes a Cargo `[patch.crates-io]` table that
points each crate at its checkout.

    python3 scripts/main_mode.py patch  <checkouts>   # writes e2e/.cargo/config.toml
    python3 scripts/main_mode.py verify               # after `cargo metadata` can resolve

`verify` reads the resolved graph of e2e/ and fails unless every crate below came
from its checkout. Cargo leaves a patch unused, and takes the crate from crates.io
instead, when the version on main does not match a requirement on it (for example a
new major version on main that the other crates do not accept yet). That is the
early warning this mode exists for, so it is a failure, not a note. A run that
found none of the crates in the graph fails too.
"""

from __future__ import annotations

import json
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
}

# checked out next to the core repositories because alice-lol names them by path
# (optional dependencies still have to be found for Cargo to resolve)
EXTRA_REPOSITORIES = ["ALICE-LLM"]


def patch(checkouts: Path) -> int:
    lines = [
        "# Written by scripts/main_mode.py: the core crates from their main branches",
        "[patch.crates-io]",
    ]
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
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"main mode: patched {len(CRATES)} crates in {CONFIG.relative_to(ROOT)}")
    return 0


def verify() -> int:
    out = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--manifest-path", str(E2E / "Cargo.toml")],
        check=True,
        capture_output=True,
        text=True,
        cwd=E2E,  # Cargo reads e2e/.cargo/config.toml from the working directory
    ).stdout
    packages = json.loads(out)["packages"]
    seen: dict[str, list[tuple[str, str]]] = {}
    for p in packages:
        if p["name"] in CRATES:
            seen.setdefault(p["name"], []).append((p["version"], p["source"] or "path"))
    problems = []
    for crate in CRATES:
        found = seen.get(crate, [])
        if not found:
            problems.append(f"{crate}: not in the graph")
        for version, source in found:
            if source != "path":
                problems.append(f"{crate} {version}: from {source}, not from main")
    compared = sum(len(v) for v in seen.values())
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
