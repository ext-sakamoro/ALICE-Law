#!/usr/bin/env python3
"""Point the end to end check at checkouts of the core repositories' main branches

e2e/Cargo.toml names every core crate from its repository's main branch (git, branch
"main", with the version on main). The CI checks the repositories out side by side (as
they expect: alice-lol depends on ../../ALICE-SDF and others by path), and this script
writes Cargo `[patch]` tables that point each crate at its checkout, so the mutations of
scripts/e2e_teeth.py reach the test and the run names the commit of each checkout. A
crate is found in its checkout by its package name (the root package or a workspace
member), so a crate that moves inside its repository is still patched from where it is.

Cargo applies a `[patch]` table to one source only: `[patch."<git URL>"]` to the
requirements naming that repository by git (a branch or a commit), `[patch.crates-io]` to
the requirements on crates.io. The core crates name each other both ways, so the patch
writes one entry per (crate, source) the resolved graph names, and repeats until a pass
names no new one: without an entry the copy a consumer names is linked beside the
checkout, two copies of one crate in one build. A source the graph does not name gets no
entry.

    python3 scripts/main_mode.py patch  <checkouts>   # writes e2e/.cargo/config.toml
    python3 scripts/main_mode.py verify               # after `cargo metadata` can resolve

`patch` first compares e2e/Cargo.toml's own requirements on these crates with the
versions of the checkouts and stops (exit 1, nothing written) when one does not accept
main: the manifest is behind (a new major on main), and the commit that takes the major
raises it. A consumer that does not accept a new major is reported before the push by the
pre-push wiring check and before publishing by `scripts/release_train.py publish-gate`.

`verify` reads the resolved graph of e2e/ and fails unless every crate below came
from its checkout, each at least once, and every written entry was used (a requirement
from that source resolved to the checkout; none used fails). Cargo leaves a patch unused,
and takes the crate from elsewhere instead, when the version on main does not match a
requirement on it: that can only be a requirement of one core crate on another (for
example a new major version on main that another crate's main does not accept yet). That
is the early warning this mode exists for, so it is a failure, not a note. A run that
found none of the crates in the graph fails too.
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

# crate -> repository; the crate's directory is found by its package name (crate_dir), so a
# crate that moves inside its repository (ALICE-Physics: the root package became
# alice-physics-core and alice-physics a member) is still patched from where it is
CRATES = {
    "alice-det-math": "ALICE-DetMath",
    "alice-crypto": "ALICE-Crypto",
    "alice-zip": "ALICE-Zip",
    "alice-analytics": "ALICE-Analytics",
    "alice-db": "ALICE-DB",
    "alice-physics": "ALICE-Physics",
    "alice-sdf": "ALICE-SDF",
    "alice-lol": "ALICE-LOL",
    "alice-lol-macro": "ALICE-LOL",
    "alice-edge": "ALICE-Edge",
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


def stale_requirements(manifest: str, versions: dict[str, str]) -> list[str]:
    """each direct requirement of e2e/Cargo.toml on a core crate that does not accept the version on
    main (`name: "<req>" does not accept main <version>`): the manifest names the crates' main
    branches, so such a requirement is the manifest lagging main (a new major there), raised in the
    commit that takes it. A core crate named without a version, or no core crate named, is refused"""
    stale: list[str] = []
    compared = 0
    for m in DEP_RE.finditer(manifest):
        name, rest = m["name"], m["rest"]
        if name not in versions:
            continue
        r = REQ_RE.match(rest)
        if not r or not r["pre"] and not rest.startswith('"'):
            raise ValueError(f"e2e/Cargo.toml: {name} has no version requirement to compare")
        compared += 1
        if not accepts(r["req"], versions[name]):
            stale.append(f'{name}: "{r["req"]}" does not accept main {versions[name]}')
    if compared == 0:
        raise ValueError("e2e/Cargo.toml names no core crate with a version")
    return stale


def crate_dir(checkout: Path, crate: str) -> Path:
    """the directory of the package named `crate` in a repository checkout: the root package or a
    workspace member (`members` globs expanded, `exclude` left out, as Cargo reads them); exactly
    one must have the name, and a listed member without a manifest is refused (LookupError)"""
    manifest = checkout / "Cargo.toml"
    if not manifest.is_file():
        raise LookupError(f"no {manifest}")
    root = tomllib.loads(manifest.read_text(encoding="utf-8"))
    dirs = [checkout] if "package" in root else []
    ws = root.get("workspace", {})
    excluded = {(checkout / e).resolve() for e in ws.get("exclude", [])}
    for pattern in ws.get("members", []):
        globbed = any(c in pattern for c in "*?[")
        hits = sorted(p for p in checkout.glob(pattern) if p.is_dir()) if globbed else [checkout / pattern]
        for d in hits:
            if d.resolve() in excluded:
                continue
            if not (d / "Cargo.toml").is_file():
                raise LookupError(f"{checkout.name}: workspace member {d.relative_to(checkout)} has no Cargo.toml")
            dirs.append(d)
    names = {}  # by directory: a member "." is the root package, read once
    for d in dirs:
        name = tomllib.loads((d / "Cargo.toml").read_text(encoding="utf-8")).get("package", {}).get("name")
        if name is None:
            raise LookupError(f"{checkout.name}: {(d / 'Cargo.toml').relative_to(checkout)} has no [package] name")
        names[d] = name
    found = [d for d, n in names.items() if n == crate]
    if len(found) != 1:
        raise LookupError(f"{checkout.name}: {len(found)} packages named {crate} (packages: "
                          f"{', '.join(sorted(set(names.values()))) or 'none'})")
    return found[0]


CRATES_IO = "crates-io"
# passes of the patch before it gives up on a fixed point (each pass can only add sources the
# checkouts name, a handful at most)
MAX_PASSES = 8


def metadata() -> dict:
    """`cargo metadata` of e2e/ as it is patched now"""
    out = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--manifest-path", str(E2E / "Cargo.toml")],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=E2E,  # Cargo reads e2e/.cargo/config.toml from the working directory
    ).stdout
    return json.loads(out)


def source_key(source: str | None) -> str | None:
    """the [patch] table a requirement's declared source belongs to: "crates-io", the git URL
    (without the commit), or None for a path requirement"""
    if source is None:
        return None
    if source.startswith("registry+https://github.com/rust-lang/crates.io-index"):
        return CRATES_IO
    if source.startswith("git+"):
        return re.split(r"[?#]", source[len("git+"):], maxsplit=1)[0]
    return source


def _requirements(meta: dict):
    """(crate, declared source key, resolved package) of each resolved requirement on a core crate"""
    by_id = {p["id"]: p for p in meta["packages"]}
    for n in meta["resolve"]["nodes"]:
        resolved = {by_id[d["pkg"]]["name"]: by_id[d["pkg"]] for d in n.get("deps", [])}
        for d in by_id[n["id"]].get("dependencies", []):
            if d["name"] in CRATES and d["name"] in resolved:
                key = source_key(d.get("source"))
                if key is not None:
                    yield d["name"], key, resolved[d["name"]]


def sources_named(meta: dict) -> set[tuple[str, str]]:
    """every (crate, source) a resolved requirement names, wherever it resolved"""
    return {(c, k) for c, k, _ in _requirements(meta)}


def used_entries(meta: dict) -> set[tuple[str, str]]:
    """the (crate, source) entries a resolved requirement used: it resolved to a path package (the checkout)"""
    return {(c, k) for c, k, p in _requirements(meta) if p.get("source") is None}


def written_entries(config: str) -> set[tuple[str, str]]:
    """the (crate, source) entries of a written config"""
    return {(crate, CRATES_IO if table == CRATES_IO else table)
            for table, entries in tomllib.loads(config).get("patch", {}).items() for crate in entries}


def entry_problems(written: set[tuple[str, str]], meta: dict) -> tuple[list[str], int]:
    """(an entry the graph did not use is a problem, the number used)"""
    used = used_entries(meta)
    problems = [f"patch entry {c} from {k}: not used" for c, k in sorted(written - used)]
    return problems, len(written & used)


def render(entries: set[tuple[str, str]], paths: dict[str, Path]) -> str:
    lines = ["# Written by scripts/main_mode.py: the core crates from their main branches"]
    tables = sorted({k for _, k in entries}, key=lambda k: (k != CRATES_IO, k))
    for table in tables:
        if len(lines) > 1:
            lines.append("")
        lines.append(f"[patch.{CRATES_IO}]" if table == CRATES_IO else f'[patch."{table}"]')
        for crate in CRATES:
            if (crate, table) in entries:
                lines.append(f'{crate} = {{ path = "{paths[crate].as_posix()}" }}')
    return "\n".join(lines) + "\n"


def patch(checkouts: Path) -> int:
    versions: dict[str, str] = {}
    paths: dict[str, Path] = {}
    for crate, repo in CRATES.items():
        try:
            paths[crate] = crate_dir(checkouts / repo, crate).resolve()
        except LookupError as e:
            print(f"main mode: {e}", file=sys.stderr)
            return 2
        manifest = paths[crate] / "Cargo.toml"
        versions[crate] = tomllib.loads(manifest.read_text(encoding="utf-8"))["package"]["version"]
    try:
        stale = stale_requirements((E2E / "Cargo.toml").read_text(encoding="utf-8"), versions)
    except ValueError as e:
        print(f"main mode: {e}", file=sys.stderr)
        return 2
    for line in stale:
        print(f"main mode: e2e/Cargo.toml is behind main: {line}", file=sys.stderr)
    if stale:
        return 1
    # from every crate on crates.io, until a pass names no source without an entry
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    entries = {(c, CRATES_IO) for c in CRATES}
    for _ in range(MAX_PASSES):
        CONFIG.write_text(render(entries, paths), encoding="utf-8")
        named = sources_named(metadata())
        if named <= entries:
            break
        entries |= named
    else:
        CONFIG.unlink()
        print(f"main mode: no fixed point after {MAX_PASSES} passes (sources still appearing)", file=sys.stderr)
        return 2
    # the entries no requirement names are not written (verify counts an unused entry as a problem)
    entries &= named
    CONFIG.write_text(render(entries, paths), encoding="utf-8")
    gits = sorted({k for _, k in entries if k != CRATES_IO})
    print(f"main mode: patched {len({c for c, _ in entries})} crates from {len(gits) + 1} source(s) "
          f"in {CONFIG.relative_to(ROOT)}")
    for url in gits:
        print(f"main mode: git source patched: {url} ({', '.join(c for c, k in sorted(entries) if k == url)})")
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


def requirers(packages: list[dict], crate: str, main_version: str) -> list[str]:
    """`name (requirement)` of every resolved package whose requirement on `crate` does not accept the
    version on main: the ones that pull a crates.io copy in beside the checkout"""
    out = []
    for p in packages:
        for d in p.get("dependencies", []):
            if d.get("name") == crate and d.get("kind") in (None, "normal", "build") and not accepts(
                    d.get("req", ""), main_version):
                out.append(f"{p['name']} ({d['req']})")
    return sorted(set(out))


def verify() -> int:
    if not CONFIG.is_file():
        # the patch step failed or was skipped: every crate would read as "from crates.io",
        # which looks like a wiring failure when it is not one
        print(f"main mode: the patch did not run (no {CONFIG.relative_to(ROOT)}), nothing to verify",
              file=sys.stderr)
        return 2
    meta = metadata()
    packages = meta["packages"]
    seen, problems, compared = problems_of(packages)
    unused, used = entry_problems(written_entries(CONFIG.read_text(encoding="utf-8")), meta)
    problems += unused
    for crate in CRATES:
        for version, source in seen.get(crate, []):
            print(f"| {crate} | {version} | {'main' if source == 'path' else source} |")
    print(f"main mode: compared {compared} resolved crates, {used} patch entries used, {len(problems)} problem(s)")
    for p in problems:
        print(f"main mode: {p}")
    # who pulls a crates.io copy in beside the checkout: the early warning, named
    for crate in CRATES:
        found = seen.get(crate, [])
        mains = [v for v, src in found if src == "path"]
        if mains and any(src != "path" for _, src in found):
            who = requirers(packages, crate, mains[0])
            print(f"main mode: {crate}: main is {mains[0]}, required by "
                  f"{', '.join(who) or '(no requirement found)'}, which do not accept it")
    if compared == 0 or used == 0:
        print("main mode: no patch entry was used (nothing taken from main)", file=sys.stderr)
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
