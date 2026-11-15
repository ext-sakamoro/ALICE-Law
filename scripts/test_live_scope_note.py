#!/usr/bin/env python3
"""Tests of scripts/live_scope_note.py: the jobs skipped on push are read from the workflow, the note is written on a
push run only, and a workflow with none (0 read) is exit 2."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "live_scope_note.py"
sys.path.insert(0, str(HERE))
import live_scope_note as lsn  # noqa: E402

WF = """name: x
on:
  push:
    branches: [main]
jobs:
  always:
    name: Runs on every event
    runs-on: ubuntu-24.04
    steps:
      - run: echo a
  wide:
    name: Reads every repository
    if: github.event_name != 'push'
    runs-on: ubuntu-24.04
    steps:
      - if: github.event_name != 'push'
        run: echo b
  plain:
    if: github.event_name != "push"
    runs-on: ubuntu-24.04
  other:
    name: Some other condition
    if: always()
"""


def run(*argv: str, wf: str, summary: Path) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False, encoding="utf-8") as f:
        f.write(wf)
    try:
        return subprocess.run([sys.executable, str(SCRIPT), "--workflow", f.name, "--summary", str(summary), *argv],
                              capture_output=True, text=True, encoding="utf-8")
    finally:
        Path(f.name).unlink()


class LiveScopeNote(unittest.TestCase):
    def test_reads_job_level_skip_only(self):
        # a step-level `if` does not skip its job; a job without a name is named by its id; another condition is not
        # a push skip
        self.assertEqual(lsn.skipped_on_push(WF), ["Reads every repository", "plain"])

    def test_the_real_workflow_skips_the_repository_wide_jobs(self):
        names = lsn.skipped_on_push(lsn.WORKFLOW.read_text(encoding="utf-8"))
        self.assertIn("Every public ALICE-* repository's production panic sites only go down", names)
        self.assertNotIn("Every release-train crate declares its conformance at main", names)

    def test_push_writes_the_note_and_other_events_do_not(self):
        with tempfile.TemporaryDirectory() as d:
            s = Path(d, "summary.md")
            p = run("--event", "push", wf=WF, summary=s)
            self.assertEqual(p.returncode, 0, p.stderr)
            text = s.read_text(encoding="utf-8")
            self.assertIn("did not run", text)
            self.assertIn("- Reads every repository", text)
            self.assertIn("- plain", text)
            self.assertNotIn("Runs on every event", text)
            self.assertIn("skipped on push (not counted in this run): plain", p.stdout)
            s2 = Path(d, "summary2.md")
            p = run("--event", "schedule", wf=WF, summary=s2)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertFalse(s2.exists())

    def test_the_expression_form_reads_and_other_event_conditions_refuse(self):
        wf = "jobs:\n  a:\n    name: A\n    if: ${{ github.event_name != 'push' }}\n"
        self.assertEqual(lsn.skipped_on_push(wf), ["A"])
        for cond in ("github.event_name != 'push' && always()", "${{ github.event_name == 'schedule' || x }}",
                     "github.event_name != 'pull_request'"):
            with self.assertRaises(lsn.Unreadable, msg=cond):
                lsn.skipped_on_push(WF + f"  b:\n    if: {cond}\n")
        with tempfile.TemporaryDirectory() as d:
            p = run("--event", "push", wf=WF + "  b:\n    if: github.event_name != 'push' && x\n",
                    summary=Path(d, "s.md"))
            self.assertEqual(p.returncode, 2)
            self.assertIn("event condition", p.stderr)

    def test_none_read_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            p = run("--event", "push", wf="jobs:\n  a:\n    runs-on: x\n", summary=Path(d, "s.md"))
            self.assertEqual(p.returncode, 2)
            self.assertIn("0 read", p.stderr)


if __name__ == "__main__":
    unittest.main()
