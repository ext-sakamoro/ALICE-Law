#!/usr/bin/env python3
"""The private name key (scripts/public_text_check.py: the HMAC key of the private-name tables) is in no tracked file.

    name_key_absent.py [--repo DIR] [--require-key]

Reads the key as the checker does (ALICE_PRIVATE_NAME_HMAC_KEY, else the key file) and looks for it in every file
git tracks at HEAD: as hex in either case, as its 32 raw bytes and as base64. A hit prints the path only (never the
key) and is exit 1. Without a key: exit 2 with --require-key (ALICE-Law's own jobs), otherwise a notice and exit 0.
0 files read is exit 2.
"""
from __future__ import annotations

import argparse
import base64
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_text_check as ptc  # noqa: E402


def forms(key: bytes) -> list[bytes]:
    h = key.hex()
    b64 = base64.b64encode(key)
    return [h.encode(), h.upper().encode(), key, b64, b64.rstrip(b"=")]


def scan(repo: Path, key: bytes) -> tuple[list[str], int]:
    names = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, check=True).stdout
    hits, read = [], 0
    needles = forms(key)
    for name in [n.decode("utf-8", "replace") for n in names.split(b"\0") if n]:
        p = repo / name
        if not p.is_file():
            continue
        data = p.read_bytes()
        read += 1
        if any(n in data for n in needles):
            hits.append(name)
    return hits, read


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--require-key", action="store_true")
    a = ap.parse_args(argv)
    try:
        key = ptc.load_name_key()
    except ValueError as e:
        print(f"name_key_absent: {e}", file=sys.stderr)
        return 2
    if key is None:
        if a.require_key:
            print(f"name_key_absent: the name key is not set ({ptc.NAME_KEY_ENV} or {ptc.NAME_KEY_FILE})",
                  file=sys.stderr)
            return 2
        print("name_key_absent: no name key here, nothing to look for (not checked)")
        return 0
    try:
        hits, read = scan(a.repo, key)
    except subprocess.CalledProcessError as e:
        print(f"name_key_absent: git ls-files failed: {e}", file=sys.stderr)
        return 2
    for h in hits:
        print(f"name_key_absent: the name key is in {h}")
    if read == 0:
        print("name_key_absent: no tracked file was read", file=sys.stderr)
        return 2
    print(f"name_key_absent: {read} tracked files read, {len(hits)} with the key")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
