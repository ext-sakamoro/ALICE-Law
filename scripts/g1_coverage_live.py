#!/usr/bin/env python3
"""G1 (no-libm-direct) over the Law-bearing repositories: each one carries G1's clippy evidence, or is on a list
that may only shrink.

    g1_coverage_live.py [--owner NAME] [--local NAME=DIR] [--jobs N]
    g1_coverage_live.py --only NAME=DIR [--baseline FILE]
    g1_coverage_live.py --write-baseline
    g1_coverage_live.py --write-own NAME=DIR --baseline FILE

Why the scope is the Law-bearing repositories, and neither all of them nor fewer: G1 exists so that a Law's
result is the same bits on every platform, and a platform libm call in the path that computes or reads a Law
breaks that. A repository whose results need not be bit-identical (a UI tool, an LLM client, CLI glue) gains
nothing from G1 and is reported only; requiring det-math there would be cost without the property. A
repository that defines a Law, or computes through one, is exactly where a libm call changes a Law's bits, so
every such repository is in scope; leaving one out leaves a path where the bits can differ. Do not widen this
to every repository or narrow it to a hand-picked list: the set is derived from the code on every run.

A repository is Law-bearing when its production Rust code (`src/` of the root and of each workspace member,
not tests / benches / examples / fuzz, comments removed):
  * defines a Law: a `pub const` / `pub static` whose name ends in `SEMANTICS_ID`, a `fn law_id`, or a use of
    `LawIdHasher`; or
  * computes through one: it names `alice_<x>` and lists `alice-<x>` under [dependencies] (not dev or build
    dependencies) of a manifest, where alice-<x> is a package of a defining repository.
The repository that holds alice-det-math is out of scope (G1 bans the calls outside alice-det-math).

G1's clippy evidence (conformance/gates.toml, the methods with verdict "banned"): a clippy configuration that
lists every banned method for both f32 and f64, and a CI `cargo clippy` run that denies the lint
(scripts/measure_conformance.py reads both; the canary stays with each crate's manifest and G1's checks).

conformance/g1-coverage-baseline.toml (public repositories only; a private repository keeps its own file,
conformance-own/g1-coverage.toml, written with --write-own and checked by its own gate with this checker at a
pinned commit):
  definers = the public repositories that define a Law (the run's derivation must match it: a new definer or a
             lost one is red until the list is regenerated in a commit)
  lacking  = the Law-bearing public repositories without G1's clippy evidence (may only shrink)
Red: a Law-bearing repository lacking the evidence and not listed; a listed repository that has it or is no
longer Law-bearing; definers that differ; a `sha256:` entry; 0 repositories read; 0 Law-bearing repositories.
Exit 0 / 1 (findings) / 2 (unreadable, nothing compared).
"""
from __future__ import annotations

import argparse
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
import measure_conformance as mc  # noqa: E402

BASELINE = ROOT / "conformance" / "g1-coverage-baseline.toml"
GATES = ROOT / "conformance" / "gates.toml"
GITHUB = "https://github.com/"
DETMATH = "alice-det-math"
DEFINE_RE = re.compile(r"\bpub\s+(?:const|static)\s+[A-Z0-9_]*SEMANTICS_ID\b|\bfn\s+law_id\b|\bLawIdHasher\b")
NAME_RE = re.compile(r"\balice_[a-z0-9_]+\b")
SKIP = ("tests", "benches", "examples", "fuzz", "target")


def banned() -> set[str]:
    gates = tomllib.loads(GATES.read_text(encoding="utf-8"))["gate"]
    g1 = next(g for g in gates if g["id"] == "G1")
    out = {m["name"] for m in g1["methods"] if m["verdict"] == "banned"}
    if not out:
        raise hl.Unreadable("conformance/gates.toml has no banned G1 method")
    return out


def production_sources(repo: mc.Repo) -> list[str]:
    return [f for f in repo.files if f.endswith(".rs") and "src" in f.split("/")[:-1]
            and not set(f.split("/")[:-1]) & set(SKIP)]


def manifests(repo: mc.Repo) -> dict[str, dict]:
    out = {}
    for f in repo.files:
        if f.split("/")[-1] == "Cargo.toml" and not set(f.split("/")[:-1]) & set(SKIP):
            try:
                out[f] = tomllib.loads(repo.show(f))
            except tomllib.TOMLDecodeError as e:
                raise hl.Unreadable(f"{f}: {e}") from e
    return out


