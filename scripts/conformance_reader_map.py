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
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reader_map_core import (  # noqa: E402  (the shared reading: scripts/reader_map_core.py)
    RUN_SCRIPT, names_path, on_ci_branches, run_texts, token, without_shell_comments)

PLANNED = "conformance/planned/*"


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


def planned_dir(source: str) -> bool:
    """the checker's PLANNED_DIR = "planned" (the planned bytes it reads next to the table)"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PLANNED_DIR" for t in n.targets)
               and isinstance(n.value, ast.Constant) and n.value.value == "planned" for n in ast.walk(tree))


def names_file(script: Path, file: str) -> bool:
    """whether `script` uses `file`'s path: for Python, in a string value (not a comment or a docstring) or through
    a path name it imports; for a shell script, outside its comments"""
    want = "conformance/planned" if file == PLANNED else file
    if script.suffix == ".py" and file == PLANNED and planned_dir(script.read_text(encoding="utf-8", errors="replace")):
        return True
    return names_path(script, want)


def readers(root: Path, file: str) -> list[str]:
    """the non-test scripts that use `file`"""
    found = []
    for s in sorted((root / "scripts").iterdir()):
        if s.suffix not in (".py", ".sh") or s.name.startswith("test_") or s.name in (Path(__file__).name, "reader_map_core.py"):
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
