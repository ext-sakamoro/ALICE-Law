#!/usr/bin/env python3
"""Check that README.md and README_JP.md say the same things where a translation must not differ.

The prose is translated by hand; what a translation must keep is compared mechanically:

- the headings: each file's headings (level, text, order) must be the pairs listed in
  `scripts/readme_headings.toml`, so a section moved, dropped, added or renamed in one file only
  is found;
- in each section, the number of paragraphs (runs of non-blank lines) and of list items;
- every fenced code block, character for character, in order;
- the inline code spans, the numbers (versions, decimals and numbers of two or more digits), the hex hashes and the `alice-*` / `ALICE-*` names, as
  multisets (a translation may reorder words, not drop or change a value);
- the link targets other than in-page anchors (the language links between the two files aside);
- every in-page anchor link resolves to a heading of its own file (GitHub's heading ids);
- each file links to the other at the top.

What it does not compare: the meaning of the prose. A sentence reworded, a negation added, a
single digit or a number written as a word ("two", "2 つ") changed inside a paragraph that keeps
its place passes; those are for the person translating and the reviewer.

    readme_parity_check.py [--en README.md] [--ja README_JP.md] [--headings scripts/readme_headings.toml]

Exit 0 when they agree, 1 on a difference (each class named, with the values on one side
only), 2 when a file cannot be read or nothing was compared.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FENCE_RE = re.compile(r"^```[^\n]*\n.*?^```[ \t]*$", re.M | re.S)
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
# a version or a decimal (`0.5.1`), or a number of two or more digits (`14`, `2000`); a single
# digit is left out, since a translation writes a count as a word in one language and a digit
# with a counter in the other ("two bodies", "2 つの物体")
NUMBER_RE = re.compile(r"(?<![0-9A-Za-z.])(?:\d+(?:\.\d+)+|\d{2,})(?![0-9])")
HASH_RE = re.compile(r"(?<![0-9a-zA-Z])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,64}(?![0-9a-zA-Z])")
NAME_RE = re.compile(r"(?<![\w-])(?:alice|ALICE)-[A-Za-z0-9][A-Za-z0-9_-]*")
LANG_LINKS = {"en": "README_JP.md", "ja": "README.md"}


def slug(heading: str) -> str:
    """GitHub's id for a heading: lower case, markup and punctuation removed, spaces to `-`"""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text).lower()
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in " -_" or cat[0] in "LN" or cat == "Mn":
            out.append("-" if ch == " " else ch)
    return "".join(out)


LIST_ITEM_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\S", re.M)


def sections(prose: str) -> list[tuple[int, int]]:
    """(paragraphs, list items) of each section: the text before the first heading, then the text
    under each heading up to the next one"""
    parts = re.split(r"^#{1,6}[ \t]+.*$", prose, flags=re.M)
    out = []
    for part in parts:
        paras = [b for b in re.split(r"\n[ \t]*\n", part) if b.strip()]
        out.append((len(paras), len(LIST_ITEM_RE.findall(part))))
    return out


def facts(text: str) -> dict:
    fences = FENCE_RE.findall(text)
    prose = FENCE_RE.sub("\n```\n", text)
    headings = HEADING_RE.findall(prose)
    ids: Counter = Counter()
    anchors = set()
    for _, h in headings:
        base = slug(h)
        anchors.add(base if ids[base] == 0 else f"{base}-{ids[base]}")
        ids[base] += 1
    codes = INLINE_CODE_RE.findall(prose)
    plain = INLINE_CODE_RE.sub(" ", prose)
    links = LINK_RE.findall(plain)
    return {
        "headings": [(len(level), h) for level, h in headings],
        "sections": sections(prose),
        "fences": fences,
        "inline code": Counter(codes),
        "numbers": Counter(NUMBER_RE.findall(plain)),
        "hashes": Counter(HASH_RE.findall(prose)),
        "names": Counter(NAME_RE.findall(plain)),
        "links": Counter(t for t in links if not t.startswith("#")),
        "anchor links": [t[1:] for t in links if t.startswith("#")],
        "anchors": anchors,
    }


