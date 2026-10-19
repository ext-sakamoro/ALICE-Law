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
LIB = ("//! the crate\n"                       # 1
       "pub fn kernel_unique(x: f32) -> f32 {\n"  # 2
       "    x * 2.0\n"                         # 3
       "}\n"                                   # 4
       "pub fn other(x: f32) -> f32 {\n"       # 5
       "    x * 2.0\n"                         # 6: the same line as 3
       "}\n")                                  # 7


class Propose(unittest.TestCase):
    def repo(self, manifest: str | None) -> Path:
        d = Path(self.tmp.name) / "crate"
        d.mkdir()
        (d / "src").mkdir()
        (d / "src" / "lib.rs").write_text(LIB, encoding="utf-8")
        if manifest is not None:
            (d / crc.MANIFEST).write_text(manifest, encoding="utf-8")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "c"]):
            subprocess.run(["git", *args], cwd=d, env=ENV, check=True, capture_output=True)
        return d

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def run_propose(self, manifest: str | None) -> tuple[int, str]:
        d = self.repo(manifest)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = lrp.main(["--repo", str(d)])
        return code, out.getvalue()

    def test_a_line_with_a_unique_token_gets_that_token(self):
        code, out = self.run_propose('[G1]\nstatus = "pass"\nevidence = ["src/lib.rs:2 the kernel"]\n')
        self.assertEqual(code, 0, out)
        self.assertIn("G1 evidence: `src/lib.rs:2` -> `src/lib.rs#kernel_unique`", out)
        self.assertIn("1 reference(s), 1 proposed, 0 unresolved", out)

    def test_a_repeated_line_is_scoped_under_its_definition(self):
        code, out = self.run_propose('[G1]\nnote = "src/lib.rs (line 6) doubles it"\n')
        self.assertEqual(code, 0, out)
        self.assertIn("-> `src/lib.rs#fn other>x * 2.0`", out)
        # and the proposal passes the roster's own anchor rule
        anchor = out.split("-> `")[1].split("`")[0]
        path, token = anchor.split("#", 1)
        d = Path(self.tmp.name) / "crate"
        text, paths, read = crc.git_fetcher(d, "HEAD")()
        self.assertIsNone(crc.anchor_gap(path, token, paths, read))

    def test_a_reference_with_no_path_or_past_the_file_is_unresolved(self):
        code, out = self.run_propose('[G1]\nnote = "see line 3"\n[G2]\nnote = "src/lib.rs:90"\n')
        self.assertEqual(code, 0, out)
        self.assertIn("G1 note: `line 3`: unresolved (no path names the file)", out)
        self.assertIn("G2 note: `src/lib.rs:90`: unresolved (src/lib.rs has no line 90 (7 lines))", out)
        self.assertIn("2 reference(s), 0 proposed, 2 unresolved", out)

    def test_a_range_is_flagged_as_naming_its_first_line(self):
        code, out = self.run_propose('[G1]\nnote = "src/lib.rs (lines 2 to 4)"\n')
        self.assertIn("(names the first line; a range or list needs one anchor per place)", out)

    def test_no_reference_and_no_manifest(self):
        code, out = self.run_propose('[G1]\nnote = "src/lib.rs#kernel_unique"\n')
        self.assertEqual(code, 0, out)
        self.assertIn("0 reference(s), 0 proposed, 0 unresolved", out)
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        code, out = self.run_propose(None)
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
