#!/usr/bin/env python3
"""Every workflow step that runs `gh`, directly or through a script of scripts/, declares GH_TOKEN.

gh in a GitHub Actions job reads its token only from the environment (GH_TOKEN or GITHUB_TOKEN); without it
every call fails with "set the GH_TOKEN environment variable", which a check reports as unreadable input, so
the job goes red on a push that broke nothing. The check finds the scripts that call gh (a `"gh"` argv in
Python, a `gh <command>` line in shell, or a call of a function of scripts/ that runs one), then each workflow
step whose `run` runs such a script (by file name, or as `python3 -m <module>`) in a mode that reaches gh (GH_FREE lists the invocations that do not:
the roster check's `--internal`, for example), directly or through a shell script of scripts/ it runs, or
calls gh itself, and requires GH_TOKEN in the env of that step, its job or the workflow. Tests (test_*) are
not callers: their gh is a stub.

  python3 scripts/workflow_gh_token_check.py [--root DIR]

Prints `gh-token: compared N steps (M reach gh), K finding(s)`. Exit 0 when every step that reaches gh has the
token, 1 on a finding, 2 when no step was compared or a file cannot be read.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_GH = re.compile(r"""\[\s*["']gh["']\s*,""")
SH_GH = re.compile(r"(?:^|[\s;&|(`])gh\s+(?:api|run|workflow|pr|release|repo|issue|auth)\b", re.M)
SCRIPT_REF = re.compile(r"\b([\w\-]+\.(?:py|sh))\b")
# `python3 -m <module>` (run from scripts/, or `-m scripts.<module>`) runs scripts/<module>.py
# (also inside a quoted `bash -c "..."`, glued as `-mcaller`, and before a redirection)
MODULE_REF = re.compile(r"""(?:^|[\s"'(;&|])-m\s*((?:scripts\.)?(\w+))(?=[\s;&|)"'<>]|$)""")
PY_IMPORT = re.compile(r"^\s*(?:import\s+([\w, ]+)|from\s+(\w+)\s+import)", re.M)


# the invocations of a gh-calling script that do not reach gh (the first argument after the script name, or a
# flag anywhere on the line); add a form here only when that code path makes no gh call
GH_FREE = {
    "conformance_roster_check.py": ("--internal", "--check-manifest"),
    "release_train.py": ("next", "feature-coverage", "semver-config-drift"),
}


def gh_callers(scripts: Path) -> set[str]:
    """the scripts (not tests) that call gh: a gh argv of their own, or a call of a function of scripts/ whose body
    runs one (importing such a module without calling it does not count)"""
    names = {p.name: p for p in scripts.iterdir() if p.suffix in (".py", ".sh") and not p.name.startswith("test_")}
    texts = {n: p.read_text(encoding="utf-8") for n, p in names.items()}
    direct = {n for n, t in texts.items() if (PY_GH if n.endswith(".py") else SH_GH).search(t)}
    funcs = set()
    for n in direct:
        if not n.endswith(".py"):
            continue
        t = texts[n]
        defs = [(m.start(), m.group(1)) for m in re.finditer(r"^def (\w+)\(", t, re.M)]
        for k, (at, fname) in enumerate(defs):
            body = t[at:defs[k + 1][0] if k + 1 < len(defs) else len(t)]
            if PY_GH.search(body):
                funcs.add(fname)
    callers = set(direct)
    for n, t in texts.items():
        if n.endswith(".py") and n not in callers and funcs:
            if re.search(r"(?<![\w])(?:\w+\.)?(?:" + "|".join(sorted(funcs)) + r")\(", re.sub(r"^def .*$", "", t, flags=re.M)):
                callers.add(n)
    return callers


def script_refs(line: str) -> list[tuple[str, int]]:
    """(script name, end of the reference) of every script this line names: by file name, or run as a module"""
    refs = [(m.group(1), m.end()) for m in SCRIPT_REF.finditer(line)]
    refs += [(m.group(2) + ".py", m.end(1)) for m in MODULE_REF.finditer(line)]
    return sorted(refs, key=lambda r: r[1])


def line_reaches(line: str, callers: set[str]) -> list[str]:
    """the gh-calling scripts this command line runs in a gh mode"""
    out = []
    for name, end in script_refs(line):
        if name not in callers:
            continue
        after = line[end:].split()
        free = GH_FREE.get(name, ())
        if any(f in after for f in free if f.startswith("--")) or (after and after[0] in free):
            continue
        out.append(name)
    return out


def reaches(run: str, callers: set[str], scripts: Path) -> list[str]:
    """the gh-calling scripts a step's run reaches: on its own lines, and one level into a shell script it runs"""
    via = []
    for line in run.splitlines():
        via += line_reaches(line, callers)
        for m in SCRIPT_REF.finditer(line):
            sh = scripts / m.group(1)
            if m.group(1).endswith(".sh") and sh.is_file():
                for inner in sh.read_text(encoding="utf-8").splitlines():
                    if not inner.lstrip().startswith("#"):
                        via += [f"{m.group(1)} -> {x}" for x in line_reaches(inner, callers)]
    return sorted(set(via))


def steps(text: str):
    """(job, step text, step env has GH_TOKEN, job env has GH_TOKEN) of every step, and workflow env's"""
    lines = text.splitlines()
    top_env = has_token_block(lines, 0, len(lines), indent=0)
    out = []
    jobs_at = next((i for i, l in enumerate(lines) if re.match(r"^jobs:\s*$", l)), None)
    if jobs_at is None:
        return top_env, out
    job_starts = [i for i in range(jobs_at + 1, len(lines)) if re.match(r"^  [\w\-]+:\s*$", lines[i])]
    for k, start in enumerate(job_starts):
        end = job_starts[k + 1] if k + 1 < len(job_starts) else len(lines)
        job = lines[start].strip().rstrip(":")
        job_env = has_token_block(lines, start + 1, end, indent=4)
        steps_at = next((i for i in range(start + 1, end) if re.match(r"^    steps:\s*$", lines[i])), None)
        if steps_at is None:
            continue
        marks = [i for i in range(steps_at + 1, end) if re.match(r"^      - ", lines[i])]
        for j, s in enumerate(marks):
            e = marks[j + 1] if j + 1 < len(marks) else end
            body = lines[s:e]
            body[0] = "        " + body[0][len("      - "):]
            out.append((job, "\n".join(body), has_token_block(body, 0, len(body), indent=8), job_env))
    return top_env, out


def has_token_block(lines: list[str], start: int, end: int, indent: int) -> bool:
    """whether an `env:` mapping at exactly this indent (in lines[start:end]) sets GH_TOKEN or GITHUB_TOKEN"""
    pad = " " * indent
    for i in range(start, end):
        if lines[i] == pad + "env:" or lines[i].startswith(pad + "env:") and lines[i][len(pad):].strip() == "env:":
            for l in lines[i + 1:end]:
                if not l.strip() or l.lstrip().startswith("#"):
                    continue
                if len(l) - len(l.lstrip()) <= indent:
                    break
                if re.match(r"^\s*(GH_TOKEN|GITHUB_TOKEN)\s*:", l):
                    return True
    return False


def run_text(step: str) -> str:
    m = re.search(r"^        run:\s*(.*)$", step, re.M)
    if not m:
        return ""
    if m.group(1).strip() not in ("|", ">", "|-", ">-", ""):
        return m.group(1)
    body = []
    for l in step[m.end():].splitlines()[1:]:
        if l.strip() and len(l) - len(l.lstrip()) <= 8:
            break
        body.append(l)
    return "\n".join(body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    a = ap.parse_args(argv)
    findings = []
    compared = reaching = 0
    try:
        callers = gh_callers(a.root / "scripts")
        for wf in sorted((a.root / ".github" / "workflows").glob("*.y*ml")):
            top_env, found = steps(wf.read_text(encoding="utf-8"))
            for job, step, step_env, job_env in found:
                run = run_text(step)
                if not run:
                    continue
                compared += 1
                via = reaches(run, callers, a.root / "scripts")
                if not via and not SH_GH.search(run):
                    continue
                reaching += 1
                if not (step_env or job_env or top_env):
                    name = re.search(r"^        name:\s*(.+)$", step, re.M)
                    what = f"step '{name.group(1).strip()}'" if name else "a step"
                    findings.append(f"{wf.name}: job {job}: {what} reaches gh ({', '.join(via) or 'gh'}) "
                                    "without GH_TOKEN in its env, its job's or the workflow's")
    except OSError as e:
        print(f"gh-token: {e}", file=sys.stderr)
        return 2
    for f in findings:
        print(f"gh-token: {f}", file=sys.stderr)
    print(f"gh-token: compared {compared} steps ({reaching} reach gh), {len(findings)} finding(s)")
    if compared == 0:
        print("gh-token: compared nothing", file=sys.stderr)
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
