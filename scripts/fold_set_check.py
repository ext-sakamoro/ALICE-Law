#!/usr/bin/env python3
"""Which alice-det-math functions a crate's source calls, against the set its identifier folds.

A crate that derives an identifier from the arithmetic it uses folds alice_det_math::function_pin(name) for each
function it calls, so a change to a function it does not call leaves its identifier alone. That is only sound if the
set is the real one, so the set is read from the source. For each Rust file under a package's src/ (tests, benches, examples and #[cfg(test)] items are not the crate's
arithmetic), with comments and string literals removed:

  alice_det_math::a::b(..), ::alice_det_math::.., crate::|self::|super::alice_det_math::..   a path, resolved through
                                       alice-det-math's function-paths.toml
  use alice_det_math::{a, b::c as d};  each imported item counts, a `pub use` too: a caller of the re-export depends on it
  use alice_det_math::metric;          a module import counts every function of the module (the names reach other files)
  a type (metric::MetricWeights)       all its methods: a method call `w.norm(..)` names no path

Names the crate's Cargo.toml gives the package (`dm = { package = "alice-det-math", .. }`, `dm = { workspace = true }`
through the workspace root) count as alice_det_math. `use alice_det_math as dm;` at the top of a file makes `dm::x` a
path in that file, wherever in the file it stands.

A call is never missed in silence, except a name built by macro expansion (only a resolver sees those): `[< >]` in
paste! is reported, but a macro of another crate that builds the name is not visible here. Everything else is a
finding, never a skip: any token naming the crate or such an alias that does not start a resolved
path (a macro taking the crate as an identifier, `extern crate`, `use {alice_det_math as dm}`, `pub use alice_det_math`),
an alias reached from elsewhere (`super::dm`) or declared public or inside a module, a glob, a path through a macro
variable, a name not in the table (`consts::*` for now: they have no pins yet), a #[path] that is missing, outside the
repository, not a plain string or inside an inline module (one at the top of a file is read like the rest, with the
plain `mod`s below it), a string naming the crate inside an attribute other than doc, include!, a lib or bin outside
src/, and a build script that uses the crate. #[cfg(test)] removes an item, or a macro invocation in its place; on anything else (a field,
an arm, a statement) what follows stays in the read: a larger set than the real one, never a call missed.
Items that are not arithmetic (the identifier API, [no_arithmetic]) are accepted and listed apart.

  fold_set_check.py print ROOT [--paths FILE]   the set found, the identifier items used, the findings
  fold_set_check.py check ROOT [--paths FILE]   also compares with DET_MATH_FUNCTIONS in the source, plus the
                                                `generated` list of .github/det-math-fold.toml (names only the crate's
                                                generated code calls); exit 1 on a difference or a finding
  fold_set_check.py write ROOT [--paths FILE]   writes that set into the crate's one DET_MATH_FUNCTIONS, sorted and as
                                                rustfmt lays it out, and changes nothing else; exit 1 on a finding (nothing
                                                written); exit 2 when there is no declaration or more than one (add
                                                `pub const DET_MATH_FUNCTIONS: &[&str] = &[];` where the crate folds it), or
                                                the file holds anything rustfmt would change
Without --paths the table is read from the alice-det-math that `cargo metadata` resolves for ROOT.
Exit 0 / 1, 2 on an input error (no Rust file, no table, nothing compared).
"""
from __future__ import annotations

import argparse
import glob
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


class Literal(str):
    """a string literal's token: equal to `""`, with the contents kept for #[path]"""
    value = ""


def literal(text: str) -> Literal:
    t = Literal('""')
    t.value = text
    return t


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
            out.append(literal(text[m.end():end]))
            i = end + 1 + len(m.group(1))
        elif c == '"' or (c in "bc" and text.startswith('"', i + 1)):
            i += 1 if c == '"' else 2
            start = i
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            out.append(literal(text[start:i]))
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
            out.append(m.group(0).removeprefix("r#"))
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


ITEM_KEYWORDS = {"mod", "fn", "use", "struct", "enum", "union", "impl", "const", "static", "trait", "type", "extern",
                 "unsafe", "async", "macro_rules"}
CFG_TEST = ["#", "[", "cfg", "(", "test", ")", "]"]


