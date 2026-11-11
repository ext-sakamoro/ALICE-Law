#!/usr/bin/env python3
"""Find alice-* dependencies that do not come from their repository's main branch.

Every alice-* crate is taken by git from its repository's main branch (`branch = "main"` + a version): a commit
or tag pin, another branch, or a crates.io release is a second generation of the crate beside main. This reads a
repository's tracked files:

- each `Cargo.toml`: an alice-* dependency (any dependency table, `[workspace.dependencies]` and target tables
  included, a renamed one by its `package`) that has `git` must name `branch = "main"` and no `rev` / `tag`; one
  with neither `git` nor `path` (a registry release) is a finding too. A `path` dependency and a
  `workspace = true` reference are left to their own checks (lock_sources_check.py, the workspace table);
- each `Cargo.lock`: an alice-* (or libasp) package with a source must come from
  `git+<url>?branch=main#<commit>`; a package without a source is one of the repository's own.

    branch_main_check.py [DIR]     (the tracked Cargo.toml / Cargo.lock of the checkout DIR, default .)

Exit 0 when nothing was found, 1 on a finding, 2 when no manifest or lock was read.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

# ALICE crates whose package name does not start with `alice`
OTHER_NAMES = {"libasp"}
DEP_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")
MAIN_SOURCE_RE = re.compile(r"^git\+[^?#]+\?branch=main#[0-9a-f]{40}$")


def alice(name: str) -> bool:
    return name.startswith("alice") or name in OTHER_NAMES


def _tables(doc: dict) -> list[tuple[str, dict]]:
    """(where, table) of every dependency table of a manifest"""
    out = [(t, doc.get(t, {})) for t in DEP_TABLES]
    out.append(("workspace.dependencies", doc.get("workspace", {}).get("dependencies", {})))
    for target, body in doc.get("target", {}).items():
        out += [(f"target.{target}.{t}", body.get(t, {})) for t in DEP_TABLES]
    return out


def manifest_findings(text: str) -> list[str]:
    """the alice-* dependencies of a manifest that do not name their repository's main branch"""
    found = []
    for where, table in _tables(tomllib.loads(text)):
        for key, spec in table.items():
            spec = {"version": spec} if isinstance(spec, str) else spec
            if not isinstance(spec, dict):
                continue
            name = spec.get("package", key)
            if not alice(name) or "path" in spec or spec.get("workspace") is True:
                continue
            if "git" not in spec:
                found.append(f"[{where}] {key}: a registry release ({spec.get('version', '?')}), not the main branch")
            elif spec.get("branch") != "main" or "rev" in spec or "tag" in spec:
                pin = ", ".join(f"{k} = {spec[k]}" for k in ("branch", "rev", "tag") if k in spec) or "no branch"
                found.append(f"[{where}] {key}: {pin}, not branch = \"main\"")
    return found


def lock_findings(text: str) -> list[str]:
    """the alice-* packages of a lock file that do not come from a main branch"""
    found = []
    for p in tomllib.loads(text).get("package", []):
        src = p.get("source")
        if alice(p["name"]) and src and not MAIN_SOURCE_RE.match(src):
            found.append(f"{p['name']} {p['version']}: {src}")
    return found


def tracked(d: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(d), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p.rsplit("/", 1)[-1] in ("Cargo.toml", "Cargo.lock")]


def check_files(files: dict[str, str]) -> tuple[list[str], int]:
    """(findings, files read) of {path: text}"""
    found = []
    for path, text in sorted(files.items()):
        rows = lock_findings(text) if path.endswith("Cargo.lock") else manifest_findings(text)
        found += [f"{path}: {r}" for r in rows]
    return found, len(files)


def check_repo(d: Path) -> tuple[list[str], int]:
    return check_files({p: (d / p).read_text(encoding="utf-8") for p in tracked(d)})


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    d = Path(argv[0] if argv else ".")
    try:
        found, n = check_repo(d)
    except (OSError, subprocess.CalledProcessError, tomllib.TOMLDecodeError, KeyError) as e:
        print(f"branch main: {e}")
        return 2
    for f in found:
        print(f"found: {f}")
    print(f"branch main: {n} manifest / lock file(s) read, {len(found)} finding(s)")
    if n == 0:
        return 2
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
