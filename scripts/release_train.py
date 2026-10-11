#!/usr/bin/env python3
"""The release train of the core ALICE crates (release-train.toml), checked against crates.io.

    next                  the crate that can be published now: not yet published, and
                          every crate it requires is published (first in `order`)
    publish-gate <crate>  run right before `cargo publish` of <crate>; refuses when
                          - <crate> is not in the roster, or its state is not `prepared`
                          - <crate> is already on crates.io at its roster version
                          - a crate it requires is not in the roster, is not `published`
                            in the roster, is missing or yanked on crates.io at its roster
                            version, or the requirement does not accept that version
                            (Cargo's rules: "0.4" does not accept 0.5.0)

usage: release_train.py [--roster release-train.toml] {next | publish-gate <crate>}
Exit 0 when the answer is "go" (next: a crate was found), 1 when it is "no", 2 when the
roster is unreadable, compares nothing, or crates.io could not be read.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import tomllib
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("dep_consistency", HERE / "dep_consistency.py")
dc = importlib.util.module_from_spec(_spec)
sys.modules["dep_consistency"] = dc
_spec.loader.exec_module(dc)

STATES = ("planned", "prepared-local", "prepared", "published", "exempt")
DEFAULT_ROSTER = HERE.parent / "release-train.toml"


class RosterError(ValueError):
    pass


def load_roster(text: str) -> dict[str, dict]:
    data = tomllib.loads(text)
    crates = data.get("crate", {})
    if not crates:
        raise RosterError("the roster lists no crate (compared nothing)")
    for name, c in crates.items():
        for key in ("version", "state", "order", "requires"):
            if key not in c:
                raise RosterError(f"{name}: missing `{key}`")
        if c["state"] not in STATES:
            raise RosterError(f"{name}: state {c['state']!r} is not one of {STATES}")
        dc.Version(c["version"])  # raises on a malformed version
        for dep, req in c["requires"].items():
            dc.parse_requirement(req)  # raises on a malformed requirement
            if dep in crates and crates[dep]["order"] >= c["order"]:
                raise RosterError(f"{name} requires {dep} but does not come after it in `order`")
    return crates


def release_state(fetch: dc.Fetch, crate: str, version: str) -> str:
    """'missing', 'yanked' or 'present' for one version on crates.io"""
    try:
        data = fetch(f"{crate}/{version}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "missing"
        raise
    v = data.get("version")
    if not v:
        return "missing"
    return "yanked" if v.get("yanked") else "present"


def next_crate(roster: dict[str, dict]) -> tuple[str | None, int]:
    """(crate, compared): the first crate in `order` that is not published / exempt and
    whose required roster crates are all published"""
    compared = 0
    for name, c in sorted(roster.items(), key=lambda kv: kv[1]["order"]):
        if c["state"] in ("published", "exempt"):
            continue
        compared += 1
        if all(roster[d]["state"] == "published" for d in c["requires"] if d in roster):
            return name, compared
    return None, compared


def publish_gate(roster: dict[str, dict], crate: str, fetch: dc.Fetch) -> tuple[list[str], int]:
    """(problems, compared) for publishing `crate` now"""
    if crate not in roster:
        return [f"{crate} is not in the roster"], 1
    c = roster[crate]
    problems: list[str] = []
    compared = 1
    if c["state"] != "prepared":
        problems.append(f"{crate}: state is {c['state']!r}, a publish needs 'prepared'")
    own = release_state(fetch, crate, c["version"])
    if own != "missing":
        problems.append(f"{crate} {c['version']} is already on crates.io ({own})")
    for dep, req in sorted(c["requires"].items()):
        compared += 1
        if dep not in roster:
            problems.append(f"{crate} requires {dep} {req!r}, which is not in the roster")
            continue
        d = roster[dep]
        if d["state"] != "published":
            problems.append(f"{crate} requires {dep} {req!r}: {dep} {d['version']} is {d['state']!r}, not published")
            continue
        state = release_state(fetch, dep, d["version"])
        if state != "present":
            problems.append(f"{crate} requires {dep} {req!r}: {dep} {d['version']} is {state} on crates.io")
            continue
        if not dc.matches(req, d["version"]):
            problems.append(f"{crate} requires {dep} {req!r}, which does not accept the published {d['version']}")
    return problems, compared


def main(argv: list[str] | None = None, fetch: dc.Fetch = dc.http_fetch) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("next")
    g = sub.add_parser("publish-gate")
    g.add_argument("crate")
    a = ap.parse_args(argv)
    try:
        roster = load_roster(a.roster.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"error: {a.roster}: {e}", file=sys.stderr)
        return 2
    if a.cmd == "next":
        name, compared = next_crate(roster)
        if compared == 0:
            print("nothing to publish: every crate in the roster is published or exempt")
            return 1
        if name is None:
            print(f"no crate can be published: each of the {compared} unpublished ones waits for an upstream")
            return 1
        print(f"next: {name} {roster[name]['version']} (state {roster[name]['state']})")
        return 0
    try:
        problems, compared = publish_gate(roster, a.crate, fetch)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"error: crates.io could not be read: {e}", file=sys.stderr)
        return 2
    for p in problems:
        print(f"refused: {p}")
    if problems:
        return 1
    print(f"publish-gate: go for {a.crate} {roster[a.crate]['version']} ({compared} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
