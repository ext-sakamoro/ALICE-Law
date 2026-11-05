#!/usr/bin/env python3
"""Which alice-det-math functions a crate's source calls, against the set its identifier folds.

A crate that derives an identifier from the arithmetic it uses folds alice_det_math::function_pin(name) for each
function it calls, so a change to a function it does not call leaves its identifier alone. That is only sound if the
set is the real one, so the set is read from the source. For each Rust file under a package's src/ (tests, benches,
examples and #[cfg(test)] items are not the crate's arithmetic), with comments and string literals removed:

  alice_det_math::a::b(..)             a path; resolved through alice-det-math's function-paths.toml
  use alice_det_math::{a, b::c as d};  each imported item counts, a `pub use` too: a caller of the re-export depends on it
  use alice_det_math::metric;          a module import: the file's `metric::x` paths are resolved
  a type (metric::MetricWeights)       all its methods: a method call `w.norm(..)` names no path

Names the crate's Cargo.toml gives the package (`dm = { package = "alice-det-math", .. }`) count as alice_det_math.
`use alice_det_math as dm;` at the top of a file makes `dm::x` a path in that file. A form it cannot resolve is a
finding, never a skip: a glob, the same alias as a `pub use` or inside a module, a path through a macro variable
(`alice_det_math::$f`), a path naming nothing in the table (`consts::*` for now: they have no pins yet). Items that are not arithmetic (the identifier
API, [no_arithmetic]) are accepted and listed apart.

  fold_set_check.py print ROOT [--paths FILE]   the set found, the identifier items used, the findings
  fold_set_check.py check ROOT [--paths FILE]   also compares with DET_MATH_FUNCTIONS in the source, plus the
                                                `generated` list of .github/det-math-fold.toml (names only the crate's
                                                generated code calls); exit 1 on a difference or a finding
Without --paths the table is read from the alice-det-math that `cargo metadata` resolves for ROOT.
Exit 0 / 1, 2 on an input error (no Rust file, no table, nothing compared).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE = "alice-det-math"
SKIP_DIRS = {"tests", "benches", "examples", "target", "fuzz"}
IDENT = re.compile(r"r#[A-Za-z_]\w*|[A-Za-z_]\w*")
PUNCT = ("::", "=>", "->", "..=", "...", "..")
# the identifier API of alice-det-math: names, not arithmetic
IDENTIFIER_ITEMS = {"SEMANTICS_ID", "PREVIOUS_SEMANTICS_IDS", "FUNCTION_PINS", "function_pin", "check_semantics",
                    "check_semantics_against", "verify_semantics_against", "SemanticsCheck", "SemanticsMismatch",
                    "semantics"}


class InputError(Exception):
    pass


def tokens(text: str) -> list[str]:
    """identifiers, `::`, and single punctuation; comments and the contents of string and char literals are gone"""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif (m := re.compile(r'(?:b|c)?r(#*)"').match(text, i)):
            end = text.find('"' + m.group(1), m.end())
            if end < 0:
                raise InputError("an unterminated raw string")
            out.append('""')
            i = end + 1 + len(m.group(1))
        elif c == '"' or (c in "bc" and text.startswith('"', i + 1)):
            i += 1 if c == '"' else 2
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            out.append('""')
            i += 1
        elif c == "'" or (c == "b" and text.startswith("'", i + 1)):
            # a char literal, or a lifetime (no closing quote right after one character or an escape)
            s = i + (2 if c == "b" else 1)
            m = re.compile(r"\\(?:.|u\{[^}]*\}|x..)'|[^\\']'", re.S).match(text, s)
            if m:
                out.append("''")
                i = m.end()
            else:
                m = IDENT.match(text, s)
                i = m.end() if m else s
        elif (m := IDENT.match(text, i)):
            out.append(m.group(0))
            i = m.end()
        elif c.isdigit():
            m = re.compile(r"[0-9][0-9a-zA-Z_]*").match(text, i)
            i = m.end()
        else:
            for p in PUNCT:
                if text.startswith(p, i):
                    out.append(p)
                    i += len(p)
                    break
            else:
                out.append(c)
                i += 1
    return out


def without_test_items(toks: list[str]) -> list[str]:
    """the tokens with each #[cfg(test)] item removed (up to its matching brace, or its `;`)"""
    out: list[str] = []
    i, n = 0, len(toks)
    while i < n:
        if toks[i:i + 7] == ["#", "[", "cfg", "(", "test", ")", "]"]:
            i += 7
            depth = 0
            while i < n:
                t = toks[i]
                i += 1
                if t == "{":
                    depth += 1
                elif t == "}":
                    depth -= 1
                    if depth == 0:
                        break
                elif t == ";" and depth == 0:
                    break
            continue
        out.append(toks[i])
        i += 1
    return out