def facts(repo: mc.Repo, banned_methods: set[str]) -> dict:
    """what the run needs of one repository"""
    mans = manifests(repo)
    packages = {m["package"]["name"] for m in mans.values() if isinstance(m.get("package"), dict)
                and isinstance(m["package"].get("name"), str)}
    deps: set[str] = set()
    for m in mans.values():
        tables = [m.get("dependencies", {})]
        tables += [t.get("dependencies", {}) for t in m.get("target", {}).values() if isinstance(t, dict)]
        for t in tables:
            for k, v in t.items():
                name = v.get("package", k) if isinstance(v, dict) else k
                if name.startswith("alice-"):
                    deps.add(name)
    srcs = production_sources(repo)
    code = "\n".join(mc.rust_uncommented(repo.show(f)) for f in srcs)
    lists = mc.clippy_lists(repo)
    full = any("error" not in d and not (banned_methods - d["methods"]["f32"]) and not (banned_methods - d["methods"]["f64"])
               for d in lists.values())
    found, _, _ = mc.ci_clippy_deny(repo, mc.workflows(repo))
    return {"files": len(srcs), "packages": packages, "deps": deps, "names": set(NAME_RE.findall(code)),
            "defines": bool(DEFINE_RE.search(code)), "g1": full and bool(found),
            "detmath": DETMATH in packages}


def law_bearing(read: dict[str, dict], definer_packages: set[str]) -> dict[str, str]:
    """name -> why, for each Law-bearing repository read (definer_packages: the packages of every defining
    repository, read here or listed)"""
    out = {}
    for name, f in read.items():
        if f["detmath"] or f["files"] == 0:
            continue
        if f["defines"]:
            out[name] = "defines a Law"
            continue
        used = sorted(p for p in f["deps"] & definer_packages if p.replace("-", "_") in f["names"])
        if used:
            out[name] = "computes through " + ", ".join(used)
    return out


def load_baseline(path: Path) -> tuple[list[str], list[str]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    extra = set(data) - {"definers", "definer_packages", "lacking"}
    if extra:
        raise ValueError(f"{path.name}: unknown keys {sorted(extra)}")
    for k in ("definers", "lacking"):
        v = data.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"{path.name}: {k} is not a list of names")
        if any(x.startswith("sha256:") for x in v):
            raise ValueError(f"{path.name}: {k} holds a hashed name; a private repository keeps its own file")
    return data.get("definers", []), data.get("lacking", [])


def load_definer_packages(path: Path) -> set[str]:
    return set(tomllib.loads(path.read_text(encoding="utf-8")).get("definer_packages", []))


def compare(read: dict[str, dict], bearing: dict[str, str], lacking: list[str],
            definers: list[str] | None) -> list[str]:
    out = []
    if definers is not None:
        have = sorted(n for n, f in read.items() if f["defines"] and not f["detmath"])
        for n in sorted(set(have) - set(definers)):
            out.append(f"{n}: defines a Law but is not in definers (regenerate the baseline)")
        for n in sorted(set(definers) - set(have)):
            out.append(f"{n}: in definers but defines no Law here (regenerate the baseline)")
    for n in sorted(bearing):
        if not read[n]["g1"] and n not in lacking:
            out.append(f"{n}: Law-bearing ({bearing[n]}) without G1's clippy evidence and not in lacking")
    for n in sorted(lacking):
        if n not in read:
            continue
        if n not in bearing:
            out.append(f"{n}: in lacking but not Law-bearing (drop it)")
        elif read[n]["g1"]:
            out.append(f"{n}: has G1's clippy evidence now (drop it from lacking)")
    return out


def render(definers: list[str], packages: set[str], lacking: list[str]) -> str:
    def arr(xs):
        return "[\n" + "".join(f'    "{x}",\n' for x in sorted(xs)) + "]"
    return ("# G1 over the Law-bearing public repositories (scripts/g1_coverage_live.py; why the scope is these\n"
            "# and not all repositories is in its docstring). Regenerate with --write-baseline.\n"
            f"definers = {arr(definers)}\n# the packages of the definers, read by a repository's own check\n"
            f"definer_packages = {arr(packages)}\nlacking = {arr(lacking)}\n")


