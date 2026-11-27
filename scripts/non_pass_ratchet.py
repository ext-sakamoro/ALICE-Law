#!/usr/bin/env python3
"""The non-pass gate ratchet: per repository, the gates of its alice-law-conformance.toml that are not met (read
`fail-known` or `unverified`) may only go down.

    non_pass_ratchet.py --states FILE [--baseline FILE]
    non_pass_ratchet.py --only NAME=DIR [--baseline FILE]
    non_pass_ratchet.py --private NAME=DIR --baseline FILE
    non_pass_ratchet.py --write-baseline --states FILE [--baseline FILE]
    non_pass_ratchet.py --check-baseline [--base-baseline FILE] [--baseline FILE] [--no-roster]

A gate counts when scripts/conformance_roster_check.py reads it `fail-known` or `unverified`: a `pass` or `n/a` cell
the roster reads as unverified (no evidence at the tree, no mutant, an `n/a` without a measured scanner) counts too,
so writing `pass` or `n/a` without its evidence does not lower the count. conformance/non-pass-baseline.toml holds one
count per release-train repository, 0 included; a private repository's row lives in a baseline outside this
repository (ALICE-Law is public and does not name it), handed in with --private and --baseline.

The rules:
  * a count above its row is red (a gate went from met to not met, or a new gate is not met);
  * --states (every release-train repository, as the roster read it at main): a count below its row is red until the
    row is lowered in the same commit, a repository with a state and no row is red, a row for a repository the roster
    does not list is red;
  * --only / --private (one repository's committed manifest at HEAD, the crate's own check): a count below its row is
    a note (the row is in another repository and is lowered there); --private refuses a repository with no row, so
    a private repository cannot sit outside the ratchet;
  * --check-baseline: the form (one integer count, 0 or more, per row, at least one row) and, unless --no-roster,
    the rows against the roster; with --base-baseline (the same file at the base scripts/ci_base.py names), a row
    whose count is above its base row must carry `raised = "<reason>"` (12 characters or more, 4 distinct), and a
    `raised` on a row that did not rise is red (a reason is for the commit that raises);
  * nothing compared is red (exit 2).

Exit 0 when nothing is found, 1 on a finding, 2 when something could not be read or nothing was compared.
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import conformance_roster_check as crc  # noqa: E402

ROOT = HERE.parent
BASELINE = ROOT / "conformance" / "non-pass-baseline.toml"
ROSTER = ROOT / "release-train.toml"
GATES = ROOT / "conformance" / "gates.toml"
NOT_MET = ("fail-known", "unverified")


def non_pass(counts: dict) -> int:
    """the gates read `fail-known` or `unverified` in one manifest's status counts"""
    try:
        return sum(int(counts[s]) for s in NOT_MET)
    except (KeyError, TypeError, ValueError) as e:
        raise crc.InputError(f"status counts {counts!r}: no {e}") from e


def reason_ok(text) -> bool:
    return isinstance(text, str) and len(text.strip()) >= 12 and len(set(text.strip())) >= 4


def load_baseline(path: Path) -> dict[str, dict]:
    """repository -> {"count": n, "raised": str | None}"""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rows = data.get("repository")
    if not isinstance(rows, dict) or not rows:
        raise crc.InputError(f"{path}: no [repository.\"…\"] row")
    out = {}
    for repo, row in rows.items():
        n = row.get("non_pass") if isinstance(row, dict) else None
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise crc.InputError(f"{path}: {repo}: `non_pass` must be a count (an integer, 0 or more)")
        extra = sorted(set(row) - {"non_pass", "raised"})
        if extra:
            raise crc.InputError(f"{path}: {repo}: unknown key(s) {', '.join(extra)}")
        out[repo] = {"count": n, "raised": row.get("raised")}
    return out


def compare(read: dict[str, int], baseline: dict[str, dict], roster: set[str] | None, below_is_note: bool,
            row_required: bool = True) -> tuple[list[str], list[str]]:
    """(findings, notes)"""
    findings, notes = [], []
    for repo, n in sorted(read.items()):
        if repo not in baseline:
            if row_required:
                findings.append(f"{repo}: no row in the baseline ({n} non-pass gate(s))")
            continue
        b = baseline[repo]["count"]
        if n > b:
            findings.append(f"{repo}: {n} non-pass gate(s) > baseline {b} (a gate is not met that the baseline "
                            f"counts as met)")
        elif n < b:
            line = f"{repo}: {n} non-pass gate(s) < baseline {b} (lower the row to {n})"
            (notes if below_is_note else findings).append(line + ("" if below_is_note else
                                                                  " in this commit: set non_pass to the count in "
                                                                  "conformance/non-pass-baseline.toml, or rewrite every "
                                                                  "row with --states FILE --write-baseline"))
    if roster is not None:
        for repo in sorted(set(baseline) - roster):
            findings.append(f"{repo}: a row for a repository the roster does not list")
    return findings, notes


