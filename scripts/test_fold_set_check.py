#!/usr/bin/env python3
"""Tests of scripts/fold_set_check.py on throwaway crates. Each fixture's expected set is written by hand from what the
fixture's source calls, never from running the tool. function-paths.toml here is a small stand-in with the three shapes
(a path, a type, a no-arithmetic item)."""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fold_set_check as fs  # noqa: E402

TABLE = '''[paths]
"sin" = "sin"
"cos" = "cos"
"single::sin" = "sin"
"metric::norm_l2" = "norm_l2"
"metric::MetricWeights::new" = "metric_new"
"metric::MetricWeights::norm" = "metric_norm"
"simd::sin_cos" = "sin_cos"

[types]
"metric::MetricWeights" = ["metric_new", "metric_norm"]

[no_arithmetic]
"law_bits::f32_bits" = "returns the bits"
'''
DEP = '[package]\nname = "x"\nversion = "0.1.0"\n[dependencies]\nalice-det-math = "0.6"\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="foldset-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.paths = self.d / "function-paths.toml"
        self.paths.write_text(TABLE, encoding="utf-8")
        self.root = self.d / "crate"
        self.root.mkdir()
        self.t = fs.load_table(self.paths)

    def crate(self, src: str, manifest: str = DEP, extra: dict[str, str] | None = None) -> None:
        for rel, text in {"Cargo.toml": manifest, "src/lib.rs": src, **(extra or {})}.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

    def found(self, src: str, **kw) -> fs.Found:
        self.crate(src, **kw)
        return fs.scan(self.root, self.t)

    def pins(self, src: str, **kw) -> set[str]:
        f = self.found(src, **kw)
        self.assertEqual(f.findings, [])
        return f.pins

    def main(self, cmd: str) -> tuple[int, str]:
        with contextlib.redirect_stdout(io.StringIO()) as o, contextlib.redirect_stderr(io.StringIO()) as e:
            rc = fs.main([cmd, str(self.root), "--paths", str(self.paths)])
        return rc, o.getvalue() + e.getvalue()


