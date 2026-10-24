#!/usr/bin/env python3
"""Fail when one build links two copies of the same `alice-*` crate

Two copies (two versions, or the same version from two sources such as crates.io and a git commit)
mean two generations of the same law in one binary: `alice-det-math` 0.5.1 from crates.io next to
0.5.2 from git evaluates the transcendentals with two different arithmetics. The check reads the
resolved graph from `cargo metadata --all-features` (every target platform, dev and build
dependencies included), so it works for a crate without a tracked Cargo.lock as well.

Fails when an `alice-*` name resolves to more than one package, or when no `alice-*` package was
found at all (the check compared nothing).

    python3 scripts/alice_dup_check.py [<dir with Cargo.toml> ...]   (default: the repository root)
    python3 scripts/alice_dup_check.py --self-test
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def metadata(root: Path) -> dict:
    cmd = ["cargo", "metadata", "--format-version", "1", "--all-features",
           "--manifest-path", str(root / "Cargo.toml")]
    run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if run.returncode != 0:
        sys.stderr.write(run.stderr)
        raise SystemExit(f"{root}: cargo metadata failed (exit {run.returncode})")
    return json.loads(run.stdout)


def duplicates(meta: dict) -> tuple[int, dict[str, list[str]]]:
    """(number of alice-* packages in the resolved graph, name -> its copies when there is more than one)"""
    by_id = {p["id"]: p for p in meta["packages"]}
    copies: dict[str, set[str]] = {}
    for node in meta["resolve"]["nodes"]:
        pkg = by_id[node["id"]]
        if pkg["name"].startswith("alice-"):
            copies.setdefault(pkg["name"], set()).add(f'{pkg["version"]} ({pkg["source"] or "path"})')
    n = sum(len(v) for v in copies.values())
    return n, {k: sorted(v) for k, v in sorted(copies.items()) if len(v) > 1}


def check(root: Path, meta: dict) -> list[str]:
    n, dup = duplicates(meta)
    if n == 0:
        return [f"{root}: no alice-* package in the resolved graph (nothing compared)"]
    print(f"{root}: {n} alice-* package(s) compared, {len(dup)} duplicated")
    return [f"{root}: {name} is linked {len(v)} times: {', '.join(v)}" for name, v in dup.items()]


def self_test() -> int:
    def meta(*pkgs: tuple[str, str, str | None]) -> dict:
        packages = [{"id": f"{n} {v} {s}", "name": n, "version": v, "source": s} for n, v, s in pkgs]
        return {"packages": packages, "resolve": {"nodes": [{"id": p["id"]} for p in packages]}}

    reg = "registry+https://github.com/rust-lang/crates.io-index"
    git = "git+https://github.com/ext-sakamoro/ALICE-DetMath?rev=6384856c#6384856c"
    cases = [
        ("one copy each", meta(("alice-text", "2.0.0", None), ("alice-det-math", "0.5.2", git)), 2, []),
        ("two versions", meta(("alice-det-math", "0.5.1", reg), ("alice-det-math", "0.5.2", git)), 2, ["alice-det-math"]),
        ("same version, two sources", meta(("alice-zip", "0.11.0", reg), ("alice-zip", "0.11.0", git)), 2, ["alice-zip"]),
        ("two versions, same source", meta(("alice-det-math", "0.5.1", reg), ("alice-det-math", "0.6.0", reg)), 2, ["alice-det-math"]),
        ("other crates may repeat", meta(("alice-text", "2.0.0", None), ("rand", "0.8.5", reg), ("rand", "0.9.0", reg)), 1, []),
        ("nothing compared", meta(("rand", "0.8.5", reg)), 0, []),
    ]
    bad = 0
    for name, m, want_n, want_dup in cases:
        n, dup = duplicates(m)
        if n != want_n or list(dup) != want_dup:
            print(f"FAIL {name}: n={n} dup={list(dup)}, want n={want_n} dup={want_dup}")
            bad += 1
    # a node absent from resolve (an unused optional dependency) is not counted
    m = meta(("alice-det-math", "0.5.1", reg), ("alice-det-math", "0.5.2", git))
    m["resolve"]["nodes"] = m["resolve"]["nodes"][1:]
    if duplicates(m) != (1, {}):
        print("FAIL unresolved package counted")
        bad += 1
    # check(): nothing compared is an error, a clean graph is not, a duplicate names both copies
    extra = [
        ("check: nothing compared", check(Path("r"), meta(("rand", "0.8.5", reg))), 1, "nothing compared"),
        ("check: clean graph", check(Path("r"), meta(("alice-text", "2.0.0", None))), 0, ""),
        ("check: duplicate", check(Path("r"), meta(("alice-zip", "0.11.0", reg), ("alice-zip", "0.11.0", git))), 1, "linked 2 times"),
    ]
    for name, errors, want, text in extra:
        if len(errors) != want or (text and text not in errors[0]):
            print(f"FAIL {name}: {errors}")
            bad += 1
    total = len(cases) + 1 + len(extra)
    print(f"self-test: {total - bad}/{total} pass")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        return self_test()
    roots = [Path(a).resolve() for a in argv] or [ROOT]
    errors = [e for r in roots for e in check(r, metadata(r))]
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
