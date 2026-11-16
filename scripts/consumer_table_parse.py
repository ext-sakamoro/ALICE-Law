#!/usr/bin/env python3
"""ALICE-Law G11 (c): the shared-file table on this revision is read by the crates' copy of the test
that reads it (scripts/test_shared_files.py, a shared file of its own), at the bytes the crates hold.

A crate's scripts/test_shared_files.py fetches ALICE-Law at its .github/shared-files-pin and parses
conformance/shared-files.toml there. A change of the table's format that a held version of that test
cannot parse turns every crate red the moment it moves its pin to this revision, and nothing in this
repository would see it: the table's own checker and generator read it fine. This check runs each
version of the test on the table:

  - the versions: the row's bytes (a uniform row) or, for a pending row, its target and every variant
    a listed main holds; the bytes come from conformance/planned/<blob> when the row plans them, else
    from a holder's main (read without credentials, as the live job reads the mains), each refused
    unless its SHA-256 is the one the table records
  - each version runs its own SharedFiles.setUpClass (which fetches the pinned revision and parses
    the table) in a scratch repository whose pin names a scratch commit holding the table, with git's
    fetch of ALICE-Law sent to that commit (no network): exit 0 = it reads the table
  - the target must read the table; any other held version must read it unless it could not read
    the base table either (--base-table): a table change may not newly break a version a main holds
    (a version already broken at the base is moving to the target, which reads it)

Exit 0 = every version that must read the table reads it; 1 = a finding; 2 = nothing compared (no
row for the test, no version, a version whose bytes could not be read).

usage: consumer_table_parse.py --table conformance/shared-files.toml [--base-table BASE]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shared_files_check as sfc  # noqa: E402

CONSUMER = "scripts/test_shared_files.py"
LAW_URL = "https://github.com/ext-sakamoro/ALICE-Law"
PROBE_TIMEOUT = 300  # seconds one version may take to read the table (it only parses)

# bytes of a version by its SHA-256 (None = could not be read: the reason)
Reader = Callable[[str], "bytes | str"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8", env=env).stdout.strip()


def versions(entry: dict) -> tuple[str, dict[str, list[str]]]:
    """(the target SHA-256, SHA-256 -> the repositories holding it on main) of the consumer's row"""
    if "pending" not in entry:
        return entry["sha256"], {entry["sha256"]: [entry["source"]["repo"]]}
    p = entry["pending"]
    held = {sha: list(repos) for sha, repos in p.get("variants", {}).items() if sha != "absent"}
    held.setdefault(p["target"], [p["owner"]])
    return p["target"], held


