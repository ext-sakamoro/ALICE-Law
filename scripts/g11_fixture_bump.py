#!/usr/bin/env python3
"""Move a holder's clone to this ALICE-Law checkout's revision, as the holder's own pin bump would.

The self-check is run in ALICE-Law's own CI on real holders before any holder takes a new revision: each holder is
cloned at its main and bumped here, then scripts/g11_self_check.py runs on it on every OS, so a change to the
self-check, the table or a shared test file is seen on Linux, macOS and Windows in this repository first.

The bump writes what a holder's bump commit writes:
  - .github/shared-files-pin: rev (this checkout's HEAD), table-sha256, checker-sha256; name and visibility kept
  - .github/g11-table-pin (where the holder has one): rev and the checker-sha256 of scripts/g11_table_check.py
  - alice-law-conformance.toml: every `ALICE-Law <hex>` naming the old pin (any prefix length of it) names the new
    revision with the same number of digits
  - every shared copy the holder carries (a row listing it, a public holder; a table path present in its tree, a
    private one) set to the row's target bytes: a planned row's bytes from conformance/planned/<blob>, a uniform
    row's from the holder's own copy when it has them, else from the source repository at the row's revision
    (fetched, SHA-256 checked)
and commits it. Rows for each repository's own path and rows exempting the holder are left alone.

usage: g11_fixture_bump.py --holder DIR [--law DIR] [--source-url TEMPLATE]
  TEMPLATE names a source repository's url, `{repo}` replaced (default https://github.com/ext-sakamoro/{repo})

Exit 0 when bumped (it prints the number of copies set), 2 when the holder or a row's bytes cannot be read: a fixture
that could not be built is never a pass.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

PIN = ".github/shared-files-pin"
G11_PIN = ".github/g11-table-pin"
MANIFEST = "alice-law-conformance.toml"
TABLE = "conformance/shared-files.toml"
SOURCE_URL = "https://github.com/ext-sakamoro/{repo}"
IDENT = ["-c", "user.name=ALICE-Law fixture", "-c", "user.email=fixture@invalid"]


class Unbuildable(Exception):
    """the fixture cannot be built (exit 2)"""


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if check and r.returncode != 0:
        raise Unbuildable(f"git {' '.join(args[:3])} in {repo} failed: {r.stderr.strip()[:300]}")
    return r


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pin_fields(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def write_pin(holder: Path, rev: str, table_sha: str, checker_sha: str) -> str:
    """the new shared-files pin; returns the old rev"""
    p = holder / PIN
    if not p.is_file():
        raise Unbuildable(f"{holder}: no {PIN} (not a holder)")
    old = pin_fields(p.read_text(encoding="utf-8"))
    if "name" not in old or "rev" not in old:
        raise Unbuildable(f"{holder}: {PIN} has no name or rev")
    lines = [f"name {old['name']}", f"rev {rev}", f"table-sha256 {table_sha}", f"checker-sha256 {checker_sha}"]
    if "visibility" in old:
        lines.append(f"visibility {old['visibility']}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return old["rev"]


def write_g11_pin(holder: Path, rev: str, checker_sha: str) -> bool:
    p = holder / G11_PIN
    if not p.is_file():
        return False
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^rev [0-9a-f]{40}$", f"rev {rev}", text)
    text = re.sub(r"(?m)^checker-sha256 [0-9a-f]{64}$", f"checker-sha256 {checker_sha}", text)
    p.write_text(text, encoding="utf-8")
    return True


def rename_rev(text: str, old: str, new: str) -> tuple[str, int]:
    """every `ALICE-Law <hex>` whose hex is a prefix (7 digits or more) of `old`, renamed to `new` with as many
    digits; the longest match is taken, so an 8-digit form is never left with a stray digit"""
    n = 0

    def sub(m: re.Match) -> str:
        nonlocal n
        h = m.group(1)
        if len(h) >= 7 and old.startswith(h):
            n += 1
            return f"ALICE-Law {new[:len(h)]}"
        return m.group(0)
    return re.sub(r"ALICE-Law ([0-9a-f]{7,40})(?![0-9a-f])", sub, text), n


def copies(rows: list[dict], name: str, private: bool, holder: Path) -> list[dict]:
    """the rows whose copy the holder carries: listed in `repos` (public) or present in the tree (private); own
    paths and per-repository rows (no bytes) and rows exempting the holder are not copies"""
    out = []
    for r in rows:
        if name in r.get("exempt", {}):
            continue
        if private:
            if (holder / r["path"]).is_file() and ("sha256" in r or "pending" in r):
                out.append(r)
        elif name in r.get("repos", []) and ("sha256" in r or "pending" in r):
            out.append(r)
    return out


def target_bytes(row: dict, law: Path, holder: Path, source_url: str, cache: Path) -> bytes:
    """the row's target bytes, SHA-256 checked"""
    if "pending" in row:
        want = row["pending"]["target"]
        blob = row["pending"].get("planned")
        if not blob:
            raise Unbuildable(f"{row['path']}: a pending row without planned bytes (its target is on the owner's main)")
        f = law / "conformance" / "planned" / blob
        if not f.is_file():
            raise Unbuildable(f"{row['path']}: conformance/planned/{blob} is missing")
        data = f.read_bytes()
    else:
        want = row["sha256"]
        own = holder / row["path"]
        if own.is_file() and sha256(own.read_bytes()) == want:
            return own.read_bytes()
        src = row["source"]
        repo = cache / src["repo"]
        if not repo.is_dir():
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        git(repo, "fetch", "-q", "--depth", "1", source_url.format(repo=src["repo"]), src["rev"])
        r = subprocess.run(["git", "-C", str(repo), "show", f"{src['rev']}:{row['path']}"], capture_output=True)
        if r.returncode != 0:
            raise Unbuildable(f"{row['path']}: not at {src['repo']}@{src['rev'][:12]}")
        data = r.stdout
    if sha256(data) != want:
        raise Unbuildable(f"{row['path']}: the bytes read have SHA-256 {sha256(data)[:12]}, the row wants {want[:12]}")
    return data