def without_test_items(toks: list[str]) -> list[str]:
    """the tokens with each #[cfg(test)] item removed, up to its matching brace or its `;` outside any bracket; a macro
    invocation in that place (thread_local! { .. }) is an item too. On anything else the attribute removes nothing:
    where a field, an arm or a statement ends need not be told, what follows is simply read"""
    out: list[str] = []
    i, n = 0, len(toks)
    while i < n:
        if toks[i:i + 7] != CFG_TEST:
            out.append(toks[i])
            i += 1
            continue
        j = i + 7
        while j < n and toks[j] == "#":    # other attributes of the same item
            depth, j = 0, j + 1
            while j < n:
                depth += {"[": 1, "]": -1}.get(toks[j], 0)
                j += 1
                if depth == 0:
                    break
        k = j
        if k < n and toks[k] == "pub":     # `pub a: u8` is a field: an item keyword must follow the visibility
            k += 1
            if k < n and toks[k] == "(":
                while k < n and toks[k] != ")":
                    k += 1
                k += 1
        is_macro = k + 1 < n and toks[k + 1] == "!" and IDENT.fullmatch(toks[k]) is not None
        if k >= n or not (toks[k] in ITEM_KEYWORDS or is_macro):
            out.extend(toks[i:i + 7])
            i += 7
            continue
        depth = 0
        while j < n:
            t = toks[j]
            j += 1
            if t in "{[(":
                depth += 1
            elif t in "}])":
                depth -= 1
                if depth == 0 and t == "}":
                    break
            elif t == ";" and depth == 0:
                break
        i = j
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


RELATIVE = {"crate", "self", "super"}


def path_prefix_ok(toks: list[str], i: int) -> bool:
    """what stands before toks[i] lets it start a path: nothing, a leading `::`, or crate:: / self:: / super::"""
    if i == 0 or toks[i - 1] != "::":
        return True
    return i < 2 or not IDENT.fullmatch(toks[i - 2]) or toks[i - 2] in RELATIVE


def module_pins(path: list[str], t: Table) -> set[str] | None:
    """every pin under the module `path`, None when it is no module of the table"""
    prefix = "::".join(path) + "::"
    pins = {pin for k, pin in t.paths.items() if k.startswith(prefix)}
    return pins or None


def file_aliases(toks: list[str], crate_names: set[str], rel: str, f: Found) -> set[str]:
    """the first pass over a file: its `use alice_det_math as dm;` aliases, wherever in the file they stand"""
    out: set[str] = set()
    depth = 0
    for i, tok in enumerate(toks):
        depth += {"{": 1, "}": -1}.get(tok, 0)
        if tok != "use":
            continue
        j = i + 1 + (toks[i + 1:i + 2] == ["::"])
        if j + 2 < len(toks) and toks[j] in crate_names and toks[j + 1] == "as":
            public = i > 0 and toks[i - 1] in ("pub", ")")
            if public or depth > 0:
                f.findings.append(f"{rel}: `use alice_det_math as {toks[j + 2]}` "
                                  f"{'public' if public else 'inside a module'}: not followed")
            else:
                out.add(toks[j + 2])
    return out


def attribute_strings(toks: list[str], names: set[str], rel: str, f: Found) -> None:
    """a string inside an attribute (serde's `with = ".."`) can name a path the compiler then resolves; doc is prose"""
    for x in range(len(toks) - 1):
        if toks[x] != "#":
            continue
        y = x + 1 + (toks[x + 1] == "!")
        if y >= len(toks) or toks[y] != "[" or toks[y + 1:y + 2] == ["doc"]:
            continue
        depth, z = 0, y
        while z < len(toks):
            depth += {"[": 1, "]": -1}.get(toks[z], 0)
            lit = toks[z]
            if isinstance(lit, Literal) and any(n in lit.value for n in names):
                f.findings.append(f"{rel}: an attribute's string names the crate: {lit.value[:40]}")
            z += 1
            if depth == 0:
                break


def paste_forms(toks: list[str], rel: str, f: Found) -> None:
    """`[<a b>]` builds an identifier by concatenation (paste!): what it names cannot be read from the tokens. Told from
    an array of a qualified type (`[<T as A>::N; 2]`) by the `>` right before the closing bracket"""
    for x in range(len(toks) - 1):
        if toks[x:x + 2] != ["[", "<"]:
            continue
        depth, z = 0, x
        while z < len(toks):
            depth += {"[": 1, "]": -1}.get(toks[z], 0)
            if depth == 0:
                if toks[z - 1] == ">":
                    f.findings.append(f"{rel}: `[< >]` builds a name by macro expansion, which only a resolver can read")
                break
            z += 1


