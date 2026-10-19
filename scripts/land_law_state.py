#!/usr/bin/env python3
"""Whether landing crate commits moves ALICE-Law's conformance state.

Law's conformance/manifest-state.toml (and the Status blocks of README.md / README_JP.md generated from it)
records the status counts of each release-train repository's alice-law-conformance.toml at its main, and the
live check fails when they differ. A crate commit that changes its manifest's statuses therefore turns Law's
main red when it lands, until a state commit follows. This script tells the lander before landing:

  land_law_state.py --candidate ALICE-X=<clone dir>@<rev> [--candidate ...] [--write]

reads the manifests of the candidates from their clones at <rev> and every other release-train repository's
from its main (GitHub), through the same checks as scripts/conformance_roster_check.py, and compares the
state that would result with the committed one. It prints `state moves: no`, or `state moves: yes` with each
changed count. With --write it writes that state and the README Status blocks (the Law commit that the
landing makes true).

It also holds each manifest to conformance/line-refs-baseline.toml: a count above its baseline is a finding
(the landing would add a line-number reference). A candidate below its baseline is a note, not a move: the Law
commit comes before the landing, and a baseline lowered then would be below the crate's main until it lands
(the live check's 3-day slack note covers the landing). Another repository's main below its baseline is a
move, and --write lowers that baseline with the state, so the slack is closed by the next landing.

Exit 0: the state does not move; 1: it moves (a Law state commit must follow the landing); 2: not computed
(a manifest with findings, a candidate the roster does not name, an unreadable input, or nothing compared).
"""
from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import conformance_roster_check as crc  # noqa: E402
import gen_law_status as gls  # noqa: E402


def parse_candidate(text: str) -> tuple[str, Path, str]:
    name, sep, rest = text.partition("=")
    where, at, rev = rest.rpartition("@")
    if not sep or not name or not at or not where or not rev:
        raise crc.InputError(f"--candidate {text!r}: needs ALICE-X=<clone dir>@<rev>")
    return name, Path(where), rev


def counts_of(state_text: str) -> dict[str, dict]:
    """repository -> {"crates": [...], status: count} of a manifest-state.toml text"""
    try:
        data = tomllib.loads(state_text)
    except tomllib.TOMLDecodeError as e:
        raise crc.InputError(f"manifest-state.toml: {e}") from e
    return data.get("manifest", {})


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
        moved = [f"{s} {a.get(s, 0)} -> {b.get(s, 0)}" for s in crc.STATUSES if a.get(s, 0) != b.get(s, 0)]
        if sorted(a.get("crates", [])) != sorted(b.get("crates", [])):
            moved.append(f"crates {sorted(a.get('crates', []))} -> {sorted(b.get('crates', []))}")
        if moved:
            out.append(f"{repo}: " + ", ".join(moved))
    return out


def render_baseline(text: str, lowered: dict[str, int]) -> str:
    """the baseline file's text with the entries of `lowered` set to their new counts (comments kept)"""
    out = []
    for line in text.splitlines(keepends=True):
        key = line.split("=", 1)[0].strip().strip('"')
        out.append(f'"{key}" = {lowered[key]}\n' if "=" in line and key in lowered else line)
    return "".join(out)


