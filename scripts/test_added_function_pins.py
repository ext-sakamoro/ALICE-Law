#!/usr/bin/env python3
"""Tests for scripts/added_function_pins.py on a made-up alice-det-math and made-up crates.

* a crate that calls an added function and folds its pin (DET_MATH_FUNCTIONS + the assert_function_pins guard) passes;
* one that calls it with no DET_MATH_FUNCTIONS, with a DET_MATH_FUNCTIONS that lacks it, or without the guard fails;
* a crate that calls only frozen functions needs nothing;
* a det-math without ADDED_FUNCTIONS, and nothing read that uses alice_det_math, are exit 2.

Usage: python3 scripts/test_added_function_pins.py
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import added_function_pins as afp  # noqa: E402

SCRATCH = tempfile.mkdtemp(prefix="added-pins-test-")

TABLE = '''[paths]
"powi64" = "powi64"
"double::powi64" = "powi64"
"sin" = "sin"

[types]

[consts]

[no_arithmetic]
'''
MANIFEST = '[package]\nname = "made-up"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\nalice-det-math = "0.6"\n'
GUARD = "const _: () = alice_det_math::assert_function_pins(DET_MATH_PINS);\n"


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


def tree(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp(dir=SCRATCH))
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    return d


def det_math(added: str | None = '"powi64"') -> Path:
    sem = "pub const SEMANTICS_ID: [u8; 32] = [0; 32];\n"
    if added is not None:
        sem += f"pub const ADDED_FUNCTIONS: &[&str] = &[{added}];\n"
    return tree({"function-paths.toml": TABLE, "src/semantics.rs": sem})


def run(crate: Path, dm: Path) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = afp.main(["--only", f"ALICE-Made-Up={crate}", "--det-math", str(dm)])
    return code, out.getvalue()


CALL = "use alice_det_math::powi64;\npub fn f(x: f64) -> f64 { powi64(x, 2) }\n"


class Rules(unittest.TestCase):
    def test_a_crate_that_folds_the_added_pin_passes(self):
        lib = CALL + 'pub const DET_MATH_FUNCTIONS: &[&str] = &["powi64"];\n' + GUARD
        code, out = run(tree({"Cargo.toml": MANIFEST, "src/lib.rs": lib}), det_math())
        self.assertEqual(code, 0, out)

    def test_no_det_math_functions_fails(self):
        code, out = run(tree({"Cargo.toml": MANIFEST, "src/lib.rs": CALL}), det_math())
        self.assertEqual(code, 1, out)
        self.assertIn("calls powi64", out)

    def test_a_list_without_the_added_function_fails(self):
        lib = CALL + "use alice_det_math::sin;\npub fn g(x: f32) -> f32 { sin(x) }\n" \
              + 'pub const DET_MATH_FUNCTIONS: &[&str] = &["sin"];\n' + GUARD
        code, out = run(tree({"Cargo.toml": MANIFEST, "src/lib.rs": lib}), det_math())
        self.assertEqual(code, 1, out)
        self.assertIn("calls powi64", out)

    def test_no_guard_fails(self):
        lib = CALL + 'pub const DET_MATH_FUNCTIONS: &[&str] = &["powi64"];\n'
        code, out = run(tree({"Cargo.toml": MANIFEST, "src/lib.rs": lib}), det_math())
        self.assertEqual(code, 1, out)
        self.assertIn("assert_function_pins", out)

    def test_frozen_functions_only_need_nothing(self):
        lib = "use alice_det_math::sin;\npub fn g(x: f32) -> f32 { sin(x) }\n"
        code, out = run(tree({"Cargo.toml": MANIFEST, "src/lib.rs": lib}), det_math())
        self.assertEqual(code, 0, out)


class Inputs(unittest.TestCase):
    def test_a_det_math_without_the_list_is_exit_2(self):
        code, _ = run(tree({"Cargo.toml": MANIFEST, "src/lib.rs": CALL}), det_math(added=None))
        self.assertEqual(code, 2)

    def test_no_consumer_read_is_exit_2(self):
        crate = tree({"Cargo.toml": MANIFEST.replace("alice-det-math", "serde"), "src/lib.rs": "pub fn f() {}\n"})
        self.assertEqual(run(crate, det_math())[0], 2)

    def test_the_added_list_is_read_from_the_source(self):
        self.assertEqual(afp.added_functions(det_math('"powi64", "made_up64"')), ["powi64", "made_up64"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
