#!/usr/bin/env python3
"""Tests for scripts/g1_clippy_block.py: the generated G1 region inside each clippy configuration.

* a repository without a configuration gets a root clippy.toml with the region, and --check then passes;
* a configuration with other keys, comments and other entries keeps every other line byte for byte; a G1 entry
  outside the region is moved into it, never duplicated; a non-G1 f64 entry (max) stays; the edit is idempotent;
* every configuration found by walking is covered (.github/clippy-lib/ included, tests/ fixtures and target/ not);
* --check: a stale region is exit 1, no configuration is exit 2;
* unsafe layouts (an inline array, two entries on one line) are exit 2 with the file untouched.

Usage: python3 scripts/test_g1_clippy_block.py
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import g1_clippy_block as gb  # noqa: E402

SCRATCH = tempfile.mkdtemp(prefix="g1-block-test-")
METHODS = gb.g1_methods()
G1 = {f"{w}::{n}" for n, _ in METHODS for w in ("f32", "f64")}

OWN = '''# the repository's own header comment
msrv = "1.80"
disallowed-methods = [
    # clock sources
    { path = "std::time::Instant::now", reason = "pass time in" },
    { path = "f64::sin", reason = "an old G1 line written by hand" },
    { path = "f64::max", reason = "G2: NaN and signed zero" },
]
disallowed-types = [
    { path = "std::collections::hash_map::RandomState", reason = "OS randomness" },
]
'''


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


def tree(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp(dir=SCRATCH))
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    return d


def run(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = gb.main(list(args))
    return code, out.getvalue()


def paths(text: str) -> list[str]:
    return [e["path"] if isinstance(e, dict) else e for e in tomllib.loads(text).get("disallowed-methods", [])]


class Write(unittest.TestCase):
    def test_a_repository_without_a_configuration_gets_one(self):
        d = tree({"src/lib.rs": "pub fn f() {}\n"})
        self.assertEqual(run("--write", str(d))[0], 0)
        text = (d / "clippy.toml").read_text(encoding="utf-8")
        self.assertEqual(set(paths(text)), G1)
        self.assertEqual(run("--check", str(d))[0], 0)

    def test_other_lines_stay_and_g1_moves_into_the_region(self):
        d = tree({"clippy.toml": OWN})
        self.assertEqual(run("--write", str(d))[0], 0)
        text = (d / "clippy.toml").read_text(encoding="utf-8")
        p = paths(text)
        self.assertEqual(len(p), len(set(p)), "a path named twice")
        self.assertTrue(G1 <= set(p))
        self.assertIn("f64::max", p)
        self.assertIn("std::time::Instant::now", p)
        self.assertEqual(tomllib.loads(text)["msrv"], "1.80")
        # every line outside the region is an original line, in order, except the hand-written G1 one
        body = text.splitlines()
        b, e = body.index("    " + gb.BEGIN), body.index("    " + gb.END)
        outside = body[:b] + body[e + 1:]
        original = [ln for ln in OWN.splitlines() if '"f64::sin"' not in ln]
        self.assertEqual(outside, original)
        # idempotent
        self.assertEqual(run("--write", str(d))[0], 0)
        self.assertEqual((d / "clippy.toml").read_text(encoding="utf-8"), text)

    def test_every_configuration_found_by_walking(self):
        d = tree({"clippy.toml": OWN, ".github/clippy-lib/clippy.toml": OWN,
                  "tests/fixtures/clippy.toml": "msrv = \"1.0\"\n", "target/clippy.toml": "msrv = \"1.0\"\n"})
        code, out = run("--write", str(d))
        self.assertEqual(code, 0, out)
        for rel in ("clippy.toml", ".github/clippy-lib/clippy.toml"):
            self.assertTrue(G1 <= set(paths((d / rel).read_text(encoding="utf-8"))), rel)
        for rel in ("tests/fixtures/clippy.toml", "target/clippy.toml"):
            self.assertEqual((d / rel).read_text(encoding="utf-8"), 'msrv = "1.0"\n', rel)


class Check(unittest.TestCase):
    def test_a_stale_region_differs(self):
        d = tree({"clippy.toml": OWN})
        run("--write", str(d))
        f = d / "clippy.toml"
        lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
        f.write_text("".join(ln for ln in lines if '"f32::cos"' not in ln), encoding="utf-8")
        self.assertEqual(run("--check", str(d))[0], 1)

    def test_no_configuration_compares_nothing(self):
        self.assertEqual(run("--check", str(tree({"src/lib.rs": ""})))[0], 2)


class Unsafe(unittest.TestCase):
    def assert_untouched(self, text: str):
        d = tree({"clippy.toml": text})
        code, _ = run("--write", str(d))
        self.assertEqual(code, 2)
        self.assertEqual((d / "clippy.toml").read_text(encoding="utf-8"), text)

    def test_an_inline_array_is_refused(self):
        self.assert_untouched('disallowed-methods = ["std::time::Instant::now"]\n')

    def test_two_entries_on_one_line_are_refused(self):
        self.assert_untouched('disallowed-methods = [\n    "f64::sin", "f64::cos",\n]\n')


if __name__ == "__main__":
    unittest.main(verbosity=1)
