#!/usr/bin/env python3
"""The release train of the core ALICE crates (release-train.toml), checked against crates.io.

    next                                 the crate to publish now: the first in `order`
                                         that is not published (or exempt)
    publish-gate <crate> --manifest M    run right before `cargo publish` of <crate>,
                                         from its release checkout (M = its Cargo.toml);
                                         refuses when
      - <crate> is not in the roster, or its state is not `prepared`
      - the manifest is not <crate> at its roster version
      - <crate> is already on crates.io at that version
      - an `alice-*` dependency of the manifest (normal or build, optional or not, any
        target; read from the manifest itself, so the roster cannot list a different
        set) has no crates.io version requirement, or
          - is a roster crate that is not `published` in the roster, is missing or yanked
            on crates.io at its roster version, or whose roster version the requirement
            does not accept (Cargo's rules: "0.4" does not accept 0.5.0)
          - is not a roster crate and its latest release on crates.io is missing, yanked
            or not accepted by the requirement

usage: release_train.py [--roster release-train.toml] {next | publish-gate <crate> --manifest M}
Exit 0 when the answer is "go" (next: a crate was found), 1 when it is "no", 2 when the
roster or the manifest is unreadable, nothing was compared, or crates.io could not be
read or answered something other than the version asked for.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
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


class RegistryError(RuntimeError):
    """crates.io answered, but not with the version asked for"""


def load_roster(text: str) -> dict[str, dict]:
    data = tomllib.loads(text)
    crates = data.get("crate", {})
    if not crates:
        raise RosterError("the roster lists no crate (compared nothing)")
    orders = set()
    for name, c in crates.items():
        for key in ("version", "state", "order"):
            if key not in c:
                raise RosterError(f"{name}: missing `{key}`")
        if "requires" in c:
            raise RosterError(f"{name}: `requires` is read from the crate's Cargo.toml, not the roster")
        if c["state"] not in STATES:
            raise RosterError(f"{name}: state {c['state']!r} is not one of {STATES}")
        dc.Version(c["version"])  # raises on a malformed version
        if c["order"] in orders:
            raise RosterError(f"{name}: order {c['order']} is used twice")
        orders.add(c["order"])
    return crates


def manifest_alice_requirements(text: str) -> tuple[str, str, dict[str, str]]:
    """(package name, package version, {alice crate: requirement}) of a Cargo.toml

    Every table cargo resolves at publish time is read: `[dependencies]` and
    `[build-dependencies]`, also under `[target.<cfg>]`; optional dependencies
    included; a renamed dependency (`package = "alice-…"`) under its crate name.
    A dependency without a version requirement (path only, git, workspace) is an
    error: crates.io would refuse it or resolve it from somewhere this cannot see.
    """
    data = tomllib.loads(text)
    pkg = data.get("package", {})
    tables = [data.get("dependencies", {}), data.get("build-dependencies", {})]
    for target in data.get("target", {}).values():
        tables += [target.get("dependencies", {}), target.get("build-dependencies", {})]
    out: dict[str, str] = {}
    for deps in tables:
        for key, spec in deps.items():
            name = spec.get("package", key) if isinstance(spec, dict) else key
            if not name.startswith("alice-"):
                continue
            if isinstance(spec, str):
                req = spec
            elif spec.get("workspace") or "git" in spec or not spec.get("version"):
                raise ValueError(f"{name}: no crates.io version requirement in the manifest: {spec!r}")
            else:
                req = spec["version"]
            dc.parse_requirement(req)
            if name in out and out[name] != req:
                raise ValueError(f"{name}: two different requirements ({out[name]!r}, {req!r})")
            out[name] = req
    return pkg.get("name", ""), pkg.get("version", ""), out


def release_state(fetch: dc.Fetch, crate: str, version: str) -> str:
    """'missing' (HTTP 404), 'yanked' or 'present' for one version on crates.io; anything
    else crates.io answers is a RegistryError"""
    try:
        data = fetch(f"{crate}/{version}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "missing"
        raise
    v = data.get("version") if isinstance(data, dict) else None
    if not isinstance(v, dict) or "num" not in v:
        raise RegistryError(f"{crate}/{version}: crates.io answered without a version: {str(data)[:200]}")
    if v["num"] != version:
        raise RegistryError(f"{crate}/{version}: crates.io answered version {v['num']}")
    return "yanked" if v.get("yanked") else "present"


def next_crate(roster: dict[str, dict]) -> tuple[str | None, int]:
    """(crate, unpublished count): the first crate in `order` that is not published / exempt
    (the publish-gate refuses it while an upstream is not published yet)"""
    pending = [(c["order"], n) for n, c in roster.items() if c["state"] not in ("published", "exempt")]
    if not pending:
        return None, 0
    return min(pending)[1], len(pending)


def publish_gate(roster: dict[str, dict], crate: str, manifest_text: str,
                 fetch: dc.Fetch) -> tuple[list[str], int]:
    """(problems, compared) for publishing `crate` now from the given manifest"""
    if crate not in roster:
        return [f"{crate} is not in the roster"], 1
    c = roster[crate]
    problems: list[str] = []
    compared = 1
    name, version, requires = manifest_alice_requirements(manifest_text)
    if name != crate or version != c["version"]:
        problems.append(f"the manifest is {name or '?'} {version or '?'}, the roster has {crate} {c['version']}")
    if c["state"] != "prepared":
        problems.append(f"{crate}: state is {c['state']!r}, a publish needs 'prepared'")
    own = release_state(fetch, crate, c["version"])
    if own != "missing":
        problems.append(f"{crate} {c['version']} is already on crates.io ({own})")
    for dep, req in sorted(requires.items()):
        compared += 1
        if dep in roster:
            d = roster[dep]
            if d["state"] != "published":
                problems.append(f"{crate} requires {dep} {req!r}: {dep} {d['version']} is {d['state']!r}, not published")
                continue
            target = d["version"]
        else:
            target = dc.latest_release(fetch, dep)
        state = release_state(fetch, dep, target)
        if state != "present":
            problems.append(f"{crate} requires {dep} {req!r}: {dep} {target} is {state} on crates.io")
            continue
        if not dc.matches(req, target):
            problems.append(f"{crate} requires {dep} {req!r}, which does not accept the published {target}")
    return problems, compared


def main(argv: list[str] | None = None, fetch: dc.Fetch = dc.http_fetch) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("next")
    g = sub.add_parser("publish-gate")
    g.add_argument("crate")
    g.add_argument("--manifest", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        roster = load_roster(a.roster.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"error: {a.roster}: {e}", file=sys.stderr)
        return 2
    if a.cmd == "next":
        name, pending = next_crate(roster)
        if name is None:
            print("nothing to publish: every crate in the roster is published or exempt")
            return 1
        print(f"next: {name} {roster[name]['version']} (state {roster[name]['state']}, {pending} unpublished)")
        return 0
    try:
        manifest_text = a.manifest.read_text(encoding="utf-8")
        problems, compared = publish_gate(roster, a.crate, manifest_text, fetch)
    except (OSError, ValueError, RegistryError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        # OSError covers a reset connection; ValueError covers a malformed manifest,
        # requirement or version, and an unreadable JSON body
        print(f"error: {e}", file=sys.stderr)
        return 2
    for p in problems:
        print(f"refused: {p}")
    if problems:
        return 1
    print(f"publish-gate: go for {a.crate} {roster[a.crate]['version']} ({compared} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
