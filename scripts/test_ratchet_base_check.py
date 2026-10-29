#!/usr/bin/env python3
"""Tests of scripts/ratchet_base_check.py"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ratchet_base_check as rbc  # noqa: E402

GOOD = """jobs:
  a:
    steps:
      - run: |
          git fetch -q origin main
          base=$(python3 scripts/ci_base.py --event "$EVENT" --ref "$REF" --before "$BEFORE")
          if git cat-file -e "$base":conformance/x.toml 2>/dev/null; then
            git show "$base":conformance/x.toml > "$RUNNER_TEMP/x.toml"
          fi
"""


class RatchetBaseCheck(unittest.TestCase):
    def tree(self, workflows: dict[str, str], preflight: str | None = None) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / "scripts").mkdir()
        for name, text in workflows.items():
            (root / ".github" / "workflows" / name).write_text(text, encoding="utf-8")
        if preflight is not None:
            (root / "scripts" / "preflight.sh").write_text(preflight, encoding="utf-8")
        return root

    def test_reads_at_the_ci_base_base_pass(self):
        findings, reads = rbc.check(self.tree({"a.yml": GOOD}))
        self.assertEqual(findings, [])
        self.assertEqual(reads, 2)

    def test_fetch_head_is_a_finding(self):
        findings, _ = rbc.check(self.tree({"a.yml": GOOD.replace('"$base":conformance/x.toml >', "FETCH_HEAD:conformance/x.toml >")}))
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("a.yml:8: reads conformance/ at FETCH_HEAD", findings[0])

    def test_origin_main_in_preflight_is_a_finding(self):
        pre = 'base=$(python3 scripts/ci_base.py)\ngit show origin/main:conformance/x.toml > "$t"\n'
        findings, _ = rbc.check(self.tree({"a.yml": GOOD}, pre))
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("scripts/preflight.sh:2: reads conformance/ at origin/main", findings[0])

    def test_a_base_not_from_ci_base_is_a_finding(self):
        text = GOOD.replace('base=$(python3 scripts/ci_base.py --event "$EVENT" --ref "$REF" --before "$BEFORE")', "base=$(git rev-parse FETCH_HEAD)")
        findings, _ = rbc.check(self.tree({"a.yml": text}))
        self.assertEqual(len(findings), 2, findings)
        self.assertIn("base is assigned from something other than scripts/ci_base.py", findings[0])
        self.assertIn("never sets base=$(python3 scripts/ci_base.py", findings[1])

    def test_a_base_handed_to_the_strict_check_must_be_the_ci_base_base(self):
        bad = GOOD + "          LAW_BASE_REF=FETCH_HEAD scripts/shared_files_strict.sh\n"
        findings, _ = rbc.check(self.tree({"a.yml": bad}))
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("LAW_BASE_REF=FETCH_HEAD is not the ci_base.py base", findings[0])
        good = GOOD + '          LAW_BASE_REF="$base" scripts/shared_files_strict.sh\n'
        self.assertEqual(rbc.check(self.tree({"a.yml": good}))[0], [])

    def test_a_fallback_ref_in_a_script_is_a_finding(self):
        pre = 'base=$(python3 scripts/ci_base.py)\ngit show "${LAW_BASE_REF:-origin/main}:conformance/x" > "$t"\n'
        findings, _ = rbc.check(self.tree({"a.yml": GOOD}, pre))
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("${LAW_BASE_REF:-origin/main} falls back to another ref", findings[0])

    def test_a_base_ref_argument_must_be_the_ci_base_base(self):
        for bad, why in (
            ('python scripts/licence_files_check.py --repo . --holder-base-ref "$(git merge-base origin/main HEAD)"\n',
             "names the base (on main it is HEAD)"),
            ("python3 scripts/x.py --base-ref origin/main\n", "a base ref argument origin/main is not"),
            ('python3 scripts/live_baseline_delta.py --checker scripts/holder_live.py --baseline b.toml\n',
             "live_baseline_delta.py without --base-ref"),
            ('base="$BEFORE"\n', "assigned from something other than scripts/ci_base.py"),
            ("base=$(git rev-parse --verify -q origin/main || true)\n", "assigned from something other than"),
        ):
            findings, _ = rbc.check(self.tree({"a.yml": GOOD + "          " + bad}))
            self.assertTrue(any(why in f for f in findings), (bad, findings))
        good = GOOD + ('          python scripts/licence_files_check.py --repo . --holder-base-ref "$base"\n'
                       '          python3 scripts/live_baseline_delta.py --base-ref "$base" --checker c.py --baseline b\n')
        self.assertEqual(rbc.check(self.tree({"a.yml": good}))[0], [])

    def test_a_comment_line_is_not_a_read(self):
        text = "# git show FETCH_HEAD:conformance/x.toml\n" + GOOD
        findings, reads = rbc.check(self.tree({"a.yml": text}))
        self.assertEqual((findings, reads), ([], 2))

    def test_nothing_compared_fails(self):
        findings, reads = rbc.check(self.tree({"a.yml": "jobs: {}\n"}, "echo hi\n"))
        self.assertEqual(reads, 0)
        self.assertEqual(len(findings), 1)
        self.assertIn("nothing compared", findings[0])

    def test_the_command_exits_1_on_a_finding_and_0_when_clean(self):
        bad = self.tree({"a.yml": GOOD.replace('"$base":conformance/x.toml >', "FETCH_HEAD:conformance/x.toml >")})
        self.assertEqual(rbc.main(["x", str(bad)]), 1)
        self.assertEqual(rbc.main(["x", str(self.tree({"a.yml": GOOD}))]), 0)


if __name__ == "__main__":
    unittest.main()