def bump(holder: Path, law: Path, source_url: str = SOURCE_URL) -> tuple[int, int, bool]:
    """(copies set, manifest names renamed, g11 pin present); raises Unbuildable"""
    rev = git(law, "rev-parse", "HEAD").stdout.strip()
    table_sha = sha256((law / TABLE).read_bytes())
    checker_sha = sha256((law / "scripts" / "shared_files_check.py").read_bytes())
    g11_sha = sha256((law / "scripts" / "g11_table_check.py").read_bytes())
    pin = pin_fields((holder / PIN).read_text(encoding="utf-8")) if (holder / PIN).is_file() else {}
    name, private = pin.get("name", ""), pin.get("visibility") == "private"
    old = write_pin(holder, rev, table_sha, checker_sha)
    has_g11 = write_g11_pin(holder, rev, g11_sha)
    renamed = 0
    m = holder / MANIFEST
    if m.is_file():
        text, renamed = rename_rev(m.read_text(encoding="utf-8"), old, rev)
        m.write_text(text, encoding="utf-8")
    rows = tomllib.loads((law / TABLE).read_text(encoding="utf-8")).get("file", [])
    held = copies(rows, name, private, holder)
    cache = holder.parent / (holder.name + "-sources")
    cache.mkdir(exist_ok=True)
    changed = 0
    for row in held:
        data = target_bytes(row, law, holder, source_url, cache)
        p = holder / row["path"]
        if not p.is_file() or p.read_bytes() != data:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            changed += 1
    git(holder, "add", "-A")
    git(holder, *IDENT, "commit", "-q", "--allow-empty", "-m", f"fixture: pin ALICE-Law {rev[:12]}")
    return changed, renamed, has_g11


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--holder", type=Path, required=True)
    ap.add_argument("--law", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--source-url", default=SOURCE_URL)
    a = ap.parse_args(argv)
    try:
        changed, renamed, has_g11 = bump(a.holder, a.law, a.source_url)
    except (Unbuildable, OSError, KeyError, ValueError) as e:  # tomllib.TOMLDecodeError is a ValueError
        print(f"g11 fixture bump: {a.holder}: not built ({e})")
        return 2
    print(f"g11 fixture bump: {a.holder.name}: {changed} copies set to their targets, {renamed} manifest name(s) "
          f"renamed, g11-table-pin {'moved' if has_g11 else 'absent'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
