#!/usr/bin/env python3
"""Propose an anchor for each line-number reference of a crate's alice-law-conformance.toml.

A manifest names a place by an anchor `path#token` (the token occurs once in the file, or `path#A>B`: A once and B
after it), which the roster checks at the ref, never by a line number, which goes stale silently (the roster
counts line-number references against conformance/line-refs-baseline.toml). For each reference this reads the
line at the ref and proposes the longest token of it that occurs once in the file, or else a scope under the
nearest enclosing definition (`fn`, `struct`, `enum`, `trait`, `impl`, `mod`, `def`, `class`, a TOML table or a
YAML job) with a token of the line after it. Every proposal is checked with the roster's own anchor rule
(conformance_roster_check.anchor_gap) before it is printed; a reference with no path, a path not at the ref or a
line no anchor can name is reported as unresolved. It writes nothing: the lines are applied by hand (or by the
crate's own change), and the roster's anchor check then reads them.

  python3 scripts/line_refs_propose.py --repo <clone of the crate> [--ref REF]

Prints one line per reference and `line_refs_propose: N reference(s), P proposed, U unresolved`. Exit 0, or 2 when
the clone, the ref or the manifest cannot be read.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import conformance_roster_check as crc  # noqa: E402

PATH_LINE = re.compile(r"((?:[\w.\-]+/)*[\w.\-]+\.(?:rs|yml|sh|toml|py|md)):(\d+)")
PATH_ANY = re.compile(r"(?:[\w.\-]+/)*[\w.\-]+\.(?:rs|yml|yaml|sh|toml|py|md|txt|json)\b")
FIRST_NUMBER = re.compile(r"\d+")
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}(?:::[A-Za-z_][A-Za-z0-9_]*)*")
DEFINITION = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+|const\s+|unsafe\s+)*"
                        r"((?:fn|struct|enum|trait|impl|mod|def|class)\s+[A-Za-z_][\w<>:, ]*?)(?=[\s(<{:]|$)"
                        r"|^\s*(\[[\w.\-\"]+\])\s*$|^  ([\w\-]+):\s*$")


def places(value: str) -> list[tuple[str | None, int, str]]:
    """(path or None, line, the reference as written) of every line-number reference in one string"""
    out = []
    taken = []
    for m in PATH_LINE.finditer(value):
        out.append((m.group(1), int(m.group(2)), m.group(0)))
        taken.append(m.span())
    for m in crc.line_ref_matches(value):
        if any(a <= m.start() < b for a, b in taken):
            continue
        before = [p.group(0) for p in PATH_ANY.finditer(value[:m.start()])]
        path = before[-1] if before else crc.first_path(value)
        num = FIRST_NUMBER.search(m.group(0))
        out.append((path, int(num.group(0)) if num else 0, m.group(0)))
    return out


def propose(path: str | None, line: int, paths: set[str], read) -> tuple[str | None, str]:
    """(the anchor, or None) and why not"""
    if not path:
        return None, "no path names the file"
    if path not in paths:
        return None, f"{path} is not a file at the ref"
    text = read(path) or ""
    lines = text.splitlines()
    if not 1 <= line <= len(lines):
        return None, f"{path} has no line {line} ({len(lines)} lines)"
    here = lines[line - 1].strip()
    # an identifier of the line first (the longest that names one place), the whole line only after them
    candidates = sorted(set(TOKEN.findall(here)), key=lambda t: (-len(t), t)) + ([here] if here else [])
    for c in candidates:
        if crc.anchor_gap(path, c, paths, read) is None and ">" not in c:
            return f"{path}#{c}", ""
    for k in range(line - 1, -1, -1):
        d = DEFINITION.match(lines[k])
        if not d:
            continue
        scope = (d.group(1) or d.group(2) or (d.group(3) + ":")).strip()
        if text.count(scope) != 1:
            continue
        for c in candidates:
            if ">" not in c and crc.anchor_gap(path, f"{scope}>{c}", paths, read) is None:
                return f"{path}#{scope}>{c}", ""
        break
    return None, f"no token of {path} line {line} names one place, and no enclosing definition scopes it"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--ref", default="HEAD")
    a = ap.parse_args(argv)
    try:
        text, paths, *rest = crc.git_fetcher(a.repo, a.ref)()
        if text is None:
            raise crc.InputError(f"{a.repo} at {a.ref}: no {crc.MANIFEST}")
        data = tomllib.loads(text)
    except (crc.InputError, tomllib.TOMLDecodeError) as e:
        print(f"line_refs_propose: {e}", file=sys.stderr)
        return 2
    read = rest[0] if rest else None
    total = proposed = 0
    for gid in sorted((k for k in data if re.fullmatch(r"G\d+", k)), key=lambda g: int(g[1:])):
        for field in sorted(data[gid]):
            for _, value in crc.strings_in(data[gid][field], field):
                for path, line, written in places(value):
                    total += 1
                    anchor, why = propose(path, line, paths, read)
                    several = len(FIRST_NUMBER.findall(written)) > 1 and ":" not in written
                    if anchor:
                        proposed += 1
                        print(f"{gid} {field}: `{written}` -> `{anchor}`"
                              + (" (names the first line; a range or list needs one anchor per place)" if several else ""))
                    else:
                        print(f"{gid} {field}: `{written}`: unresolved ({why})")
    print(f"line_refs_propose: {total} reference(s), {proposed} proposed, {total - proposed} unresolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
