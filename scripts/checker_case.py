#!/usr/bin/env python3
"""Run a checker in a test case and judge it by what it reported, not only by its exit code.

A test of a CI checker runs the checker on a hand-made input and expects it to pass or to fail.
Comparing the exit code alone cannot tell a checker that found the problem from a checker that
crashed: a Python crash exits 1 too, so a test whose failing cases compare only exit codes counts
a crash (for example the check removed, or a KeyError on the input) as the check working.

    from checker_case import run_check, run_cases

    def main():
        return run_cases("test_my_check", [
            ("a clean tree passes", lambda: run_check(cmd(clean), expect_exit=0)),
            ("a stale entry fails", lambda: run_check(cmd(stale), expect_exit=1,
                                                      expect_text="[unread] p: its gate is readable now")),
        ])

run_check(argv, *, expect_exit, expect_text=None, cwd=None, env=None, input=None,
          crash_markers=PYTHON_CRASH) -> None | str
    Runs argv and returns None when it behaved as expected, else the reason. In order:
      - a crash marker in its output (stdout and stderr) is "crashed", whatever the exit code
        and the text: the default marker is Python's traceback header; a bash checker passes
        BASH_CRASH (also `command not found`, `syntax error`, `unbound variable`);
      - an exit code other than expect_exit;
      - an expected text (a string or a list, each must appear) missing from the output.
    A case expecting a failure (expect_exit != 0) must name the text of the problem: without one
    it cannot tell which problem, if any, was found (ValueError, the test is written wrong).

The expected text is not what tells a crash apart: a traceback prints the source line that
failed, and that line can hold the very message the case expects. The crash markers do that.
The text tells which problem the checker reported. So a text that crash output carries anyway
is refused (ValueError): one that occurs, ignoring case, in the crash corpus (each builtin
exception as `<Name>: `, the traceback header and frame line, bash's error forms). That refuses
`error:` (in `KeyError: `), `Error`, `line 1`, `not found` (in `command not found`), and passes
`finding`, `no such file`, `exit 1`. A length rule would not: `error:` and `exit 1` are both 6
characters.

run_cases(name, cases) -> int
    Runs (description, function) pairs, a function returning None or a reason (raising counts
    as failing); prints each failing case with its reason and `<name>: <passed>/<total> cases`;
    returns 0 when all passed and there was at least one case, else 1.

Standard library only, Python 3.9 or later.
"""

from __future__ import annotations

import builtins
import subprocess
from typing import Callable, Iterable, Optional, Sequence, Union

PYTHON_CRASH = ("Traceback (most recent call last)",)
BASH_CRASH = PYTHON_CRASH + ("command not found", "syntax error", "unbound variable")


def crash_corpus() -> list[str]:
    """Text a crash prints whatever the checker meant to report (lower case)"""
    names = sorted(n for n, v in vars(builtins).items() if isinstance(v, type) and issubclass(v, BaseException))
    lines = [f"{n}: " for n in names]
    lines += [
        "Traceback (most recent call last):",
        '  File "checker.py", line 1, in main',
        "checker.sh: line 1: x: command not found",
        "checker.sh: line 1: syntax error near unexpected token",
        "checker.sh: line 1: x: unbound variable",
    ]
    return [line.lower() for line in lines]


def weak_text(text: str) -> Optional[str]:
    """None when text tells a reported problem apart, else why not"""
    if not text.strip():
        return "is empty"
    low = text.lower()
    for line in crash_corpus():
        if low in line:
            return f"occurs in crash output ({line.strip()!r})"
    return None


def run_check(
    argv: Sequence[str],
    *,
    expect_exit: int,
    expect_text: Union[None, str, Sequence[str]] = None,
    cwd=None,
    env=None,
    input: Optional[str] = None,
    crash_markers: Iterable[str] = PYTHON_CRASH,
) -> Optional[str]:
    texts = [expect_text] if isinstance(expect_text, str) else list(expect_text or [])
    if expect_exit != 0 and not texts:
        raise ValueError("a case expecting a failure must name the text of the problem it reports")
    for t in texts:
        why = weak_text(t)
        if why:
            raise ValueError(f"expected text {t!r} {why}: it does not tell which problem was reported")
    r = subprocess.run(
        list(argv), cwd=cwd, env=env, input=input, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    out = r.stdout + r.stderr
    for marker in crash_markers:
        if marker in out:
            return f"crashed ({marker!r} in the output)\n{out}"
    if r.returncode != expect_exit:
        return f"exit {r.returncode}, expected {expect_exit}\n{out}"
    for t in texts:
        if t not in out:
            return f"did not report {t!r}\n{out}"
    return None


def run_cases(name: str, cases: Sequence[tuple[str, Callable[[], Optional[str]]]]) -> int:
    if not cases:
        print(f"{name}: no case (a test that compares nothing)")
        return 1
    passed = 0
    for what, case in cases:
        try:
            why = case()
        except Exception as e:  # a case that raises is a failing case, reported with its type
            why = f"raised {type(e).__name__}: {e}"
        if why is None:
            passed += 1
        else:
            print(f"{name}: {what}: {why}")
    print(f"{name}: {passed}/{len(cases)} cases")
    return 0 if passed == len(cases) else 1
