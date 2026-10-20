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
  F7  the checker is a place on a scripts/run_tests.sh line, and the canary is not a test of a binary
      (tests/<name>.rs of the root or of a workspace package) that line names, or a Rust zero test is in a
      binary it does not name (a unit test of the lib counts as named when the line names `lib`, run_tests.sh's
      `Running unittests src/lib.rs` section, and the line's own command runs that test), or the line names a
      binary with no such file, asks a `name=N` floor below 1 or above the #[test] functions of that file,
      or has no `--` after its names on that line
An anchor `A>B` in a workflow resolves B only inside the step (or block) the line of A opens: a step that
does not run B never borrows the next step's.
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
import shlex
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


def step_block(text: str, at: int) -> tuple[int, int]:
    """The YAML block that the line holding offset `at` opens: that line and the lines after it that are
    indented deeper (blank lines and comments included), up to the next line at its indentation or less (the
    next step of a list, or the next key of its parent)"""
    start = text.rfind("\n", 0, at) + 1
    first = text[start:text.find("\n", start) if text.find("\n", start) >= 0 else len(text)]
    indent = len(first) - len(first.lstrip(" "))
    end = text.find("\n", start)
    while end >= 0:
        nxt = text.find("\n", end + 1)
        line = text[end + 1:nxt if nxt >= 0 else len(text)]
        body = line.lstrip(" ")
        if body and not body.startswith("#") and len(line) - len(body) <= indent:
            return start, end
        if nxt < 0:
            return start, len(text)
        end = nxt
    return start, len(text)


def anchor_at(tree: Tree, path: str, token: str) -> tuple[int | None, str | None]:
    """(offset of the place the anchor names, None), or (None, why it names no single place). A token
    `A>B` names the first B after the one A; in a workflow (.yml / .yaml) B must be inside the step or block
    the line of A opens, so a step without B never borrows the next step's"""
    if path not in tree.paths:
        return None, f"{path} is not in the tree"
    text = tree.read(path)
    if text is None:
        return None, f"{path} could not be read"
    n = text.count(token)
    if n == 1:
        return text.index(token), None
    for i, ch in enumerate(token):
        if ch == ">" and 0 < i < len(token) - 1:
            a, b = token[:i], token[i + 1:]
            if text.count(a) != 1:
                continue
            after = text.index(a) + len(a)
            end = step_block(text, text.index(a))[1] if path.endswith((".yml", ".yaml")) else len(text)
            at = text.find(b, after, end)
            if at >= 0:
                return at, None
            if path.endswith((".yml", ".yaml")) and b in text[after:]:
                return None, f"{b!r} is not in the step {a!r} opens in {path} (it occurs only after that step)"
    return None, f"{token!r} occurs {n} times in {path} (an anchor names one place)"


def anchor_gap(tree: Tree, path: str, token: str) -> str | None:
    return anchor_at(tree, path, token)[1]


# a scripts/run_tests.sh invocation: its comma separated binary names (each `name` or `name=N`), then `--`
RUN_TESTS_RE = re.compile(r"scripts/run_tests\.sh\s+(?P<names>\S+)\s+--(?:\s|$)")
RUST_TEST_ATTR_RE = re.compile(r"#\[\s*(?:\w+\s*::\s*)*test\s*\]")


def leg_findings(tree: Tree, checker: str | None, canary: str | None, zero: str | None) -> list[str]:
    """When the checker is a place on a line that runs scripts/run_tests.sh: the line's names are followed by
    `--` on that line, each `name=N` floor is at least 1 and at most the `#[test]` functions of tests/<name>.rs
    (so the floor can be met), the canary is a test of an integration binary the line names
    (tests/<name>.rs::...; a unit test or a Python test is not run by the leg, so run_tests.sh's zero test says
    nothing about it), and a zero that is an integration test is one of a named binary too (the zero may also
    be run_tests.sh's own test). Empty otherwise, or when the place cannot be read (the anchor check says so)"""
    m = ANCHOR_REF_RE.match(checker or "")
    if not m:
        return []
    at, why = anchor_at(tree, m["path"], m["token"])
    if why or at is None:
        return []
    text = tree.read(m["path"]) or ""
    end = text.find("\n", at)
    line = text[text.rfind("\n", 0, at) + 1:end if end >= 0 else len(text)]
    if "scripts/run_tests.sh" not in line or line.lstrip().startswith("#"):
        return []
    leg = RUN_TESTS_RE.search(line)
    if not leg:
        return [f"the run_tests.sh line of {checker} has no `--` after its names on that line"]
    out = []
    names = []
    for spec in leg["names"].split(","):
        name, _, floor = spec.partition("=")
        names.append(name)
        if name == "lib":
            # the library's unit tests (run_tests.sh counts cargo's `Running unittests src/lib.rs` section)
            files = sorted(p for p in tree.paths if p.endswith(".rs") and ("/" + p).find("/src/") >= 0
                           and "/tests/" not in "/" + p)
        else:
            # the integration test file of that binary: tests/<name>.rs of the root or of a workspace package
            files = sorted(p for p in tree.paths if p == f"tests/{name}.rs" or p.endswith(f"/tests/{name}.rs"))
        if not files:
            out.append(f"the run_tests.sh line of {checker} names {name}, and no tests/{name}.rs is in the tree")
            continue
        if floor:
            counts = [len(RUST_TEST_ATTR_RE.findall(rust_code(tree.read(f) or ""))) for f in files]
            count = sum(counts) if name == "lib" else max(counts)
            if not floor.isdigit() or int(floor) < 1 or int(floor) > count:
                out.append(f"the run_tests.sh line of {checker} asks {spec}: the floor must be at least 1 and at "
                           f"most the {count} #[test] functions of {' / '.join(files)}")
    shown = ", ".join(sorted(names))
    for ref, must in ((canary, True), (zero, False)):
        if not ref:
            continue  # a missing reference is F4's
        r = RS_REF_RE.match(ref or "")
        integration = bool(r) and "/tests/" in "/" + r["path"]
        if r and not integration and "lib" in names:
            # a unit test of the lib: the line names `lib` and its own command runs that test
            if not unit_test_run_by(tree, ref, line):
                out.append(f"{ref} is a unit test this run_tests.sh line names `lib` for, but the line's command "
                           f"does not run it (package, --lib or no target selection, features, filters)")
            continue
        if (must and not integration) or (integration and Path(r["path"]).stem not in names):
            out.append(f"{ref} is not a test of a binary this run_tests.sh line names ({shown})")
    return out


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


RUNNER_RE = re.compile(r"\bcargo\s+(?:\+\S+\s+)?(?:test|nextest\s+run)\b|\bscripts/(?:run_tests|cargo_test_nonzero)\.sh\b")
SELECTORS = ("--lib", "--bin", "--bins", "--test", "--tests", "--example", "--examples", "--bench", "--benches",
             "--doc", "--all-targets")


def ci_commands(tree: Tree) -> list[str]:
    """every command of the workflows and scripts/preflight.sh: comment lines and trailing ` # ...` dropped,
    `\\` continuations joined, a YAML `run:` key taken off (a feature named in a comment, or on a clippy line,
    builds nothing)"""
    out = []
    for p in sorted(tree.paths):
        if not (any(p.startswith(g + "/") and p.endswith((".yml", ".yaml")) for g in CI_GLOBS) or p in CI_FILES):
            continue
        buf = ""
        for raw in (tree.read(p) or "").splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue
            line = re.sub(r"\s#\s.*$", "", line)
            line = re.sub(r"^-?\s*run:\s*\|?", "", line).strip()
            if line.endswith("\\"):
                buf += line[:-1] + " "
                continue
            if buf + line:
                out.append(buf + line)
            buf = ""
    return out


def words(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def option_values(ws: list[str], *names: str) -> list[str]:
    vals = []
    for i, w in enumerate(ws):
        for n in names:
            if w == n and i + 1 < len(ws):
                vals.append(ws[i + 1])
            elif w.startswith(n + "="):
                vals.append(w[len(n) + 1:])
    return vals


def package_of(tree: Tree, path: str) -> tuple[str | None, dict]:
    """(package name, its manifest) of the nearest Cargo.toml with a [package] above `path`"""
    parts = path.split("/")[:-1]
    while True:
        manifest = "/".join(parts + ["Cargo.toml"])
        if manifest in tree.paths:
            try:
                data = tomllib.loads(tree.read(manifest) or "")
            except tomllib.TOMLDecodeError:
                data = {}
            if "package" in data:
                return data["package"].get("name"), data
        if not parts:
            return None, {}
        parts.pop()


# cargo test options that take a value (their value is not a test-name filter)
VALUE_OPTIONS = {"-p", "--package", "--features", "-F", "--test", "--bin", "--example", "--bench", "--target",
                 "--target-dir", "--manifest-path", "-j", "--jobs", "--profile", "--color", "--message-format",
                 "--exclude", "-Z", "--config", "--test-threads", "--format", "--logfile", "--run-ignored"}


def test_args(ws: list[str]) -> list[str]:
    """the words after `cargo test` / `cargo nextest run` / scripts/cargo_test_nonzero.sh (what cargo test reads)"""
    for i, w in enumerate(ws):
        if w == "test" and i > 0 and ws[i - 1] == "cargo":
            return ws[i + 1:]
        if w == "run" and i > 0 and ws[i - 1] == "nextest":
            return ws[i + 1:]
        if w.endswith("scripts/cargo_test_nonzero.sh"):
            return ws[i + 1:]
    for i, w in enumerate(ws):
        if w == "test" and i > 1 and ws[i - 2] == "cargo":  # cargo +toolchain test
            return ws[i + 1:]
    return []


def filters_select(args: list[str], test_path: str | None) -> bool:
    """the test-name filters of a cargo test command (free words before `--`, and after it) select `test_path`
    (`a::b::tests::fn`): with no filter, every test; a filter is a substring match, or equality under
    `--exact`; a `--skip` that matches it rejects the command. An unknown expansion (`$x`, `${{ ... }}`) is not
    read as a filter. Without a module path, a command with a filter or a skip does not count"""
    filters, skips, exact, after = [], [], False, False
    i = 0
    while i < len(args):
        w = args[i]
        if w == "--":
            after = True
        elif w == "--exact":
            exact = True
        elif w == "--skip" and i + 1 < len(args):
            skips.append(args[i + 1])
            i += 1
        elif w.startswith("--skip="):
            skips.append(w[len("--skip="):])
        elif w in VALUE_OPTIONS:
            i += 1
        elif w.startswith("-") or "$" in w:
            pass
        else:
            filters.append(w)
        i += 1
    if not filters and not skips:
        return True
    if test_path is None:
        return False
    match = (lambda f: f == test_path) if exact else (lambda f: f in test_path)
    if any(match(sk) for sk in skips):
        return False
    return not filters or any(match(f) for f in filters)


def module_path(tree: Tree, path: str, code: str, start: int) -> str | None:
    """`a::b::m::fn` for the fn at `start` in `path`: the file's module from its place under src/ (lib.rs and
    main.rs are the root, mod.rs its directory) or the root of a tests/<t>.rs target, then the inline `mod`
    blocks enclosing it; None when a `#[path]` attribute makes the file's module unknowable here"""
    if re.search(r"#\s*\[\s*path\s*=", code):
        return None
    m = re.search(r"(?:^|/)src/(?P<rel>.+)\.rs$", path)
    if m:
        parts = m["rel"].split("/")
        if parts[-1] in ("lib", "main", "mod"):
            parts = parts[:-1]
        if parts and parts[0] == "bin":
            parts = parts[2:] if len(parts) > 1 else []
    elif re.search(r"(?:^|/)tests/[\w-]+\.rs$", path):
        parts = []
    else:
        return None
    for mm in re.finditer(r"\bmod\s+(\w+)\s*\{", code[:start]):
        depth, i = 0, mm.end() - 1
        while i < start:
            depth += {"{": 1, "}": -1}.get(code[i], 0)
            if depth == 0:
                break
            i += 1
        if depth > 0:
            parts.append(mm.group(1))
    name = re.match(r"\s*fn\s+(\w+)", code[start:])
    return "::".join(parts + [name.group(1)]) if name else None


def command_runs(cmd: str, package: str | None, defaults: set[str], needs: set[str], target: str | None,
                 test_path: str | None = None) -> bool:
    """the command runs cargo's tests of `package` with every feature in `needs` turned on, building the lib's
    unit tests (`target` None) or the integration test `target`, and its test-name filters select `test_path`"""
    ws = words(cmd)
    if not RUNNER_RE.search(cmd):
        return False
    if not filters_select(test_args(ws), test_path):
        return False
    pkgs = option_values(ws, "-p", "--package")
    if pkgs and package not in pkgs and "--workspace" not in ws and "--all" not in ws:
        return False
    sel = [w for w in ws if w.split("=")[0] in SELECTORS]
    if sel and not ({"--all-targets", "--tests"} & set(sel)):
        if target is None and "--lib" not in sel:
            return False
        if target is not None and target not in option_values(ws, "--test"):
            return False
    if "--all-features" in ws:
        return True
    on = set() if "--no-default-features" in ws else set(defaults)
    for v in option_values(ws, "--features", "-F"):
        if "${{" in v:
            continue  # a matrix value this check cannot expand; not counted
        for f in re.split(r"[,\s]+", v):
            if f:
                on.add(f.split("/")[-1] if "/" not in f or f.split("/")[0] == package else "")
    return needs <= on


def rs_test_needs(tree: Tree, path: str, text: str, code: str, start: int):
    """(package, default features, features the test needs, integration target or None for the lib's unit
    tests, module path) of the test whose `fn` is at `start` in `path`"""
    package, manifest = package_of(tree, path)
    needs = set(enclosing_features(code, text, start))
    m = re.match(r"(?:.*/)?tests/(?P<t>[\w-]+)\.rs$", path)
    target = m["t"] if m else None
    if target is not None:
        for t in manifest.get("test", []):
            if t.get("name") == target:
                needs |= set(t.get("required-features", []))
    defaults = set(manifest.get("features", {}).get("default", []))
    return package, defaults, needs, target, module_path(tree, path, code, start)


def unit_test_run_by(tree: Tree, ref: str, cmd: str) -> bool:
    """`ref` (path::name) is a unit test of a package's lib that `cmd` runs (package, features, filters)"""
    r = RS_REF_RE.match(ref or "")
    if not r or "/tests/" in "/" + r["path"] or r["path"] not in tree.paths:
        return False
    text = tree.read(r["path"]) or ""
    code = rust_code(text)
    at = list(re.finditer(r"\bfn\s+" + r["name"] + r"\b", code))
    if len(at) != 1:
        return False
    package, defaults, needs, target, tpath = rs_test_needs(tree, r["path"], text, code, at[0].start())
    return target is None and command_runs(cmd, package, defaults, needs, None, tpath)


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
    package, defaults, needs, target, tpath = rs_test_needs(tree, path, text, code, at[0].start())
    if not any(command_runs(c, package, defaults, needs, target, tpath) for c in ci_commands(tree)):
        what = f"the integration test {target}" if target else "the lib's unit tests"
        feats = f" with feature(s) {sorted(needs)}" if needs else ""
        return (f"{path}: {name}: no workflow or scripts/preflight.sh command runs {what} of {package}{feats} "
                f"(a cargo test / run_tests.sh / cargo_test_nonzero.sh line selecting it, the features on that line)")
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
    if not any(re.search(r"(?<![\w./-])" + re.escape(path) + r"(?![\w./-])", c) for c in ci_commands(tree)):
        return f"{path}: no workflow or scripts/preflight.sh command runs it"
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


def git_law(rev: str, url: str = LAW_URL) -> Tree | None:
    """ALICE-Law at `rev`, fetched (depth 1) into a temporary repository; git's output is read as UTF-8 (the
    locale's code page on Windows cannot decode the files' text)"""
    tmp = tempfile.mkdtemp(prefix="g11-law-")
    run = lambda *a: subprocess.run(["git", "-C", tmp, *a], capture_output=True, text=True, encoding="utf-8")
    if run("init", "-q").returncode or run("fetch", "-q", "--depth", "1", url, rev).returncode:
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
        for finding in leg_findings(tree, chk if isinstance(chk, str) else None, r.get("canary"), r.get("zero")):
            out.append(f"F7 {where}: {finding}")
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