def git_resolve(where: Path, rev: str) -> str:
    """the full commit id of `rev` in the clone `where`"""
    p = subprocess.run(["git", "-C", str(where), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise crc.InputError(f"{where}: ref {rev!r} not found")
    return p.stdout.strip()


def main(argv: list[str] | None = None, remote=None, local=crc.git_fetcher, root: Path | None = None,
         resolve=git_resolve, at=None) -> int:
    # the default root is read at call time, so a test that points crc.ROOT elsewhere covers a call without root=
    root = crc.ROOT if root is None else root
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--candidate", action="append", default=[], metavar="ALICE-X=DIR@REV",
                    help="a crate repository's clone and the commit about to land on its main")
    ap.add_argument("--write", action="store_true",
                    help="write the resulting manifest-state.toml, pending-state.toml and README Status blocks")
    ap.add_argument("--owner", default=None, help="the GitHub owner (default: [train] github of the roster)")
    ap.add_argument("--today", help="the date a new pending entry records (default: today, UTC)")
    a = ap.parse_args(argv)
    state_path = root / "conformance" / "manifest-state.toml"
    pending_path = root / "conformance" / "pending-state.toml"
    baseline_path = root / "conformance" / "line-refs-baseline.toml"
    today = (datetime.date.fromisoformat(a.today) if a.today
             else datetime.datetime.now(datetime.timezone.utc).date())
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
        owner = a.owner or roster.get("train", {}).get("github")
        if remote is None or at is None:
            if not owner:
                raise crc.InputError("no GitHub owner ([train] github in the roster, or --owner)")
        remote = remote or crc.gh_fetcher(owner)
        at = at or (lambda repo, sha: crc.gh_fetcher(owner, sha)(repo))

        def fetch(repo: str):
            return candidates[repo]() if repo in candidates else remote(repo)

        texts: dict[str, str] = {}
        findings, notes, landed, read, compared, files = crc.check(roster, gates, missing, fetch, texts)
        baseline = crc.load_line_baseline(baseline_path)
        lowered = {}
        for repo in sorted(texts):
            n, allowed = crc.line_refs(texts[repo]), (baseline or {}).get(repo, 0)
            if n > allowed:
                findings.append(f"{repo}: {crc.MANIFEST} would hold {n} line-number reference(s), the baseline "
                                f"allows {allowed}: name the place by an anchor `path#token` "
                                f"(scripts/line_refs_propose.py proposes one)")
            elif n < allowed and repo in candidates:
                notes.append(f"{repo}: the landing lowers its line-number references from {allowed} to {n} (the "
                             f"baseline is lowered by the Law state change after it lands)")
            elif n < allowed:
                lowered[repo] = n
        if baseline is not None:
            findings += [f"{r}: in {baseline_path.name} but no manifest was read for it"
                         for r in sorted(set(baseline) - set(texts))]
        mfindings, _, at_main, *_ = crc.check(roster, gates, missing, remote)
        findings += [f for f in mfindings if f not in findings]
        # the entries of other repositories still pending (a landed one is dropped: part of the Law change)
        pending = crc.load_pending(pending_path)
        others = {r: e for r, e in pending.items() if r not in candidates}
        current = state_path.read_text(encoding="utf-8") if state_path.exists() else ""
        expected, kept, pf, pn = crc.apply_pending(at_main, crc.table_counts(current), others, gates, at, today,
                                                   strict=False)
        findings += pf
        notes += pn
        new_pending = dict(kept)
        for repo in sorted(candidates):
            if repo not in landed or repo not in at_main:
                continue  # a manifest with findings, or none: not computed (exit 2 below)
            expected[repo] = landed[repo]
            if landed[repo]["counts"] != at_main[repo]["counts"]:
                # a re-rolled candidate keeps the date its entry was first recorded
                since = pending[repo]["since"] if repo in pending else today.isoformat()
                new_pending[repo] = {"sha": shas[repo], "since": since}
        current_pending = pending_path.read_text(encoding="utf-8") if pending_path.exists() else ""
        old = counts_of(current) if current else {}
    except crc.InputError as e:
        print(f"land_law_state: {e}", file=sys.stderr)
        return 2
    for n in notes:
        print(f"note: {n}")
    for f in findings:
        print(f"found: {f}")
    if read == 0 or compared == 0:
        print(f"land_law_state: nothing was compared ({read} manifests, {compared} gates)", file=sys.stderr)
        return 2
    if findings:
        print(f"land_law_state: state not computed ({len(findings)} finding(s))", file=sys.stderr)
        return 2
    rendered = crc.render_state(expected)
    rendered_pending = crc.render_pending(new_pending) if (new_pending or current_pending) else ""
    # a move is a change of the recorded state or of which repositories are pending; an entry's sha or date alone
    # is not (the live check matches on the counts, so a rebased candidate already recorded needs no Law change)
    moved = rendered != current or set(new_pending) != set(pending) or bool(lowered)
    lines = changes(old, counts_of(rendered))
    lines += [f"{r}: line-reference baseline {baseline[r]} -> {n}" for r, n in sorted(lowered.items())]
    for repo in sorted(set(pending) | set(new_pending)):
        a_, b_ = pending.get(repo), new_pending.get(repo)
        if a_ != b_:
            lines.append(f"{repo}: pending " + (f"{a_['sha'][:12]} " if a_ else "") + "-> "
                         + (f"{b_['sha'][:12]} (since {b_['since']})" if b_ else "dropped (landed)"))
    if moved:
        print("land_law_state: state moves: yes")
        for line in lines or ["(the state files differ only in their form: regenerate them)"]:
            print(f"  {line}")
    else:
        print("land_law_state: state moves: no")
    print(f"land_law_state: {files} manifests read for {read} crates, {compared} gates compared "
          f"(candidates: {', '.join(sorted(candidates))})")
    if a.write and moved:
        state_path.write_text(rendered, encoding="utf-8")
        print(f"land_law_state: wrote {state_path.name}")
        if rendered_pending:
            pending_path.write_text(rendered_pending, encoding="utf-8")
            print(f"land_law_state: wrote {pending_path.name} ({len(new_pending)} pending)")
        if lowered:
            baseline_path.write_text(render_baseline(baseline_path.read_text(encoding="utf-8"), lowered),
                                     encoding="utf-8")
            print(f"land_law_state: wrote {baseline_path.name}")
        code = gls.main([], root=root)
        if code != 0:
            return 2
    return 1 if moved else 0


if __name__ == "__main__":
    sys.exit(main())
