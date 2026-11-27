#!/usr/bin/env python3
"""The no-panic ratchet: every repository's production panic sites may only go down.

    no_panic_ratchet.py [--owner NAME] [--local NAME=DIR] [--jobs N]
    no_panic_ratchet.py --only NAME=DIR [--baseline OWN.toml] [--landing --base MAINDIR]
    no_panic_ratchet.py --write-baseline
    no_panic_ratchet.py --write-own NAME=DIR --baseline conformance-own/no-panic.toml
    no_panic_ratchet.py --prepare-scanner --scanner-cache DIR

--scanner-cache DIR (any mode) builds the scanner once per source revision into DIR/<sha256 of tools/no-panic-scan>/
and verifies it before each use (the recorded source key and the binary's recorded sha256): a mismatch or a missing
file rebuilds, never reuses. --prepare-scanner builds or verifies that entry and exits (a caller that runs builds
under a resource limit prepares the scanner there, then runs the check).

The count comes from tools/no-panic-scan (syntax tree, no build of the repository): per repository, the sites of
`.unwrap()`, `.expect(..)`, `panic!`, `todo!`, `unimplemented!`, `unreachable!` and `assert!` / `assert_eq!` /
`assert_ne!` (column `assert`: each panics in a release build when its condition fails) in its production code,
and every `allow(..)` / `expect(..)` attribute naming the clippy lint of one of them (a deny of the lint is void
there, so it is a site). `debug_assert!` and its forms are not counted: a release build compiles them out. conformance/no-panic-baseline.toml holds the counts of the public repositories, keyed by name. A private
repository keeps its own row in its own repository (conformance-own/no-panic.toml, the same format with its one row)
and checks it in its own CI with `--only NAME=. --baseline conformance-own/no-panic.toml` from a pinned revision of
this checker: no private name, nor a hash of one (a name of the form ALICE-<word> is recovered from its hash by
trying words), is written here. A `sha256:` key in the shared baseline is refused.

The rules, on every repository read:
  * a count above the baseline is red (a new site);
  * a count below the baseline is red until the baseline is lowered in the same commit (a stale baseline would let
    the count rise back unnoticed);
  * a repository with production Rust code and no row is red; a repository whose code does not parse is red;
  * 0 files read over all the repositories is red (a check that compared nothing passes nothing).
At a landing (--landing, with --only: the push gate of a repository landing on its main) a count below the row is
not red: the landing prints one line naming the row to lower ("lower the row ... in <baseline>") and passes, so a
repository that removes sites is not held until the row is lowered; the baseline's owner lowers the row within a day
of the landing. The limit at a landing is the smaller of the row and the count of the main being landed onto
(--base, required with --landing: a checkout of the repository's current main, read with the same scanner): while a row waits to be lowered,
a second landing that adds a site back is red against main, never hidden under the stale row. A count above the row
or above main, a file that does not parse (in the landing or in main), no row, and 0 files read (in either) stay red.
The live check (the default mode) keeps a decrease red until the row is lowered.
When a repository reaches 0 for a lint, its row keeps the 0: from then on the repository carries
`#![deny(clippy::<lint>)]` at its crate roots and the ratchet holds it at 0 even if the deny is removed.

Modes:
  (default)      every ALICE-* repository (--prefix) the GitHub API lists as public now (holder_live.public_repos), each default branch
                 fetched at depth 1; --local NAME=DIR reads repository NAME from the checkout DIR instead. A public
                 row whose repository is no longer listed as public is red (it was removed or made private: move or
                 drop its row). Private rows are not read here (a CI token sees only public repositories).
  --only N=DIR   one repository from DIR against its row in --baseline (the form a repository's own CI runs; a
                 private repository passes its own file)
  --write-baseline
                 rewrite the shared baseline from the live read above (every public ALICE-* repository at its
                 default branch), after a deliberate change of counts
  --write-own N=DIR
                 write the one row of N, read from DIR, to --baseline (a private repository's own file)

Exit 0 when nothing was found, 1 on a finding, 2 when something could not be read or nothing was compared.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import holder_live as hl  # noqa: E402

BASELINE = ROOT / "conformance" / "no-panic-baseline.toml"
SCANNER_DIR = ROOT / "tools" / "no-panic-scan"
LINTS = ("unwrap", "expect", "panic", "todo", "unimplemented", "unreachable", "assert")
GITHUB = "https://github.com/"


def scanner() -> Path:
    """the scanner binary: NO_PANIC_SCAN if set, else built from tools/no-panic-scan (release)"""
    env = os.environ.get("NO_PANIC_SCAN")
    if env:
        return Path(env)
    target = Path(os.environ.get("CARGO_TARGET_DIR", SCANNER_DIR / "target"))
    subprocess.run(["cargo", "build", "-q", "--release", "--manifest-path", str(SCANNER_DIR / "Cargo.toml")],
                   check=True, env={**os.environ, "CARGO_TARGET_DIR": str(target)})
    return target / "release" / "no-panic-scan"


def scanner_source_key(src: Path = SCANNER_DIR) -> str:
    """sha256 over the scanner's source (every file under its directory but target/, path and bytes, sorted): the
    key of a cached binary"""
    h = hashlib.sha256()
    files = sorted(p for p in src.rglob("*") if p.is_file() and "target" not in p.relative_to(src).parts)
    if not any(p.name == "Cargo.toml" for p in files):
        raise hl.Unreadable(f"{src}: no Cargo.toml (no scanner source)")
    for p in files:
        rel = p.relative_to(src).as_posix().encode()
        h.update(len(rel).to_bytes(8, "big") + rel)
        data = p.read_bytes()
        h.update(len(data).to_bytes(8, "big") + data)
    return h.hexdigest()


def file_sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def cached_scanner(cache: Path, src: Path = SCANNER_DIR, build=None) -> Path:
    """the scanner binary from cache/<source key>/, verified before use: the recorded source key must be the key of
    the source at hand and the binary's sha256 the one recorded when it was built; a mismatch or a missing file
    rebuilds (the entry is removed first), never reuses. An entry is built in a temporary directory beside it and
    renamed into place whole, so a reader never sees a binary without its records (two builders: the first rename
    wins, the other's build is dropped and the winner's entry verified). `build(target_dir)` builds and returns the
    binary (the tests pass their own)"""
    key = scanner_source_key(src)
    entry = cache / key

    def verified() -> Path | None:
        binary = entry / "no-panic-scan"
        try:
            if ((entry / "source.sha256").read_text(encoding="utf-8").strip() == key and binary.is_file()
                    and (entry / "binary.sha256").read_text(encoding="utf-8").strip() == file_sha256(binary)):
                return binary
        except OSError:
            pass
        return None

    got = verified()
    if got:
        return got
    shutil.rmtree(entry, ignore_errors=True)
    cache.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{key[:16]}-", dir=cache))
    try:
        with tempfile.TemporaryDirectory() as t:
            built = (build or _cargo_build(src))(Path(t))
            shutil.copy2(built, staging / "no-panic-scan")
        (staging / "binary.sha256").write_text(file_sha256(staging / "no-panic-scan") + "\n", encoding="utf-8")
        (staging / "source.sha256").write_text(key + "\n", encoding="utf-8")
        try:
            os.rename(staging, entry)
        except OSError:
            pass  # another builder renamed its entry first: verify that one below
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    got = verified()
    if got is None:
        raise hl.Unreadable(f"{entry}: the scanner entry does not verify after a build")
    return got


def _cargo_build(src: Path):
    def build(target: Path) -> Path:
        subprocess.run(["cargo", "build", "-q", "--release", "--manifest-path", str(src / "Cargo.toml")], check=True,
                       env={**os.environ, "CARGO_TARGET_DIR": str(target)})
        return target / "release" / "no-panic-scan"
    return build


def parse_scan(out: str) -> dict:
    """the scanner's tab-separated output → {"files", "unparsed", <lint>: n, "unparsed_files": [...]}"""
    r: dict = {"unparsed_files": []}
    for line in out.splitlines():
        if not line.strip() or line.startswith("site "):
            continue
        k, _, v = line.partition("\t")
        if k == "unparsed-file":
            r["unparsed_files"].append(v)
        else:
            r[k] = int(v)
    missing = [k for k in ("files", "unparsed", *LINTS) if k not in r]
    if missing:
        raise hl.Unreadable(f"scanner output lacks {missing}")
    return r


