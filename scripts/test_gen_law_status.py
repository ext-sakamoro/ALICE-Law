#!/usr/bin/env python3
"""Tests for gen_law_status.py (fixture files in a temporary directory)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gls", HERE / "gen_law_status.py")
gls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gls)

ROSTER = '''[crate.alpha]
version = "1.2.0"
state = "published"
order = 1
repo = "Alpha"

[crate.beta]
version = "0.3.0"
state = "planned"
order = 0
repo = "Beta"
tag = "beta-v{version}"

[crate.law]
version = "0.0.0"
state = "exempt"
order = 2
repo = "Law"

[expected-failure.x]
step = "some-step"
until = "beta 0.3.0"
expect = ["e"]
why = "beta is | not out"
exit = 1
'''
STATE = '[manifest."Alpha"]\ncrates = ["alpha", "alpha-cli"]\n"pass" = 3\n"fail-known" = 1\n"unverified" = 10\n"n/a" = 0\n'
MISSING = '[missing."beta"]\nfixed-in = "Beta"\nwhy = "not written"\n'
GOLDEN = "0.5.0 aaaa\n0.5.1 bbbb\n"
README = "# T\n\n## Status\n\n<!-- law-status:begin -->\nold text\n<!-- law-status:end -->\n\n## License\n"


class Gen(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        # registered per directory: a test that calls setUp again gets a fresh one, and each is removed
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        (self.d / "conformance").mkdir()
        (self.d / "e2e" / "golden").mkdir(parents=True)
        self.files = {"conformance/gates.toml": '[[gate]]\nid = "G1"\n\n[[gate]]\nid = "G2"\n', "release-train.toml": ROSTER, "conformance/manifest-state.toml": STATE,
                      "conformance/missing-manifests.toml": MISSING, "e2e/golden/run.digest": GOLDEN,
                      "README.md": README}
        for k, v in self.files.items():
            (self.d / k).write_text(v, encoding="utf-8")

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = gls.main(list(args), root=self.d)
        return code, out.getvalue()

    def readme(self, name="README.md"):
        return (self.d / name).read_text(encoding="utf-8")

    def test_the_section_is_rendered_from_the_files_and_then_checks_clean(self):
        self.assertEqual(self.run_main()[0], 0)
        text = self.readme()
        self.assertNotIn("old text", text)
        self.assertTrue(text.startswith("# T\n\n## Status\n\n<!-- law-status:begin -->"))
        self.assertTrue(text.endswith("<!-- law-status:end -->\n\n## License\n"))
        # order by `order`, the default tag form, a roster tag form, no tag for an exempt crate
        self.assertLess(text.index("| beta | 0.3.0 | planned | 0 | Beta | beta-v0.3.0 |"),
                        text.index("| alpha | 1.2.0 | published | 1 | Alpha | v1.2.0 |"))
        self.assertIn("| law | 0.0.0 | exempt | 2 | Law | (none) |", text)
        self.assertIn("| some-step | beta 0.3.0 | beta is \\| not out |", text)
        self.assertIn("| 0.5.1 | `bbbb` |", text)
        self.assertIn("| Alpha | alpha, alpha-cli | 3 | 1 | 10 | 0 |", text)
        self.assertIn("so each manifest is counted once; 1 manifests × 2 gates = 2 gate entries.", text)
        self.assertIn("| beta | Beta | not written |", text)
        self.assertNotRegex(text, r"20\d\d-\d\d-\d\d")
        self.assertIn("### Conformance manifests (status counts of the 2 gates in each repository's manifest at main)", text)
        self.assertEqual(self.run_main("--check"), (0, "law-status: 1 README(s) compared, 0 differ\n"))

    def test_a_roster_change_without_regenerating_fails_the_check(self):
        self.run_main()
        (self.d / "release-train.toml").write_text(ROSTER.replace('version = "1.2.0"', 'version = "1.3.0"'),
                                                   encoding="utf-8")
        code, out = self.run_main("--check")
        self.assertEqual(code, 1, out)
        self.assertIn("found: README.md: the Status section differs", out)

    def test_a_state_or_golden_change_fails_the_check(self):
        for name, text in (("conformance/manifest-state.toml", STATE.replace('"pass" = 3', '"pass" = 4')),
                           ("e2e/golden/run.digest", GOLDEN + "0.5.2 cccc\n")):
            self.setUp()
            self.run_main()
            (self.d / name).write_text(text, encoding="utf-8")
            self.assertEqual(self.run_main("--check")[0], 1, name)

    def test_every_cell_is_escaped(self):
        (self.d / "release-train.toml").write_text(ROSTER.replace('repo = "Alpha"', 'repo = "Al|pha"'), encoding="utf-8")
        (self.d / "conformance/missing-manifests.toml").write_text(MISSING.replace('"Beta"', '"Be|ta"'), encoding="utf-8")
        self.run_main()
        text = self.readme()
        self.assertIn("| Al\\|pha |", text)
        self.assertIn("| beta | Be\\|ta |", text)

    def test_a_gates_file_with_no_gate_exits_2(self):
        (self.d / "conformance/gates.toml").write_text("", encoding="utf-8")
        self.assertEqual(self.run_main("--check")[0], 2)

    def test_a_hand_edit_inside_the_markers_fails_the_check(self):
        self.run_main()
        (self.d / "README.md").write_text(self.readme().replace("| 0.5.1 | `bbbb` |", "| 0.5.1 | `bbbc` |"),
                                          encoding="utf-8")
        self.assertEqual(self.run_main("--check")[0], 1)

    def test_markers_missing_doubled_or_reversed_exit_2(self):
        for text in ("# T\n", README + "<!-- law-status:begin -->\n",
                     "<!-- law-status:end -->\n<!-- law-status:begin -->\n"):
            (self.d / "README.md").write_text(text, encoding="utf-8")
            self.assertEqual(self.run_main("--check")[0], 2, text)

    def test_the_japanese_readme_is_rendered_too_and_must_carry_the_markers(self):
        (self.d / "README_JP.md").write_text(README, encoding="utf-8")
        self.assertEqual(self.run_main()[0], 0)
        ja = self.readme("README_JP.md")
        self.assertIn("| crate | 版 | 状態 | 順 | repository | tag |", ja)
        self.assertIn("| alpha | 1.2.0 | published | 1 | Alpha | v1.2.0 |", ja)
        self.assertEqual(self.run_main("--check")[1], "law-status: 2 README(s) compared, 0 differ\n")
        (self.d / "README_JP.md").write_text("# T\n", encoding="utf-8")
        self.assertEqual(self.run_main("--check")[0], 2)

    def test_nothing_to_render_exits_2(self):
        for name, text in (("release-train.toml", ""), ("conformance/manifest-state.toml", ""),
                           ("e2e/golden/run.digest", "0.5.0\n"), ("e2e/golden/run.digest", "")):
            self.setUp()
            (self.d / name).write_text(text, encoding="utf-8")
            self.assertEqual(self.run_main("--check")[0], 2, (name, text))
        self.setUp()
        (self.d / "README.md").unlink()
        self.assertEqual(self.run_main("--check")[0], 2)

    def test_no_missing_manifest_and_no_expected_failure_say_none(self):
        (self.d / "conformance/missing-manifests.toml").write_text("", encoding="utf-8")
        (self.d / "release-train.toml").write_text(ROSTER.split("[expected-failure.x]")[0], encoding="utf-8")
        self.run_main()
        sec = self.readme().split("### Steps expected to fail until a release")[1]
        self.assertTrue(sec.lstrip().startswith("None."))
        self.assertTrue(self.readme().split("### Crates whose manifest is not written yet")[1].lstrip()
                        .startswith("None."))


if __name__ == "__main__":
    unittest.main()