def raise_findings(rows: dict[str, dict], base: dict[str, dict]) -> list[str]:
    out = []
    for repo, row in sorted(rows.items()):
        was = base.get(repo, {}).get("count")
        rose = was is not None and row["count"] > was
        if rose and not reason_ok(row["raised"]):
            out.append(f"{repo}: non_pass {was} -> {row['count']} rises without `raised = \"<reason>\"` "
                       f"(12 characters or more, 4 distinct)")
        if not rose and row["raised"] is not None:
            out.append(f"{repo}: `raised` on a row that does not rise against the base (remove it)")
    return out


def render(rows: dict[str, int]) -> str:
    out = ["# Generated by scripts/non_pass_ratchet.py --write-baseline: per release-train repository, the gates of its",
           "# alice-law-conformance.toml read fail-known or unverified (scripts/conformance_roster_check.py). Counts only",
           "# go down; a row that rises carries `raised = \"<reason>\"`.", ""]
    for repo in sorted(rows):
        out += [f'[repository."{repo}"]', f"non_pass = {rows[repo]}", ""]
    return "\n".join(out)


def read_states(path: Path) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise crc.InputError(f"{path}: not an object of repository -> status counts")
    return {repo: non_pass(counts) for repo, counts in data.items()}


def read_one(spec: str, gates: Path) -> tuple[str, int]:
    name, _, d = spec.partition("=")
    if not name or not d:
        raise crc.InputError(f"{spec}: NAME=DIR")
    findings, _notes, counts, compared, _n = crc.check_one(Path(d), "HEAD", crc.load_toml(gates))
    if compared == 0:
        raise crc.InputError(f"{name}: the manifest has no gate entry (compared nothing)")
    if findings:
        raise crc.InputError(f"{name}: the manifest has findings, its gates are not counted ({findings[0]})")
    return name, non_pass(counts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--states", type=Path, help="JSON repository -> status counts (conformance_roster_check.py --states-out)")
    mode.add_argument("--only", metavar="NAME=DIR")
    mode.add_argument("--private", metavar="NAME=DIR")
    mode.add_argument("--check-baseline", action="store_true")
    ap.add_argument("--write-baseline", action="store_true", help="with --states: write the baseline from it")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--base-baseline", type=Path, help="with --check-baseline: the baseline at the base")
    ap.add_argument("--no-roster", action="store_true", help="with --check-baseline: rows are not release-train names")
    ap.add_argument("--gates", type=Path, default=GATES)
    ap.add_argument("--roster", type=Path, default=ROSTER)
    a = ap.parse_args(argv)
    notes: list[str] = []
    try:
        if a.check_baseline:
            rows = load_baseline(a.baseline)
            findings = []
            if not a.no_roster:
                roster = set(crc.repo_crates(crc.load_toml(a.roster)))
                findings += [f"{r}: a row for a repository the roster does not list" for r in sorted(set(rows) - roster)]
            if a.base_baseline:
                findings += raise_findings(rows, load_baseline(a.base_baseline))
            for f in findings:
                print(f"found: {f}")
            print(f"non_pass_ratchet: baseline {len(rows)} row(s), {sum(r['count'] for r in rows.values())} non-pass "
                  f"gate(s) in all, {'compared with the base, ' if a.base_baseline else ''}{len(findings)} finding(s)")
            return 1 if findings else 0
        if a.states:
            read = read_states(a.states)
            if not read:
                raise crc.InputError(f"{a.states}: no repository state (compared nothing)")
            if a.write_baseline:
                a.baseline.write_text(render(read), encoding="utf-8")
                print(f"non_pass_ratchet: wrote {a.baseline} ({len(read)} repositories)")
                return 0
            roster = set(crc.repo_crates(crc.load_toml(a.roster)))
            findings, notes = compare(read, load_baseline(a.baseline), roster, below_is_note=False)
        else:
            name, n = read_one(a.only or a.private, a.gates)
            read = {name: n}
            baseline = load_baseline(a.baseline)
            if a.only and name not in baseline:
                notes.append(f"{name}: no row in {a.baseline.name} (a repository outside the release train)")
                findings = []
            else:
                findings, notes = compare(read, {k: v for k, v in baseline.items() if k == name}, None,
                                          below_is_note=True)
    except (crc.InputError, tomllib.TOMLDecodeError, json.JSONDecodeError, OSError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for n in notes:
        print(f"note: {n}")
    for f in findings:
        print(f"found: {f}")
    print(f"non_pass_ratchet: {len(read)} repositor{'y' if len(read) == 1 else 'ies'} compared, "
          f"{sum(read.values())} non-pass gate(s) in all, {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