def plain_mods(toks: list[str]) -> list[str]:
    """the `mod name;` at the top of a file with no #[path] of their own"""
    out = []
    depth = 0
    for i, tok in enumerate(toks):
        depth += {"{": 1, "}": -1}.get(tok, 0)
        if depth or tok != "mod" or toks[i + 2:i + 3] != [";"]:
            continue
        j = i - 1
        if j >= 0 and toks[j] == ")":      # pub(crate)
            while j >= 0 and toks[j] != "(":
                j -= 1
            j -= 1
        if j >= 0 and toks[j] == "pub":
            j -= 1
        has_path = False
        while j >= 0 and toks[j] == "]":   # the attributes before it
            d = 0
            while j >= 0:
                d += {"]": 1, "[": -1}.get(toks[j], 0)
                j -= 1
                if d == 0:
                    break
            has_path = has_path or toks[j + 2:j + 3] == ["path"]
            j -= 1                         # the `#`
        if not has_path:
            out.append(toks[i + 1])
    return out


def scan_file(rel: str, text: str, crate_names: set[str], aliases: set[str], t: Table, f: Found) -> None:
    """the second pass; `aliases` are this file's, and the other files' are refused when reached by a path"""
    toks = without_test_items(tokens(text))
    f.files += 1
    names = crate_names | file_aliases(toks, crate_names, rel, f)
    n = len(toks)
    i = 0

    attribute_strings(toks, names, rel, f)
    paste_forms(toks, rel, f)
    for x in range(n):
        if toks[x:x + 2] == ["include", "!"]:
            f.findings.append(f"{rel}: include! brings in source that is not read")

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
                f.findings.append(f"{rel}: `use alice_det_math::{{self as {alias}}}`: an alias of the crate")
                return i
            joined = "::".join(path)
            pins = None
            if joined not in t.paths and joined not in t.types and joined not in t.no_arithmetic \
                    and path[0] not in IDENTIFIER_ITEMS and path[0] != "law_bits":
                pins = module_pins(path, t)
            if pins is not None:
                f.uses += 1
                f.pins.update(pins)
            else:
                resolve(path, t, f, rel)
            return i
        return i

    while i < n:
        tok = toks[i]
        if tok == "use" and i + 1 < n:
            j = i + 1 + (toks[i + 1] == "::")
            if j < n and toks[j] in names:
                if j + 1 < n and toks[j + 1] == "::":
                    i = use_tree(j + 2, [])
                    continue
                if j + 1 < n and toks[j + 1] == "as":
                    i = j + 3      # the alias, already taken (or refused) by the first pass
                    continue
        if tok in aliases - names and i > 0 and toks[i - 1] == "::":
            f.findings.append(f"{rel}: {'::'.join(toks[max(i - 2, 0):i + 1])}: an alias of alice_det_math reached by a path")
        if tok in names:
            if i + 1 < n and toks[i + 1] == "::" and path_prefix_ok(toks, i):
                j, path = i + 2, []
                while j < n:
                    if toks[j] == "$":
                        f.findings.append(f"{rel}: {'::'.join(['alice_det_math', *path])}::$… through a macro variable")
                        path = []
                        break
                    if toks[j] in ("<", "*", "{"):
                        if toks[j] != "<":
                            j = use_tree(j, path)
                            path = []
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
            f.findings.append(f"{rel}: `{tok}` here does not start a path this can resolve")
        i += 1


def crate_names_in(manifest: dict, workspace: dict | None = None) -> set[str]:
    """the names `manifest` gives alice-det-math; `dm = { workspace = true }` is looked up in the workspace root's table"""
    names = {"alice_det_math"}
    shared = (workspace or {}).get("dependencies") or {}
    kinds = ("dependencies", "build-dependencies", "dev-dependencies")
    tables = [manifest.get(k) for k in kinds]
    tables.append((manifest.get("workspace") or {}).get("dependencies"))
    for spec in (manifest.get("target") or {}).values():
        if isinstance(spec, dict):
            tables += [spec.get(k) for k in kinds]
    for tb in tables:
        for key, spec in (tb or {}).items():
            if isinstance(spec, dict) and spec.get("workspace") is True:
                spec = shared.get(key, spec)
            if isinstance(spec, dict) and spec.get("package") == PACKAGE:
                names.add(key.replace("-", "_"))
    return names