def read_live(owner: str, local: tuple[str, Path] | None, jobs: int, b: set[str]) -> tuple[dict, list[str]]:
    repos = [r for r in hl.public_repos(owner) if r["name"].startswith("ALICE-")]
    if not repos:
        raise hl.Unreadable("no public repository listed")
    errors: list[str] = []
    read: dict[str, dict] = {}

    def one(base: Path, r: dict):
        name = r["name"]
        if local and name == local[0]:
            return name, facts(mc.Repo(local[1], "HEAD"), b), None
        if r.get("size", 1) == 0:
            return name, None, None
        g = base / name
        try:
            subprocess.run(["git", "init", "-q", str(g)], check=True, capture_output=True)
            hl.git(g, "remote", "add", "origin", GITHUB + r["full_name"])
            hl.git(g, "fetch", "-q", "--depth", "1", "--no-tags", "origin",
                   f"+refs/heads/{r['default_branch']}:refs/remotes/origin/main")
            return name, facts(mc.Repo(g, "origin/main"), b), None
        except (hl.Unreadable, OSError, subprocess.CalledProcessError) as e:
            return name, None, str(e)
        finally:
            shutil.rmtree(g, ignore_errors=True)

    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=jobs) as ex:
        for name, f, err in ex.map(lambda r: one(Path(tmp), r), repos):
            if err:
                errors.append(f"{name}: {err}")
            elif f is not None:
                read[name] = f
    return read, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--local")
    ap.add_argument("--only")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--write-own")
    ap.add_argument("--baseline", type=Path)
    a = ap.parse_args(argv)
    try:
        b = banned()
    except (hl.Unreadable, OSError, KeyError, StopIteration) as e:
        print(f"g1_coverage: {e}", file=sys.stderr)
        return 2

    if a.only or a.write_own:
        spec = a.only or a.write_own
        name, _, d = spec.partition("=")
        if a.write_own and (a.baseline is None or a.baseline.resolve() == BASELINE.resolve()):
            print("g1_coverage: --write-own needs --baseline (the repository's own file)", file=sys.stderr)
            return 2
        try:
            f = facts(mc.Repo(Path(d), "HEAD"), b)
            pkgs = load_definer_packages(BASELINE)
        except (hl.Unreadable, OSError, tomllib.TOMLDecodeError) as e:
            print(f"g1_coverage: {e}", file=sys.stderr)
            return 2
        if f["files"] == 0:
            print(f"g1_coverage: {name}: no production Rust code read", file=sys.stderr)
            return 2
        if not pkgs:
            print("g1_coverage: the baseline lists no definer packages", file=sys.stderr)
            return 2
        bearing = law_bearing({name: f}, pkgs | (f["packages"] if f["defines"] else set()))
        if a.write_own:
            a.baseline.parent.mkdir(parents=True, exist_ok=True)
            lacking = [name] if name in bearing and not f["g1"] else []
            a.baseline.write_text(render([], set(), lacking), encoding="utf-8")
            print(f"g1_coverage: wrote {a.baseline} (Law-bearing: {bearing.get(name, 'no')}, G1: {f['g1']})")
            return 0
        path = a.baseline or BASELINE
        try:
            _, lacking = load_baseline(path)
        except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
            print(f"g1_coverage: {e}", file=sys.stderr)
            return 2
        findings = compare({name: f}, bearing, lacking, None)
        for x in findings:
            print(f"g1_coverage: {x}")
        print(f"g1_coverage: {name}: Law-bearing {bearing.get(name, 'no')}, G1 clippy evidence {f['g1']}, "
              f"{len(findings)} finding(s)")
        return 1 if findings else 0

    local = None
    if a.local:
        n, _, d = a.local.partition("=")
        local = (n, Path(d))
    try:
        read, errors = read_live(a.owner, local, a.jobs, b)
    except hl.Unreadable as e:
        print(f"g1_coverage: {e}", file=sys.stderr)
        return 2
    definers = sorted(n for n, f in read.items() if f["defines"] and not f["detmath"])
    pkgs = set().union(*(read[n]["packages"] for n in definers)) if definers else set()
    bearing = law_bearing(read, pkgs)
    for e in errors:
        print(f"g1_coverage: unreadable: {e}", file=sys.stderr)
    if errors or not read or not bearing:
        print(f"g1_coverage: {len(read)} read, {len(bearing)} Law-bearing (nothing to compare)", file=sys.stderr)
        return 2
    if a.write_baseline:
        path = a.baseline or BASELINE
        lacking = sorted(n for n in bearing if not read[n]["g1"])
        path.write_text(render(definers, pkgs, lacking), encoding="utf-8")
        print(f"g1_coverage: wrote {path}: {len(definers)} definers, {len(bearing)} Law-bearing, {len(lacking)} lacking")
        return 0
    try:
        listed_definers, lacking = load_baseline(a.baseline or BASELINE)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        print(f"g1_coverage: {e}", file=sys.stderr)
        return 2
    findings = compare(read, bearing, lacking, listed_definers)
    for n in sorted(set(lacking) - set(read)):
        findings.append(f"{n}: in lacking but not a public repository read here (drop it)")
    for x in findings:
        print(f"g1_coverage: {x}")
    with_g1 = sum(1 for n in bearing if read[n]["g1"])
    print(f"g1_coverage: {len(read)} repositories read, {len(definers)} define a Law, {len(bearing)} Law-bearing, "
          f"{with_g1} with G1's clippy evidence, {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
