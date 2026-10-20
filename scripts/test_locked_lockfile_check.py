#!/usr/bin/env python3
"""Tests for locked_lockfile_check.py: a `--locked` step that may run after its lock file's
generation failed is refused, the safe forms pass, and a run that compared nothing fails."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("locked_lockfile_check", HERE / "locked_lockfile_check.py")
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)

JOB = """on: push
jobs:
  roundtrip:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v5
      - name: A check that may fail first
        run: python3 scripts/check.py
      # a comment that names cargo test --locked belongs to no step
      - &patch
        name: Patch
        id: patch
        run: |
          python3 scripts/main_mode.py patch "$RUNNER_TEMP/core"
          cd e2e && cargo generate-lockfile
      - name: Test
        if: {cond}
        working-directory: e2e
        run: cargo test --release --locked
  digest:
    runs-on: macos-15
    steps:
      - *patch
      - name: Digest
        if: steps.patch.outputs.expected != 'true'
        run: cargo test --release --locked
"""


def run(cond: str) -> tuple[list[str], int]:
    return lc.problems_of("w.yml", JOB.replace("{cond}", cond))


class Rule(unittest.TestCase):
    def test_always_without_the_generating_step_outcome_is_refused(self):
        problems, compared = run("always() && steps.patch.outputs.expected != 'true'")
        self.assertEqual(compared, 2)
        self.assertEqual(len(problems), 1)
        self.assertIn("'Test'", problems[0])

    def test_not_cancelled_and_failure_are_overrides_too(self):
        for cond in ("${{ !cancelled() }}", "failure() || success()"):
            self.assertEqual(len(run(cond)[0]), 1, cond)

    def test_always_with_the_generating_step_success_passes(self):
        problems, compared = run("always() && steps.patch.outcome == 'success' && steps.patch.outputs.expected != 'true'")
        self.assertEqual((problems, compared), ([], 2))

    def test_another_steps_outcome_does_not_count(self):
        self.assertEqual(len(run("always() && steps.other.outcome == 'success'")[0]), 1)

    def test_a_default_condition_needs_nothing_more(self):
        # without always() / !cancelled() / failure() the step runs only after every earlier step succeeded
        self.assertEqual(run("steps.patch.outputs.expected != 'true'"), ([], 2))

    def test_a_block_scalar_condition_is_read_whole(self):
        for style in (">-", ">", "|", "|-", ">2-"):
            bad = JOB.replace("if: {cond}", f"if: {style}\n          always() &&\n          steps.patch.outputs.expected != 'true'")
            problems, compared = lc.problems_of("w.yml", bad)
            self.assertEqual((len(problems), compared), (1, 2), style)
            good = JOB.replace("if: {cond}", f"if: {style}\n          always() &&\n          steps.patch.outcome == 'success'")
            self.assertEqual(lc.problems_of("w.yml", good), ([], 2), style)

    def test_a_block_scalar_ends_at_the_next_key(self):
        step = ["      - name: Test", "        if: >-", "          always() &&", "          steps.patch.outcome == 'success'",
                "        run: cargo test --locked"]
        self.assertEqual(lc.condition(step), "always() && steps.patch.outcome == 'success'")

    def test_a_comment_line_is_not_read_as_a_step(self):
        # the comment above Patch names --locked; read as part of the check step it would count a third step
        self.assertEqual(run("steps.patch.outputs.expected != 'true'")[1], 2)


class Main(unittest.TestCase):
    def call(self, files: dict[str, str]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as d:
            for name, text in files.items():
                (Path(d) / name).write_text(text, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = lc.main(["x", d])
        return code, out.getvalue() + err.getvalue()

    def test_nothing_compared_fails(self):
        code, text = self.call({"a.yml": "on: push\njobs:\n  j:\n    steps:\n      - run: cargo test --locked\n"})
        self.assertEqual(code, 2)
        self.assertIn("nothing compared", text)

    def test_a_problem_fails_and_a_clean_tree_passes(self):
        bad = JOB.replace("{cond}", "always()")
        good = JOB.replace("{cond}", "always() && steps.patch.outcome == 'success'")
        self.assertEqual(self.call({"a.yml": bad})[0], 1)
        self.assertEqual(self.call({"a.yml": good})[0], 0)

    def test_this_repository_passes(self):
        code, text = self.call({})  # an empty directory compares nothing
        self.assertEqual(code, 2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lc.main(["x", str(HERE.parent / ".github" / "workflows")]), 0)
        self.assertNotIn(" 0 --locked", out.getvalue())


if __name__ == "__main__":
    unittest.main()