def has_det_math(table: dict | None, names: set[str]) -> bool:
    for key, spec in (table or {}).items():
        if key.replace("-", "_") in names or (isinstance(spec, dict) and spec.get("package") == PACKAGE):
            return True
    return False


def packages(root: Path) -> list[Path]:
    """the packages under root, outside the directories that hold fixtures, and every workspace member wherever it is
    (a member under a directory named examples/ is part of the build)"""
    found = {p.parent for p in root.rglob("Cargo.toml")
             if not (SKIP_DIRS & set(p.relative_to(root).parts)) and "[package]" in p.read_text(encoding="utf-8")}
    manifest = root / "Cargo.toml"
    if manifest.is_file():
        members = (read_toml(manifest).get("workspace") or {}).get("members") or []
        for pattern in members:
            for match in glob.glob(str(root / pattern)):    # (Path.glob refuses `.`, which workspaces use for the root)
                d = Path(match)
                if (d / "Cargo.toml").is_file() and "[package]" in (d / "Cargo.toml").read_text(encoding="utf-8"):
                    found.add(d)
    return sorted(found)


def read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise InputError(f"{path}: {e}") from None


def path_targets(toks: list[str], src: Path, root: Path, rel: str, f: Found) -> list[Path]:
    """the files this file's #[path = ".."] attributes bring in, when they are files of the repository; anything
    else about them is a finding (what they bring in could not be read)"""
    out: list[Path] = []
    top = root.resolve()
    depth_at = 0
    for x in range(len(toks)):
        depth_at += {"{": 1, "}": -1}.get(toks[x], 0)
        if toks[x:x + 2] == ["#", "["] and toks[x + 2:x + 3] == ["cfg_attr"]:
            end = x + 2
            depth = 1
            while end < len(toks) and depth:
                end += 1
                depth += {"[": 1, "]": -1}.get(toks[end] if end < len(toks) else "", 0)
            if "path" in toks[x + 3:end]:
                f.findings.append(f"{rel}: a #[cfg_attr(.., path = ..)]: which file it names depends on the build")
        if toks[x:x + 4] != ["#", "[", "path", "="]:
            continue
        lit = toks[x + 4] if x + 4 < len(toks) else ""
        if depth_at > 0:   # inside an inline module the base directory is another one: not guessed
            f.findings.append(f"{rel}: a #[path] inside a block or an inline module")
            continue
        if not isinstance(lit, Literal):
            f.findings.append(f"{rel}: a #[path] that is not a plain string")
            continue
        target = (src.parent / lit.value).resolve()
        if not target.is_relative_to(top):
            f.findings.append(f"{rel}: #[path = \"{lit.value}\"] is outside the repository")
        elif not target.is_file():
            f.findings.append(f"{rel}: #[path = \"{lit.value}\"] names no file")
        else:
            out.append(target)
    return out


def scan(root: Path, t: Table) -> Found:
    f = Found()
    root_manifest = root / "Cargo.toml"
    workspace = (read_toml(root_manifest).get("workspace") or {}) if root_manifest.is_file() else {}
    for pkg in packages(root):
        manifest = read_toml(pkg / "Cargo.toml")
        names = crate_names_in(manifest, workspace)
        rel_pkg = pkg.relative_to(root)
        targets = [manifest.get("lib") or {}, *(manifest.get("bin") or [])]
        for target in targets:
            p = target.get("path")
            if p and "src" not in Path(p).parts:
                f.findings.append(f"{rel_pkg / 'Cargo.toml'}: a target at {p}, outside src/, is not read")
        build = pkg / str((manifest.get("package") or {}).get("build") or "build.rs")
        if has_det_math(manifest.get("build-dependencies"), names) or (
                build.is_file() and any(n in tokens(build.read_text(encoding="utf-8")) for n in names)):
            f.findings.append(f"{rel_pkg / build.name}: a build script using alice-det-math is not read")
        pending = sorted((pkg / "src").rglob("*.rs")) if (pkg / "src").is_dir() else []
        texts: dict[str, str] = {}
        seen: set[Path] = set()
        every: set[str] = set()
        while pending:     # each file once: two files naming each other through #[path] end here
            src = pending.pop(0)
            key = src.resolve()
            if key in seen:
                continue
            seen.add(key)
            rel = str(key.relative_to(root.resolve()))
            texts[rel] = src.read_text(encoding="utf-8")
            try:
                toks = without_test_items(tokens(texts[rel]))
            except InputError:
                continue   # reported when the file is scanned
            every |= file_aliases(toks, names, rel, Found())
            pending += path_targets(toks, key, root, rel, f)
            if not key.is_relative_to((pkg / "src").resolve()):
                # a file brought in by #[path]: what its plain `mod`s load is not under src/ either
                for name in plain_mods(toks):
                    found = [c for c in (key.parent / f"{name}.rs", key.parent / name / "mod.rs",
                                         key.parent / key.stem / f"{name}.rs") if c.is_file()]
                    if found:
                        pending += found
                    else:
                        f.findings.append(f"{rel}: `mod {name};` below a #[path] file: no file found for it")
        for rel, text in texts.items():
            try:
                scan_file(rel, text, names, every, t, f)
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


