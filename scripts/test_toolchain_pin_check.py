#!/usr/bin/env python3
"""Tests of scripts/toolchain_pin_check.py on workflows of known shape."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import toolchain_pin_check as t  # noqa: E402

SHA = "683b588c487a69d01f6c6ddf40f036ff682cc2c8"
OLD = "686976e191b89faba57d3206551f0f330d8cb249"


def wf(steps: str) -> str:
    return "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-24.04\n    steps:\n" + steps


class Pins(unittest.TestCase):
    def test_a_commit_pin_with_a_toolchain_input_passes(self):
        text = wf(f"      - uses: dtolnay/rust-toolchain@{SHA} # master\n        with:\n          toolchain: stable\n"
                  "      - run: cargo test\n")
        self.assertEqual(t.findings("ci.yml", text), ([], 1))

    def test_a_commit_pin_without_a_toolchain_input_is_found(self):
        text = wf(f"      - uses: dtolnay/rust-toolchain@{OLD} # stable (branch)\n      - run: cargo test\n")
        found, n = t.findings("ci.yml", text)
        self.assertEqual(n, 1)
        self.assertEqual(len(found), 1)
        self.assertIn("ci.yml:7:", found[0])
        self.assertIn(OLD[:12], found[0])

    def test_with_but_other_inputs_only_is_found(self):
        text = wf(f"      - uses: dtolnay/rust-toolchain@{SHA}\n        with:\n          components: clippy\n")
        self.assertEqual(len(t.findings("ci.yml", text)[0]), 1)

    def test_the_next_steps_toolchain_does_not_count_for_this_one(self):
        # the input must be in the same step: a later step's `with: toolchain:` does not cover an earlier pin
        text = wf(f"      - uses: dtolnay/rust-toolchain@{OLD}\n"
                  f"      - uses: dtolnay/rust-toolchain@{SHA}\n        with:\n          toolchain: nightly\n")
        found, n = t.findings("ci.yml", text)
        self.assertEqual(n, 2)
        self.assertEqual(len(found), 1)
        self.assertIn("ci.yml:7:", found[0])

    def test_a_named_step_with_uses_on_its_second_line_is_read(self):
        text = wf(f"      - name: Toolchain\n        uses: dtolnay/rust-toolchain@{SHA}\n        with:\n"
                  "          toolchain: 1.85\n")
        self.assertEqual(t.findings("ci.yml", text), ([], 1))

    def test_a_commented_toolchain_is_not_an_input(self):
        text = wf(f"      - uses: dtolnay/rust-toolchain@{SHA}\n        with:\n          # toolchain: stable\n"
                  "          components: rustfmt\n")
        self.assertEqual(len(t.findings("ci.yml", text)[0]), 1)

    def test_a_comment_after_with_does_not_hide_the_input(self):
        # comments are dropped before reading, so `with:` followed by a comment still opens the inputs
        text = wf(f"      - uses: dtolnay/rust-toolchain@{SHA}  # master\n        with:  # the toolchain is this input\n"
                  "          toolchain: stable\n")
        self.assertEqual(t.findings("ci.yml", text), ([], 1))

    def test_a_ref_that_is_not_a_commit_is_not_judged(self):
        text = wf("      - uses: dtolnay/rust-toolchain@stable\n      - uses: dtolnay/rust-toolchain@1.85\n")
        self.assertEqual(t.findings("ci.yml", text), ([], 0))


class Repo(unittest.TestCase):
    def run_cli(self, files: dict[str, str]) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as d:
            wfd = Path(d) / ".github" / "workflows"
            if files:
                wfd.mkdir(parents=True)
            for name, text in files.items():
                (wfd / name).write_text(text, encoding="utf-8")
            return subprocess.run([sys.executable, str(Path(t.__file__)), "--repo", d], capture_output=True, text=True, encoding="utf-8")

    def test_no_workflow_read_is_exit_2(self):
        self.assertEqual(self.run_cli({}).returncode, 2)

    def test_a_finding_is_exit_1(self):
        r = self.run_cli({"ci.yml": wf(f"      - uses: dtolnay/rust-toolchain@{OLD}\n")})
        self.assertEqual(r.returncode, 1)
        self.assertIn("found: ci.yml:7:", r.stdout)

    def test_clean_workflows_are_exit_0(self):
        r = self.run_cli({"ci.yml": wf(f"      - uses: dtolnay/rust-toolchain@{SHA}\n        with:\n          toolchain: stable\n")})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("1 pinned dtolnay/rust-toolchain steps, 0 without", r.stdout)


class Baseline(unittest.TestCase):
    """the live run's ratchet: counts per <repository>/<workflow> may only go down, and a lower count must be written"""

    def setUp(self):
        import toolchain_pin_live as live
        from collections import Counter
        self.live, self.C = live, Counter

    def test_equal_counts_pass(self):
        self.assertEqual(self.live.compare(self.C({"A/ci.yml": 2}), {"A/ci.yml": 2}), [])

    def test_a_new_pin_is_found(self):
        out = self.live.compare(self.C({"A/ci.yml": 3, "B/x.yml": 1}), {"A/ci.yml": 2})
        self.assertEqual([x.split(":")[0] for x in out], ["found", "found"])

    def test_a_fixed_pin_makes_the_row_stale(self):
        out = self.live.compare(self.C({"A/ci.yml": 1}), {"A/ci.yml": 2, "B/x.yml": 1})
        self.assertEqual([x.split(":")[0] for x in out], ["stale", "stale"])

    def test_write_then_load_round_trips_and_drops_zero_rows(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.toml"
            self.live.write_baseline(p, self.C({"A/ci.yml": 2, "B/x.yml": 0}))
            self.assertEqual(self.live.load_baseline(p), {"A/ci.yml": 2})

    def test_the_committed_baseline_is_well_formed(self):
        rows = self.live.load_baseline(self.live.BASELINE)
        self.assertTrue(rows)
        self.assertEqual(self.live.lint_baseline(self.live.BASELINE), [])

    def test_a_malformed_row_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.toml"
            p.write_text('[pins]\n"A/ci.yml" = 0\n"no-workflow" = 1\n', encoding="utf-8")
            self.assertEqual(len(self.live.lint_baseline(p)), 2)


if __name__ == "__main__":
    unittest.main()
