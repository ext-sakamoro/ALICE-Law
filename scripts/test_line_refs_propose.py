#!/usr/bin/env python3
"""Tests of scripts/line_refs_propose.py (python3 scripts/test_line_refs_propose.py)."""
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import conformance_roster_check as crc  # noqa: E402
import line_refs_propose as lrp  # noqa: E402

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent"}
LIB = ("//! the crate\n"                            # 1
       "pub fn kernel(x: f32) -> f32 {\n"           # 2
       "    let y = clamp_unit(x);\n"               # 3
       "    y * 2.0\n"                              # 4
       "}\n"                                        # 5
       "pub fn other(x: f32) -> f32 {\n"            # 6
       "    let y = clamp_unit(x);\n"               # 7: the same line as 3
       "    y * 2.0\n"                              # 8
       "}\n"                                        # 9
       "\n"                                         # 10
       "// a comment about the kernel\n"            # 11
       "pub fn twice(x: f32) -> f32 {\n"            # 12
       "    let y = clamp_unit(x);\n"               # 13
       "    clamp_unit(y)\n"                        # 14: clamp_unit's first place in twice is line 13
       "}\n")                                       # 15
CI = ("name: ci\n"                                  # 1
      "jobs:\n"                                     # 2
      "  lint:\n"                                   # 3
      "    steps:\n"                                # 4
      "      # the clippy step\n"                   # 5
      "      - name: clippy (encoder)\n"            # 6
      "        working-directory: crate\n"          # 7
      "        run: cargo clippy --features encoder\n"  # 8
      "      - name: clippy (all targets)\n"        # 9
      "        run: cargo clippy --all-targets\n"    # 10
      "      - name: clippy (std, all targets)\n"   # 11
      "        run: cargo clippy --features std\n")  # 12
DOC = "# Title\n\n## Determinism\n\nevery result is bit-exact\n"
SCRIPT = ('"""The canary.\n'                    # 1
          '\n'                                     # 2
          'It plants every clippy entry.\n'        # 3
          '"""\n'                                  # 4
          'def plant_entry(row):\n'                # 5
          '    return row\n')                      # 6
SHADER = "fn sdf_sphere(p: vec3<f32>) -> f32 {\n    return length(p) - 1.0;\n}\n"


