#!/usr/bin/env python3
"""Every file under conformance/ is read by a checker that runs on a ci/ branch.

A conformance file changes on a ci/ branch before it reaches main; a checker that only runs on main reads it for
the first time after the landing. For each tracked file under conformance/ (the planned/ bytes count as one file,
`conformance/planned/*`), this finds the reader scripts that name it (scripts/*.py and scripts/*.sh, the tests
excepted) and requires one of them to be run by a workflow whose `on.push.branches` includes `ci/**`.

usage: conformance_reader_map.py [--root DIR]

Fails on an unmapped file, on no conformance file read, and on no ci/** workflow read (nothing compared is a
failure).
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

PLANNED = "conformance/planned/*"
RUN_SCRIPT = re.compile(r"scripts/([A-Za-z0-9_.-]+\.(?:py|sh))")


def tracked(root: Path, prefix: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "--", prefix], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout.split()
    return sorted(out)


def conformance_files(root: Path) -> list[str]:
    files = []
    for f in tracked(root, "conformance"):
        if f.startswith("conformance/planned/"):
            if PLANNED not in files:
                files.append(PLANNED)
        else:
            files.append(f)
    return files


def on_ci_branches(text: str) -> bool:
    """`ci/**` in the `branches` of the `on.push` block (the text between `on:` and `jobs:`)"""
    m = re.search(r"(?ms)^on:\s*$(.*?)^jobs:", text)
    if not m:
        return False
    block = m.group(1)
    push = re.search(r"(?ms)^  push:\s*$(.*?)(?=^  [a-z_]+:\s*$|\Z)", block)
    return bool(push and re.search(r"""branches:.*["']ci/\*\*["']""", push.group(1)))


RUN_KEY = re.compile(r"^(\s*)(?:- )?run:\s*(.*)$")


def run_texts(text: str) -> list[str]:
    """the text of every `run:` step of a workflow: a single-line value, or a block scalar (`|` / `>`) and the
    lines indented under it (a script named elsewhere, as in `paths:`, is not run)"""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = RUN_KEY.match(lines[i])
        i += 1
        if not m:
            continue
        indent = len(m.group(1)) + (2 if lines[i - 1].lstrip().startswith("- ") else 0)
        value = m.group(2).strip()
        if value and value[0] not in "|>":
            out.append(value)
            continue
        block = []
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
            block.append(lines[i])
            i += 1
        out.append("\n".join(block))
    return out


def without_shell_comments(text: str) -> str:
    """`text` with its shell comments taken out: a line whose first non-blank character is `#`, and from a `#` that
    starts a word (at the line start or after a blank) to the end of the line, outside single and double quotes.
    Limits: a quote is open until the same quote closes it on that line (a backslash-escaped quote, and a string that
    spans lines, are not followed), and a here-document's body is read like any other line."""
    out = []
    for line in text.splitlines():
        quote = ""
        cut = len(line)
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def ci_runs(root: Path) -> tuple[list[tuple[str, str]], int]:
    """(workflow file name, a run step's text without its shell comments) for every step of a ci/** workflow, and
    how many ci/** workflows there are"""
    runs: list[tuple[str, str]] = []
    n = 0
    for wf in sorted((root / ".github" / "workflows").glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        if on_ci_branches(text):
            n += 1
            runs += [(wf.name, without_shell_comments(r)) for r in run_texts(text)]
    return runs, n


def ci_scripts(root: Path) -> tuple[set[str], int, list[tuple[str, str]]]:
    """the scripts a step of a ci/** workflow runs, how many ci/** workflows there are, and the run steps"""
    runs, n = ci_runs(root)
    scripts: set[str] = set()
    for _, run in runs:
        scripts |= set(RUN_SCRIPT.findall(run))
    return scripts, n, runs


def token(file: str) -> re.Pattern[str]:
    """`file`'s repository-relative path as one token of a string (no path character before or after it)"""
    return re.compile(r"(?<![\w./-])" + re.escape(file) + r"(?![\w.-])")


def python_strings(source: str) -> list[str]:
    """the strings a Python script uses as values: its string constants (f-string parts included) except the
    docstrings, and each `/` chain of them joined with `/` (`ROOT / "conformance" / "a.toml"` gives
    "conformance/a.toml"); comments are not part of the syntax tree. A script that does not parse uses nothing."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return strings_in(tree)


def strings_in(tree: ast.AST) -> list[str]:
    """[`python_strings`] of a syntax tree (a whole module, or one assignment's value)"""
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docs.add(id(first.value))
    out = [n.value for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]

    def chain(n: ast.AST) -> list[ast.AST]:
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            return chain(n.left) + chain(n.right)
        return [n]
    for n in ast.walk(tree):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            run: list[str] = []
            for part in chain(n) + [None]:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    run.append(part.value)
                else:
                    if len(run) > 1:
                        out.append("/".join(run))
                    run = []
    return out


def planned_dir(source: str) -> bool:
    """the checker's PLANNED_DIR = "planned" (the planned bytes it reads next to the table)"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PLANNED_DIR" for t in n.targets)
               and isinstance(n.value, ast.Constant) and n.value.value == "planned" for n in ast.walk(tree))


def path_names(module: Path, want: str) -> set[str]:
    """the module-level names a scripts/ module binds to a value that uses the path `want`
    (`HOLDER_BASELINE = … / "conformance" / "holder-baseline.toml"`)"""
    if not module.is_file():
        return set()
    try:
        tree = ast.parse(module.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    found = set()
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(token(want).search(v) for v in strings_in(n.value)):
                found |= {t.id for t in targets if isinstance(t, ast.Name)}
    return found


def uses_imported_path(script: Path, tree: ast.AST, want: str) -> bool:
    """whether `script` uses a path name of a scripts/ module it imports: `alias.NAME` after `import m as alias`, or
    `from m import NAME`; importing a module for something else does not read its file"""
    aliases: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            names = path_names(script.parent / f"{n.module.split('.')[0]}.py", want)
            if any(a.name in names for a in n.names):
                return True
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in aliases:
            if n.attr in path_names(script.parent / f"{aliases[n.value.id]}.py", want):
                return True
    return False


def names_file(script: Path, file: str) -> bool:
    """whether `script` uses `file`'s path: for Python, in a string value (not a comment or a docstring) or through
    a path name it imports; for a shell script, outside its comments"""
    source = script.read_text(encoding="utf-8", errors="replace")
    want = "conformance/planned" if file == PLANNED else file
    if script.suffix == ".py":
        if file == PLANNED and planned_dir(source):
            return True
        if any(token(want).search(v) for v in python_strings(source)):
            return True
        try:
            return uses_imported_path(script, ast.parse(source), want)
        except SyntaxError:
            return False
    return bool(token(want).search(without_shell_comments(source)))


def readers(root: Path, file: str) -> list[str]:
    """the non-test scripts that use `file`"""
    found = []
    for s in sorted((root / "scripts").iterdir()):
        if s.suffix not in (".py", ".sh") or s.name.startswith("test_") or s.name == Path(__file__).name:
            continue  # the tests, and this check (it spells the paths it looks for), are not readers
        if names_file(s, file):
            found.append(s.name)
    return found


def check(root: Path) -> tuple[int, list[str]]:
    files = conformance_files(root)
    run, workflows, steps = ci_scripts(root)
    out = []
    if not files:
        return 2, ["conformance_reader_map: no tracked file under conformance/ (compared nothing)"]
    if workflows == 0:
        return 2, ["conformance_reader_map: no workflow runs on ci/** (compared nothing)"]
    bad = 0
    for f in files:
        rs = readers(root, f)
        on_ci = [r for r in rs if r in run]
        if f != PLANNED:
            # a run step that passes the path itself (`--table conformance/shared-files.toml`) reads it
            on_ci += sorted({f"a run step of {wf}" for wf, text in steps if token(f).search(text)})
        if on_ci:
            out.append(f"ok: {f}: read on ci/** by {', '.join(on_ci)}")
        else:
            bad += 1
            why = f"its readers ({', '.join(rs)}) run on main only" if rs else "no script reads it"
            out.append(f"unmapped: {f}: {why}")
    out.append(f"conformance_reader_map: {len(files)} file(s), {workflows} ci/** workflow(s), {bad} unmapped")
    return (1 if bad else 0), out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    a = ap.parse_args(argv)
    code, lines = check(a.root)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
