#!/usr/bin/env python3
"""Tests of scripts/runner_pin_check.py on workflows of known shape."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner_pin_check as r  # noqa: E402

PATS = r.patterns()
GPU = "      - run: sudo apt-get install -y mesa-vulkan-drivers\n"
PW = "      - run: npx playwright install --with-deps chromium\n"


def wf(*jobs: str) -> str:
    return "name: CI\non: push\njobs:\n" + "".join(jobs)


def job(name: str, runs_on: str, steps: str, matrix: str = "") -> str:
    m = f"    strategy:\n      matrix:\n        os: [{matrix}]\n" if matrix else ""
    return f"  {name}:\n    runs-on: {runs_on}\n{m}    steps:\n{steps}"


class Offending(unittest.TestCase):
    def test_a_gpu_job_on_ubuntu_latest_is_found(self):
        self.assertEqual(r.offending(wf(job("gpu", "ubuntu-latest", GPU)), PATS), [("gpu", "gpu-runtime")])

    def test_a_playwright_job_on_ubuntu_latest_is_found(self):
        self.assertEqual(r.offending(wf(job("web", "ubuntu-latest", PW)), PATS), [("web", "playwright")])

    def test_a_matrix_that_lists_ubuntu_latest_is_found(self):
        t = wf(job("gpu", "${{ matrix.os }}", GPU, "ubuntu-latest, macos-latest"))
        self.assertEqual(r.offending(t, PATS), [("gpu", "gpu-runtime")])

    def test_a_pinned_job_passes(self):
        self.assertEqual(r.offending(wf(job("gpu", "ubuntu-24.04", GPU)), PATS), [])
        t = wf(job("gpu", "${{ matrix.os }}", GPU, "ubuntu-24.04, macos-latest"))
        self.assertEqual(r.offending(t, PATS), [])

    def test_a_job_that_only_mentions_lavapipe_passes(self):
        # measured on ALICE-SDF: a path-filter job echoes "the lavapipe lane only" and installs nothing
        t = wf(job("changes", "ubuntu-latest", '      - run: echo "weekly run: the lavapipe lane only (gpu_emit)"\n'))
        self.assertEqual(r.offending(t, PATS), [])

    def test_a_job_that_loads_the_lavapipe_icd_is_found(self):
        steps = "      - run: echo VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.x86_64.json >> $GITHUB_ENV\n"
        self.assertEqual(r.offending(wf(job("gpu", "ubuntu-latest", steps)), PATS), [("gpu", "gpu-runtime")])

    def test_a_job_with_no_pattern_passes(self):
        self.assertEqual(r.offending(wf(job("fmt", "ubuntu-latest", "      - run: cargo fmt --check\n")), PATS), [])

    def test_a_comment_before_the_next_job_is_not_the_previous_jobs(self):
        # measured on ALICE-LOL: the next job's preamble comment sat in the block of the job above it
        t = wf(job("fmt", "ubuntu-latest", "      - run: cargo fmt --check\n") + "  # installs lavapipe below\n",
               job("gpu", "ubuntu-24.04", GPU))
        self.assertEqual(r.offending(t, PATS), [])

    def test_only_the_jobs_section_is_read(self):
        t = "name: x\nenv:\n  A: lavapipe\njobs:\n" + job("fmt", "ubuntu-latest", "      - run: true\n")
        self.assertEqual(r.offending(t, PATS), [])


class Repo(unittest.TestCase):
    def run_on(self, files: dict[str, str]):
        with tempfile.TemporaryDirectory() as d:
            for name, text in files.items():
                p = Path(d) / ".github" / "workflows" / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            return subprocess.run([sys.executable, str(Path(r.__file__)), "--repo", d], capture_output=True, text=True, encoding="utf-8")

    def test_exit_codes(self):
        self.assertEqual(self.run_on({"ci.yml": wf(job("gpu", "ubuntu-24.04", GPU))}).returncode, 0)
        bad = self.run_on({"ci.yml": wf(job("gpu", "ubuntu-latest", GPU))})
        self.assertEqual(bad.returncode, 1)
        self.assertIn("found: ci.yml: gpu: gpu-runtime", bad.stdout)

    def test_no_workflow_compares_nothing(self):
        self.assertEqual(self.run_on({}).returncode, 2)


if __name__ == "__main__":
    unittest.main()