def scratch_env(law: Path) -> dict[str, str]:
    """git's fetches of ALICE-Law sent to the local repository `law` (and nothing else of the user's config)"""
    env = {k: v for k, v in os.environ.items() if not sfc.CONFIG_FROM_ENV.fullmatch(k)}
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{law.resolve().as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": LAW_URL, "GIT_TERMINAL_PROMPT": "0"})
    return env


def reads(consumer: bytes, table: bytes, work: Path) -> tuple[bool, str]:
    """(whether this version of the consumer's setUpClass reads `table`, its last output line)"""
    work.mkdir(parents=True)
    law, repo = work / "law", work / "repo"
    ident = {"GIT_AUTHOR_NAME": "probe", "GIT_AUTHOR_EMAIL": "probe@invalid", "GIT_COMMITTER_NAME": "probe",
             "GIT_COMMITTER_EMAIL": "probe@invalid", **{k: v for k, v in os.environ.items()
                                                        if not sfc.CONFIG_FROM_ENV.fullmatch(k)}}
    (law / "conformance").mkdir(parents=True)
    (law / "conformance/shared-files.toml").write_bytes(table)
    git("init", "-q", "-b", "main", cwd=law, env=ident)
    git("add", ".", cwd=law, env=ident)
    git("commit", "-q", "-m", "table", cwd=law, env=ident)
    rev = git("rev-parse", "HEAD", cwd=law, env=ident)
    (repo / "scripts").mkdir(parents=True)
    (repo / ".github").mkdir()
    (repo / "scripts/test_shared_files.py").write_bytes(consumer)
    (repo / ".github/shared-files-pin").write_text(
        f"name ALICE-Law\nrev {rev}\ntable-sha256 {sha256(table)}\nchecker-sha256 {sha256(b'')}\n", encoding="utf-8")
    code = ("import sys; sys.path.insert(0, 'scripts'); import test_shared_files as t; "
            "t.SharedFiles.setUpClass(); getattr(t.SharedFiles, 'tearDownClass', lambda: None)()")
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=repo, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=scratch_env(law), timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"did not finish in {PROBE_TIMEOUT} s"
    lines = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
    return r.returncode == 0, lines[-1] if lines else "(no output)"


def check(table: bytes, base: bytes | None, target: str, held: dict[str, list[str]], read: Reader,
          work: Path) -> tuple[list[str], list[str], int]:
    """(findings, unreadable versions, versions run) of the consumer on `table` (and `base`)"""
    findings, unreadable, n = [], [], 0
    for i, (sha, repos) in enumerate(sorted(held.items(), key=lambda kv: (kv[0] != target, kv[0]))):
        data = read(sha)
        if isinstance(data, str):
            unreadable.append(f"{CONSUMER} {sha[:12]} ({', '.join(repos)}): {data}")
            continue
        if sha256(data) != sha:
            unreadable.append(f"{CONSUMER} {sha[:12]}: the bytes read have SHA-256 {sha256(data)[:12]}")
            continue
        n += 1
        ok, last = reads(data, table, work / f"v{i}-table")
        kind = "target" if sha == target else "held by " + ", ".join(repos)
        print(f"consumer_table_parse: {CONSUMER} {sha[:12]} ({kind}): "
              f"{'reads the table' if ok else 'cannot read the table: ' + last}")
        if ok:
            continue
        if sha == target:
            findings.append(f"{CONSUMER} {sha[:12]} is the row's target and cannot read this table: {last}")
        elif base is None:
            findings.append(f"{CONSUMER} {sha[:12]} ({kind}) cannot read this table: {last}")
        else:
            ok_base, _ = reads(data, base, work / f"v{i}-base")
            if ok_base:
                findings.append(f"{CONSUMER} {sha[:12]} ({kind}) reads the base table and not this one: the table "
                                f"change breaks it ({last})")
            else:
                print(f"consumer_table_parse: {CONSUMER} {sha[:12]}: could not read the base table either "
                      "(moving to the target)")
    return findings, unreadable, n


def main_reader(table_path: Path, remote: sfc.Remote, entry: dict, held: dict[str, list[str]]) -> Reader:
    """bytes by SHA-256: the planned file next to the table, else a holder's main"""
    proofs = sfc.planned_proofs(table_path)
    planned = entry.get("pending", {}).get("planned")

    def read(sha: str) -> bytes | str:
        if planned and planned in proofs and sha256(proofs[planned]) == sha:
            return proofs[planned]
        last = "no holder"
        for repo in held.get(sha, []):
            try:
                data = sfc.show(remote(repo), "origin/main", CONSUMER)
            except sfc.Unreadable as e:
                last = str(e)
                continue
            if data is not None and sha256(data) == sha:
                return data
            last = f"{repo}'s main does not hold these bytes"
        return last
    return read


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--table", type=Path, required=True)
    ap.add_argument("--base-table", type=Path)
    a = ap.parse_args(argv)
    text = a.table.read_bytes()
    try:
        entry = next((e for e in sfc.load(text.decode("utf-8")) if e["path"] == CONSUMER), None)
    except sfc.Unreadable as e:
        print(f"consumer_table_parse: {a.table}: {e}", file=sys.stderr)
        return 2
    if entry is None:
        print(f"consumer_table_parse: {a.table} has no row for {CONSUMER}: compared nothing", file=sys.stderr)
        return 2
    target, held = versions(entry)
    base = a.base_table.read_bytes() if a.base_table else None
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "mains").mkdir()
        remote = sfc.Remote(Path(tmp) / "mains")
        findings, unreadable, n = check(text, base, target, held, main_reader(a.table, remote, entry, held),
                                        Path(tmp) / "probe")
    for f in findings:
        print(f"consumer_table_parse: {f}", file=sys.stderr)
    for u in unreadable:
        print(f"consumer_table_parse: not read: {u}", file=sys.stderr)
    print(f"consumer_table_parse: {n} version(s) of {CONSUMER} run on {a.table}, {len(findings)} finding(s)")
    return verdict(findings, unreadable, n)


def verdict(findings: list[str], unreadable: list[str], n: int) -> int:
    """the exit code: 2 when a version could not be read or none ran (compared nothing), else 1 on a finding"""
    if unreadable or n == 0:
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