@dataclass
class Table:
    paths: dict[str, str]
    types: dict[str, list[str]]
    no_arithmetic: dict[str, str]


@dataclass
class Found:
    pins: set[str] = field(default_factory=set)
    identifier: set[str] = field(default_factory=set)
    findings: list[str] = field(default_factory=list)
    files: int = 0
    uses: int = 0


def resolve(path: list[str], t: Table, f: Found, where: str) -> None:
    """one path after the crate name"""
    f.uses += 1
    p = "::".join(path)
    if path[0] in IDENTIFIER_ITEMS or path[0] == "law_bits" or p in t.no_arithmetic:
        f.identifier.add(p)
    elif len(path) > 1 and "::".join(path[:-1]) in t.types:
        # a method through its type, before its own row: `MetricWeights::new()` gives a value whose
        # `w.norm(..)` names no path, so reaching the type counts all its methods
        f.pins.update(t.types["::".join(path[:-1])])
    elif p in t.types:
        f.pins.update(t.types[p])
    elif p in t.paths:
        f.pins.add(t.paths[p])
    else:
        f.findings.append(f"{where}: alice_det_math::{p} names nothing in function-paths.toml")


def path_starts_here(toks: list[str], i: int) -> bool:
    """toks[i] begins a path: nothing before it, or a leading `::` with no segment before that (`a::alice_det_math`
    is another crate's item of that name)"""
    if i == 0 or toks[i - 1] != "::":
        return True
    return i < 2 or not IDENT.fullmatch(toks[i - 2])


def scan_file(rel: str, text: str, crate_names: set[str], t: Table, f: Found) -> None:
    toks = without_test_items(tokens(text))
    f.files += 1
    crate_names = set(crate_names)
    modules: dict[str, list[str]] = {}
    n = len(toks)
    i = 0
    depth = 0

    def use_tree(i: int, prefix: list[str]) -> int:
        path = list(prefix)
        while i < n:
            tok = toks[i]
            if tok == "{":
                i += 1
                while i < n and toks[i] != "}":
                    i = use_tree(i, path)
                    if i < n and toks[i] == ",":
                        i += 1
                return i + 1
            if tok == "*":
                f.findings.append(f"{rel}: a glob import of alice_det_math::{'::'.join(path)}")
                return i + 1
            path = path + [tok]
            i += 1
            if i < n and toks[i] == "::":
                i += 1
                continue
            alias = None
            if i + 1 < n and toks[i] == "as":
                alias = toks[i + 1]
                i += 2
            if path[-1] == "self":
                path = path[:-1]
            if not path:
                f.findings.append(f"{rel}: `use alice_det_math as {alias}`: an alias of the crate")
                return i
            joined = "::".join(path)
            is_module = joined not in t.paths and joined not in t.types and joined not in t.no_arithmetic \
                and path[0] not in IDENTIFIER_ITEMS and any(k.startswith(joined + "::") for k in t.paths)
            if is_module:
                modules[alias or path[-1]] = path
            else:
                resolve(path, t, f, rel)
            return i
        return i

    while i < n:
        tok = toks[i]
        if tok == "use":
            j = i + 1
            if j < n and toks[j] == "::":
                j += 1
            if j < n and toks[j] in crate_names:
                j += 1
                if j < n and toks[j] == "::":
                    i = use_tree(j + 1, [])
                elif j + 1 < n and toks[j] == "as":
                    public = i > 0 and (toks[i - 1] == "pub" or toks[i - 1] == ")")
                    if public or depth > 0:
                        f.findings.append(f"{rel}: `use alice_det_math as {toks[j + 1]}` "
                                          f"{'public' if public else 'inside a module'}: not followed")
                    else:
                        crate_names.add(toks[j + 1])
                    i = j + 2
                else:
                    i = j
                continue
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
        if tok in crate_names and i + 1 < n and toks[i + 1] == "::" and path_starts_here(toks, i):
            j, path = i + 2, []
            while j < n:
                if toks[j] == "$":
                    f.findings.append(f"{rel}: {'::'.join(['alice_det_math', *path])}::$… through a macro variable")
                    path = []
                    break
                if toks[j] == "<":     # a turbofish or generic argument ends the path
                    break
                path.append(toks[j])
                j += 1
                if j < n and toks[j] == "::" and j + 1 < n and toks[j + 1] != "<":
                    j += 1
                    continue
                break
            if path:
                resolve(path, t, f, rel)
            i = j
            continue
        if tok in modules and i + 1 < n and toks[i + 1] == "::" and not (i > 0 and toks[i - 1] == "::"):
            j, path = i + 2, list(modules[tok])
            while j < n:
                if toks[j] == "$":
                    f.findings.append(f"{rel}: {tok}::$… through a macro variable")
                    path = []
                    break
                if toks[j] == "<":
                    break
                path.append(toks[j])
                j += 1
                if j < n and toks[j] == "::" and j + 1 < n and toks[j + 1] != "<":
                    j += 1
                    continue
                break
            if path:
                resolve(path, t, f, rel)
            i = j
            continue
        i += 1