class Forms(Base):
    def test_a_path(self):
        self.assertEqual(self.pins("fn f(x: f32) -> f32 { alice_det_math::sin(x) }"), {"sin"})
        self.assertEqual(self.pins("fn f(x: f32) -> f32 { ::alice_det_math::single::sin(x) }"), {"sin"})

    def test_a_use_tree_with_alias_and_self(self):
        src = "use alice_det_math::{sin as s, cos, metric::{self, norm_l2}};\nfn f() { metric::MetricWeights::new(); }"
        self.assertEqual(self.pins(src), {"sin", "cos", "norm_l2", "metric_new", "metric_norm"})

    def test_a_reexport_counts(self):
        self.assertEqual(self.pins("pub use alice_det_math::{sin, cos};"), {"sin", "cos"})
        self.assertEqual(self.pins("pub(crate) use alice_det_math::sin;"), {"sin"})

    def test_a_module_import_resolves_the_file_s_paths(self):
        self.assertEqual(self.pins("use alice_det_math::metric;\nfn f() { metric::norm_l2(); }"), {"norm_l2"})
        self.assertEqual(self.pins("use alice_det_math::simd as v;\nfn f() { v::sin_cos(); }"), {"sin_cos"})

    def test_a_type_counts_all_its_methods(self):
        self.assertEqual(self.pins("use alice_det_math::metric::MetricWeights;"), {"metric_new", "metric_norm"})
        self.assertEqual(self.pins("fn f(w: alice_det_math::metric::MetricWeights) {}"), {"metric_new", "metric_norm"})

    def test_a_method_path_counts_all_the_type_s_methods(self):
        # `w.norm(..)` on the value `new` returns names no path
        src = "fn f() { let w = alice_det_math::metric::MetricWeights::new(); w.norm(1.0); }"
        self.assertEqual(self.pins(src), {"metric_new", "metric_norm"})

    def test_a_renamed_dependency_and_a_file_alias(self):
        renamed = DEP.replace('alice-det-math = "0.6"', 'dm-core = { package = "alice-det-math", version = "0.6" }')
        self.assertEqual(self.pins("fn f() { dm_core::sin(); }", manifest=renamed), {"sin"})
        self.assertEqual(self.pins("use alice_det_math as dm;\nfn f() { dm::cos(); }"), {"cos"})

    def test_a_turbofish_ends_the_path(self):
        self.assertEqual(self.pins("fn f() { alice_det_math::sin::<f32>(1.0); }"), {"sin"})

    def test_identifier_items_are_apart(self):
        f = self.found("const A: [u8; 32] = alice_det_math::SEMANTICS_ID;\nuse alice_det_math::law_bits::f32_bits;")
        self.assertEqual((f.pins, f.findings), (set(), []))
        self.assertEqual(f.identifier, {"SEMANTICS_ID", "law_bits::f32_bits"})

    def test_what_is_not_the_crate_s_arithmetic_is_not_read(self):
        src = '''// alice_det_math::sin
/* alice_det_math::sin /* nested */ alice_det_math::sin */
/// alice_det_math::sin
const A: &str = "alice_det_math::sin";
const B: &str = r#"alice_det_math::sin "# ;
const C: char = '"';
fn lifetime<'a>(x: &'a str) -> &'a str { x }
fn other() { other_crate::alice_det_math::sin(); }
#[cfg(test)]
mod tests { fn f() { alice_det_math::sin(1.0); } }
#[cfg(test)]
use alice_det_math::sin;
fn real() { alice_det_math::cos(1.0); }
'''
        self.assertEqual(self.pins(src, extra={"tests/t.rs": "fn f() { alice_det_math::sin(); }",
                                               "benches/b.rs": "fn f() { alice_det_math::sin(); }",
                                               "examples/e.rs": "fn f() { alice_det_math::sin(); }"}), {"cos"})

    def test_a_fixture_crate_under_tests_is_not_read(self):
        self.assertEqual(self.pins("fn f() { alice_det_math::cos(); }",
                                   extra={"tests/fixture/Cargo.toml": DEP,
                                          "tests/fixture/src/lib.rs": "fn f() { alice_det_math::sin(); }"}), {"cos"})

    def test_a_macro_body_is_read(self):
        self.assertEqual(self.pins("macro_rules! m { () => { alice_det_math::sin(1.0) } }"), {"sin"})

    def test_every_package_of_a_workspace_is_read(self):
        self.crate("", manifest='[workspace]\nmembers = ["a"]\n[package]\nname = "r"\nversion = "0.1.0"\n',
                   extra={"a/Cargo.toml": DEP, "a/src/lib.rs": "fn f() { alice_det_math::sin(); }"})
        self.assertEqual(fs.scan(self.root, self.t).pins, {"sin"})


class NotUnderstood(Base):
    """each form is a finding, not a skip"""

    def assert_finding(self, src: str, text: str) -> None:
        f = self.found(src)
        self.assertTrue(any(text in x for x in f.findings), f.findings)

    def test_a_glob(self):
        self.assert_finding("use alice_det_math::*;", "a glob import")
        self.assert_finding("use alice_det_math::metric::*;", "a glob import")

    def test_a_public_alias(self):
        self.assert_finding("pub use alice_det_math as dm;", "public")
        self.assert_finding("pub(crate) use alice_det_math as dm;", "public")

    def test_an_alias_inside_a_module(self):
        self.assert_finding("mod a { use alice_det_math as dm; }", "inside a module")

    def test_a_path_through_a_macro_variable(self):
        self.assert_finding("macro_rules! m { ($f:ident) => { alice_det_math::$f(1.0) } }", "macro variable")
        self.assert_finding("use alice_det_math::metric;\nmacro_rules! m { ($f:ident) => { metric::$f(1.0) } }",
                            "macro variable")

    def test_a_path_naming_nothing(self):
        self.assert_finding("fn f() { alice_det_math::no_such(); }", "names nothing")
        self.assert_finding("const A: u32 = alice_det_math::consts::SPLIT_MASK;", "names nothing")

    def test_an_unterminated_raw_string(self):
        self.assert_finding('const A: &str = r#"never closed;', "unterminated")


