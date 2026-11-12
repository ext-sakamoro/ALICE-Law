#!/usr/bin/env python3
"""A crate that calls an alice-det-math function added after SEMANTICS_ID was cut folds that function's own pin.

    added_function_pins.py [--owner NAME] [--local NAME=DIR] [--det-math DIR] [--jobs N]
    added_function_pins.py --only NAME=DIR [--det-math DIR]

alice-det-math's SEMANTICS_ID covers a frozen list of functions (SEMANTICS_ID_FUNCTIONS); a function added later
(ADDED_FUNCTIONS, powi64 first) is pinned only by its own FUNCTION_PINS entry, so a crate that names its arithmetic by
SEMANTICS_ID alone would not see a change to it. Here, for every public ALICE-* repository whose source uses
alice_det_math: the functions it calls are read as scripts/fold_set_check.py reads them (the same table,
function-paths.toml), and each called added function must be in the crate's DET_MATH_FUNCTIONS with the
`const _: () = alice_det_math::assert_function_pins(DET_MATH_PINS)` guard in its source. A crate that calls none needs
nothing.

A call fold_set_check cannot resolve ("not understood") is not a finding here unless it names an added function: the
crate's own fold check rules on the rest; an unresolved form that names one is a finding, since the call would be
missed. Without --det-math, ALICE-DetMath's default branch is fetched (depth 1) for ADDED_FUNCTIONS
(src/semantics.rs) and the table (function-paths.toml). Exit 0 / 1 (a finding) / 2 (unreadable, no det-math list, no
consumer read).
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fold_set_check as fs  # noqa: E402
import holder_live as hl  # noqa: E402

GITHUB = "https://github.com/"
ADDED_RE = re.compile(r"pub const ADDED_FUNCTIONS\s*:\s*&\[&str\]\s*=\s*&\[(.*?)\]\s*;", re.S)


def added_functions(det_math: Path) -> list[str]:
    text = (det_math / "src" / "semantics.rs").read_text(encoding="utf-8")
    m = ADDED_RE.search(text)
    if m is None:
        raise hl.Unreadable(f"{det_math}: no ADDED_FUNCTIONS in src/semantics.rs (a det-math from before the list)")
    return re.findall(r'"([a-z0-9_]+)"', m.group(1))


def fetch(base: Path, full_name: str, branch: str) -> Path:
    g = base / full_name.split("/")[-1]
    subprocess.run(["git", "init", "-q", str(g)], check=True, capture_output=True)
    hl.git(g, "remote", "add", "origin", GITHUB + full_name)
    hl.git(g, "fetch", "-q", "--depth", "1", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/main")
    hl.git(g, "checkout", "-q", "--detach", "origin/main")
    return g


def uses_det_math(root: Path) -> bool:
    return any(fs.PACKAGE in p.read_text(encoding="utf-8", errors="replace")
               for p in root.rglob("Cargo.toml") if "target" not in p.parts)


def check_repo(root: Path, table: fs.Table, added: list[str]) -> tuple[bool, list[str]]:
    """(the source uses alice_det_math, findings)"""
    f = fs.scan(root, table)
    if f.uses == 0:
        return False, []
    out = []
    called = sorted(set(f.pins) & set(added))
    for x in f.findings:
        if any(re.search(rf"\b{re.escape(n)}\b", x) for n in added):
            out.append(f"an unresolved use names an added function: {x}")
    if called:
        decl = fs.declared(root)
        for n in called:
            if decl is None or n not in decl:
                out.append(f"calls {n} (added after SEMANTICS_ID was cut) without its pin in DET_MATH_FUNCTIONS")
        if decl is not None and not fs.guarded(root):
            out.append("calls an added function but has no `const _: () = alice_det_math::assert_function_pins(..)`")
    return True, out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--local", help="NAME=DIR: read public repository NAME from DIR")
    ap.add_argument("--only", help="NAME=DIR: check only this repository")
    ap.add_argument("--det-math", type=Path, help="an alice-det-math checkout (default: its default branch, fetched)")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        try:
            dm = a.det_math or fetch(base, f"{a.owner}/ALICE-DetMath", "main")
            added = added_functions(dm)
            table = fs.load_table(dm / "function-paths.toml")
        except (hl.Unreadable, fs.InputError, OSError, subprocess.CalledProcessError) as e:
            print(f"added_function_pins: {e}", file=sys.stderr)
            return 2
        if a.only:
            name, _, d = a.only.partition("=")
            targets = [(name, Path(d), None)]
        else:
            try:
                repos = [r for r in hl.public_repos(a.owner) if r["name"].startswith("ALICE-")
                         and r["name"] != "ALICE-DetMath"]
            except hl.Unreadable as e:
                print(f"added_function_pins: {e}", file=sys.stderr)
                return 2
            local = None
            if a.local:
                n, _, d = a.local.partition("=")
                local = (n, Path(d))
            targets = [(r["name"], local[1] if local and r["name"] == local[0] else None, r) for r in repos]

        def one(t):
            name, d, r = t
            try:
                if d is None:
                    if r.get("size", 1) == 0:
                        return name, None, None
                    d = fetch(base / name, r["full_name"], r["default_branch"])
                if not uses_det_math(d):
                    return name, (False, []), None
                return name, check_repo(d, table, added), None
            except (hl.Unreadable, fs.InputError, OSError, UnicodeDecodeError, subprocess.CalledProcessError) as e:
                return name, None, str(e)
            finally:
                if r is not None and d is not None and str(d).startswith(str(base)):
                    shutil.rmtree(d, ignore_errors=True)

        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            results = list(ex.map(one, targets))
    errors = [f"{n}: {e}" for n, _, e in results if e]
    consumers = [(n, r[1]) for n, r, e in results if r and r[0]]
    findings = [f"{n}: {x}" for n, xs in consumers for x in xs]
    for e in errors:
        print(f"added_function_pins: unreadable: {e}", file=sys.stderr)
    for x in findings:
        print(f"added_function_pins: {x}")
    print(f"added_function_pins: added functions {added}; {len(results)} repositories read, {len(consumers)} use "
          f"alice_det_math, {len(findings)} finding(s)")
    if errors or not consumers:
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
