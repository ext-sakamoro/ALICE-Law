#!/usr/bin/env python3
"""G15 surfaces proposal: write the declarations and the README surface table a repository's code shows.

surface_claims_check.py finds, per crate, the surfaces the code has; a repository that has not adopted G15 yet has
no `[package.metadata.alice] surfaces` and no `<!-- surfaces -->` table, which is most of its baseline row
(undeclared + readme). This script proposes both from the same detection (surface_claims_check.detect), so the
proposal and the gate cannot drift. It only proposes: the owner reads the change against the code before
committing it, and CI and the baseline gate stay as they are.

What is declared: only a surface shown by a code construct or a CI step —
  rust (src/lib.rs), browser (#[wasm_bindgen]), c (extern "C" with no_mangle), python (PyO3 attributes),
  wasi (a push-triggered wasm32-wasip1 build of the crate), unity (a C# DllImport naming the crate's library).
unreal is found by a name (a Build.cs that mentions the library, or a header under a directory named ue4 / ue5 /
unreal), so it is never declared here: it is listed for review and the owner declares it by hand after reading
the binding. A crate with no surface at all (a fuzz or tool crate without src/lib.rs) is declared `surfaces = []`.
A crate that already declares is left as it is.

Read the diff before writing it. A declaration written from detection agrees with the check by construction, so for
that crate G15 stops being an independent signal: a surface the crate exposes by accident (an extern "C" item left
public, a stray PyO3 attribute) is declared as if it were meant. The diff is for the crate's owner to read, crate by
crate, against what the crate is meant to offer, removing what it should not expose, and only then `--write`. Do not
run `--write` across repositories in bulk.

The README table (README.md, and README_JP.md when it exists) lists the union of the declarations, one row per
surface, with the crates and the files that show it. An existing table between the markers is replaced; otherwise
a section is inserted before the license heading, or appended.

    surface_claims_propose.py --repo DIR            print the change as a unified diff, and the review list
    surface_claims_propose.py --repo DIR --write    write it

Exit 0 on success (also when nothing needs to change), 2 when the repository cannot be read or has no crate.
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import surface_claims_check as scc  # noqa: E402

# surfaces a code construct or a CI step shows; the rest (unreal) is found by a name and only reviewed
STRUCTURAL = ("rust", "browser", "wasi", "c", "python", "unity")
REVIEW_ONLY = ("unreal",)
assert set(STRUCTURAL) | set(REVIEW_ONLY) == set(scc.VOCABULARY)

HEAD = {
    "README.md": ("## Surfaces",
                  "The surfaces the crates declare (`[package.metadata.alice] surfaces` in each `Cargo.toml`):",
                  "| Surface | Crate | Shown by |"),
    "README_JP.md": ("## 出口",
                     "crate が宣言する出口 (各 `Cargo.toml` の `[package.metadata.alice] surfaces`):",
                     "| 出口 | crate | 根拠 |"),
}
LICENSE = {"README.md": re.compile(r"^##\s+License\b", re.M | re.I),
           "README_JP.md": re.compile(r"^##\s+(?:ライセンス|License)\b", re.M | re.I)}
TABLE_RX = re.compile(r"<!--\s*surfaces\s*-->.*?<!--\s*/surfaces\s*-->", re.S)


def order(surfaces) -> list[str]:
    have = set(surfaces)
    return [s for s in scc.VOCABULARY if s in have]


def propose(repo: Path) -> dict:
    """{"crates": [{manifest, name, declared (existing or None), declare (proposed or None), review: {s: files},
    evidence: {s: files}}], "union": [...]} from the repository's tracked files"""
    files = scc.tracked(repo)
    cs = scc.crates(repo, files)
    if not cs:
        raise scc.InputError("no crate manifest (Cargo.toml with [package]) is tracked")
    workflows = {f: scc.read(repo, f) for f in files if f.startswith(".github/workflows/") and f.endswith((".yml", ".yaml"))}
    dirs = [c["dir"] for c in cs]
    wasi = scc.wasi_dirs(cs, workflows)
    out = []
    union: set[str] = set()
    for c in cs:
        found = scc.detect(repo, files, c, dirs, workflows, wasi)
        review = {s: f for s, f in found.items() if s in REVIEW_ONLY}
        if c["declared"] is not None:
            declare = None
            union |= set(c["declared"]) & set(scc.VOCABULARY)
        else:
            declare = order(s for s in found if s in STRUCTURAL)
            union |= set(declare)
        out.append({"manifest": c["manifest"], "name": c["name"], "declared": c["declared"], "declare": declare,
                    "review": review, "evidence": {s: f for s, f in found.items() if s not in REVIEW_ONLY}})
    return {"crates": out, "union": order(union)}


