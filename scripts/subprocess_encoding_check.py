#!/usr/bin/env python3
"""Every subprocess call in text mode names its encoding.

`subprocess.run(..., text=True)` (or `universal_newlines=True`) without `encoding=` decodes the
child's output with the locale's code page. On a Windows runner that is cp1252, and git's output
of a UTF-8 file (a manifest, a table, a CHANGELOG with Japanese text) fails with
UnicodeDecodeError, or decodes to other characters, only on that lane. This walks the syntax
tree of every `scripts/*.py` and fails on a `subprocess.run` / `Popen` / `check_output` /
`check_call` / `call` in text mode with no `encoding` keyword.

A file can be exempt only through scripts/subprocess_encoding_exempt.toml, with the owner who
changes it, the reason and an `until` date (YYYY-MM-DD): an exemption past its date fails, and
so does one for a file that no longer has such a call. Nothing inspected is a failure.

usage: subprocess_encoding_check.py [scripts dir] [exempt toml] [--today YYYY-MM-DD]
"""
from __future__ import annotations

import ast
import datetime
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
FUNCS = {"run", "Popen", "check_output", "check_call", "call"}
TEXT_KEYWORDS = ("text", "universal_newlines")


def text_mode_calls(source: str) -> tuple[int, list[int]]:
    """(subprocess calls seen, lines of those in text mode without an encoding)"""
    seen, missing = 0, []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                and node.func.attr in FUNCS):
            continue
        seen += 1
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        text = any(isinstance(kw.get(a), ast.Constant) and kw[a].value is True for a in TEXT_KEYWORDS)
        if text and "encoding" not in kw:
            missing.append(node.lineno)
    return seen, sorted(missing)


def load_exempt(path: Path) -> tuple[dict[str, dict], list[str]]:
    if not path.is_file():
        return {}, []
    entries, problems = {}, []
    for e in tomllib.loads(path.read_text(encoding="utf-8")).get("exempt", []):
        name = e.get("path", "")
        for key in ("path", "owner", "reason", "until"):
            if not str(e.get(key, "")).strip():
                problems.append(f"{path.name}: an entry for {name or '?'} has no {key}")
        try:
            e["until"] = datetime.date.fromisoformat(str(e.get("until")))
        except ValueError:
            problems.append(f"{path.name}: {name}: until is not a YYYY-MM-DD date")
            continue
        if name in entries:
            problems.append(f"{path.name}: {name} is listed twice")
        entries[name] = e
    return entries, problems


def check(scripts: Path, exempt_file: Path, today: datetime.date) -> tuple[int, list[str]]:
    exempt, problems = load_exempt(exempt_file)
    inspected = 0
    found: dict[str, list[int]] = {}
    for p in sorted(scripts.glob("*.py")):
        seen, missing = text_mode_calls(p.read_text(encoding="utf-8"))
        inspected += seen
        if missing:
            found[p.name] = missing
    for name, lines in sorted(found.items()):
        e = exempt.get(name)
        if e is None:
            problems.append(f"{name}: text-mode subprocess call without encoding= at line(s) "
                            f"{', '.join(map(str, lines))} (pass encoding=\"utf-8\")")
        elif e["until"] < today:
            problems.append(f"{name}: exemption expired on {e['until']} (owner {e['owner']}): "
                            f"{len(lines)} call(s) still without encoding=")
    for name in sorted(set(exempt) - set(found)):
        problems.append(f"{exempt_file.name}: {name} has no text-mode call without encoding=: remove its entry")
    return inspected, problems


def main(argv: list[str]) -> int:
    today = datetime.date.today()
    if "--today" in argv:
        i = argv.index("--today")
        today = datetime.date.fromisoformat(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    scripts = Path(argv[0]) if argv else HERE
    exempt_file = Path(argv[1]) if len(argv) > 1 else HERE / "subprocess_encoding_exempt.toml"
    inspected, problems = check(scripts, exempt_file, today)
    if inspected == 0:
        print(f"subprocess_encoding_check: no subprocess call found under {scripts}: nothing was checked",
              file=sys.stderr)
        return 2
    for p in problems:
        print(f"subprocess_encoding_check: {p}", file=sys.stderr)
    print(f"subprocess_encoding_check: {inspected} subprocess call(s) inspected, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
