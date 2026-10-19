#!/usr/bin/env python3
"""Propose an anchor for each line-number reference of a crate's alice-law-conformance.toml.

A manifest names a place by an anchor `path#token` (the token occurs once in the file, or `path#A>B`: A once and B
after it), which the roster checks at the ref, never by a line number, which goes stale silently (the roster
counts line-number references against conformance/line-refs-baseline.toml). For each reference this reads the
line at the ref and proposes, in this order: the definition the line is (`fn`, `struct`, ..., a TOML table, a
YAML job or step, a preflight step, a Markdown heading); a scope under the definition that encloses the line with
a token of the line; or the whole line when it occurs once. A token that merely occurs once is not proposed (it
names a place, not the construct the manifest means).

Each proposal must pass two checks before it is printed: the roster's own anchor rule
(conformance_roster_check.anchor_gap), and the oracle that the anchor resolves to the line the reference names (or
to the definition that encloses it, when the manifest's text names that construct rather than the line) and that
a word of the manifest's text just before the reference occurs in the line or that definition (a line number that has
drifted to another construct is reported, not anchored). A range (`lines 23 to 74`) is not proposed: it names a
span, which an anchor of one place cannot. Everything else is reported as unresolved, for editing by hand.

  python3 scripts/line_refs_propose.py --repo <clone of the crate> [--ref REF]

Prints one line per reference and `line_refs_propose: N reference(s), P proposed (A anchors compared with their lines),
U unresolved`. Exit 0; 1 when references exist and no proposal could be compared (nothing the oracle checked);
2 when the clone, the ref or the manifest cannot be read.
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

EXT = r"\.[A-Za-z][A-Za-z0-9]{0,7}"
PATH_LINE = re.compile(r"((?:[\w.\-]+/)*[\w.\-]+" + EXT + r"):(\d+)")
PATH_ANY = re.compile(r"(?:[\w.\-]+/)*[\w.\-]+" + EXT + r"\b")
FIRST_NUMBER = re.compile(r"\d+")
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}(?:::[A-Za-z_][A-Za-z0-9_]*)*")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")
# the definitions that scope a token, each giving the text (unique in the file) that names the scope; `True`: it
# encloses every line after it (a heading, a table), `False`: only the lines indented deeper than it
DEFINITIONS = (
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+|const\s+|unsafe\s+)*"
                r"((?:fn|struct|enum|trait|impl|mod|def|class)\s+[A-Za-z_][\w<>:, ]*?)(?=[\s(<{:]|$)"), False),
    (re.compile(r"^\s*(\[[\w.\-\"]+\])\s*$"), True),         # a TOML table
    (re.compile(r"^  ([\w\-]+:)\s*$"), False),                 # a YAML job
    (re.compile(r"^\s*-\s+(name:\s*.+?)\s*$"), False),         # a YAML step
    (re.compile(r"^(step\s+\".+\")\s*$"), True),               # a preflight step
    (HEADING := re.compile(r"^(#{1,6}\s+.+?)\s*$"), True),    # a Markdown heading
)
RANGE = re.compile(r"\d+\s*(?:to|-|–)\s*L?\d+")
# words of the manifest's text that say nothing about the construct
STOP = {"line", "lines", "with", "that", "this", "from", "each", "every", "the", "and", "runs", "same", "sets",
        "where", "which", "when", "then", "there", "their", "them", "they", "also", "only", "under", "over", "into",
        "after", "before", "here", "what", "file", "files", "step", "steps", "test", "tests", "check", "checks"}


def places(value: str, paths: set[str] | None = None) -> list[tuple[str | None, list[int], str, bool, str]]:
    """(path or None, the lines, the reference as written, whether it is a range, the text just before it) of
    every line-number reference in one string"""
    out = []
    taken = []

    def context(start: int) -> str:
        head = value[:start]
        cut = max(head.rfind(";"), head.rfind(". "), 0)
        return head[cut:][-80:]
    for m in PATH_LINE.finditer(value):
        out.append((m.group(1), [int(m.group(2))], m.group(0), False, context(m.start())))
        taken.append(m.span())
    for m in crc.line_ref_matches(value):
        if any(a <= m.start() < b for a, b in taken):
            continue
        before = [p.group(0) for p in PATH_ANY.finditer(value[:m.start()])]
        known = [b for b in before if paths is None or resolve_path(b, paths)[0]]
        path = known[-1] if known else (before[-1] if before else crc.first_path(value))
        nums = [int(n) for n in FIRST_NUMBER.findall(m.group(0))]
        ranged = bool(RANGE.search(m.group(0)))
        out.append((path, nums[:1] if ranged else nums, m.group(0), ranged, context(m.start())))
    return out


def resolve_path(path: str | None, paths: set[str]) -> tuple[str | None, str]:
    """the repository path a reference means: as written, or a bare file name that one path of the tree ends with"""
    if not path:
        return None, "no path names the file"
    if path in paths:
        return path, ""
    ends = sorted(p for p in paths if p.endswith("/" + path))
    if len(ends) == 1:
        return ends[0], ""
    if ends:
        return None, f"{path} names {len(ends)} files of the tree ({', '.join(ends[:3])}...)"
    return None, f"{path} is not a file at the ref"


def anchor_line(text: str, token: str) -> int | None:
    """the line an anchor token names in `text`, by the roster's rule (the whole token first, then A>B)"""
    if text.count(token) == 1:
        return text.count("\n", 0, text.index(token)) + 1
    for i, ch in enumerate(token):
        if ch == ">" and 0 < i < len(token) - 1:
            a, b = token[:i], token[i + 1:]
            if text.count(a) == 1:
                at = text.find(b, text.index(a) + len(a))
                if at >= 0:
                    return text.count("\n", 0, at) + 1
    return None


