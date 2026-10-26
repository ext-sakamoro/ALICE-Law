#!/usr/bin/env python3
"""Run a CI step that is expected to fail until a release, and say so instead of going red.

    expected_failure.py <step> [--roster release-train.toml] -- <command> [args...]

The roster lists such steps under `[expected-failure.<name>]`:

    step   = "<step>"                   the name a workflow passes here
    until  = "<crate> <version>"        a roster crate at its roster version: the release
                                        after which the step must pass
    expect = ["<text>", ...]            text of the error lines the failure must have
    why    = "..."                      one line for the job summary
    exit   = <status>                   optional: the exit status the failure must have
                                        (101 for a cargo panic)

The command runs with its output shown as it comes. Then:

    passed, no entry                      exit 0
    passed, an entry                      exit 1: the entry is out of date, remove it
    failed, no entry                      exit with the command's status
    failed, an entry, `until` published   exit 1: the entry is stale (the release is out
                                          and the step still fails)
    failed, an entry, and the error       exit 1: it failed for another reason, or for
      lines do not match `expect`         this reason and another one too
    failed as the entry expects           exit 0, `expected=true` in $GITHUB_OUTPUT (the
                                          workflow skips the steps that need this one) and
                                          a line in $GITHUB_STEP_SUMMARY

The failure is read from its own error lines: a line that starts (at its first
column, after terminal colour codes are removed) with `error` in any case followed by
`:`, `[...]`, a space or the end of the line (`error:`, `Error: ...` from anyhow,
`ERROR ...`), `fatal:`, or a `thread '...' panicked at` (with or without the thread id).
Every such line must contain one of the `expect` texts, and every `expect` text must be
in one of them; a failure with no error line is not one this can explain. A log line
that quotes an error further in is not an error line.

    expected_failure.py --check [--roster FILE] [--workflows DIR]
                                          every entry's step is run through this script
                                          by a workflow (an entry no workflow uses is
                                          an orphan: exit 1), and an entry whose step
                                          runs `cargo` names its `exit` (cargo fails
                                          with 101; another status, such as a crash,
                                          is then red)

A malformed entry, or a step listed twice, exits 2. So a red step is a problem to fix;
a step that fails as planned is named, explained and bounded by a release.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_ROSTER = Path(__file__).resolve().parent.parent / "release-train.toml"
DEFAULT_WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
# (a panic names the thread, and since Rust 1.86 also its id: `thread 'main' (1234) panicked at`)
ERROR_LINE = re.compile(r"^(?:error(?:\[[A-Za-z0-9]+\])?(?::|\s|$)|fatal:|thread '[^']*'(?: \(\d+\))? panicked at )", re.IGNORECASE)
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
WRAPPED_STEP = re.compile(r"(?<![A-Za-z0-9_])expected_failure\.py[ \t]+([A-Za-z][A-Za-z0-9_.-]*)([^\n]*)")


class EntryError(ValueError):
    pass


def load_entries(text: str) -> dict[str, dict]:
    """{step: entry} of the roster's [expected-failure.*], with `past` (until is published)"""
    data = tomllib.loads(text)
    crates = data.get("crate", {})
    out: dict[str, dict] = {}
    for name, e in data.get("expected-failure", {}).items():
        unknown = set(e) - {"step", "until", "expect", "why", "exit"}
        if unknown:
            raise EntryError(f"expected-failure.{name}: unknown keys {sorted(unknown)}")
        step, until, expect, why = e.get("step"), e.get("until"), e.get("expect"), e.get("why")
        if not isinstance(step, str) or not step:
            raise EntryError(f"expected-failure.{name}: `step` must be a step name")
        if not isinstance(until, str) or len(until.split()) != 2:
            raise EntryError(f"expected-failure.{name}: `until` must be \"<crate> <version>\"")
        crate, version = until.split()
        if crate not in crates or crates[crate].get("version") != version:
            raise EntryError(f"expected-failure.{name}: until {until!r} is not a roster crate at its roster version")
        if not isinstance(expect, list) or not expect or not all(isinstance(x, str) and x for x in expect):
            raise EntryError(f"expected-failure.{name}: `expect` must be a non-empty list of texts")
        if not isinstance(why, str) or not why:
            raise EntryError(f"expected-failure.{name}: `why` must say why the step fails")
        status = e.get("exit")
        if status is not None and (not isinstance(status, int) or isinstance(status, bool) or status == 0):
            raise EntryError(f"expected-failure.{name}: `exit` must be a non-zero exit status")
        if step in out:
            raise EntryError(f"expected-failure.{name}: step {step!r} is listed twice")
        out[step] = {"name": name, "until": until, "expect": expect, "why": why, "exit": status,
                     "past": crates[crate].get("state") == "published"}
    return out


def run(cmd: list[str]) -> tuple[int, str]:
    """the command's status and its output (stdout and stderr), shown as it comes"""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", encoding="utf-8")
    lines = []
    assert p.stdout is not None
    for line in p.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        lines.append(line)
    return p.wait(), "".join(lines)