class ResolvedTable(Base):
    """without --paths the table comes from the alice-det-math cargo metadata resolves"""

    def metadata(self, rc: int, packages: list[tuple[str, str]], stderr: str = "") -> None:
        import json
        import subprocess
        real = fs.subprocess.run
        out = json.dumps({"packages": [{"name": n, "manifest_path": m} for n, m in packages]})
        fs.subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(argv, rc, out, stderr)
        self.addCleanup(setattr, fs.subprocess, "run", real)

    def test_one_det_math_gives_its_table(self):
        self.metadata(0, [("alice-det-math", "/x/dm/Cargo.toml"), ("other", "/x/o/Cargo.toml")])
        self.assertEqual(fs.resolved_table(self.root), Path("/x/dm/function-paths.toml"))

    def test_two_or_none_is_an_error(self):
        self.metadata(0, [("alice-det-math", "/a/Cargo.toml"), ("alice-det-math", "/b/Cargo.toml")])
        with self.assertRaisesRegex(fs.InputError, "2 alice-det-math in the graph"):
            fs.resolved_table(self.root)
        self.metadata(0, [("other", "/x/Cargo.toml")])
        with self.assertRaisesRegex(fs.InputError, "0 alice-det-math in the graph"):
            fs.resolved_table(self.root)

    def test_a_failed_cargo_is_an_error(self):
        self.metadata(101, [], "no such manifest")
        with self.assertRaisesRegex(fs.InputError, "no such manifest"):
            fs.resolved_table(self.root)


class Check(Base):
    DECL = 'pub const DET_MATH_FUNCTIONS: &[&str] = &["sin"];\nfn f() { alice_det_math::sin(); }'

    def test_the_declared_set_matches(self):
        self.crate(self.DECL)
        self.assertEqual(self.main("check")[0], 0)

    def test_called_and_not_declared(self):
        self.crate(self.DECL + "\nfn g() { alice_det_math::cos(); }")
        rc, out = self.main("check")
        self.assertEqual(rc, 1)
        self.assertIn("cos is called but is not in DET_MATH_FUNCTIONS", out)

    def test_declared_and_not_called(self):
        self.crate('pub const DET_MATH_FUNCTIONS: &[&str] = &["sin", "cos"];\nfn f() { alice_det_math::sin(); }')
        rc, out = self.main("check")
        self.assertEqual(rc, 1)
        self.assertIn("cos is in DET_MATH_FUNCTIONS but is not called", out)

    def test_generated_names_join_the_called_set(self):
        src = 'pub const DET_MATH_FUNCTIONS: &[&str] = &["sin", "cos"];\nfn f() { alice_det_math::sin(); }'
        self.crate(src, extra={".github/det-math-fold.toml": 'generated = ["cos"]\n'})
        self.assertEqual(self.main("check")[0], 0)

    def test_no_declaration_is_a_problem(self):
        self.crate("fn f() { alice_det_math::sin(); }")
        rc, out = self.main("check")
        self.assertEqual(rc, 1)
        self.assertIn("no DET_MATH_FUNCTIONS", out)

    def test_a_finding_fails_check_and_print_does_not(self):
        self.crate(self.DECL + "\nuse alice_det_math::*;")
        self.assertEqual(self.main("check")[0], 1)
        self.assertEqual(self.main("print")[0], 0)

    def test_nothing_compared_fails(self):
        self.crate('pub const DET_MATH_FUNCTIONS: &[&str] = &[];\nfn f() {}')
        rc, out = self.main("check")
        self.assertEqual(rc, 1)
        self.assertIn("compared nothing", out)

    def test_no_rust_source_and_no_table_are_input_errors(self):
        self.assertEqual(self.main("print")[0], 2)
        self.crate("fn f() {}")
        self.paths.unlink()
        rc, out = self.main("print")
        self.assertEqual(rc, 2)
        self.assertIn("function-paths.toml", out)


if __name__ == "__main__":
    unittest.main()
