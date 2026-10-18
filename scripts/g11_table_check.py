#!/usr/bin/env python3
"""ALICE-Law G11 table check: every gate a crate declares `pass` names, per checker, the canary the checker
must flag, the test that fails when it compared nothing, and where the checker is shared, the rows of the
shared-files table that keep its copies identical.

A crate keeps the table in conformance/g11.toml, one row per checker:

    [[row]]
    gate = "G4"
    checker = ".github/workflows/ci.yml#run --test geometry_id_oracle"
    canary = "alice-lol/tests/geometry_id_oracle.rs::a_wrong_digest_is_refused"
    zero = "scripts/test_run_tests.py::RunTests.test_no_test_ran_fails"
    shared = ["scripts/run_tests.sh", "scripts/test_run_tests.py"]

A reference is one of
  <path>.rs::<fn>                a test that runs: `#[test]`, not `#[ignore]`, not under `#[cfg(any())]`, under
                                 tests/ or after a `#[cfg(test)]`, and when it sits under `#[cfg(feature = "x")]`,
                                 a CI workflow or scripts/preflight.sh that builds feature x (or all features)
  <path>.py::<test>              a `def test...` that occurs once in the file
  <path>.py::<Class>.<method>    a `def test...` method of that class
  <path>#<token>                 a place: the token occurs exactly once in the file (or A>B: A once, then B
                                 after it); any file, workflows and scripts/preflight.sh included
  law@<pin>:<one of the above>   the same, in ALICE-Law at the revision `.github/<pin>` names (the Law checker
                                 a crate fetches at a pin is tested in ALICE-Law, not in the crate)
`checker` is a path or a <path>#<token>. `shared` is a list of paths, or "n/a: <reason>".

Findings (each fails the run, exit 1):
  F1  a gate the manifest marks pass (other than G11) has no row
  F2  a row's gate is not pass in the manifest
  F3  the checker path or its anchor is not in the tree
  F4  a canary or zero reference does not resolve as above
  F5  a row's canary and zero are the same reference (they test different things)
  F6  a shared path is not a row of ALICE-Law's conformance/shared-files.toml (at the revision
      .github/shared-files-pin names) listing this crate, or shared is "n/a" while that table lists the
      checker's path for this crate
It exits 2 when it compared nothing (no row, or no pass gate) or cannot read what it needs.

The table does not make G11 pass by itself: it is the prerequisite the conformance roster reads.

    python3 scripts/g11_table_check.py --repo DIR --name ALICE-<crate>
        [--law REV=DIR ...]   read ALICE-Law at REV from DIR instead of fetching it (the tests)
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

LAW_URL = "https://github.com/ext-sakamoro/ALICE-Law"
MANIFEST = "alice-law-conformance.toml"
TABLE = "conformance/g11.toml"
SHARED_PIN = ".github/shared-files-pin"
CI_GLOBS = (".github/workflows",)
CI_FILES = ("scripts/preflight.sh",)

RUST_NON_CODE_RE = re.compile(r'//[^\n]*|/\*.*?\*/|(?<![\w])b?r(#*)".*?"\1|"(?:\\.|[^"\\])*"'
                              r"|(?<![\w])b?'(?:\\.|[^'\\\n])'", re.S)
RS_REF_RE = re.compile(r"^(?P<path>[\w./\-]+\.rs)::(?P<name>[A-Za-z_][A-Za-z0-9_]*)$")
PY_REF_RE = re.compile(r"^(?P<path>[\w./\-]+\.py)::(?:(?P<cls>[A-Za-z_]\w*)\.)?(?P<name>[A-Za-z_]\w*)$")
ANCHOR_REF_RE = re.compile(r"^(?P<path>[^#\s]+)#(?P<token>.+)$", re.S)
LAW_REF_RE = re.compile(r"^law@(?P<pin>[\w.\-]+):(?P<ref>.+)$", re.S)
FEATURE_RE = re.compile(r'feature\s*=\s*"([^"]+)"')


class Tree:
    """files of one tree, read by path from the root"""

    def __init__(self, read, paths: set[str]):
        self.read, self.paths = read, paths

    @staticmethod
    def of_dir(root: Path) -> "Tree":
        paths = set()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "target")]
            for f in filenames:
                paths.add(Path(dirpath, f).relative_to(root).as_posix())

        def read(p: str) -> str | None:
            try:
                return (root / p).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None
        return Tree(read, paths)


def rust_code(text: str) -> str:
    """Rust source with comments and string literals blanked to spaces (newlines and offsets kept, so a
    position in it is the same position in the source)"""
    return RUST_NON_CODE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def anchor_gap(tree: Tree, path: str, token: str) -> str | None:
    if path not in tree.paths:
        return f"{path} is not in the tree"
    text = tree.read(path)
    if text is None:
        return f"{path} could not be read"
    n = text.count(token)
    if n == 1:
        return None
    for i, ch in enumerate(token):
        if ch == ">" and 0 < i < len(token) - 1:
            a, b = token[:i], token[i + 1:]
            if text.count(a) == 1 and b in text[text.index(a) + len(a):]:
                return None
    return f"{token!r} occurs {n} times in {path} (an anchor names one place)"


def attributes_above(code: str, start: int) -> str:
    """the attribute lines directly above position `start` (a #[...] may span lines)"""
    lines = code[:start].split("\n")
    attrs: list[str] = []
    depth = 0
    for line in reversed(lines[:-1] if lines and not lines[-1].strip() else lines):
        t = line.strip()
        if depth == 0 and not t:
            break
        if depth == 0 and not (t.startswith("#[") or t.startswith("pub") or t.endswith("]") or t.endswith(",")
                               or t.startswith("async")):
            break
        attrs.append(t)
        depth += t.count("]") - t.count("[")
    return " ".join(attrs)


def enclosing_features(code: str, text: str, start: int) -> set[str]:
    """the features a `#[cfg(feature = ...)]` requires on the fn at `start`, on a module enclosing it, or
    for the whole file (`#![cfg(...)]`); a feature under `not(...)` is not required"""
    def positive(block: str) -> set[str]:
        out = set()
        for m in re.finditer(r"#!?\[\s*cfg\s*\((.*?)\)\s*\]", block, re.S):
            body = re.sub(r"not\s*\([^()]*\)", "", m.group(1))
            out |= set(FEATURE_RE.findall(body))
        return out
    # the attributes are located in the blanked code and read from the source (the feature name is a string)
    feats = positive(attributes_above(text, start))
    feats |= positive(" ".join(text[m.start():m.end()] for m in re.finditer(r"#!\[\s*cfg\s*\(.*?\)\s*\]", code, re.S)))
    for m in re.finditer(r"\bmod\s+\w+\s*\{", code[:start]):
        depth, i = 0, m.end() - 1
        while i < start:
            depth += {"{": 1, "}": -1}.get(code[i], 0)
            if depth == 0:
                break
            i += 1
        if depth > 0:
            feats |= positive(attributes_above(text, m.start()))
    return feats


def ci_text(tree: Tree) -> str:
    parts = []
    for p in sorted(tree.paths):
        if any(p.startswith(g + "/") and p.endswith((".yml", ".yaml")) for g in CI_GLOBS) or p in CI_FILES:
            parts.append(tree.read(p) or "")
    return "\n".join(parts)


def feature_built(ci: str, feature: str) -> bool:
    if "--all-features" in ci:
        return True
    for line in ci.splitlines():
        if re.search(r"--features[\s=]|features:", line) and re.search(r"(?<![\w-])" + re.escape(feature) +
                                                                        r"(?![\w-])", line):
            return True
    return False


def rs_test_gap(tree: Tree, path: str, name: str) -> str | None:
    if path not in tree.paths:
        return f"{path} is not in the tree"
    text = tree.read(path)
    if text is None:
        return f"{path} could not be read"
    code = rust_code(text)
    at = list(re.finditer(r"\bfn\s+" + name + r"\b", code))
    if len(at) != 1:
        return f"{path}: {len(at)} fn named {name}"
    block = attributes_above(code, at[0].start())
    if not re.search(r"#\[\s*test\s*\]", block):
        return f"{path}: {name} has no #[test]"
    if re.search(r"#\[\s*ignore\b", block):
        return f"{path}: {name} is #[ignore]d"
    if re.search(r"#\[\s*cfg\s*\(\s*any\s*\(\s*\)\s*\)\s*\]", block):
        return f"{path}: {name} is under #[cfg(any())], never built"
    if not re.search(r"(^|/)tests/", path) and not re.search(r"#\[\s*cfg\s*\(\s*test\s*\)\s*\]",
                                                              code[:at[0].start()]):
        return f"{path}: {name} is outside tests/ and no #[cfg(test)] precedes it"
    ci = ci_text(tree)
    for f in sorted(enclosing_features(code, text, at[0].start())):
        if not feature_built(ci, f):
            return (f"{path}: {name} needs feature {f!r} and no workflow or scripts/preflight.sh builds it "
                    f"(--features {f} or --all-features)")
    return None


def py_test_gap(tree: Tree, path: str, cls: str | None, name: str) -> str | None:
    if path not in tree.paths:
        return f"{path} is not in the tree"
    text = tree.read(path)
    if text is None:
        return f"{path} could not be read"
    if not name.startswith("test"):
        return f"{path}: {name} is not a test (unittest and pytest collect test* only)"
    try:
        mod = ast.parse(text)
    except SyntaxError as e:
        return f"{path}: does not parse ({e.msg})"
    found = []
    for node in mod.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name and cls is None:
            found.append(name)
        if isinstance(node, ast.ClassDef) and (cls is None or node.name == cls):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                    found.append(f"{node.name}.{name}")
    if not found:
        return f"{path}: no {cls + '.' if cls else ''}{name}"
    if len(found) > 1:
        return f"{path}: {name} is ambiguous ({', '.join(found)}); write <Class>.<method>"
    return None


def ref_gap(tree: Tree, ref: str, law) -> str | None:
    """None when the reference resolves; otherwise why not"""
    if not isinstance(ref, str) or not ref.strip():
        return "empty reference"
    m = LAW_REF_RE.match(ref)
    if m:
        lt, why = law(m["pin"])
        if why:
            return why
        return ref_gap(lt, m["ref"], lambda pin: (None, f"law@{pin}: nested law reference"))
    m = RS_REF_RE.match(ref)
    if m:
        return rs_test_gap(tree, m["path"], m["name"])
    m = PY_REF_RE.match(ref)
    if m:
        return py_test_gap(tree, m["path"], m["cls"], m["name"])
    m = ANCHOR_REF_RE.match(ref)
    if m:
        return anchor_gap(tree, m["path"], m["token"])
    return f"{ref!r} is not <path>.rs::<fn>, <path>.py::[<Class>.]<test>, <path>#<token> or law@<pin>:<ref>"


def pin_rev(tree: Tree, pin: str) -> tuple[str | None, str | None]:
    path = f".github/{pin}"
    text = tree.read(path) if path in tree.paths else None
    if text is None:
        return None, f"{path} is not in the tree"
    revs = [line.split()[1] for line in text.splitlines() if len(line.split()) == 2 and line.split()[0] == "rev"]
    if len(revs) != 1 or not re.fullmatch(r"[0-9a-f]{40}", revs[0]):
        return None, f"{path}: needs one 'rev <40 hex>'"
    return revs[0], None


def git_law(rev: str) -> Tree | None:
    """ALICE-Law at `rev`, fetched (depth 1) into a temporary repository"""
    tmp = tempfile.mkdtemp(prefix="g11-law-")
    run = lambda *a: subprocess.run(["git", "-C", tmp, *a], capture_output=True, text=True)
    if run("init", "-q").returncode or run("fetch", "-q", "--depth", "1", LAW_URL, rev).returncode:
        return None
    ls = run("ls-tree", "-r", "--name-only", "FETCH_HEAD")
    if ls.returncode:
        return None

    def read(p: str) -> str | None:
        r = run("show", f"FETCH_HEAD:{p}")
        return r.stdout if r.returncode == 0 else None
    return Tree(read, set(ls.stdout.split()))


def check(tree: Tree, name: str, law_at) -> tuple[list[str], int, int]:
    """(findings, rows, pass gates); raises ValueError when it cannot read what it needs"""
    try:
        manifest = tomllib.loads(tree.read(MANIFEST) or "")
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{MANIFEST}: {e}")
    if TABLE not in tree.paths:
        raise ValueError(f"{TABLE} is not in the tree")
    try:
        rows = tomllib.loads(tree.read(TABLE) or "").get("row", [])
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{TABLE}: {e}")
    passing = sorted(g for g, v in manifest.items()
                     if isinstance(v, dict) and v.get("status") == "pass" and g != "G11")

    cache: dict[str, tuple] = {}

    def law(pin: str):
        if pin not in cache:
            rev, why = pin_rev(tree, pin)
            if why:
                cache[pin] = (None, f"law@{pin}: {why}")
            else:
                lt = law_at(rev)
                cache[pin] = (lt, None) if lt is not None else (None, f"law@{pin}: ALICE-Law {rev} could not be read")
        return cache[pin]

    shared_rows: list[dict] | None = None
    shared_why = None
    lt, why = law(SHARED_PIN.removeprefix(".github/"))
    if why:
        shared_why = why
    else:
        try:
            shared_rows = tomllib.loads(lt.read("conformance/shared-files.toml") or "").get("file", [])
        except tomllib.TOMLDecodeError as e:
            shared_why = f"conformance/shared-files.toml: {e}"
    mine = {f.get("path") for f in (shared_rows or []) if name in f.get("repos", [])}

    out = []
    covered = set()
    for i, r in enumerate(rows, 1):
        g = r.get("gate")
        where = f"{TABLE} row {i} ({g})"
        covered.add(g)
        if g not in passing:
            out.append(f"F2 {where}: {g} is not pass in {MANIFEST}")
        chk = r.get("checker")
        if not isinstance(chk, str) or not chk:
            out.append(f"F3 {where}: no checker")
            chk_path = None
        else:
            m = ANCHOR_REF_RE.match(chk)
            chk_path = m["path"] if m else chk
            gap = anchor_gap(tree, m["path"], m["token"]) if m else (
                None if chk in tree.paths else f"{chk} is not in the tree")
            if gap:
                out.append(f"F3 {where}: checker {gap}")
        for field in ("canary", "zero"):
            gap = ref_gap(tree, r.get(field), law)
            if gap:
                out.append(f"F4 {where}: {field} {gap}")
        if r.get("canary") and r.get("canary") == r.get("zero"):
            out.append(f"F5 {where}: canary and zero are the same reference")
        sh = r.get("shared")
        if isinstance(sh, str) and sh.startswith("n/a:") and sh[4:].strip():
            if shared_rows is not None and chk_path in mine:
                out.append(f"F6 {where}: shared is n/a but the shared-files table lists {chk_path} for {name}")
        elif isinstance(sh, list) and sh and all(isinstance(x, str) for x in sh):
            if shared_rows is None:
                out.append(f"F6 {where}: {shared_why}")
            for p in sh if shared_rows is not None else []:
                if p not in mine:
                    out.append(f"F6 {where}: {p} is not a shared-files row listing {name}")
        else:
            out.append(f"F6 {where}: shared must be a non-empty list of paths or \"n/a: <reason>\"")
    for g in passing:
        if g not in covered:
            out.append(f"F1 {g} is pass in {MANIFEST} and has no row in {TABLE}")
    return out, len(rows), len(passing)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--name", required=True)
    ap.add_argument("--law", action="append", default=[], metavar="REV=DIR")
    a = ap.parse_args(argv)
    local = {}
    for spec in a.law:
        rev, _, d = spec.partition("=")
        local[rev] = Tree.of_dir(Path(d))
    try:
        found, rows, gates = check(Tree.of_dir(a.repo), a.name, lambda rev: local.get(rev) or git_law(rev))
    except ValueError as e:
        print(f"g11_table: {e}", file=sys.stderr)
        return 2
    for f in found:
        print(f"error: {f}", file=sys.stderr)
    print(f"g11_table: {a.name}: {rows} rows, {gates} pass gates, {len(found)} findings")
    if rows == 0 or gates == 0:
        print("g11_table: compared nothing (no row or no pass gate)", file=sys.stderr)
        return 2
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
