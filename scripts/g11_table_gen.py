#!/usr/bin/env python3
"""ALICE-Law G11 table generator: writes a crate's conformance/g11.toml from what ALICE-Law already knows,
so the rows of a checker shared across crates are written once.

A row comes from one of two places:
  conformance/g11-shared-rows.toml (ALICE-Law)  one entry per shared checker: its canary, its zero test and
                                                its shared paths. The crate gets the row for gate G when its
                                                alice-law-conformance.toml names the checker in G's evidence,
                                                G is pass there, and ALICE-Law's conformance/shared-files.toml
                                                (at the revision .github/shared-files-pin names) lists the
                                                checker for the crate. `shared` keeps the entry's paths that
                                                table lists for the crate.
  conformance/g11-own.toml (the crate)          the crate's own rows, in the table's row format, copied as
                                                they are (a checker of its own, or a cargo test oracle).
The table is the generated rows and the crate's own rows, ordered by gate and checker.

Each row's canary and zero are resolved the way scripts/g11_table_check.py resolves them; a row that does
not resolve is reported and still written, so the check names it too. Nothing is written in place of a row
that does not exist: a pass gate without a row is listed, and the crate's G11 stays unmet until it has one.

Exit 0 when every pass gate has a row and every row resolves (and with --check, the file is the generated
one byte for byte); 1 otherwise, naming each gap; 2 when it compared nothing (no pass gate, or no row) or
cannot read what it needs.

    python3 scripts/g11_table_gen.py --repo DIR --name ALICE-<crate> (--write | --check)
        [--catalog FILE]      the shared-rows catalog (default: conformance/g11-shared-rows.toml next to
                              this script's scripts/ directory)
        [--law REV=DIR ...]   read ALICE-Law at REV from DIR instead of fetching it (the tests)
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("g11_table_check", HERE / "g11_table_check.py")
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)

CATALOG = HERE.parent / "conformance" / "g11-shared-rows.toml"
# the table builder lives in g11_table_check.py (the pinned checker regenerates the table to compare it)
OWN = chk.OWN
FIELDS = chk.FIELDS
HEADER = chk.HEADER
gate_key = chk.gate_key
mentions = chk.mentions
value = chk.value
render = chk.render
leg_gap = chk.leg_gap
load_catalog = chk.load_catalog
generate = chk.generate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--name", required=True)
    ap.add_argument("--catalog", type=Path, default=CATALOG)
    ap.add_argument("--law", action="append", default=[], metavar="REV=DIR")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    local = {}
    for spec in a.law:
        rev, _, d = spec.partition("=")
        local[rev] = chk.Tree.of_dir(Path(d))
    try:
        catalog = load_catalog(a.catalog.read_text(encoding="utf-8"))
        rows, gaps, gates = generate(chk.Tree.of_dir(a.repo), a.name, lambda rev: local.get(rev) or chk.git_law(rev),
                                     catalog)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as e:
        print(f"g11_table_gen: {e}", file=sys.stderr)
        return 2
    text = render(rows)
    target = a.repo / chk.TABLE
    if a.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    else:
        try:
            have = target.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            have = None
        if have != text:
            gaps.append(f"{chk.TABLE} is not the generated table (run scripts/g11_table_gen.py --write)")
    for gap in gaps:
        print(f"error: {gap}", file=sys.stderr)
    generated = sum(1 for r in rows if any(r["checker"] == e["path"] for e in catalog))
    print(f"g11_table_gen: {a.name}: {len(rows)} rows ({generated} from the catalog), {gates} pass gates, "
          f"{len(gaps)} gaps")
    if not rows or not gates:
        print("g11_table_gen: compared nothing (no row or no pass gate)", file=sys.stderr)
        return 2
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
