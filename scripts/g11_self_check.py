#!/usr/bin/env python3
"""ALICE-Law G11 self-check: the seven checks a crate's G11 rests on, in one command, against the ALICE-Law
revision the crate pins.

    python3 <ALICE-Law checkout>/scripts/g11_self_check.py --repo . --name ALICE-<crate>

ALICE-Law is the checkout this script is in. It must be at the revision the crate's .github/shared-files-pin
names (`rev`), and that revision's conformance/shared-files.toml and scripts/shared_files_check.py must have the
SHA-256 the pin records (`table-sha256`, `checker-sha256`); otherwise nothing runs (check out the pinned revision
first: `git -C <ALICE-Law checkout> checkout --detach <rev>`). Then, from that checkout:

  1. g11-table   scripts/g11_table_gen.py --check: conformance/g11.toml is the generated table (the shared
                 checkers' rows and conformance/g11-own.toml), every pass gate has a row, every reference resolves
  2. g11-rows    scripts/g11_table_check.py: the table's rows (F1-F7 in that script)
  3. copies      scripts/shared_files_check.py --repo: every shared copy listed for the crate has its row's bytes,
                 and how many of its copies on pending rows are not yet the row's target (N); a manifest that
                 declares G11 `pass` while N > 0 is a finding (G11 is a pass per crate: a row still pending on
                 other repositories' mains does not hold it back, the crate's own copies must all be at target)
  4. manifest    scripts/conformance_roster_check.py --check-manifest: the manifest and its anchors at HEAD
  5. non-pass    scripts/non_pass_ratchet.py: the gates the manifest reads fail-known or unverified, not above the
                 crate's row (a public crate: conformance/non-pass-baseline.toml at the pinned revision, no row is a
                 note; a private crate: the baseline --non-pass-baseline FILE names, kept outside ALICE-Law, and no
                 row or no such file is a finding / not checked); a count below the row is a note (lower the row)
  6. no-panic    scripts/no_panic_ratchet.py --only --landing: the committed tree's production panic sites against
                 the crate's row (a public crate: conformance/no-panic-baseline.toml at the pinned revision; a
                 private crate: its own conformance-own/no-panic.toml) and against the crate's main, fetched from its
                 origin at check time (a failed fetch is not checked, never a skip): a site added over the row or
                 over main is a finding, a fall names the row to lower; the scanner is built once per source
                 revision into NO_PANIC_CACHE (~/.cache/alice-law/no-panic-scan) and verified before each use
  7. shared-tests every shared test file the crate holds (a shared row's path scripts/test_*.py present in the
                 committed tree) run as the crate's CI runs it, in a clone of the crate at HEAD, with ALICE-Law's url
                 sent to this checkout: a failing (or hung) one is a finding, none held is not checked; so a green
                 self-check predicts the CI of the same tree (2026-11-28: the copies check passed at a new pin while
                 every holder's scripts/test_shared_files.py failed its private-mode tests in CI); the rustc
                 targets a held test needs (conformance/shared-test-setup.toml, beside the table) are installed
                 first with `rustup target add` for the crate's toolchain, and a setup that cannot be installed is
                 not checked (exit 2), never a skip (2026-11-28: scripts/test_fneg_fma_ir_check.py needs both linux
                 targets, which the holders' CI step had not added); run on a developer's machine, the added
                 targets stay installed in that toolchain

A private repository (the pin's line `visibility private`; a pin without the line, or with `visibility public`, is a
public one) is never named in the shared-files table, which lists public repositories only. Its copies check runs in
scripts/shared_files_check.py's private mode (--private): every table path present in its tree is compared with the
row's bytes (a pending row's target), so a stale copy is a finding and no copy is "adopting" or off target. The
per-repository data files that mode reports (a row no repository holds, nothing to compare) must each be declared in
the crate's conformance/g11-own.toml under [private-data], path = reason (12 characters or more, 4 distinct): an
undeclared one, a short reason, or a declared path the check does not report is a finding, and a public crate that
declares [private-data] is a finding too (its per-repository rows are the table's `exempt`).

The pin's SHA-256 values are compared with the blobs at the pinned revision, and the table, rows and copies
checks read the crate's committed tree (each blob at HEAD, raw), so a checkout that converts line endings (Windows,
core.autocrlf) gives the same result as any other.
Each prints one line (its exit status and its summary). No GitHub API is used. Exit 0 when all seven pass, 1 when
any has a finding, 2 when any compared nothing or could not read its input, or the pin does not match.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

LAW = Path(__file__).resolve().parent.parent
PIN = ".github/shared-files-pin"
G11_PIN = ".github/g11-table-pin"
MANIFEST = "alice-law-conformance.toml"
OFF_TARGET_RE = re.compile(r"^shared_files_check: own copies not at a pending row's target: (\d+)", re.M)
PRIVATE_DATA_RE = re.compile(r"^shared_files_check: (?:pending: )?(\S.*?): per-repository data \(no repository holds "
                             r"the row\), not compared$", re.M)
OWN = "conformance/g11-own.toml"
NO_PANIC_OWN = "conformance-own/no-panic.toml"
NO_PANIC_CACHE = Path.home() / ".cache" / "alice-law" / "no-panic-scan"
# the shared test files a holder carries (a shared row's path of this form): the self-check runs each one, as the
# holder's CI does, so a green self-check predicts the CI of the same tree
SHARED_TEST_RE = re.compile(r"^scripts/test_[A-Za-z0-9_]+\.py$")
LAW_URL = "https://github.com/ext-sakamoro/ALICE-Law"
# the bound on one shared test file's run (seconds): a test that hangs is a finding, never a silent wait
SHARED_TEST_TIMEOUT = 1200
# what a shared test needs on the machine before it runs (rustc targets), declared next to the shared-files table:
# the self-check installs it in the crate's clone (its rust-toolchain), so a holder's CI needs no step of its own
SETUP = "conformance/shared-test-setup.toml"
SETUP_KEYS = {"path", "why"}  # and at least one of SETUP_NEEDS
SETUP_NEEDS = {"rustc-targets", "platforms", "cargo-tools"}
CARGO_TOOL_RE = re.compile(r"^(cargo-[a-z0-9-]+)@([0-9]+\.[0-9]+\.[0-9]+)$")
CARGO = ["cargo"]
TOOL_KEYS = {"name", "version", "url", "sha256"}
TOOL_CACHE = Path.home() / ".cache" / "alice-law" / "tools"


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 (an https url of a declared release)
        return r.read()
PLATFORMS = ("linux", "macos", "windows")
TARGET_RE = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_.]+){1,3}$")
RUSTUP = ["rustup"]


def declared_g11(repo: Path) -> str | None:
    """the G11 status the crate's manifest declares in its working tree, or None when it declares none"""
    try:
        doc = tomllib.loads((repo / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None  # the manifest check reports an unreadable or missing manifest
    g = doc.get("G11")
    return g.get("status") if isinstance(g, dict) else None


def pin_fields(text: str) -> dict[str, str]:
    """the pin's `key value` lines; each of rev / table-sha256 / checker-sha256 exactly once and well formed"""
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            fields.setdefault(parts[0], []).append(parts[1])
    out = {}
    for key, pattern in (("rev", r"[0-9a-f]{40}"), ("table-sha256", r"[0-9a-f]{64}"), ("checker-sha256", r"[0-9a-f]{64}")):
        values = fields.get(key, [])
        if len(values) != 1 or not re.fullmatch(pattern, values[0]):
            raise ValueError(f"{PIN}: needs one '{key} <{'40' if key == 'rev' else '64'} hex>'")
        out[key] = values[0]
    return out


def visibility(text: str) -> str:
    """the pin's `visibility` (public when the line is absent); anything but one public / private line is an error"""
    values = [ln.split()[1] for ln in text.splitlines() if len(ln.split()) == 2 and ln.split()[0] == "visibility"]
    if not values:
        return "public"
    if len(values) != 1 or values[0] not in ("public", "private"):
        raise ValueError(f"{PIN}: 'visibility' is one line, 'public' or 'private' (got {', '.join(values)})")
    return values[0]


def private_data_declared(repo: Path) -> dict[str, str]:
    """conformance/g11-own.toml [private-data] as committed at HEAD (path -> reason); {} when the file or the table is
    absent; ValueError when the file cannot be read"""
    r = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{OWN}"], capture_output=True)
    if r.returncode != 0:
        return {}
    try:
        table = tomllib.loads(r.stdout.decode("utf-8")).get("private-data", {})
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ValueError(f"{OWN}: {e}")
    if not isinstance(table, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in table.items()):
        raise ValueError(f"{OWN}: [private-data] is path = reason (strings)")
    return table


def private_data_findings(reported: list[str], declared: dict[str, str]) -> list[str]:
    out = [f"{p}: per-repository data the private copies check does not compare, not declared in {OWN} [private-data]"
           for p in reported if p not in declared]
    out += [f"{p}: {OWN} [private-data] reason {r!r} is too short (12 characters or more, 4 distinct)"
            for p, r in sorted(declared.items()) if len(r.strip()) < 12 or len(set(r.strip())) < 4]
    out += [f"{p}: declared in {OWN} [private-data], but the private copies check does not report it as "
            "per-repository data (remove the line)" for p in sorted(declared) if p not in reported]
    return out


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def blob_sha256(law: Path, rev: str, rel: str) -> str | None:
    """SHA-256 of the file as committed at rev (the blob, not the checkout: a Windows checkout with core.autocrlf
    holds other bytes than the ones pinned), or None when rev has no such file"""
    r = subprocess.run(["git", "-C", str(law), "show", f"{rev}:{rel}"], capture_output=True)
    return hashlib.sha256(r.stdout).hexdigest() if r.returncode == 0 else None


def committed_tree(repo: Path, dest: Path, ref: str = "HEAD") -> None:
    """the crate's committed tree at `ref` (HEAD) into dest, each file written as its raw blob (git archive would apply
    core.autocrlf and eol attributes, a converted checkout holds other bytes again): the bytes the crates share"""
    ls = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-z", ref], capture_output=True)
    if ls.returncode != 0:
        raise ValueError(f"{repo}: git ls-tree {ref} failed ({ls.stderr.decode(errors='replace').strip()[:200]})")
    entries = []
    for item in ls.stdout.split(b"\0"):
        if not item:
            continue
        meta, path = item.split(b"\t", 1)
        mode, kind, oid = meta.split()
        # a symbolic link is not a shared copy: it is left out (a listed copy there is reported missing), never
        # written as a file holding its target path, nor followed out of the tree
        if kind == b"blob" and mode != b"120000":
            entries.append((oid.decode(), path.decode("utf-8", errors="surrogateescape")))
    if not entries:
        raise ValueError(f"{repo}: {ref} has no file (nothing to check)")
    batch = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                           input=b"".join(f"{oid}\n".encode() for oid, _ in entries), capture_output=True)
    if batch.returncode != 0:
        raise ValueError(f"{repo}: git cat-file --batch failed")
    out, pos = batch.stdout, 0
    for oid, rel in entries:
        header_end = out.index(b"\n", pos)
        size = int(out[pos:header_end].split()[2])
        data = out[header_end + 1:header_end + 1 + size]
        pos = header_end + 1 + size + 1  # the blob, then its newline
        target = dest / rel
        if dest.resolve() not in target.resolve().parents:
            raise ValueError(f"{repo}: path {rel!r} leaves the tree")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def fetched_main(repo: Path, dest: Path) -> None:
    """the crate's main as its origin has it now (fetched at check time, never a local ref that may be stale), its
    committed tree written into dest; raises ValueError when the fetch fails"""
    f = subprocess.run(["git", "-C", str(repo), "fetch", "-q", "--no-tags", "origin",
                        "+refs/heads/main:refs/remotes/origin/main"], capture_output=True)
    if f.returncode != 0:
        raise ValueError(f"git fetch origin main failed ({f.stderr.decode(errors='replace').strip()[:200]})")
    committed_tree(repo, dest, "refs/remotes/origin/main")


def pin_problem(repo: Path, law: Path) -> str | None:
    """why the ALICE-Law checkout is not the crate's pinned one, or None"""
    pin = repo / PIN
    if not pin.is_file():
        return f"{pin} is not there (the crate pins no ALICE-Law revision)"
    try:
        f = pin_fields(pin.read_text(encoding="utf-8"))
        visibility(pin.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return str(e)
    head = subprocess.run(["git", "-C", str(law), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8")
    if head.returncode != 0:
        return f"{law}: not a git checkout"
    if head.stdout.strip() != f["rev"]:
        return (f"ALICE-Law is at {head.stdout.strip()[:12]}, the crate pins {f['rev'][:12]} "
                f"(git -C {law} checkout --detach {f['rev']})")
    for key, rel in (("table-sha256", "conformance/shared-files.toml"), ("checker-sha256", "scripts/shared_files_check.py")):
        if blob_sha256(law, f["rev"], rel) != f[key]:
            return f"{rel} at {f['rev'][:12]} does not have the pin's {key}"
    # the checks run from the working tree: it must be the pinned revision's files, nothing edited or added (an
    # untracked module would be imported); the one exception is an untracked copy of this script, for a pin that
    # predates it (a tracked one is held to HEAD like any other file)
    st = subprocess.run(["git", "-C", str(law), "status", "--porcelain", "--untracked-files=all", "--",
                         "scripts", "conformance"], capture_output=True, text=True, encoding="utf-8")
    if st.returncode != 0:
        return f"{law}: git status failed"
    dirty = [ln for ln in st.stdout.splitlines() if ln.strip() and ln != f"?? scripts/{Path(__file__).name}"]
    if dirty:
        return (f"{law} differs from {f['rev'][:12]} in {len(dirty)} file(s) ({dirty[0][3:]}…): the checks would not be "
                "the pinned ones (use a clean checkout of the pinned revision)")
    g11 = repo / G11_PIN
    if g11.is_file():
        fields: dict[str, list[str]] = {}
        for line in g11.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2:
                fields.setdefault(parts[0], []).append(parts[1])
        rev, chk = fields.get("rev", []), fields.get("checker-sha256", [])
        if len(rev) != 1 or len(chk) != 1 or not re.fullmatch(r"[0-9a-f]{40}", rev[0]) \
                or not re.fullmatch(r"[0-9a-f]{64}", chk[0]):
            return f"{G11_PIN}: needs one 'rev <40 hex>' and one 'checker-sha256 <64 hex>'"
        if rev[0] != f["rev"]:
            return (f"{G11_PIN} names {rev[0][:12]} and {PIN} names {f['rev'][:12]}: the crate's G11 table would be "
                    "checked by another revision than its own scripts check it with (pin both to one revision)")
        if blob_sha256(law, f["rev"], "scripts/g11_table_check.py") != chk[0]:
            return f"scripts/g11_table_check.py at {f['rev'][:12]} does not have {G11_PIN}'s checker-sha256"
    return None


def shared_tests(table: Path, tree: Path) -> list[str]:
    """the shared rows' test files (scripts/test_*.py, a row with `repos`) the committed tree holds, in path order"""
    rows = tomllib.loads(table.read_text(encoding="utf-8")).get("file", [])
    paths = sorted({r["path"] for r in rows if "repos" in r and SHARED_TEST_RE.match(r["path"])})
    return [p for p in paths if (tree / p).is_file()]


def setup_rows(law: Path, table: Path) -> dict[str, dict]:
    """{shared test path: {"rustc-targets": [...], "platforms": [...]}} from ALICE-Law's
    conformance/shared-test-setup.toml ({} when the file is absent; a key not given is absent from the dict);
    ValueError on an unknown or missing key (path and why, and at least one of rustc-targets and platforms), a path that
    is no shared test row of the table, a target that is not a target triple, a platform not in PLATFORMS, an empty
    list or a path declared twice"""
    f = law / SETUP
    if not f.is_file():
        return {}
    rows = tomllib.loads(table.read_text(encoding="utf-8")).get("file", [])
    shared = {r["path"] for r in rows if "repos" in r and SHARED_TEST_RE.match(r["path"])}
    data = tomllib.loads(f.read_text(encoding="utf-8"))
    if set(data) - {"test", "tool"}:
        raise ValueError(f"{SETUP}: unknown top-level keys {sorted(set(data) - {'test', 'tool'})}")
    declared = {f"{t['name']}@{t['version']}" for t in tool_rows(data)}
    out: dict[str, dict] = {}
    for i, t in enumerate(data.get("test", []), 1):
        where = f"{SETUP} test {i}"
        if not isinstance(t, dict) or not SETUP_KEYS <= set(t) or not set(t) - SETUP_KEYS \
                or set(t) - SETUP_KEYS - SETUP_NEEDS:
            got = sorted(t) if isinstance(t, dict) else type(t).__name__
            raise ValueError(f"{where}: needs the keys {sorted(SETUP_KEYS)} and at least one of {sorted(SETUP_NEEDS)}, "
                             f"nothing else, has {got}")
        path, why = t["path"], t["why"]
        if path not in shared:
            raise ValueError(f"{where}: {path!r} is no shared test row (scripts/test_*.py with repos) of the table")
        if path in out:
            raise ValueError(f"{where}: {path} is declared twice")
        if not isinstance(why, str) or not why.strip():
            raise ValueError(f"{where}: why must say what the test needs")
        entry: dict = {}
        if "rustc-targets" in t:
            targets = t["rustc-targets"]
            if not isinstance(targets, list) or not targets or not all(isinstance(x, str) and TARGET_RE.match(x)
                                                                        for x in targets):
                raise ValueError(f"{where}: rustc-targets must be a non-empty list of target triples, has {targets!r}")
            entry["rustc-targets"] = sorted(set(targets))
        if "platforms" in t:
            plats = t["platforms"]
            if not isinstance(plats, list) or not plats or not all(x in PLATFORMS for x in plats) \
                    or len(set(plats)) != len(plats):
                raise ValueError(f"{where}: platforms must be a non-empty list of distinct names from {PLATFORMS}, "
                                 f"has {plats!r}")
            entry["platforms"] = [p for p in PLATFORMS if p in plats]
        if "cargo-tools" in t:
            tools = t["cargo-tools"]
            if not isinstance(tools, list) or not tools or not all(isinstance(x, str) and CARGO_TOOL_RE.match(x)
                                                                    for x in tools):
                raise ValueError(f"{where}: cargo-tools must be a non-empty list of cargo-<name>@<x.y.z>, has {tools!r}")
            if set(tools) - declared:
                raise ValueError(f"{where}: cargo-tools {sorted(set(tools) - declared)} have no [[tool]] row")
            entry["cargo-tools"] = sorted(set(tools))
        out[path] = entry
    return out


def shared_test_setup(law: Path, table: Path) -> dict[str, list[str]]:
    """{shared test path: the rustc targets it needs} (the tests that declare targets); see setup_rows"""
    return {p: e["rustc-targets"] for p, e in setup_rows(law, table).items() if "rustc-targets" in e}


def shared_test_platforms(law: Path, table: Path) -> dict[str, list[str]]:
    """{shared test path: the platforms its tool runs on} (the tests that declare them; any other runs everywhere)"""
    return {p: e["platforms"] for p, e in setup_rows(law, table).items() if "platforms" in e}


def tool_rows(data: dict) -> list[dict]:
    """the [[tool]] rows of the setup file: name (cargo-<x>), version (x.y.z), url (https, `{version}` and
    `{target}` replaced) and sha256 {target triple: the release archive's SHA-256}; ValueError on another form"""
    out = []
    seen = set()
    for i, t in enumerate(data.get("tool", []), 1):
        where = f"{SETUP} tool {i}"
        if not isinstance(t, dict) or set(t) != TOOL_KEYS:
            raise ValueError(f"{where}: needs exactly the keys {sorted(TOOL_KEYS)}")
        if not CARGO_TOOL_RE.match(f"{t['name']}@{t['version']}"):
            raise ValueError(f"{where}: name must be cargo-<name> and version x.y.z")
        if not isinstance(t["url"], str) or not t["url"].startswith("https://") or "{target}" not in t["url"]:
            raise ValueError(f"{where}: url must be an https url naming {{target}}")
        sums = t["sha256"]
        if not isinstance(sums, dict) or not sums or not all(TARGET_RE.match(k) and isinstance(v, str)
                                                             and re.fullmatch(r"[0-9a-f]{64}", v)
                                                             for k, v in sums.items()):
            raise ValueError(f"{where}: sha256 must map target triples to 64 hex digits")
        key = (t["name"], t["version"])
        if key in seen:
            raise ValueError(f"{where}: {t['name']}@{t['version']} is declared twice")
        seen.add(key)
        out.append(t)
    return out


def setup_tools(law: Path) -> dict[str, dict]:
    """{cargo-<name>@<version>: its [[tool]] row} ({} when the setup file is absent)"""
    f = law / SETUP
    if not f.is_file():
        return {}
    return {f"{t['name']}@{t['version']}": t for t in tool_rows(tomllib.loads(f.read_text(encoding="utf-8")))}


def shared_test_cargo_tools(law: Path, table: Path) -> dict[str, list[str]]:
    """{shared test path: the cargo subcommands (cargo-<name>@<version>) it needs installed}"""
    return {p: e["cargo-tools"] for p, e in setup_rows(law, table).items() if "cargo-tools" in e}


def host_triple(clone: Path) -> str | None:
    try:
        r = subprocess.run(["rustc", "-vV"], cwd=clone, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    except OSError:
        return None
    m = re.search(r"(?m)^host: (\S+)$", r.stdout)
    return m.group(1) if r.returncode == 0 and m else None


def unpack(archive: bytes, name: str, url: str, into: Path) -> Path | None:
    """the executable `name` (or name.exe) from a .tar.gz or .zip release archive, written under `into`"""
    import io
    exe = {name, name + ".exe"}
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            for m in z.namelist():
                if Path(m).name in exe:
                    out = into / Path(m).name
                    out.write_bytes(z.read(m))
                    out.chmod(0o755)
                    return out
        return None
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as t:
        for m in t.getmembers():
            if m.isfile() and Path(m.name).name in exe:
                out = into / Path(m.name).name
                out.write_bytes(t.extractfile(m).read())
                out.chmod(0o755)
                return out
    return None


def install_cargo_tools(clone: Path, tools: list[str], cargo: list[str], decls: dict[str, dict] | None = None,
                        bin_dir: Path | None = None, host: str | None = None, cache: Path | None = None,
                        fetch_url=None) -> tuple[bool, str]:
    """(installed, line) for each declared cargo tool: kept when `cargo <sub> --version` already prints that version;
    else the release archive its [[tool]] row names for this host is fetched (cached by its SHA-256 under
    TOOL_CACHE), checked against the row's SHA-256 and unpacked into `bin_dir` (put on the tests' PATH, nothing is
    installed on the machine); a host the row has no archive for builds it with `cargo install --locked <name>
    --version <v>`; False when the archive does not match, cannot be fetched or unpacked, or the build fails"""
    decls = decls or {}
    fetch_url = fetch_url or fetch
    cache = cache or TOOL_CACHE

    def run(args: list[str]) -> subprocess.CompletedProcess | str:
        try:
            return subprocess.run(cargo + args, cwd=clone, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace")
        except OSError as e:
            return str(e)
    done = []
    for spec in tools:
        name, version = CARGO_TOOL_RE.match(spec).groups()
        have = run([name.removeprefix("cargo-"), "--version"])
        if not isinstance(have, str) and have.returncode == 0 and f"{name} {version}" in have.stdout:
            continue
        row = decls.get(spec)
        triple = host or host_triple(clone)
        if row and triple in row["sha256"] and bin_dir is not None:
            want = row["sha256"][triple]
            url = row["url"].replace("{version}", version).replace("{target}", triple)
            cached = cache / f"{want}{'.zip' if url.endswith('.zip') else '.tar.gz'}"
            try:
                data = cached.read_bytes() if cached.is_file() else fetch_url(url)
            except OSError as e:
                return False, f"setup: {url} could not be fetched ({e}): the shared tests are not run"
            if hashlib.sha256(data).hexdigest() != want:
                return False, (f"setup: {url} has SHA-256 {hashlib.sha256(data).hexdigest()[:12]}, the setup file "
                               f"names {want[:12]}: the shared tests are not run")
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(data)
            except OSError:
                pass  # the cache is only a speed-up; the checked bytes are used either way
            bin_dir.mkdir(parents=True, exist_ok=True)
            if unpack(data, name, url, bin_dir) is None:
                return False, f"setup: {url} holds no {name} executable: the shared tests are not run"
            done.append(f"{spec} (release archive for {triple}, SHA-256 checked)")
            continue
        r = run(["install", "--locked", name, "--version", version])
        if isinstance(r, str) or r.returncode != 0:
            why = r if isinstance(r, str) else r.stderr.strip()[-200:]
            return False, f"setup: cargo install {name} --version {version} failed ({why}): the shared tests are not run"
        done.append(f"{spec} (built)")
    if not done:
        return True, f"setup: cargo tools {', '.join(tools)} already installed"
    return True, f"setup: cargo tools installed: {', '.join(done)}"


def this_platform(name: str = sys.platform) -> str:
    return "windows" if name.startswith(("win", "cygwin", "msys")) else "macos" if name == "darwin" else "linux"


def install_setup(clone: Path, targets: list[str], rustup: list[str]) -> tuple[bool, str]:
    """(installed, line): `rustup target add` for the targets the crate's toolchain (rustup run in the clone, so its
    rust-toolchain file chooses) does not have yet; False when rustup cannot be run or either call fails"""
    def run(args: list[str]) -> subprocess.CompletedProcess | str:
        try:
            return subprocess.run(rustup + args, cwd=clone, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace")
        except OSError as e:
            return str(e)
    lst = run(["target", "list", "--installed"])
    if isinstance(lst, str) or lst.returncode != 0:
        why = lst if isinstance(lst, str) else lst.stderr.strip()[:200]
        return False, f"setup: rustup target list --installed failed ({why}): the shared tests are not run"
    have = set(lst.stdout.split())
    missing = [t for t in targets if t not in have]
    if not missing:
        return True, f"setup: rustc targets {', '.join(targets)} already installed"
    add = run(["target", "add", *missing])
    if isinstance(add, str) or add.returncode != 0:
        why = add if isinstance(add, str) else add.stderr.strip()[:200]
        return False, f"setup: rustup target add {' '.join(missing)} failed ({why}): the shared tests are not run"
    return True, f"setup: rustc targets added: {', '.join(missing)}"


def run_shared_tests(repo: Path, law: Path, tests: list[str], timeout: int = SHARED_TEST_TIMEOUT,
                     setup: dict[str, list[str]] | None = None,
                     rustup: list[str] | None = None,
                     platforms: dict[str, list[str]] | None = None,
                     platform: str | None = None,
                     cargo_tools: dict[str, list[str]] | None = None,
                     cargo: list[str] | None = None,
                     tool_decls: dict[str, dict] | None = None,
                     host: str | None = None,
                     tool_cache: Path | None = None,
                     fetch_url=None) -> tuple[int, list[str]]:
    """(exit status, lines): each shared test file run in a clone of the crate at HEAD (the tests read their own git
    repository), with ALICE-Law's url sent to this checkout (the pinned revision, so a test that fetches ALICE-Law reads
    the same bytes, no network), after the rustc targets `setup` declares for the held tests are installed; 1 when any
    fails or exceeds `timeout`, 2 when none is held, the clone fails or the setup cannot be installed; a test whose
    `platforms` (declared in ALICE-Law) leave out this platform is not run here and is named (its tool does not run on
    this platform in any holder, so its other legs run it)"""
    if not tests:
        return 2, ["the committed tree holds no shared test file (nothing compared)"]
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if head.returncode != 0:
        return 2, [f"git rev-parse HEAD failed ({head.stderr.strip()[:200]})"]
    env = dict(os.environ)
    n = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    env[f"GIT_CONFIG_KEY_{n}"] = f"url.{law.resolve().as_uri()}.insteadOf"
    env[f"GIT_CONFIG_VALUE_{n}"] = LAW_URL
    env["GIT_CONFIG_COUNT"] = str(n + 1)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    failed: list[str] = []
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "clone"
        for cmd in (["git", "clone", "-q", "--no-checkout", "--shared", str(repo), str(clone)],
                    ["git", "-C", str(clone), "checkout", "-q", "--detach", head.stdout.strip()]):
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                return 2, [f"{' '.join(cmd[:4])} failed ({r.stderr.strip()[:200]})"]
        here = this_platform() if platform is None else platform
        elsewhere = [rel for rel in tests if here not in (platforms or {}).get(rel, PLATFORMS)]
        if elsewhere:
            lines.append(f"not run on {here} (declared for other platforms): "
                         + ", ".join(f"{rel} ({'/'.join((platforms or {})[rel])})" for rel in elsewhere))
        run = [rel for rel in tests if rel not in elsewhere]
        targets = sorted({t for rel in run for t in (setup or {}).get(rel, [])})
        if targets:
            ok, line = install_setup(clone, targets, RUSTUP if rustup is None else rustup)
            if not ok:
                return 2, [line]
            lines.append(line)
        tools = sorted({t for rel in run for t in (cargo_tools or {}).get(rel, [])})
        if tools:
            bin_dir = Path(tmp) / "bin"
            ok, line = install_cargo_tools(clone, tools, CARGO if cargo is None else cargo, tool_decls, bin_dir,
                                           host, tool_cache, fetch_url)
            if not ok:
                return 2, [line]
            lines.append(line)
            if bin_dir.is_dir():
                env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        for rel in run:
            try:
                r = subprocess.run([sys.executable, rel], cwd=clone, env=env, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout)
            except subprocess.TimeoutExpired:
                failed.append(rel)
                lines.append(f"{rel}: did not finish in {timeout} s")
                continue
            if r.returncode != 0:
                failed.append(rel)
                tail = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()][-6:]
                lines.append(f"{rel}: exit {r.returncode}")
                lines.extend(f"  {ln}" for ln in tail)
    if failed:
        lines.append(f"{len(failed)} of {len(run)} shared test file(s) failed: {', '.join(failed)}")
        return 1, lines
    tail = f" ({len(elsewhere)} declared for other platforms, not run here)" if elsewhere else ""
    lines.append(f"{len(run)} shared test file(s) passed{tail}: {', '.join(run) or '(none)'}")
    return 0, lines


def checks(repo: Path, name: str, law: Path, rev: str, private: bool = False,
           non_pass_baseline: Path | None = None, main_tree: Path | None = None,
           cache: Path = NO_PANIC_CACHE, tree: Path | None = None) -> list[tuple[str, list[str]]]:
    s = str(law / "scripts")
    # the no-panic check scans the committed tree (and the private row committed there), against main's tree
    src = tree or repo
    no_panic = [sys.executable, f"{s}/no_panic_ratchet.py", "--only", f"{name}={src}", "--landing",
                "--base", str(main_tree), "--baseline",
                str(src / NO_PANIC_OWN) if private else str(law / "conformance/no-panic-baseline.toml"),
                "--scanner-cache", str(cache)]
    if private:
        non_pass = [sys.executable, f"{s}/non_pass_ratchet.py", "--private", f"{name}={repo}",
                    "--baseline", str(non_pass_baseline), "--gates", str(law / "conformance/gates.toml")]
    else:
        non_pass = [sys.executable, f"{s}/non_pass_ratchet.py", "--only", f"{name}={repo}",
                    "--baseline", str(law / "conformance/non-pass-baseline.toml"),
                    "--gates", str(law / "conformance/gates.toml")]
    return [
        ("g11-table", [sys.executable, f"{s}/g11_table_gen.py", "--repo", str(repo), "--name", name, "--check",
                       "--law", f"{rev}={law}"]),
        ("g11-rows", [sys.executable, f"{s}/g11_table_check.py", "--repo", str(repo), "--name", name,
                      "--law", f"{rev}={law}"]),
        ("copies", [sys.executable, f"{s}/shared_files_check.py", "--table", str(law / "conformance/shared-files.toml"),
                    "--repo", str(repo), "--name", name] + (["--private"] if private else [])),
        ("manifest", [sys.executable, f"{s}/conformance_roster_check.py", "--check-manifest", str(repo), "--ref", "HEAD"]),
        ("non-pass", non_pass),
        ("no-panic", no_panic),
    ]


def run_checks(repo: Path, name: str, law: Path, rev: str, tree: Path, private: bool = False,
               non_pass_baseline: Path | None = None, main_tree: Path | None = None,
               main_problem: str | None = None, cache: Path = NO_PANIC_CACHE) -> list[int]:
    """each check's exit status (and the own-target verdict), printing one line per check"""
    codes: list[int] = []
    # the tree checks read the committed tree; the manifest check reads HEAD itself (--ref HEAD)
    # the manifest and non-pass checks read HEAD itself (a git tree), the others the committed tree
    commands = [(label, [tree.as_posix() if x == str(repo) and label not in ("manifest", "non-pass") else x
                         for x in cmd])
                for label, cmd in checks(repo, name, law, rev, private, non_pass_baseline, main_tree, cache, tree)]
    for label, cmd in commands:
        if label == "no-panic" and main_problem:
            codes.append(2)
            print(f"g11 self-check: no-panic: the crate's main could not be read ({main_problem}): not checked")
            continue
        if label == "no-panic" and private and not (tree / NO_PANIC_OWN).is_file():
            codes.append(2)
            print(f"g11 self-check: no-panic: a private crate keeps its row in {NO_PANIC_OWN}, not committed (write it "
                  "with no_panic_ratchet.py --write-own NAME=. --baseline " + NO_PANIC_OWN + "): not checked")
            continue
        if label == "non-pass" and private and non_pass_baseline is None:
            codes.append(2)
            print("g11 self-check: non-pass: a private crate's baseline is not handed in (--non-pass-baseline FILE): "
                  "not checked")
            continue
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        lines = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
        codes.append(r.returncode)
        print(f"g11 self-check: {label}: exit {r.returncode}: {lines[-1] if lines else '(no output)'}")
        if r.returncode != 0:
            for ln in lines[-12:-1]:
                print(f"  {ln}")
        if label == "copies" and r.returncode in (0, 1) and private:
            # private mode: every present copy is compared with its row's bytes (a pending row's target), so a
            # copy off target is already a finding of the check; what it cannot compare must be declared
            print("g11 self-check: own-target: private mode (each present copy compared with its row's bytes or "
                  "target; an off-target copy is a finding of the copies check)")
            reported = PRIVATE_DATA_RE.findall(r.stdout)
            try:
                declared = private_data_declared(repo)
            except ValueError as e:
                codes.append(2)
                print(f"g11 self-check: private-data: {e}: not checked")
                continue
            found = private_data_findings(reported, declared)
            codes.append(1 if found else 0)
            for ln in found:
                print(f"g11 self-check: private-data: {ln}")
            if not found:
                print(f"g11 self-check: private-data: {len(reported)} per-repository data file(s) declared")
        elif label == "copies" and r.returncode in (0, 1):
            try:
                if private_data_declared(repo):
                    codes.append(1)
                    print(f"g11 self-check: private-data: {OWN} declares [private-data], but the pin is public "
                          "(a public crate's per-repository rows are the table's exempt)")
            except ValueError as e:
                codes.append(2)
                print(f"g11 self-check: private-data: {e}: not checked")
            m = OFF_TARGET_RE.search(r.stdout)
            if m is None:
                codes.append(2)
                print("g11 self-check: own-target: the copies check printed no count of own copies off target")
            elif declared_g11(repo) == "pass" and int(m.group(1)) > 0:
                codes.append(1)
                print(f"g11 self-check: own-target: {MANIFEST} declares G11 pass, and {m.group(1)} own cop"
                      f"{'y is' if m.group(1) == '1' else 'ies are'} not at a pending row's target (a pass needs "
                      "every own copy at target)")
            else:
                print(f"g11 self-check: own-target: {m.group(1)} own copies off target, G11 declared "
                      f"{declared_g11(repo) or '(none)'}")
    try:
        tests = shared_tests(law / "conformance/shared-files.toml", tree)
        setup = shared_test_setup(law, law / "conformance/shared-files.toml")
        platforms = shared_test_platforms(law, law / "conformance/shared-files.toml")
        cargo_tools = shared_test_cargo_tools(law, law / "conformance/shared-files.toml")
        tool_decls = setup_tools(law)
    except (OSError, ValueError, KeyError) as e:  # tomllib.TOMLDecodeError is a ValueError
        codes.append(2)
        print(f"g11 self-check: shared-tests: the table or {SETUP} could not be read ({e}): not checked")
        return codes
    code, lines = run_shared_tests(repo, law, tests, setup=setup, platforms=platforms, cargo_tools=cargo_tools,
                                   tool_decls=tool_decls)
    codes.append(code)
    for ln in lines[:-1] if code == 0 else []:  # a failure prints its lines below
        if ln.startswith(("setup: ", "not run on ")):
            print(f"g11 self-check: shared-tests: {ln}")
    print(f"g11 self-check: shared-tests: exit {code}: {lines[-1]}")
    if code != 0:
        for ln in lines[:-1][-12:]:
            print(f"  {ln}")
    return codes


def main(argv: list[str] | None = None, law: Path = LAW) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--name", required=True)
    ap.add_argument("--non-pass-baseline", type=Path, help="a private crate's non-pass baseline (kept outside ALICE-Law)")
    ap.add_argument("--no-panic-cache", type=Path, default=NO_PANIC_CACHE,
                    help="where the no-panic scanner is built once per source revision (verified before each use)")
    a = ap.parse_args(argv)
    repo = a.repo.resolve()
    why = pin_problem(repo, law)
    if why:
        print(f"g11 self-check: {why}: nothing was checked", file=sys.stderr)
        return 2
    pin_text = (repo / PIN).read_text(encoding="utf-8")
    rev = pin_fields(pin_text)["rev"]
    private = visibility(pin_text) == "private"
    with tempfile.TemporaryDirectory() as tmp:  # removed however the checks end
        tree, main_tree = Path(tmp) / "head", Path(tmp) / "main"
        tree.mkdir()
        main_tree.mkdir()
        try:
            committed_tree(repo, tree)
        except (ValueError, OSError) as e:
            print(f"g11 self-check: {e}: nothing was checked", file=sys.stderr)
            return 2
        try:
            fetched_main(repo, main_tree)
            main_problem = None
        except (ValueError, OSError) as e:
            main_problem = str(e)
        codes = run_checks(repo, a.name, law, rev, tree, private, a.non_pass_baseline, main_tree, main_problem,
                           a.no_panic_cache)
    verdict = 1 if 1 in codes else 2 if any(c != 0 for c in codes) else 0
    print(f"g11 self-check: {a.name} at ALICE-Law {rev[:12]}: "
          + ("pass" if verdict == 0 else "finding" if verdict == 1 else "not checked (an input could not be read or nothing was compared)"))
    return verdict


if __name__ == "__main__":
    sys.exit(main())