def scan_dir(bin_: Path, d: Path) -> dict:
    p = subprocess.run([str(bin_), str(d)], capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise hl.Unreadable(f"{d}: no-panic-scan exit {p.returncode}: {p.stderr.strip()[:200]}")
    return parse_scan(p.stdout)


def load_baseline(path: Path = BASELINE) -> dict[str, dict[str, int]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rows = data.get("repo", {})
    for k, row in rows.items():
        if k.startswith("sha256:"):
            raise ValueError(f"{path.name}: row {k!r}: a hashed name (a private repository keeps its own row in "
                             "its own repository)")
        bad = [lint for lint in LINTS if not isinstance(row.get(lint), int) or row[lint] < 0]
        extra = [f for f in row if f not in LINTS]
        if bad or extra:
            raise ValueError(f"{path.name}: row {k!r}: bad {bad} / unknown {extra}")
    return rows


def compare(read: dict[str, dict], baseline: dict[str, dict[str, int]], landing: bool = False,
            lowered: list[str] | None = None, base: dict[str, dict] | None = None) -> list[str]:
    """findings for the repositories read (key → scan); a repository with no production Rust code is not compared;
    with `landing`, a count below the row is no finding but a line in `lowered` naming the row to lower, and a count
    above the scan of the main being landed onto (`base`, required) is a new site against main"""
    out = []
    if landing:
        for k in sorted(read):
            b = (base or {}).get(k)
            if b is None:
                out.append(f"{k}: no read of the main being landed onto (a landing compares against main: pass --base)")
            elif b["unparsed"]:
                out.append(f"{k}: main: {b['unparsed']} file(s) do not parse ({', '.join(b['unparsed_files'][:5])})")
            elif b["files"] == 0 and read[k]["files"]:
                out.append(f"{k}: main: no production Rust code read (a landing compares against main)")
            else:
                for lint in LINTS:
                    if read[k][lint] > b[lint]:
                        out.append(f"{k}: {lint} {read[k][lint]} > main {b[lint]} (a new site against main)")
    for k in sorted(read):
        s = read[k]
        if s["unparsed"]:
            out.append(f"{k}: {s['unparsed']} file(s) do not parse ({', '.join(s['unparsed_files'][:5])})")
        if s["files"] == 0:
            if k in baseline:
                out.append(f"{k}: no production Rust code read but it has a row (drop the row)")
            continue
        row = baseline.get(k)
        if row is None:
            out.append(f"{k}: production Rust code and no row in the baseline")
            continue
        for lint in LINTS:
            have, base = s[lint], row[lint]
            if have > base:
                out.append(f"{k}: {lint} {have} > baseline {base} (a new site)")
            elif have < base and landing:
                if lowered is not None:
                    lowered.append(f"{k}: {lint} {have} < baseline {base}: lower the row [repo.{k}] {lint} to {have}")
            elif have < base:
                out.append(f"{k}: {lint} {have} < baseline {base} (lower the baseline in this commit)")
    return out


def render(rows: dict[str, dict[str, int]]) -> str:
    head = (
        "# The no-panic ratchet's baseline (scripts/no_panic_ratchet.py): each repository's production panic sites\n"
        "# counted by tools/no-panic-scan, keyed by repository name. A count may only go down; lower it in the commit\n"
        "# that removes sites. Regenerate with `no_panic_ratchet.py --write-baseline` (public repositories only: a\n"
        "# private one keeps its own row in its own repository, conformance-own/no-panic.toml).\n"
    )
    body = []
    for k in sorted(rows):
        body.append(f'\n[repo."{k}"]')
        body += [f"{lint} = {rows[k][lint]}" for lint in LINTS]
    return head + "\n".join(body) + "\n"


def read_live(owner: str, local: tuple[str, Path] | None, jobs: int, bin_: Path,
              prefix: str) -> tuple[dict, list[str]]:
    repos = [r for r in hl.public_repos(owner) if r["name"].startswith(prefix)]
    if not repos:
        raise hl.Unreadable("no public repository listed")
    names = {r["name"] for r in repos}
    if local and local[0] not in names:
        raise hl.Unreadable(f"--local {local[0]} is not a listed public repository")
    errors: list[str] = []
    read: dict[str, dict] = {}

    def one(base: Path, repo: dict):
        name = repo["name"]
        if local and name == local[0]:
            return name, scan_dir(bin_, local[1]), None
        if repo.get("size", 1) == 0:
            return name, None, None
        g = base / name
        try:
            subprocess.run(["git", "init", "-q", str(g)], check=True, capture_output=True)
            hl.git(g, "remote", "add", "origin", GITHUB + repo["full_name"])
            hl.git(g, "fetch", "-q", "--depth", "1", "--no-tags", "origin",
                   f"+refs/heads/{repo['default_branch']}:refs/remotes/origin/main")
            hl.git(g, "checkout", "-q", "--detach", "origin/main")
            return name, scan_dir(bin_, g), None
        except (hl.Unreadable, subprocess.CalledProcessError) as e:
            return name, None, str(e)
        finally:
            shutil.rmtree(g, ignore_errors=True)

    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=jobs) as ex:
        for name, s, err in ex.map(lambda r: one(Path(tmp), r), repos):
            if err:
                errors.append(f"{name}: {err}")
            elif s is not None:
                read[name] = s
    return read, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default="ext-sakamoro")
    ap.add_argument("--local", help="NAME=DIR: read public repository NAME from the checkout DIR")
    ap.add_argument("--only", help="NAME=DIR: compare only repository NAME, read from DIR")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--prefix", default="ALICE-", help="only repositories whose name starts with this")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--write-own", help="NAME=DIR: write the one row of NAME, read from DIR, to --baseline")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--landing", action="store_true",
                    help="with --only and --base: a count below the row prints the row to lower and passes; growth "
                         "against the row or against main stays red")
    ap.add_argument("--base", help="with --landing: a checkout of the repository's current main")
    ap.add_argument("--scanner-cache", type=Path,
                    help="build the scanner once per source revision into DIR/<source sha256>/ and verify it there "
                         "before each use (a mismatch or a missing binary rebuilds)")
    ap.add_argument("--prepare-scanner", action="store_true",
                    help="with --scanner-cache: build or verify the cached scanner, print its path and exit")
    a = ap.parse_args(argv)
    if a.prepare_scanner:
        if not a.scanner_cache:
            print("no_panic_ratchet: --prepare-scanner needs --scanner-cache DIR", file=sys.stderr)
            return 2
        try:
            print(f"no_panic_ratchet: scanner {cached_scanner(a.scanner_cache)}")
        except (hl.Unreadable, OSError, subprocess.CalledProcessError) as e:
            print(f"no_panic_ratchet: the scanner could not be built: {e}", file=sys.stderr)
            return 2
        return 0
    if a.landing and not a.only:
        print("no_panic_ratchet: --landing reads one repository: pass --only NAME=DIR", file=sys.stderr)
        return 2
    if a.landing and not a.base:
        print("no_panic_ratchet: --landing compares against the main being landed onto: pass --base MAINDIR",
              file=sys.stderr)
        return 2
    if a.base and not a.landing:
        print("no_panic_ratchet: --base is the main a landing compares against: pass --landing", file=sys.stderr)
        return 2
    try:
        bin_ = cached_scanner(a.scanner_cache) if a.scanner_cache else scanner()
    except (hl.Unreadable, OSError, subprocess.CalledProcessError) as e:
        print(f"no_panic_ratchet: the scanner could not be built: {e}", file=sys.stderr)
        return 2

    if a.write_own:
        if a.baseline == BASELINE:
            print("no_panic_ratchet: --write-own writes a repository's own file: pass --baseline", file=sys.stderr)
            return 2
        name, _, d = a.write_own.partition("=")
        s1 = scan_dir(bin_, Path(d))
        if s1["unparsed"]:
            print(f"no_panic_ratchet: {name}: {s1['unparsed']} file(s) do not parse", file=sys.stderr)
            return 2
        if not s1["files"]:
            print(f"no_panic_ratchet: {name}: no production Rust code read", file=sys.stderr)
            return 2
        a.baseline.write_text(render({name: {lint: s1[lint] for lint in LINTS}}), encoding="utf-8")
        print(f"no_panic_ratchet: wrote the row of {name} to {a.baseline}")
        return 0

    if a.write_baseline:
        rows: dict[str, dict[str, int]] = {}
        try:
            live, errors = read_live(a.owner, None, a.jobs, bin_, a.prefix)
        except hl.Unreadable as e:
            print(f"no_panic_ratchet: {e}", file=sys.stderr)
            return 2
        if errors:
            for e in errors:
                print(f"no_panic_ratchet: unreadable: {e}", file=sys.stderr)
            return 2
        for k, s1 in live.items():
            if s1["unparsed"]:
                print(f"no_panic_ratchet: {k}: {s1['unparsed']} file(s) do not parse", file=sys.stderr)
                return 2
            if s1["files"]:
                rows[k] = {lint: s1[lint] for lint in LINTS}
        if not rows:
            print("no_panic_ratchet: no repository with production Rust code read", file=sys.stderr)
            return 2
        a.baseline.write_text(render(rows), encoding="utf-8")
        print(f"no_panic_ratchet: wrote {len(rows)} rows")
        return 0

    baseline = load_baseline(a.baseline)
    base_read: dict[str, dict] = {}
    if a.only:
        name, _, d = a.only.partition("=")
        read = {name: scan_dir(bin_, Path(d))}
        errors: list[str] = []
        if a.base:
            if not Path(a.base).is_dir():
                print(f"no_panic_ratchet: --base {a.base}: not a directory", file=sys.stderr)
                return 2
            base_read[name] = scan_dir(bin_, Path(a.base))
    else:
        local = None
        if a.local:
            n, _, d = a.local.partition("=")
            local = (n, Path(d))
        try:
            read, errors = read_live(a.owner, local, a.jobs, bin_, a.prefix)
        except hl.Unreadable as e:
            print(f"no_panic_ratchet: {e}", file=sys.stderr)
            return 2
    files = sum(s["files"] for s in read.values())
    lowered: list[str] = []
    findings = compare(read, baseline, landing=a.landing, lowered=lowered, base=base_read)
    if not a.only:
        listed = set(read)
        for k in sorted(baseline):
            if k not in listed:
                findings.append(f"{k}: has a row but is not a listed public repository read here (move or drop it)")
    for e in errors:
        print(f"no_panic_ratchet: unreadable: {e}", file=sys.stderr)
    for f in findings:
        print(f"no_panic_ratchet: {f}")
    for line in lowered:
        print(f"no_panic_ratchet: landing: {line} in {a.baseline.name}")
    sites = sum(s[lint] for s in read.values() for lint in LINTS)
    print(f"no_panic_ratchet: {len(read)} repositories read, {files} production files, {sites} sites, "
          f"{len(findings)} finding(s)")
    if errors or files == 0:
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