def crate_names_in(manifest: dict) -> set[str]:
    names = {"alice_det_math"}
    tables = [manifest.get(k) for k in ("dependencies", "build-dependencies")]
    tables.append((manifest.get("workspace") or {}).get("dependencies"))
    for spec in (manifest.get("target") or {}).values():
        if isinstance(spec, dict):
            tables += [spec.get(k) for k in ("dependencies", "build-dependencies")]
    for tb in tables:
        for key, spec in (tb or {}).items():
            if isinstance(spec, dict) and spec.get("package") == PACKAGE:
                names.add(key.replace("-", "_"))
    return names


def packages(root: Path) -> list[Path]:
    return sorted(p.parent for p in root.rglob("Cargo.toml")
                  if not (SKIP_DIRS & set(p.relative_to(root).parts)) and "[package]" in p.read_text(encoding="utf-8"))


def scan(root: Path, t: Table) -> Found:
    f = Found()
    for pkg in packages(root):
        try:
            names = crate_names_in(tomllib.loads((pkg / "Cargo.toml").read_text(encoding="utf-8")))
        except tomllib.TOMLDecodeError as e:
            raise InputError(f"{pkg}/Cargo.toml: {e}") from None
        for src in sorted((pkg / "src").rglob("*.rs")) if (pkg / "src").is_dir() else []:
            rel = str(src.relative_to(root))
            try:
                scan_file(rel, src.read_text(encoding="utf-8"), names, t, f)
            except InputError as e:
                f.findings.append(f"{rel}: {e}")
    return f


def load_table(path: Path) -> Table:
    try:
        d = tomllib.loads(path.read_text(encoding="utf-8"))
        return Table(d["paths"], d["types"], d["no_arithmetic"])
    except (OSError, tomllib.TOMLDecodeError, KeyError) as e:
        raise InputError(f"function-paths.toml ({path}): {e}") from None


def resolved_table(root: Path) -> Path:
    p = subprocess.run(["cargo", "metadata", "-q", "--format-version", "1", "--manifest-path", str(root / "Cargo.toml")],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise InputError(f"cargo metadata: {p.stderr.strip()[-400:]}")
    found = {Path(k["manifest_path"]).parent for k in json.loads(p.stdout)["packages"] if k["name"] == PACKAGE}
    if len(found) != 1:
        raise InputError(f"{len(found)} {PACKAGE} in the graph, need one to read its table")
    return found.pop() / "function-paths.toml"


DECLARED_RE = re.compile(r"DET_MATH_FUNCTIONS\s*:\s*&\[&str\]\s*=\s*&\[(.*?)\]", re.S)


def declared(root: Path) -> set[str] | None:
    out: set[str] | None = None
    for pkg in packages(root):
        for src in (pkg / "src").rglob("*.rs") if (pkg / "src").is_dir() else []:
            m = DECLARED_RE.search(src.read_text(encoding="utf-8"))
            if m:
                out = (out or set()) | set(re.findall(r'"([a-z0-9_]+)"', m.group(1)))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=("print", "check"))
    ap.add_argument("root", type=Path)
    ap.add_argument("--paths", type=Path)
    a = ap.parse_args(argv)
    try:
        t = load_table(a.paths or resolved_table(a.root))
        f = scan(a.root, t)
        if f.files == 0:
            raise InputError(f"no Rust source under {a.root}")
    except InputError as e:
        print(f"fold set: {e}", file=sys.stderr)
        return 2
    print(f"fold set: {f.files} files, {f.uses} uses: {len(f.pins)} functions, {len(f.identifier)} identifier items, "
          f"{len(f.findings)} findings")
    print("fold set: functions: " + " ".join(sorted(f.pins)))
    if f.identifier:
        print("fold set: identifier items: " + " ".join(sorted(f.identifier)))
    for x in f.findings:
        print(f"fold set: not understood: {x}")
    if a.cmd == "print":
        return 0
    bad = list(f.findings)
    decl = declared(a.root)
    if decl is None:
        bad.append("no DET_MATH_FUNCTIONS in the source")
    else:
        gen_file = a.root / ".github" / "det-math-fold.toml"
        generated = set(tomllib.loads(gen_file.read_text(encoding="utf-8")).get("generated", [])) if gen_file.is_file() else set()
        want = f.pins | generated
        for n in sorted(want - decl):
            bad.append(f"{n} is called but is not in DET_MATH_FUNCTIONS")
        for n in sorted(decl - want):
            bad.append(f"{n} is in DET_MATH_FUNCTIONS but is not called")
    if f.uses == 0:
        bad.append("no use of alice_det_math found (compared nothing)")
    for b in bad:
        print(f"fold set: {b}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
