#!/usr/bin/env python3
"""Run a CI step that is expected to fail until a release, and say so instead of going red.

    expected_failure.py <step> [--roster release-train.toml] -- <command> [args...]

The roster lists such steps under `[expected-failure.<name>]`:

    step   = "<step>"                   the name a workflow passes here
    until  = "<crate> <version>"        a roster crate at its roster version: the release
                                        after which the step must pass
    expect = ["<text>", ...]            text the failing output must contain (why it fails)
    why    = "..."                      one line for the job summary

The command runs with its output shown as it comes. Then:

    passed, no entry                      exit 0
    passed, an entry                      exit 1: the entry is out of date, remove it
    failed, no entry                      exit with the command's status
    failed, an entry, `until` published   exit 1: the entry is stale (the release is out
                                          and the step still fails)
    failed, an entry, an `expect` text    exit 1: it failed for another reason
      missing from the output
    failed as the entry expects           exit 0, `expected=true` in $GITHUB_OUTPUT (the
                                          workflow skips the steps that need this one) and
                                          a line in $GITHUB_STEP_SUMMARY

A malformed entry, or a step listed twice, exits 2. So a red step is a problem to fix;
a step that fails as planned is named, explained and bounded by a release.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_ROSTER = Path(__file__).resolve().parent.parent / "release-train.toml"


class EntryError(ValueError):
    pass


def load_entries(text: str) -> dict[str, dict]:
    """{step: entry} of the roster's [expected-failure.*], with `past` (until is published)"""
    data = tomllib.loads(text)
    crates = data.get("crate", {})
    out: dict[str, dict] = {}
    for name, e in data.get("expected-failure", {}).items():
        unknown = set(e) - {"step", "until", "expect", "why"}
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
        if step in out:
            raise EntryError(f"expected-failure.{name}: step {step!r} is listed twice")
        out[step] = {"name": name, "until": until, "expect": expect, "why": why,
                     "past": crates[crate].get("state") == "published"}
    return out


def run(cmd: list[str]) -> tuple[int, str]:
    """the command's status and its output (stdout and stderr), shown as it comes"""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    lines = []
    assert p.stdout is not None
    for line in p.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        lines.append(line)
    return p.wait(), "".join(lines)


def append(env: str, text: str) -> None:
    path = os.environ.get(env)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def main(argv: list[str] | None = None, runner=run) -> int:
    argv = sys.argv[1:] if argv is None else argv
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
        entry = load_entries(a.roster.read_text(encoding="utf-8")).get(a.step)
    except (OSError, EntryError, tomllib.TOMLDecodeError) as e:
        print(f"error: {a.roster}: {e}", file=sys.stderr)
        return 2
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
    missing = [t for t in entry["expect"] if t not in output]
    if missing:
        print(f"expected failure: {a.step} failed, but not as expected-failure.{entry['name']} says "
              f"(missing from the output: {missing})")
        return 1
    append("GITHUB_OUTPUT", "expected=true\n")
    append("GITHUB_STEP_SUMMARY", f"\n**Expected failure until {entry['until']}** ({a.step}, "
                                  f"expected-failure.{entry['name']}): {entry['why']}. The steps that need it did not run.\n")
    print(f"expected failure: {a.step} failed as expected until {entry['until']} ({entry['why']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