class Propose(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def repo(self, manifest: str | None) -> Path:
        d = Path(self.tmp.name) / "crate"
        files = {"src/lib.rs": LIB, ".github/workflows/ci.yml": CI, "docs/DESIGN.md": DOC,
                 "shaders/sdf.wgsl": SHADER, "scripts/canary.py": SCRIPT}
        for p, t in files.items():
            (d / p).parent.mkdir(parents=True, exist_ok=True)
            (d / p).write_text(t, encoding="utf-8")
        if manifest is not None:
            (d / crc.MANIFEST).write_text(manifest, encoding="utf-8")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "c"]):
            subprocess.run(["git", *args], cwd=d, env=ENV, check=True, capture_output=True)
        return d

    def run_propose(self, manifest: str | None) -> tuple[int, str]:
        d = self.repo(manifest)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = lrp.main(["--repo", str(d)])
        return code, out.getvalue()

    def note(self, text: str) -> tuple[int, str]:
        return self.run_propose(f'[G1]\nstatus = "unverified"\nnote = "{text}"\n')

    def proposed(self, out: str) -> list[str]:
        return [x.split("-> `", 1)[1].rstrip("`") for x in out.splitlines() if "-> `" in x]

    def test_a_definition_line_is_named_by_the_definition(self):
        code, out = self.note("the kernel doubles it (src/lib.rs:2)")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.proposed(out), ["src/lib.rs#fn kernel"])
        self.assertIn("1 reference(s), 1 proposed (1 anchors compared with their lines), 0 unresolved", out)

    def test_a_line_the_text_names_is_scoped_under_its_definition(self):
        # line 7 repeats line 3: only the scope tells them apart
        code, out = self.note("other clamps with clamp_unit (src/lib.rs line 7)")
        self.assertEqual(self.proposed(out), ["src/lib.rs#fn other>clamp_unit"])

    def test_text_naming_the_construct_gets_the_construct(self):
        # line 7 of ci.yml is the step's working-directory; the text means the clippy step
        code, out = self.note("the clippy job (.github/workflows/ci.yml line 7)")
        self.assertEqual(self.proposed(out), [".github/workflows/ci.yml#name: clippy (encoder)"])

    def test_an_anchor_that_resolves_to_another_line_is_not_proposed(self):
        # `fn twice>clamp_unit` passes the roster's rule but names line 13; line 14 needs its own text
        code, out = self.note("twice clamps again with clamp_unit (src/lib.rs line 14)")
        self.assertEqual(self.proposed(out), ["src/lib.rs#fn twice>clamp_unit(y)"])
        self.assertTrue(lrp.verify("src/lib.rs#fn twice>clamp_unit(y)", 14, {"src/lib.rs"}, lambda p: LIB))
        self.assertFalse(lrp.verify("src/lib.rs#fn twice>clamp_unit", 14, {"src/lib.rs"}, lambda p: LIB))
        # the enclosing definition names the line only when it is the line's own scope
        self.assertTrue(lrp.verify("src/lib.rs#fn twice", 14, {"src/lib.rs"}, lambda p: LIB))
        self.assertFalse(lrp.verify("src/lib.rs#fn kernel", 14, {"src/lib.rs"}, lambda p: LIB))

    def test_a_line_drifted_into_a_neighbouring_construct_is_not_anchored(self):
        # the Kinematics sentence: each reference's text is its own ("encoder", "std", "all features"); line 8 has
        # drifted into the encoder step, which shares `features` only on the line, not in the step's name
        code, out = self.note("ci.yml clippy steps (encoder .github/workflows/ci.yml line 6, std line 11, "
                              "all features line 8)")
        self.assertIn("`line 6` -> `.github/workflows/ci.yml#- name: clippy (encoder)`".replace("#- ", "#"), out)
        self.assertIn("`line 11` -> `.github/workflows/ci.yml#name: clippy (std, all targets)`", out)
        self.assertIn("line 8 ('run: cargo clippy --features encoder') shares no word with the text before the "
                      "reference (features)", out)

    def test_each_bound_of_a_reference_s_text_holds_on_its_own(self):
        # a ', ' bounds the text: without it "encoder" (said of another step) would name line 8's step
        code, out = self.note("ci.yml clippy steps (encoder, all features: .github/workflows/ci.yml line 8)")
        self.assertEqual(self.proposed(out), [], out)
        # the previous reference bounds it with no punctuation between
        code, out = self.note("encoder .github/workflows/ci.yml line 6 all features line 8")
        self.assertEqual(self.proposed(out), [".github/workflows/ci.yml#name: clippy (encoder)"], out)

    def test_a_definition_encloses_only_the_lines_indented_under_it(self):
        self.assertIsNone(lrp.enclosing(LIB.splitlines(), 11, "src/lib.rs"))  # after fn other, at its indent
        self.assertEqual(lrp.enclosing(LIB.splitlines(), 8, "src/lib.rs"), (6, "fn other"))

    def test_two_lines_of_a_list_on_one_construct_are_not_merged(self):
        # line 6 is the encoder step and line 7 sits in it: one anchor would drop a place the text names
        code, out = self.note("the clippy (encoder) step (.github/workflows/ci.yml lines 6 and 7)")
        self.assertEqual(self.proposed(out), [])
        self.assertIn("two of its lines resolve to one construct (.github/workflows/ci.yml#name: clippy (encoder))",
                      out)

    def test_a_line_inside_a_docstring_is_not_anchored(self):
        code, out = self.note("every clippy entry is planted (scripts/canary.py line 3)")
        self.assertIn("scripts/canary.py line 3 is inside a string (a docstring names no construct)", out)
        code, out = self.note("plant_entry plants a row (scripts/canary.py line 5)")
        self.assertEqual(self.proposed(out), ["scripts/canary.py#def plant_entry"])

    def test_a_drifted_line_is_not_anchored(self):
        # the text speaks of the encoder flag, line 2 is fn kernel: the number has moved
        code, out = self.note("the decoder flag (src/lib.rs:2)")
        self.assertEqual(self.proposed(out), [])
        self.assertIn("shares no word with the text before the reference (decoder, flag): the line may have drifted",
                      out)
        self.assertEqual(code, 1, out)  # references and nothing compared
        self.assertIn("no proposal was compared with its line", out)

    def test_a_range_is_refused(self):
        code, out = self.note("the kernel body (src/lib.rs lines 2 to 5)")
        self.assertIn("unresolved (a range names a span: anchor each place it means by hand)", out)
        code, out = self.note("the kernel body src/lib.rs#L2-L5")
        self.assertIn("a range names a span", out)

    def test_a_list_gets_one_anchor_per_line(self):
        code, out = self.note("kernel and other in src/lib.rs (lines 2 and 6)")
        self.assertEqual(self.proposed(out), ["src/lib.rs#fn kernel`, `src/lib.rs#fn other"])

    def test_comments_empty_lines_missing_paths_and_lines_past_the_end(self):
        code, out = self.note("the kernel comment (src/lib.rs:11)")
        self.assertEqual(self.proposed(out), [])  # a comment is not an anchor
        code, out = self.note("the kernel (src/lib.rs:10)")
        self.assertIn("src/lib.rs line 10 is empty (the reference has drifted)", out)
        code, out = self.note("the kernel at line 3")
        self.assertIn("no path names the file", out)
        code, out = self.note("the kernel (src/lib.rs:90)")
        self.assertIn("src/lib.rs has no line 90 (15 lines)", out)

    def test_a_hash_line_is_a_heading_only_in_markdown(self):
        code, out = self.note("determinism, bit-exact results (docs/DESIGN.md line 5)")
        self.assertEqual(self.proposed(out), ["docs/DESIGN.md### Determinism>result"])
        code, out = self.note("determinism (docs/DESIGN.md line 5)")
        self.assertEqual(self.proposed(out), ["docs/DESIGN.md### Determinism"])
        # in YAML `# the clippy step` is a comment, not a scope
        self.assertEqual(lrp.enclosing(CI.splitlines(), 5, ".github/workflows/ci.yml"), (3, "lint:"))
        self.assertEqual(lrp.enclosing(DOC.splitlines(), 5, "docs/DESIGN.md"), (3, "## Determinism"))
        self.assertIsNone(lrp.enclosing(DOC.splitlines(), 5, "notes.yml"))

    def test_other_extensions_bare_names_and_the_github_form(self):
        code, out = self.note("the sphere distance (shaders/sdf.wgsl:2)")
        self.assertEqual(self.proposed(out), ["shaders/sdf.wgsl#fn sdf_sphere"])  # the text names the function
        code, out = self.note("the sphere distance sdf.wgsl#L1")
        self.assertEqual(self.proposed(out), ["shaders/sdf.wgsl#fn sdf_sphere"])

    def test_every_proposal_passes_the_roster_rule_and_names_its_line(self):
        code, out = self.run_propose('[G1]\nstatus = "unverified"\nnote = "the kernel (src/lib.rs:2)"\n'
                                     'evidence = ["other clamps with clamp_unit (src/lib.rs line 7)", '
                                     '"the clippy job (.github/workflows/ci.yml line 7)"]\n')
        d = Path(self.tmp.name) / "crate"
        text, paths, read = crc.git_fetcher(d, "HEAD")()
        got = self.proposed(out)
        self.assertEqual(len(got), 3, out)
        for anchor in got:
            path, token = anchor.split("#", 1)
            self.assertIsNone(crc.anchor_gap(path, token, paths, read))
        self.assertIn("3 proposed (3 anchors compared with their lines)", out)

    def test_no_reference_and_no_manifest(self):
        code, out = self.note("src/lib.rs#kernel")
        self.assertEqual(code, 0, out)
        self.assertIn("0 reference(s), 0 proposed (0 anchors compared with their lines), 0 unresolved", out)
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        code, out = self.run_propose(None)
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
