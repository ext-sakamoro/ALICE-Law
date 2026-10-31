#!/usr/bin/env python3
"""What landing crate commits does to the conformance state ALICE-Law observes (a report, never a gate).

Law records no other repository's status counts: scripts/conformance_roster_check.py computes them from the
mains on every run and publishes them in the job summary. This script tells a lander what its landing will
show there, before it lands:

  land_law_state.py --candidate ALICE-X=<clone dir>@<rev> [--candidate ...]

reads the candidates' manifests from their clones at <rev> and every release-train repository's from its
main (GitHub), through the same checks as scripts/conformance_roster_check.py, and compares the state with
the candidates landed against the state at the mains. It prints each changed count and the verdict
`land_law_state: state moves: no` or `land_law_state: state moves: yes`, and the findings the landed state
would show in Law's live check (a manifest with a finding, a line-number reference above the baseline in
conformance/line-refs-baseline.toml). It writes nothing: the landing needs no change in this repository,
and nothing here refuses it (each repository's own gates decide its landing).

Exit 0 when the state was computed (whether it moves or not, with or without findings); 2 when it was not
(no candidate, a candidate the roster does not name, an unreadable input, or nothing compared).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import conformance_roster_check as crc  # noqa: E402


def parse_candidate(text: str) -> tuple[str, Path, str]:
    name, sep, rest = text.partition("=")
    where, at, rev = rest.rpartition("@")
    if not sep or not name or not at or not where or not rev:
        raise crc.InputError(f"--candidate {text!r}: needs ALICE-X=<clone dir>@<rev>")
    return name, Path(where), rev


def changes(old: dict[str, dict], new: dict[str, dict]) -> list[str]:
    out = []
    for repo in sorted(set(old) | set(new)):
        a, b = old.get(repo), new.get(repo)
        if a is None:
            out.append(f"{repo}: new in the state")
            continue
        if b is None:
            out.append(f"{repo}: leaves the state")
            continue
        moved = [f"{s} {a['counts'][s]} -> {b['counts'][s]}" for s in crc.STATUSES if a["counts"][s] != b["counts"][s]]
        if sorted(a["crates"]) != sorted(b["crates"]):
            moved.append(f"crates {sorted(a['crates'])} -> {sorted(b['crates'])}")
        if moved:
            out.append(f"{repo}: " + ", ".join(moved))
    return out


def git_resolve(where: Path, rev: str) -> str:
    """the full commit id of `rev` in the clone `where`"""
    p = subprocess.run(["git", "-C", str(where), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise crc.InputError(f"{where}: ref {rev!r} not found")
    return p.stdout.strip()


def main(argv: list[str] | None = None, remote=None, local=crc.git_fetcher, root: Path | None = None,
         resolve=git_resolve) -> int:
    # the default root is read at call time, so a test that points crc.ROOT elsewhere covers a call without root=
    root = crc.ROOT if root is None else root
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--candidate", action="append", default=[], metavar="ALICE-X=DIR@REV",
                    help="a crate repository's clone and the commit about to land on its main")
    ap.add_argument("--owner", default=None, help="the GitHub owner (default: [train] github of the roster)")
    a = ap.parse_args(argv)
    try:
        if not a.candidate:
            raise crc.InputError("no --candidate given (nothing to land, nothing to compare)")
        roster = crc.load_toml(root / "release-train.toml")
        gates = crc.load_toml(root / "conformance" / "gates.toml")
        missing_path = root / "conformance" / "missing-manifests.toml"
        missing = crc.load_toml(missing_path) if missing_path.exists() else {}
        repos = set(crc.roster_crates(roster).values())
        candidates, shas = {}, {}
        for c in a.candidate:
            name, where, rev = parse_candidate(c)
            if name not in repos:
                raise crc.InputError(f"--candidate {name}: not a release-train repository ({', '.join(sorted(repos))})")
            if name in candidates:
                raise crc.InputError(f"--candidate {name} given twice")
            candidates[name] = local(where, rev)
            shas[name] = resolve(where, rev)
        if remote is None:
            owner = a.owner or roster.get("train", {}).get("github")
            if not owner:
                raise crc.InputError("no GitHub owner ([train] github in the roster, or --owner)")
            remote = crc.gh_fetcher(owner)
        cache: dict[str, tuple] = {}

        def at_main(repo: str):
            if repo not in cache:
                cache[repo] = remote(repo)
            return cache[repo]

        def landed(repo: str):
            return candidates[repo]() if repo in candidates else at_main(repo)

        _, _, before, read0, compared0, _ = crc.check(roster, gates, missing, at_main)
        texts: dict[str, str] = {}
        findings, notes, after, read, compared, files = crc.check(roster, gates, missing, landed, texts)
        lf, ln = crc.line_ref_findings(texts, crc.load_line_baseline(root / "conformance" / "line-refs-baseline.toml"))
        findings += lf
        notes += ln
    except (crc.InputError, OSError) as e:
        print(f"land_law_state: {e}", file=sys.stderr)
        return 2
    for n in notes:
        print(f"note: {n}")
    for f in findings:
        print(f"found: {f} (Law's live check will show it; this does not refuse the landing)")
    if read == 0 or compared == 0 or read0 == 0 or compared0 == 0:
        print(f"land_law_state: nothing was compared ({read} manifests, {compared} gates)", file=sys.stderr)
        return 2
    lines = changes(before, after)
    print("land_law_state: state moves: " + ("yes" if lines else "no"))
    for line in lines:
        print(f"  {line}")
    print(f"land_law_state: {files} manifests read for {read} crates, {compared} gates compared, "
          f"{len(findings)} findings (candidates: "
          + ", ".join(f"{r}@{shas[r][:12]}" for r in sorted(candidates)) + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