def compare(en_text: str, ja_text: str, pairs: list[dict]) -> tuple[list[str], int]:
    en, ja = facts(en_text), facts(ja_text)
    problems: list[str] = []
    compared = 0
    for lang, f in (("en", en), ("ja", ja)):
        other = LANG_LINKS[lang]
        top = (en_text if lang == "en" else ja_text).split("\n## ", 1)[0]
        if f"]({other})" not in top:
            problems.append(f"{'README.md' if lang == 'en' else 'README_JP.md'}: no link to {other} before the "
                            f"first section")
        f["links"][other] -= 1
        f["links"] += Counter()
        for a in f["anchor links"]:
            compared += 1
            if a not in f["anchors"]:
                problems.append(f"{'README.md' if lang == 'en' else 'README_JP.md'}: the link #{a} names no heading")
    compared += len(en["headings"]) + len(en["fences"])
    want = [((p["level"], p["en"]), (p["level"], p["ja"])) for p in pairs]
    if len(en["headings"]) != len(want) or len(ja["headings"]) != len(want):
        problems.append(f"headings: README.md has {len(en['headings'])}, README_JP.md {len(ja['headings'])}, "
                        f"readme_headings.toml lists {len(want)}")
    for i, ((we, wj), ge, gj) in enumerate(zip(want, en["headings"], ja["headings"]), 1):
        if ge != we or gj != wj:
            problems.append(f"headings: heading {i} is {ge} / {gj}, readme_headings.toml pairs {we} / {wj}")
    if len(en["sections"]) == len(ja["sections"]):
        for i, (a, b) in enumerate(zip(en["sections"], ja["sections"])):
            if a != b:
                name = "the text before the first heading" if i == 0 else f"section {i} ({en['headings'][i - 1][1]})" \
                    if i - 1 < len(en["headings"]) else f"section {i}"
                problems.append(f"sections: {name} has {a[0]} paragraphs and {a[1]} list items in README.md, "
                                f"{b[0]} and {b[1]} in README_JP.md")
    if len(en["fences"]) != len(ja["fences"]):
        problems.append(f"code blocks: en has {len(en['fences'])}, ja {len(ja['fences'])}")
    for i, (a, b) in enumerate(zip(en["fences"], ja["fences"]), 1):
        if a != b:
            problems.append(f"code blocks: block {i} differs")
    for key in ("inline code", "numbers", "hashes", "names", "links"):
        compared += sum(en[key].values())
        only_en = en[key] - ja[key]
        only_ja = ja[key] - en[key]
        if only_en or only_ja:
            problems.append(f"{key}: only in README.md {sorted(only_en.elements())[:10]}, "
                            f"only in README_JP.md {sorted(only_ja.elements())[:10]}")
    return problems, compared


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--en", default=str(ROOT / "README.md"))
    ap.add_argument("--ja", default=str(ROOT / "README_JP.md"))
    ap.add_argument("--headings", default=str(ROOT / "scripts" / "readme_headings.toml"))
    a = ap.parse_args(argv)
    try:
        en_text = Path(a.en).read_text(encoding="utf-8")
        ja_text = Path(a.ja).read_text(encoding="utf-8")
        pairs = tomllib.loads(Path(a.headings).read_text(encoding="utf-8")).get("heading", [])
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not pairs or not all(isinstance(p, dict) and {"level", "en", "ja"} <= set(p) for p in pairs):
        print("error: readme_headings.toml lists no heading pair (level, en, ja)", file=sys.stderr)
        return 2
    problems, compared = compare(en_text, ja_text, pairs)
    if compared == 0:
        print("error: nothing was compared (no heading, code block, value or link)", file=sys.stderr)
        return 2
    for p in problems:
        print(f"found: {p}")
    print(f"readme parity: {compared} items compared, {len(problems)} differences")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