def cargo_with(text: str, surfaces: list[str]) -> str:
    """the manifest with `surfaces = [...]` under [package.metadata.alice] (the table is added when missing)"""
    line = "surfaces = [" + ", ".join(f'"{s}"' for s in surfaces) + "]"
    m = re.search(r"^\[package\.metadata\.alice\][ \t]*$", text, re.M)
    if m:
        new = text[:m.end()] + "\n" + line + text[m.end():]
    else:
        new = text.rstrip("\n") + "\n\n[package.metadata.alice]\n" + line + "\n"
    got = (((tomllib.loads(new).get("package") or {}).get("metadata") or {}).get("alice") or {}).get("surfaces")
    if got != surfaces:
        raise scc.InputError(f"the manifest does not read back the proposed surfaces ({got!r})")
    return new


def table_rows(p: dict, name: str) -> list[str]:
    rows = []
    for s in p["union"]:
        crates = [c for c in p["crates"] if s in (c["declare"] if c["declare"] is not None else c["declared"] or [])]
        who = ", ".join(f"`{c['name']}`" for c in crates)
        shown = sorted({f for c in crates for f in c["evidence"].get(s, [])})
        where = ", ".join(f"`{f}`" for f in shown[:3]) + (" …" if len(shown) > 3 else "")
        if not where:
            where = "declared" if name == "README.md" else "宣言のみ"
        rows.append(f"| {s} | {who} | {where} |")
    return rows


def readme_with(text: str, name: str, p: dict) -> str:
    heading, intro, header = HEAD[name]
    block = "\n".join(["<!-- surfaces -->", header, "|---|---|---|", *table_rows(p, name), "<!-- /surfaces -->"])
    if TABLE_RX.search(text):
        return TABLE_RX.sub(lambda _m: block, text, count=1)
    section = f"{heading}\n\n{intro}\n\n{block}\n\n"
    m = LICENSE[name].search(text)
    if m:
        return text[:m.start()] + section + text[m.start():]
    return text.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n"


def changes(repo: Path, p: dict) -> dict[str, tuple[str, str]]:
    """{relative path: (old text, new text)} for every file the proposal changes"""
    out: dict[str, tuple[str, str]] = {}
    for c in p["crates"]:
        if c["declare"] is None:
            continue
        old = scc.read(repo, c["manifest"])
        out[c["manifest"]] = (old, cargo_with(old, c["declare"]))
    files = set(scc.tracked(repo))
    for name in HEAD:
        if name not in files:
            continue
        old = scc.read(repo, name)
        new = readme_with(old, name, p)
        if new != old:
            out[name] = (old, new)
    return out


def report(p: dict) -> list[str]:
    lines = []
    for c in p["crates"]:
        if c["declare"] is None:
            lines.append(f"{c['manifest']}: already declares {c['declared']}, left as it is")
        else:
            lines.append(f"{c['manifest']}: surfaces = {c['declare']}")
        for s, f in sorted(c["review"].items()):
            lines.append(f"  review (found by a name, not declared): {s} in {', '.join(f[:3])}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    repo = Path(a.repo)
    try:
        p = propose(repo)
        ch = changes(repo, p)
    except (scc.InputError, OSError, tomllib.TOMLDecodeError) as e:
        print(f"surface_claims_propose: {e}", file=sys.stderr)
        return 2
    for line in report(p):
        print(line)
    if a.write:
        for rel, (_old, new) in ch.items():
            (repo / rel).write_text(new, encoding="utf-8")
        print(f"surface_claims_propose: wrote {len(ch)} file(s)")
    else:
        for rel, (old, new) in ch.items():
            sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}"))
        print(f"surface_claims_propose: {len(ch)} file(s) would change (--write to write them)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
