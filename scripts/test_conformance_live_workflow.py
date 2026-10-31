#!/usr/bin/env python3
"""tests that read .github/workflows/conformance-live.yml itself: the daily (schedule) run is the strict one and every
other run checks a change in flight, so a step that moves the strictness off the schedule is red here

    python3 scripts/test_conformance_live_workflow.py
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "conformance-live.yml"
STEP_RE = re.compile(r"^      - (?:name: (?P<name>.*)|uses: .*)$")


def steps(text: str) -> list[dict]:
    """each step of the workflow: its name, its `if:` and its run text (the block under `run: |` or the one line)"""
    out: list[dict] = []
    cur: dict | None = None
    in_run = False
    for line in text.splitlines():
        m = STEP_RE.match(line)
        if m:
            cur = {"name": (m["name"] or "").strip(), "if": "", "run": ""}
            out.append(cur)
            in_run = False
            continue
        if cur is None:
            continue
        if line.startswith("  ") and not line.startswith("      ") and line.strip():
            cur, in_run = None, False  # the next job
            continue
        s = line.strip()
        if in_run and (line.startswith("          ") or not s):
            cur["run"] += s + "\n"
            continue
        in_run = False
        if s.startswith("if: "):
            cur["if"] = s[4:]
        elif s == "run: |":
            in_run = True
        elif s.startswith("run: "):
            cur["run"] = s[5:] + "\n"
    return out


def branches(run: str) -> tuple[str, str]:
    """(the schedule branch, the other branch) of `if [ "$EVENT" = schedule ]; then ... else ... fi`, nested ifs kept
    inside their branch"""
    lines = run.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.startswith('if [ "$EVENT" = schedule ]; then')), None)
    if start is None:
        raise AssertionError(f"no schedule branch in: {run!r}")
    parts: list[list[str]] = [[], []]
    depth, side = 1, 0
    for ln in lines[start + 1:]:
        if ln.startswith("if "):
            depth += 1
        elif ln.strip() == "fi":
            depth -= 1
            if depth == 0:
                return "".join(parts[0]), "".join(parts[1])
        elif ln.strip() == "else" and depth == 1:
            side = 1
            continue
        parts[side].append(ln)
    raise AssertionError(f"an unclosed schedule branch in: {run!r}")


class Workflow(unittest.TestCase):
    def setUp(self):
        self.steps = steps(WORKFLOW.read_text(encoding="utf-8"))
        self.assertGreater(len(self.steps), 10)  # the parser read the file (compared something)

    def step(self, name: str) -> dict:
        found = [s for s in self.steps if s["name"] == name]
        self.assertEqual(len(found), 1, (name, [s["name"] for s in self.steps]))
        return found[0]

    def test_the_holder_job_is_strict_daily_and_growth_only_otherwise(self):
        daily, other = branches(self.step("The public repositories against the baseline")["run"])
        self.assertIn("holder_live.py", daily)
        self.assertNotIn("--growth-only", daily)
        self.assertIn("holder_live.py", other)
        self.assertIn("--growth-only", other)

    def test_the_manifests_are_read_at_main_and_the_counts_go_to_the_job_summary(self):
        run = self.step("Every release-train manifest at main (the status counts go to the job summary)")["run"]
        self.assertEqual(run.strip(), 'python3 scripts/conformance_roster_check.py --summary "$GITHUB_STEP_SUMMARY"')
        # no committed state is compared or written: neither the drift note nor a state file is named
        whole = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("--state-drift-note", whole)
        self.assertNotIn("manifest-state.toml", whole)
        self.assertNotIn("pending-state.toml", whole)

    def test_the_strict_shared_file_check_runs_daily_and_the_landing_gate_on_a_push(self):
        daily = self.step("Strict every day (scripts/shared_files_strict.sh --daily)")
        self.assertEqual(daily["if"], "github.event_name == 'schedule'")
        self.assertIn("scripts/shared_files_strict.sh --daily", daily["run"])
        push = self.step("Strict on a push to main (scripts/shared_files_strict.sh)")
        self.assertEqual(push["if"], "github.event_name == 'push'")
        self.assertIn("scripts/shared_files_strict.sh", push["run"])
        self.assertNotIn("--daily", push["run"])
        # the base ref is the fetched previous main, not the commit under check (HEAD) nor the working tree
        self.assertIn('git fetch -q --depth 1 origin "$BEFORE"', push["run"])
        self.assertRegex(push["run"], r"LAW_BASE_REF=FETCH_HEAD scripts/shared_files_strict\.sh")
        self.assertNotRegex(push["run"], r"LAW_BASE_(TABLE|STATE)=")

    def test_the_parser_reads_a_branch_and_refuses_a_step_without_one(self):
        s = steps("jobs:\n  a:\n    steps:\n      - name: x\n        if: y\n        run: |\n"
                  "          if [ \"$EVENT\" = schedule ]; then\n            one\n          else\n            two\n"
                  "          fi\n")
        self.assertEqual((s[0]["name"], s[0]["if"]), ("x", "y"))
        self.assertEqual(branches(s[0]["run"]), ("one\n", "two\n"))
        with self.assertRaises(AssertionError):
            branches("python3 x.py\n")


if __name__ == "__main__":
    unittest.main()