RUSTFMT = "rustfmt"


def declarations(root: Path) -> list[tuple[Path, str, re.Match]]:
    """each DET_MATH_FUNCTIONS of the source (file, its text, the match), a commented-out line not being one"""
    out = []
    for pkg in packages(root):
        for src in sorted((pkg / "src").rglob("*.rs")) if (pkg / "src").is_dir() else []:
            text = src.read_text(encoding="utf-8")
            for m in DECLARED_RE.finditer(text):
                if "//" not in text[text.rfind("\n", 0, m.start()) + 1:m.start()]:
                    out.append((src, text, m))
    return out


def declared(root: Path) -> set[str] | None:
    found = declarations(root)
    if not found:
        return None
    return {n for _, _, m in found for n in re.findall(r'"([a-z0-9_:]+)"', m.group(1))}


def wanted(root: Path, f: Found) -> set[str]:
    gen_file = root / ".github" / "det-math-fold.toml"
    return f.pins | (set(tomllib.loads(gen_file.read_text(encoding="utf-8")).get("generated", [])) if gen_file.is_file() else set())


def rustfmt(text: str, where: Path) -> subprocess.CompletedProcess:
    # from stdin rustfmt does not follow `mod`s; run in the file's directory so it finds the crate's rustfmt.toml
    try:
        return subprocess.run([RUSTFMT, "--edition", "2021"], input=text, capture_output=True, text=True,
                              encoding="utf-8", cwd=where.parent)
    except OSError as e:
        raise InputError(f"{RUSTFMT}: {e}") from None


def write(root: Path, want: set[str]) -> str:
    """writes `want` into the one declaration; what happened, as a line"""
    found = declarations(root)
    if not found:
        raise InputError("no DET_MATH_FUNCTIONS in the source: add `pub const DET_MATH_FUNCTIONS: &[&str] = &[];` "
                         "where the crate folds it")
    if len(found) > 1:
        raise InputError(f"{len(found)} declarations of DET_MATH_FUNCTIONS (write needs one): "
                         + ", ".join(str(x[0]) for x in found))
    src, text, m = found[0]
    new = text[:m.start(1)] + ", ".join(f'"{n}"' for n in sorted(want)) + text[m.end(1):]
    now = rustfmt(text, src)
    if now.returncode != 0 or now.stdout != text:
        raise InputError(f"{src}: rustfmt would change this file: format it first (a write changes the declaration only)")
    p = rustfmt(new, src)
    if p.returncode != 0:
        raise InputError(f"{src}: rustfmt: {p.stderr.strip()[:200]}")
    if p.stdout == text:
        return f"{src}: unchanged ({len(want)} functions)"
    src.write_text(p.stdout, encoding="utf-8")
    return f"{src}: written ({len(want)} functions)"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=("print", "check", "write"))
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
    if a.cmd == "write":
        if f.findings:
            print("fold set: nothing written: resolve what is not understood first")
            return 1
        if f.uses == 0:
            print("fold set: no use of alice_det_math found (compared nothing): nothing written", file=sys.stderr)
            return 2
        try:
            print(f"fold set: {write(a.root, wanted(a.root, f))}")
        except InputError as e:
            print(f"fold set: {e}", file=sys.stderr)
            return 2
        return 0
    bad = list(f.findings)
    decl = declared(a.root)
    if decl is None:
        bad.append("no DET_MATH_FUNCTIONS in the source")
    else:
        want = wanted(a.root, f)
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