def error_lines(output: str) -> list[str]:
    plain = (ANSI.sub("", line) for line in output.splitlines())
    return [line for line in plain if ERROR_LINE.match(line)]


def explain(entry: dict, output: str, code: int) -> list[str]:
    """why the failure is not the one the entry expects (empty when it is)"""
    if entry["exit"] is not None and code != entry["exit"]:
        return [f"exit status {code}, the entry expects {entry['exit']}"]
    errors = error_lines(output)
    if not errors:
        return ["the output has no error line"]
    out = [f"error line not explained by `expect`: {e[:200]}" for e in errors
           if not any(x in e for x in entry["expect"])]
    out += [f"`expect` text in no error line: {x!r}" for x in entry["expect"] if not any(x in e for e in errors)]
    return out


def summary_table(entries: dict[str, dict], step: str) -> str:
    rows = "".join(f"| {'**' + s + '**' if s == step else s} | {e['until']} | {e['why']} |\n"
                   for s, e in sorted(entries.items()))
    return f"\nSteps expected to fail until a release (`[expected-failure.*]`):\n\n| step | until | why |\n|---|---|---|\n{rows}"


def check(entries: dict[str, dict], workflows: Path) -> int:
    files = sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml"))
    if not files:
        print(f"error: no workflow in {workflows}", file=sys.stderr)
        return 2
    commands: dict[str, list[str]] = {}
    for f in files:
        for step, rest in WRAPPED_STEP.findall(f.read_text(encoding="utf-8")):
            commands.setdefault(step, []).append(rest.partition(" -- ")[2].strip())
    orphans = sorted(s for s in entries if s not in commands)
    for s in orphans:
        print(f"orphan: expected-failure.{entries[s]['name']} names step {s!r}, which no workflow runs "
              f"through expected_failure.py")
    no_exit = sorted(s for s in entries if s in commands and entries[s]["exit"] is None
                     and any(c.split()[:1] == ["cargo"] for c in commands[s]))
    for s in no_exit:
        print(f"no exit: expected-failure.{entries[s]['name']} wraps a cargo command, so it must name "
              f"`exit = 101` (another status, such as a crash, is then red)")
    print(f"expected failure: {len(entries)} entries, {len(commands)} wrapped steps in {len(files)} workflows, "
          f"{len(orphans)} orphans, {len(no_exit)} cargo steps without exit")
    return 1 if orphans or no_exit else 0


def append(env: str, text: str) -> None:
    path = os.environ.get(env)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def main(argv: list[str] | None = None, runner=run) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--check"]:
        ap = argparse.ArgumentParser()
        ap.add_argument("--check", action="store_true")
        ap.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
        ap.add_argument("--workflows", type=Path, default=DEFAULT_WORKFLOWS)
        a = ap.parse_args(argv)
        try:
            entries = load_entries(a.roster.read_text(encoding="utf-8"))
        except (OSError, EntryError, tomllib.TOMLDecodeError) as e:
            print(f"error: {a.roster}: {e}", file=sys.stderr)
            return 2
        return check(entries, a.workflows)
    if "--" not in argv:
        print("usage: expected_failure.py <step> [--roster FILE] -- <command> [args...]", file=sys.stderr)
        return 2
    i = argv.index("--")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("step")
    ap.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    a = ap.parse_args(argv[:i])
    cmd = argv[i + 1:]
    if not cmd:
        print("error: no command after --", file=sys.stderr)
        return 2
    try:
        entries = load_entries(a.roster.read_text(encoding="utf-8"))
        entry = entries.get(a.step)
    except (OSError, EntryError, tomllib.TOMLDecodeError) as e:
        print(f"error: {a.roster}: {e}", file=sys.stderr)
        return 2
    if entries:
        append("GITHUB_STEP_SUMMARY", summary_table(entries, a.step))
    code, output = runner(cmd)
    append("GITHUB_OUTPUT", "expected=false\n" if code == 0 or entry is None else "")
    if code == 0:
        if entry is not None:
            print(f"expected failure: {a.step} passed, so expected-failure.{entry['name']} is out of date: "
                  f"remove it from the roster")
            return 1
        return 0
    if entry is None:
        return code
    if entry["past"]:
        print(f"expected failure: {a.step} still fails after {entry['until']} is published: "
              f"expected-failure.{entry['name']} is stale")
        return 1
    why_not = explain(entry, output, code)
    if why_not:
        print(f"expected failure: {a.step} failed, but not (only) as expected-failure.{entry['name']} says:")
        for w in why_not:
            print(f"  {w}")
        return 1
    append("GITHUB_OUTPUT", "expected=true\n")
    append("GITHUB_STEP_SUMMARY", f"\n**Expected failure until {entry['until']}** ({a.step}, "
                                  f"expected-failure.{entry['name']}): {entry['why']}. The steps that need it did not run.\n")
    print(f"expected failure: {a.step} failed as expected until {entry['until']} ({entry['why']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
