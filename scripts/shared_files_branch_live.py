#!/usr/bin/env python3
"""The shared-file table of a change against every listed main, in the landing mode (preflight, ci/ branches).

A rollout lands in the crates one repository at a time, so while a change to this repository is checked a planned row
can converge on the mains (every copy at its target). That is the rollout's, not the change's: the table on main
still plans the row, and the run on main (conformance-live.yml) asks for the regeneration. So on a change both
checks read the table origin/main has as their base: shared_files_check.py --live reports a row the change left as
the base has it and that converged since as a note, gen_shared_files.py --check puts such a row back to the table's.
A change that edits a converged row, or anything else the change does, is still a finding (2026-10-30: two
preflights failed on rows that converged while they ran).

usage: shared_files_branch_live.py [--table FILE] [--base-ref REF] [--lock-file FILE] [--expect-copies N] [--today D]

Exit 0: both pass. 1: a finding. 2: the base has no table, or a check could not run.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gen_shared_files as gen  # noqa: E402
import shared_files_check as sfc  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--table", type=Path, default=Path("conformance/shared-files.toml"))
    ap.add_argument("--base-ref", default="origin/main")
    ap.add_argument("--lock-file", type=Path)
    ap.add_argument("--expect-copies", type=int)
    ap.add_argument("--today", help="the date the live check reads as today (the tests)")
    a = ap.parse_args(argv)
    r = subprocess.run(["git", "show", f"{a.base_ref}:./{a.table.name}"], cwd=a.table.resolve().parent,
                       capture_output=True)
    if r.returncode != 0:
        print(f"shared_files_branch_live: {a.base_ref} has no {a.table} (no base to compare with)")
        return 2
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "base-shared-files.toml"
        base.write_bytes(r.stdout)
        live = ["--table", str(a.table), "--live", "--base-table", str(base)]
        if a.expect_copies is not None:
            live += ["--expect-copies", str(a.expect_copies)]
        if a.today is not None:
            live += ["--today", a.today]
        check = ["--table", str(a.table), "--check", "--base-table", str(base)]
        if a.lock_file is not None:
            check += ["--lock-file", str(a.lock_file)]
        codes = [sfc.main(live), gen.main(check)]
    if any(c not in (0, 1) for c in codes):
        return 2
    return 1 if any(codes) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
