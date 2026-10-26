#!/usr/bin/env python3
"""ALICE-Law G11 self-check: the four checks a crate's G11 rests on, in one command, against the ALICE-Law
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

The pin's SHA-256 values are compared with the blobs at the pinned revision, and the table, rows and copies
checks read the crate's committed tree (each blob at HEAD, raw), so a checkout that converts line endings (Windows,
core.autocrlf) gives the same result as any other.
Each prints one line (its exit status and its summary). No GitHub API is used. Exit 0 when all four pass, 1 when
any has a finding, 2 when any compared nothing or could not read its input, or the pin does not match.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

LAW = Path(__file__).resolve().parent.parent
PIN = ".github/shared-files-pin"
G11_PIN = ".github/g11-table-pin"
MANIFEST = "alice-law-conformance.toml"
OFF_TARGET_RE = re.compile(r"^shared_files_check: own copies not at a pending row's target: (\d+)", re.M)


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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def blob_sha256(law: Path, rev: str, rel: str) -> str | None:
    """SHA-256 of the file as committed at rev (the blob, not the checkout: a Windows checkout with core.autocrlf
    holds other bytes than the ones pinned), or None when rev has no such file"""
    r = subprocess.run(["git", "-C", str(law), "show", f"{rev}:{rel}"], capture_output=True)
    return hashlib.sha256(r.stdout).hexdigest() if r.returncode == 0 else None


def committed_tree(repo: Path, dest: Path) -> None:
    """the crate's committed tree at HEAD into dest, each file written as its raw blob (git archive would apply
    core.autocrlf and eol attributes, a converted checkout holds other bytes again): the bytes the crates share"""
    ls = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-z", "HEAD"], capture_output=True)
    if ls.returncode != 0:
        raise ValueError(f"{repo}: git ls-tree HEAD failed ({ls.stderr.decode(errors='replace').strip()[:200]})")
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
        raise ValueError(f"{repo}: HEAD has no file (nothing to check)")
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


def pin_problem(repo: Path, law: Path) -> str | None:
    """why the ALICE-Law checkout is not the crate's pinned one, or None"""
    pin = repo / PIN
    if not pin.is_file():
        return f"{pin} is not there (the crate pins no ALICE-Law revision)"
    try:
        f = pin_fields(pin.read_text(encoding="utf-8"))
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


def checks(repo: Path, name: str, law: Path, rev: str) -> list[tuple[str, list[str]]]:
    s = str(law / "scripts")
    return [
        ("g11-table", [sys.executable, f"{s}/g11_table_gen.py", "--repo", str(repo), "--name", name, "--check",
                       "--law", f"{rev}={law}"]),
        ("g11-rows", [sys.executable, f"{s}/g11_table_check.py", "--repo", str(repo), "--name", name,
                      "--law", f"{rev}={law}"]),
        ("copies", [sys.executable, f"{s}/shared_files_check.py", "--table", str(law / "conformance/shared-files.toml"),
                    "--repo", str(repo), "--name", name]),
        ("manifest", [sys.executable, f"{s}/conformance_roster_check.py", "--check-manifest", str(repo), "--ref", "HEAD"]),
    ]


def run_checks(repo: Path, name: str, law: Path, rev: str, tree: Path) -> list[int]:
    """each check's exit status (and the own-target verdict), printing one line per check"""
    codes: list[int] = []
    # the tree checks read the committed tree; the manifest check reads HEAD itself (--ref HEAD)
    commands = [(label, [tree.as_posix() if x == str(repo) and label != "manifest" else x for x in cmd])
                for label, cmd in checks(repo, name, law, rev)]
    for label, cmd in commands:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        lines = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
        codes.append(r.returncode)
        print(f"g11 self-check: {label}: exit {r.returncode}: {lines[-1] if lines else '(no output)'}")
        if r.returncode != 0:
            for ln in lines[-12:-1]:
                print(f"  {ln}")
        if label == "copies" and r.returncode in (0, 1):
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
    return codes


def main(argv: list[str] | None = None, law: Path = LAW) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--name", required=True)
    a = ap.parse_args(argv)
    repo = a.repo.resolve()
    why = pin_problem(repo, law)
    if why:
        print(f"g11 self-check: {why}: nothing was checked", file=sys.stderr)
        return 2
    rev = pin_fields((repo / PIN).read_text(encoding="utf-8"))["rev"]
    with tempfile.TemporaryDirectory() as tmp:  # removed however the checks end
        tree = Path(tmp)
        try:
            committed_tree(repo, tree)
        except (ValueError, OSError) as e:
            print(f"g11 self-check: {e}: nothing was checked", file=sys.stderr)
            return 2
        codes = run_checks(repo, a.name, law, rev, tree)
    verdict = 1 if 1 in codes else 2 if any(c != 0 for c in codes) else 0
    print(f"g11 self-check: {a.name} at ALICE-Law {rev[:12]}: "
          + ("pass" if verdict == 0 else "finding" if verdict == 1 else "not checked (an input could not be read or nothing was compared)"))
    return verdict


if __name__ == "__main__":
    sys.exit(main())
