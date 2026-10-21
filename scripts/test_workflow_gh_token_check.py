#!/usr/bin/env python3
"""Tests of scripts/workflow_gh_token_check.py (python3 scripts/test_workflow_gh_token_check.py)."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import workflow_gh_token_check as wgt  # noqa: E402

CALLER = 'import subprocess\n\n\ndef api(p):\n    return subprocess.run(["gh", "api", p])\n\n\napi("x")\n'
USER = "import caller\n\ncaller.api('y')\n"         # calls a gh function of another script
IMPORTER = "import caller\n\nprint(caller.__name__)\n"  # imports it, calls no gh
QUIET = "print('no gh here')\n"


def workflow(run: str, step_env: bool = False, job_env: bool = False, top_env: bool = False) -> str:
    out = ["name: t", "on: push"]
    if top_env:
        out += ["env:", "  GH_TOKEN: ${{ github.token }}"]
    out += ["jobs:", "  check:", "    runs-on: ubuntu-latest"]
    if job_env:
        out += ["    env:", "      GH_TOKEN: ${{ github.token }}"]
    out += ["    steps:", "      - uses: actions/checkout@v5", "      - name: The step"]
    if step_env:
        out += ["        env:", "          GH_TOKEN: ${{ github.token }}"]
    out += ["        run: " + run, ""]
    return "\n".join(out)


class GhToken(unittest.TestCase):
    def check(self, wf: str, scripts: dict | None = None) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)
            for name, text in (scripts or {"caller.py": CALLER, "quiet.py": QUIET}).items():
                (root / "scripts" / name).write_text(text, encoding="utf-8")
            (root / ".github" / "workflows" / "w.yml").write_text(wf, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = wgt.main(["--root", str(root)])
            return code, out.getvalue()

    def test_a_step_running_a_gh_script_without_the_token_is_a_finding(self):
        code, out = self.check(workflow("python3 scripts/caller.py"))
        self.assertEqual(code, 1, out)
        self.assertIn("step 'The step' reaches gh (caller.py) without GH_TOKEN", out)
        self.assertIn("compared 1 steps (1 reach gh), 1 finding(s)", out)

    def test_the_token_in_the_step_job_or_workflow_env_is_enough(self):
        for kw in ("step_env", "job_env", "top_env"):
            with self.subTest(env=kw):
                code, out = self.check(workflow("python3 scripts/caller.py", **{kw: True}))
                self.assertEqual(code, 0, out)

    def test_a_step_with_no_gh_script_needs_no_token(self):
        code, out = self.check(workflow("python3 scripts/quiet.py"))
        self.assertEqual(code, 0, out)
        self.assertIn("compared 1 steps (0 reach gh)", out)

    def test_a_shell_script_that_runs_a_gh_script_reaches_gh(self):
        scripts = {"caller.py": CALLER, "wrap.sh": "#!/bin/sh\npython3 scripts/caller.py --x\n"}
        code, out = self.check(workflow("scripts/wrap.sh"), scripts)
        self.assertEqual(code, 1, out)
        self.assertIn("(wrap.sh -> caller.py)", out)
        # a comment naming it is not a call
        scripts["wrap.sh"] = "#!/bin/sh\n# see caller.py\necho hi\n"
        self.assertEqual(self.check(workflow("scripts/wrap.sh"), scripts)[0], 0)

    def test_calling_a_gh_function_of_another_script_is_a_call_and_importing_it_is_not(self):
        scripts = {"caller.py": CALLER, "user.py": USER, "importer.py": IMPORTER}
        code, out = self.check(workflow("python3 scripts/user.py"), scripts)
        self.assertEqual(code, 1, out)
        code, out = self.check(workflow("python3 scripts/importer.py"), scripts)
        self.assertEqual(code, 0, out)

    def test_an_invocation_that_does_not_reach_gh_needs_no_token(self):
        scripts = {"conformance_roster_check.py": CALLER, "release_train.py": CALLER}
        for run in ("python3 scripts/conformance_roster_check.py --internal",
                    "python3 scripts/conformance_roster_check.py --check-manifest . --ref HEAD",
                    "python3 scripts/release_train.py next"):
            with self.subTest(run=run):
                self.assertEqual(self.check(workflow(run), scripts)[0], 0)
        for run in ("python3 scripts/conformance_roster_check.py", "python3 scripts/conformance_roster_check.py --strict",
                    "python3 scripts/release_train.py publish-gate alice-x"):
            with self.subTest(run=run):
                self.assertEqual(self.check(workflow(run), scripts)[0], 1)

    def test_a_script_run_as_a_module_is_read(self):
        for run in ("cd scripts && python3 -m caller --x", "python3 -m scripts.caller", "python -m caller",
                    'bash -c "cd scripts && python3 -m caller"', "bash -c 'cd scripts && python3 -m caller'",
                    "cd scripts && python3 -m caller>out.txt", "cd scripts && python3 -mcaller",
                    'cd scripts && python3 "-mcaller"'):
            with self.subTest(run=run):
                code, out = self.check(workflow(run))
                self.assertEqual(code, 1, out)
                self.assertIn("reaches gh (caller.py)", out)
                self.assertEqual(self.check(workflow(run, step_env=True))[0], 0)
        # a module that is not a gh caller, a mode that does not reach gh, and a module of another package
        scripts = {"caller.py": CALLER, "quiet.py": QUIET, "release_train.py": CALLER}
        for run in ("cd scripts && python3 -m quiet", "cd scripts && python3 -m release_train next",
                    "python3 -m pip install x", "python3 -m other.caller", "python3 -m caller.tools",
                    "python3 -c caller"):
            with self.subTest(run=run):
                self.assertEqual(self.check(workflow(run), scripts)[0], 0)
        # through a shell script of scripts/
        scripts["wrap.sh"] = "#!/bin/sh\ncd scripts && python3 -m caller\n"
        code, out = self.check(workflow("scripts/wrap.sh"), scripts)
        self.assertEqual(code, 1, out)
        self.assertIn("(wrap.sh -> caller.py)", out)

    def test_gh_in_the_step_itself_needs_the_token(self):
        code, out = self.check(workflow("gh api repos/o/r"))
        self.assertEqual(code, 1, out)
        self.assertIn("reaches gh (gh)", out)

    def test_a_block_run_is_read(self):
        wf = workflow("|").replace("        run: |\n", "        run: |\n          echo a\n          python3 scripts/caller.py\n")
        code, out = self.check(wf)
        self.assertEqual(code, 1, out)

    def test_no_step_compared_is_not_a_pass(self):
        code, out = self.check("name: t\non: push\njobs:\n  check:\n    runs-on: ubuntu-latest\n")
        self.assertEqual(code, 2, out)
        self.assertIn("compared nothing", out)


class TheRepository(unittest.TestCase):
    def test_this_repository_passes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = wgt.main([])
        self.assertEqual(code, 0, out.getvalue())


if __name__ == "__main__":
    unittest.main()