def names(text: str, token: str, line: int, path: str) -> bool:
    """the oracle: the anchor resolves to `line`, or to the definition that encloses `line`"""
    at = anchor_line(text, token)
    if at == line:
        return True
    scope = enclosing(text.splitlines(), line, path) if 1 <= line <= len(text.splitlines()) else None
    return at is not None and scope is not None and scope[0] == at and scope[1] == token


def words_of(text: str) -> set[str]:
    """the lower-case words of a line, whole identifiers and their `_` parts (sdf_sphere: sdf_sphere, sdf, sphere)"""
    return {w.lower() for w in re.findall(r"[A-Za-z0-9_]+", text)} | {w.lower() for w in re.findall(r"[A-Za-z0-9]+", text)}


def indent(s: str) -> int:
    return len(s) - len(s.lstrip())


def enclosing(lines: list[str], line: int, path: str = "") -> tuple[int, str] | None:
    """(the line, the scope text) of the nearest definition that is or encloses `line` (a `#` line is a heading
    only in Markdown; elsewhere it is a comment)"""
    for k in range(line - 1, -1, -1):
        for d, everything_after in DEFINITIONS:
            if d is HEADING and not path.endswith(".md"):
                continue
            m = d.match(lines[k])
            if m and (k == line - 1 or everything_after or indent(lines[line - 1]) > indent(lines[k])):
                return k + 1, m.group(1).strip()
    return None


def propose(path: str | None, line: int, paths: set[str], read, context: str = "") -> tuple[str | None, str]:
    """(the anchor, or None) and why not; an anchor is returned only when the oracle holds: it resolves to `line`
    and a word of `context` occurs in the construct (the definition's line and `line`)"""
    path, why = resolve_path(path, paths)
    if path is None:
        return None, why
    text = read(path) or ""
    lines = text.splitlines()
    if not 1 <= line <= len(lines):
        return None, f"{path} has no line {line} ({len(lines)} lines)"
    here = lines[line - 1].strip()
    if not here:
        return None, f"{path} line {line} is empty (the reference has drifted)"
    scope = enclosing(lines, line, path)
    words = {w.lower() for w in WORD.findall(context)} - STOP - {p.lower() for p in re.split(r"[/.]", path)}
    if not words:
        return None, f"no word just before the reference to compare with {path} line {line}"
    in_line = words & words_of(here)
    in_scope = words & words_of(scope[1]) if scope else set()
    if not in_line and not in_scope:
        return None, (f"{path} line {line} ({here[:60]!r}) shares no word with the text before the reference "
                      f"({', '.join(sorted(words)[:6])}): the line may have drifted")
    tokens = sorted(set(TOKEN.findall(here)), key=lambda t: (-len(t), t))
    candidates = []
    if scope and scope[0] == line:
        candidates.append(scope[1])
    elif in_line:
        if scope and text.count(scope[1]) == 1:
            candidates += [f"{scope[1]}>{c}" for c in tokens + [here]]
        if not here.startswith(("#", "//")):  # a comment names no construct
            candidates.append(here)
    else:
        # the text means the construct the line sits in, not the line: name the construct
        candidates.append(scope[1])
    for c in candidates:
        if crc.anchor_gap(path, c, paths, read) is None and names(text, c, line, path):
            return f"{path}#{c}", ""
    return None, f"no definition, scope or whole line of {path} line {line} names it once"


def verify(anchor: str, line: int, paths: set[str], read) -> bool:
    """the oracle, read again from the printed anchor: it passes the roster's rule and resolves to `line`"""
    path, token = anchor.split("#", 1)
    return crc.anchor_gap(path, token, paths, read) is None and names(read(path) or "", token, line, path)


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
    total = proposed = compared = 0
    for gid in sorted((k for k in data if re.fullmatch(r"G\d+", k)), key=lambda g: int(g[1:])):
        for field in sorted(data[gid]):
            for _, value in crc.strings_in(data[gid][field], field):
                for path, lines, written, ranged, context in places(value, paths):
                    total += 1
                    if ranged:
                        print(f"{gid} {field}: `{written}`: unresolved (a range names a span: anchor each place "
                              f"it means by hand)")
                        continue
                    got = [propose(path, line, paths, read, context) for line in lines]
                    anchors = [anchor for anchor, _ in got if anchor]
                    if anchors and len(anchors) == len(got):
                        proposed += 1
                        compared += sum(1 for a_, ln in zip(anchors, lines) if verify(a_, ln, paths, read))
                        print(f"{gid} {field}: `{written}` -> " + ", ".join(f"`{x}`" for x in anchors))
                    else:
                        whys = "; ".join(f"line {line}: {why}" for line, (anchor, why) in zip(lines, got) if not anchor)
                        print(f"{gid} {field}: `{written}`: unresolved ({whys or 'no line number'})")
    print(f"line_refs_propose: {total} reference(s), {proposed} proposed ({compared} anchors compared with their lines), "
          f"{total - proposed} unresolved")
    if total and not compared:
        print("line_refs_propose: no proposal was compared with its line (nothing the oracle checked)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
