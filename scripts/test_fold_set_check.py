#!/usr/bin/env python3
"""Tests of scripts/fold_set_check.py on throwaway crates. Each fixture's expected set is written by hand from what the
fixture's source calls, never from running the tool. function-paths.toml here is a small stand-in with the four shapes
(a path, a type, a kernel constant, a no-arithmetic item)."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
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

[consts]
"consts::SIN_P" = ["cos", "sin", "sin_cos"]

[no_arithmetic]
"law_bits::f32_bits" = "returns the bits"
'''
DEP = '[package]\nname = "x"\nversion = "0.1.0"\n[dependencies]\nalice-det-math = "0.6"\n'


GUARD = 'const _: () = alice_det_math::assert_function_pins(DET_MATH_PINS);\n'


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

    def test_a_path_through_crate_self_or_super_is_followed(self):
        for prefix in ("crate", "self", "super"):
            self.assertEqual(self.pins(f"fn f() {{ {prefix}::alice_det_math::cos(1.0); }}"), {"cos"}, prefix)

    def test_a_use_tree_with_alias_and_self(self):
        src = "use alice_det_math::{sin as s, cos, metric::{self, norm_l2}};\nfn f() { metric::MetricWeights::new(); }"
        self.assertEqual(self.pins(src), {"sin", "cos", "norm_l2", "metric_new", "metric_norm"})
        # `metric::{self, ..}` is a module import: all of metric, whatever the file then calls
        self.assertEqual(self.pins("use alice_det_math::metric::{self};"), {"norm_l2", "metric_new", "metric_norm"})

    def test_a_reexport_counts(self):
        self.assertEqual(self.pins("pub use alice_det_math::{sin, cos};"), {"sin", "cos"})
        self.assertEqual(self.pins("pub(crate) use alice_det_math::sin;"), {"sin"})

    def test_a_module_import_counts_the_whole_module(self):
        # its names reach other files, so what one file calls through it is not the whole answer
        self.assertEqual(self.pins("use alice_det_math::metric;\nfn f() { metric::norm_l2(); }"),
                         {"norm_l2", "metric_new", "metric_norm"})
        self.assertEqual(self.pins("pub use alice_det_math::simd as v;"), {"sin_cos"})

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

    def test_the_pin_guard_is_an_identifier_item(self):
        f = self.found("const _: () = alice_det_math::assert_function_pins(DET_MATH_PINS);\n")
        self.assertEqual(f.identifier, {"assert_function_pins"})
        self.assertEqual(f.findings, [])

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

    def test_cfg_test_ends_at_its_own_item(self):
        # a `;` inside an array type is not the end of the item, and what follows the item is read
        src = '#[cfg(test)]\nconst A: [u8; 2] = [alice_det_math::sin as u8, 0];\nfn f() { alice_det_math::cos(); }'
        self.assertEqual(self.pins(src), {"cos"})
        src = '#[cfg(test)]\n#[allow(unused)]\npub fn g() -> [u8; 2] { alice_det_math::sin(); [0, 0] }\nfn f() { alice_det_math::cos(); }'
        self.assertEqual(self.pins(src), {"cos"})

    def test_cfg_test_on_a_public_item_is_removed(self):
        self.assertEqual(self.pins('#[cfg(test)]\npub fn g() { alice_det_math::sin(); }\n#[cfg(test)]\npub(crate) mod h { fn i() { alice_det_math::sin(); } }\nfn f() { alice_det_math::cos(); }'), {"cos"})

    def test_cfg_test_on_a_macro_item_removes_it_and_only_directly_under_it(self):
        src = "#[cfg(test)]\nthread_local! { static A: f32 = alice_det_math::sin(1.0); }\nfn f() { alice_det_math::cos(); }"
        self.assertEqual(self.pins(src), {"cos"})
        src = "#[cfg(test)]\nm!(alice_det_math::sin(1.0));\nfn f() { alice_det_math::cos(); }"
        self.assertEqual(self.pins(src), {"cos"})
        self.assertEqual(self.pins("thread_local! { static A: f32 = alice_det_math::sin(1.0); }"), {"sin"})

    def test_cfg_test_on_a_statement_keeps_it_in_the_read_without_a_finding(self):
        src = "fn f() {\n#[cfg(test)]\nalice_det_math::sin(1.0);\nalice_det_math::cos(1.0);\n}"
        self.assertEqual(self.pins(src), {"sin", "cos"})
        self.assertEqual(self.pins("fn f() {\n#[cfg(test)]\n{ alice_det_math::sin(1.0); }\n}"), {"sin"})

    def test_a_path_attribute_inside_the_repository_is_read(self):
        extra = {"shared/x.rs": "pub fn g() { alice_det_math::sin(1.0); }\n"}
        self.assertEqual(self.pins('#[path = "../shared/x.rs"]\nmod x;\nfn f() { alice_det_math::cos(); }', extra=extra),
                         {"sin", "cos"})

    def test_a_path_attribute_on_a_test_item_is_not_read(self):
        extra = {"shared/x.rs": "pub fn g() { alice_det_math::sin(1.0); }\n"}
        src = '#[cfg(test)]\n#[path = "../shared/x.rs"]\nmod x;\nfn f() { alice_det_math::cos(); }'
        self.assertEqual(self.pins(src, extra=extra), {"cos"})

    def test_path_attributes_that_name_each_other_end(self):
        extra = {"shared/a.rs": '#[path = "b.rs"]\nmod b;\npub fn g() { alice_det_math::sin(1.0); }\n',
                 "shared/b.rs": '#[path = "a.rs"]\nmod a;\npub fn h() { alice_det_math::cos(1.0); }\n'}
        self.assertEqual(self.pins('#[path = "../shared/a.rs"]\nmod a;', extra=extra), {"sin", "cos"})
        self.assertEqual(self.found('#[path = "../shared/a.rs"]\nmod a;', extra=extra).files, 3)

    def test_a_workspace_whose_members_name_the_root_or_a_glob(self):
        member = '[package]\nname = "e"\nversion = "0.1.0"\n[dependencies]\nalice-det-math = "0.6"\n'
        self.assertEqual(self.pins("fn f() { alice_det_math::cos(); }", manifest=DEP + '[workspace]\nmembers = [".", "examples/*"]\n',
                                   extra={"examples/a/Cargo.toml": member, "examples/a/src/lib.rs": "fn g() { alice_det_math::sin(); }"}),
                         {"cos", "sin"})

    def test_a_plain_mod_in_src_needs_no_lookup(self):
        # everything under src/ is read anyway; a mod generated at build time has no file there
        self.assertEqual(self.pins("mod generated;\nfn f() { alice_det_math::cos(); }"), {"cos"})

    def test_what_must_stay_clean(self):
        # prose and data that name the crate; an array of a qualified type is not paste
        src = ('#[doc = "see alice_det_math::cos"]\n/// alice_det_math::cos\npub const S: &str = "alice_det_math::cos";\n'
               "pub const N: [u8; <u8 as T>::N] = [0; <u8 as T>::N];\npub const M: [<u8 as T>::X; 1] = [X];\n"
               "#[cfg(test)]\nmod t { fn a() { alice_det_math::cos(1.0); } }\nfn f() { alice_det_math::sin(1.0); }")
        self.assertEqual(self.pins(src), {"sin"})
        self.assertEqual(self.pins('#[path = "alt.rs"] mod alt;', extra={"src/alt.rs": "fn g() { alice_det_math::cos(); }"}), {"cos"})

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

    def test_a_path_naming_nothing(self):
        self.assert_finding("fn f() { alice_det_math::no_such(); }", "names nothing")
        # a constant without a [consts] row (the stand-in table maps SIN_P only)
        self.assert_finding("const A: u32 = alice_det_math::consts::SPLIT_MASK;", "names nothing")

    def test_a_kernel_constant_counts_as_the_functions_whose_law_reads_it(self):
        # a JIT emitting sin_cos from the constants: it folds the pins of the kernel's functions
        f = self.found("fn emit() -> [f32; 3] { alice_det_math::consts::SIN_P }")
        self.assertEqual(f.findings, [])
        self.assertEqual(f.pins, {"cos", "sin", "sin_cos"})

    def test_a_table_without_consts_still_loads_and_leaves_constants_unresolved(self):
        old = self.d / "old-paths.toml"
        old.write_text(TABLE.replace('[consts]\n"consts::SIN_P" = ["cos", "sin", "sin_cos"]\n\n', ""), encoding="utf-8")
        t = fs.load_table(old)
        self.assertEqual(t.consts, {})
        self.crate("fn emit() -> [f32; 3] { alice_det_math::consts::SIN_P }")
        f = fs.scan(self.root, t)
        self.assertTrue(any("names nothing" in x for x in f.findings), f.findings)

    def test_a_self_alias_of_the_crate(self):
        self.assert_finding("use alice_det_math::{self as dm};", "an alias of the crate")

    def test_a_plain_mod_with_no_file_below_a_path_file(self):
        f = self.found('#[path = "../shared/x.rs"] mod x;', extra={"shared/x.rs": "pub mod gone;\n"})
        self.assertTrue(any("no file found" in x for x in f.findings), f.findings)

    def test_a_path_attribute_it_cannot_read(self):
        self.assert_finding('#[path = "../../outside.rs"]\nmod x;', "outside the repository")
        self.assert_finding('#[path = "../missing.rs"]\nmod x;', "names no file")
        self.assert_finding('#[path = concat!("a", ".rs")]\nmod x;', "not a plain string")
        self.assert_finding('#[cfg_attr(unix, path = "a.rs")]\nmod x;', "cfg_attr")
        # inside an inline module rustc's base directory differs: the file next to this one is not what it reads
        f = self.found('mod inner {\n#[path = "x.rs"]\nmod x;\n}', extra={"src/x.rs": "fn g() { alice_det_math::sin(1.0); }\n"})
        self.assertTrue(any("inside a block or an inline module" in x for x in f.findings), f.findings)

    def test_include(self):
        self.assert_finding('include!("../gen.rs");', "include!")

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


class Silent(Base):
    """each really calls sin (or sin64); only tan is declared. Exit 0 here is a call missed in silence"""

    ANCHOR = GUARD + 'pub const DET_MATH_FUNCTIONS: &[&str] = &["tan"];\npub fn anchor(x: f32) -> f32 { alice_det_math::tan(x) }\n'
    TABLE_MORE = '"tan" = "tan"\n"double::sin64" = "sin64"\n'

    def setUp(self):
        super().setUp()
        self.paths.write_text(TABLE.replace("[paths]\n", "[paths]\n" + self.TABLE_MORE), encoding="utf-8")

    def red(self, lib: str, extra: dict[str, str] | None = None, manifest: str = DEP) -> None:
        self.crate(self.ANCHOR + lib, manifest=manifest, extra=extra)
        rc, out = self.main("check")
        self.assertNotEqual(rc, 0, "a real call was missed:\n" + out)

    def test_pub_use_of_a_module_used_in_another_file(self):
        self.red("pub use alice_det_math::double;\nmod a;\n", {"src/a.rs": "pub fn f(x: f64) -> f64 { crate::double::sin64(x) }\n"})

    def test_a_module_re_imported_in_another_file(self):
        self.red("use alice_det_math::double;\nmod a;\n", {"src/a.rs": "use super::double; pub fn f(x: f64) -> f64 { double::sin64(x) }\n"})

    def test_pub_use_of_the_crate_used_through_crate(self):
        self.red("pub use alice_det_math;\nmod a;\n", {"src/a.rs": "pub fn f(x: f32) -> f32 { crate::alice_det_math::sin(x) }\n"})

    def test_a_path_through_self(self):
        self.red("use alice_det_math;\npub fn f(x: f32) -> f32 { self::alice_det_math::sin(x) }\n")

    def test_extern_crate_alias(self):
        self.red("extern crate alice_det_math as dm;\npub fn f(x: f32) -> f32 { dm::sin(x) }\n")

    def test_use_braces_alias_of_the_crate(self):
        self.red("use {alice_det_math as dm};\npub fn f(x: f32) -> f32 { dm::sin(x) }\n")

    def test_an_alias_after_its_use(self):
        self.red("pub fn f(x: f32) -> f32 { dm::sin(x) }\nuse alice_det_math as dm;\n")

    def test_a_module_import_after_its_use(self):
        self.red("pub fn f(x: f64) -> f64 { double::sin64(x) }\nuse alice_det_math::double;\n")

    def test_an_alias_from_another_file(self):
        self.red("mod a;\nuse alice_det_math as dm;\n", {"src/a.rs": "use super::dm; pub fn f(x: f32) -> f32 { dm::sin(x) }\n"})

    def test_a_macro_taking_the_crate_and_the_function_as_identifiers(self):
        self.red("macro_rules! s { ($c:ident, $f:ident, $x:expr) => { $c::$f($x) } }\n"
                 "pub fn f(x: f32) -> f32 { s!(alice_det_math, sin, x) }\n")

    def test_cfg_test_on_a_field(self):
        self.red("pub struct S { #[cfg(test)] pub a: u8, pub b: u8 }\npub fn f(x: f32) -> f32 { alice_det_math::sin(x) }\n")

    def test_cfg_test_on_a_match_arm(self):
        self.red("pub fn f(n: u8, x: f32) -> f32 { match n { #[cfg(test)] 0 => 0.0, _ => alice_det_math::sin(x), } }\n")

    def test_cfg_test_on_a_parameter(self):
        self.red("pub fn f(#[cfg(test)] a: u8, x: f32) -> f32 { alice_det_math::sin(x) }\n")

    def test_a_path_attribute(self):
        self.red('#[path = "../shared/x.rs"] mod x;\n', {"shared/x.rs": "pub fn f(a: f32) -> f32 { alice_det_math::sin(a) }\n"})

    def test_include(self):
        self.red('include!("../gen.rs");\n', {"gen.rs": "pub fn f(a: f32) -> f32 { alice_det_math::sin(a) }\n"})

    def test_a_tests_directory_inside_src_is_read(self):
        self.red("mod tests;\n", {"src/tests/mod.rs": "pub fn f(x: f32) -> f32 { alice_det_math::sin(x) }\n"})

    def test_a_rename_in_the_workspace_table(self):
        root = DEP.replace("[dependencies]\n", '[workspace]\nmembers = ["m"]\n[workspace.dependencies]\n'
                           'dm = { package = "alice-det-math", version = "0.6" }\n[dependencies]\n')
        self.red("", {"m/Cargo.toml": '[package]\nname = "m"\nversion = "0.1.0"\n[dependencies]\ndm = { workspace = true }\n',
                      "m/src/lib.rs": "pub fn f(x: f32) -> f32 { dm::sin(x) }\n"}, manifest=root)

    def test_a_build_script(self):
        self.red("", {"build.rs": "fn main() { let _ = alice_det_math::sin(1.0); }\n"},
                 manifest=DEP + '[build-dependencies]\nalice-det-math = "0.6"\n')
        self.red("", {"build.rs": "fn main() { let _ = alice_det_math::sin(1.0); }\n"})

    def test_a_target_outside_src(self):
        self.red("", manifest=DEP + '[[bin]]\nname = "b"\npath = "tools/b.rs"\n',
                 extra={"tools/b.rs": "fn main() { alice_det_math::sin(1.0); }\n"})

    def test_a_plain_mod_below_a_path_file(self):
        call = "pub fn f(x: f32) -> f32 { alice_det_math::sin(x) }\n"
        head = '#[path = "../shared/x.rs"] mod x;\n'
        self.red(head, {"shared/x.rs": "pub mod y;\n", "shared/y.rs": call})
        self.red(head, {"shared/x.rs": "pub mod y;\n", "shared/x/y.rs": call})
        self.red(head, {"shared/x.rs": "pub mod y;\n", "shared/y/mod.rs": call})
        self.red(head, {"shared/x.rs": '#[path = "deep/y.rs"] mod y;\n', "shared/deep/y.rs": call})

    def test_a_raw_identifier(self):
        self.red("pub fn f(x: f32) -> f32 { r#alice_det_math::sin(x) }\n")

    def test_a_string_in_an_attribute(self):
        self.red('pub struct S { #[serde(with = "alice_det_math::sin")] pub a: f32 }\n')

    def test_paste_concatenation(self):
        self.red("pub fn f(x: f32) -> f32 { paste::paste! { [<alice_ det_math>]::sin(x) } }\n")

    def test_a_workspace_member_under_a_directory_named_examples_or_tests(self):
        member = '[package]\nname = "e"\nversion = "0.1.0"\n[dependencies]\nalice-det-math = "0.6"\n'
        call = "pub fn f(x: f32) -> f32 { alice_det_math::sin(x) }\n"
        for d in ("examples/engine", "tests/kit"):
            with self.subTest(d):
                self.red("", {f"{d}/Cargo.toml": member, f"{d}/src/lib.rs": call},
                         manifest=DEP + f'[workspace]\nmembers = ["{d}"]\n')
                shutil.rmtree(self.root / d.split("/")[0])

    def test_a_rename_in_a_target_table_or_dev_dependencies(self):
        call = "pub fn f(x: f32) -> f32 { dm::sin(x) }\n"
        spec = 'dm = { package = "alice-det-math", version = "0.6" }\n'
        self.red(call, manifest=DEP + "[target.'cfg(unix)'.dependencies]\n" + spec)
        self.red(call, manifest=DEP + "[dev-dependencies]\n" + spec)

    def test_a_cfg_test_item_does_not_swallow_the_next(self):
        call = "pub fn f(x: f32) -> f32 { alice_det_math::sin(x) }\n"
        for item in ("const X: [u8; 2] = [1, 2];", "const X: u8 = { 1 };", "struct U;", "thing!(a);", "use std::{f32};",
                     "impl X<{ 1 }> for Y { fn g() {} }", "fn g() where [u8; { 1 }]: Sized {}"):
            with self.subTest(item):
                self.red(f"#[cfg(test)] {item}\n{call}")
        self.red("pub trait T { #[cfg(test)] fn a(); fn b(x: f32) -> f32 { alice_det_math::sin(x) } }\n")

    def test_another_crate_s_item_of_that_name(self):
        self.red("fn f() { other_crate::alice_det_math::sin(1.0); }\n")
        f = self.found("fn f() { other_crate::alice_det_math::sin(1.0); }\n")
        self.assertEqual(f.pins, set())
        self.assertTrue(any("does not start a path" in x for x in f.findings), f.findings)


class Check(Base):
    DECL = GUARD + 'pub const DET_MATH_FUNCTIONS: &[&str] = &["sin"];\nfn f() { alice_det_math::sin(); }'

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

    def test_a_name_with_a_module_in_it_is_read_from_the_declaration(self):
        # function pins name a function of a module as module::name
        self.crate('pub const DET_MATH_FUNCTIONS: &[&str] = &["metric::dot", "sin"];\nfn f() { alice_det_math::sin(); }')
        self.assertEqual(fs.declared(self.root), {"metric::dot", "sin"})

    def test_a_commented_out_declaration_is_not_a_declaration(self):
        self.crate('// pub const DET_MATH_FUNCTIONS: &[&str] = &["sin"];\nfn f() { alice_det_math::sin(); }')
        rc, out = self.main("check")
        self.assertEqual(rc, 1)
        self.assertIn("no DET_MATH_FUNCTIONS in the source", out)

    def guard_red(self, src: str) -> None:
        self.crate('pub const DET_MATH_FUNCTIONS: &[&str] = &["sin"];\nfn f() { alice_det_math::sin(); }\n' + src)
        rc, out = self.main("check")
        self.assertEqual(rc, 1, out)
        self.assertIn("are not compared with this build", out)

    def test_a_crate_without_the_guard(self):
        self.guard_red("")

    def test_the_guard_in_a_comment_or_a_string(self):
        self.guard_red("// " + GUARD)
        self.guard_red('pub const S: &str = "' + GUARD.strip().replace('"', "") + '";\n')

    def test_the_guard_only_in_a_comment_or_a_string_with_the_name_really_used(self):
        used = "fn g() { alice_det_math::assert_function_pins(DET_MATH_PINS); }\n"
        self.guard_red(used + "// " + GUARD)
        self.guard_red(used + "/* " + GUARD + "*/\n")
        self.guard_red(used + 'pub const S: &str = "' + GUARD.strip() + '";\n')

    def test_the_guard_under_cfg_test(self):
        self.guard_red("#[cfg(test)]\nmod t {\n    " + GUARD + "}\n")

    def test_the_guard_called_at_run_time_only(self):
        self.guard_red("fn g() { alice_det_math::assert_function_pins(DET_MATH_PINS); }\n")

    def test_the_guard_named_and_not_run(self):
        self.guard_red("use alice_det_math::assert_function_pins;\n")

    def test_the_guard_on_another_table(self):
        self.guard_red('const _: () = alice_det_math::assert_function_pins(&[("tan", TAN)]);\n')
        self.guard_red("const _: () = alice_det_math::assert_function_pins(OTHER_PINS);\n")

    def test_the_guard_by_its_module_path_passes(self):
        self.crate('pub const DET_MATH_FUNCTIONS: &[&str] = &["sin"];\nfn f() { alice_det_math::sin(); }\n'
                   "const _: () = alice_det_math::semantics::assert_function_pins(DET_MATH_PINS);\n")
        rc, out = self.main("check")
        self.assertEqual(rc, 0, out)

    def test_the_guard_through_an_import_passes(self):
        self.crate('pub const DET_MATH_FUNCTIONS: &[&str] = &["sin"];\nfn f() { alice_det_math::sin(); }\n'
                   "use alice_det_math::assert_function_pins;\nconst _: () = assert_function_pins(DET_MATH_PINS);\n")
        rc, out = self.main("check")
        self.assertEqual(rc, 0, out)

    def test_generated_names_join_the_called_set(self):
        src = GUARD + 'pub const DET_MATH_FUNCTIONS: &[&str] = &["sin", "cos"];\nfn f() { alice_det_math::sin(); }'
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


class Write(Base):
    FORMATTED = ('//! a crate\n\npub const DET_MATH_FUNCTIONS: &[&str] = &["tan"];\n\n'
                 'pub fn f(x: f32) -> f32 {\n    alice_det_math::sin(x) + alice_det_math::cos(x)\n}\n\n' + GUARD)

    def lib(self) -> str:
        return (self.root / "src/lib.rs").read_text(encoding="utf-8")

    def test_the_declaration_is_written_with_the_called_set_and_check_then_passes(self):
        self.crate(self.FORMATTED)
        rc, out = self.main("write")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.lib(), self.FORMATTED.replace('&["tan"]', '&["cos", "sin"]'))
        self.assertEqual(self.main("check")[0], 0)

    def test_a_second_write_changes_nothing(self):
        self.crate(self.FORMATTED)
        self.main("write")
        once = self.lib()
        rc, out = self.main("write")
        self.assertEqual((rc, self.lib()), (0, once), out)
        self.assertIn("unchanged", out)

    def test_generated_names_are_written(self):
        (self.root / ".github").mkdir()
        (self.root / ".github" / "det-math-fold.toml").write_text('generated = ["atan2"]\n', encoding="utf-8")
        self.crate(self.FORMATTED)
        self.assertEqual(self.main("write")[0], 0)
        self.assertIn('&["atan2", "cos", "sin"]', self.lib())

    def test_a_long_set_is_written_as_rustfmt_lays_it_out(self):
        (self.root / ".github").mkdir()
        (self.root / ".github" / "det-math-fold.toml").write_text(
            'generated = ["asin", "acos", "atan", "ln", "exp", "cbrt", "hypot", "atan2", "sqrt"]\n', encoding="utf-8")
        self.crate(self.FORMATTED)
        self.assertEqual(self.main("write")[0], 0)
        self.assertIn('= &[\n    "acos",', self.lib())  # past the width of the declaration's line
        q = subprocess.run(["rustfmt", "--edition", "2021"], input=self.lib(), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertEqual(q.stdout, self.lib())
        self.assertEqual(self.main("check")[0], 0)

    def test_a_finding_writes_nothing(self):
        src = self.FORMATTED + "\nuse alice_det_math::*;\n"
        self.crate(src)
        rc, out = self.main("write")
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.lib(), src)

    def test_no_declaration_is_an_input_error_naming_what_to_add(self):
        src = "pub fn f(x: f32) -> f32 {\n    alice_det_math::sin(x)\n}\n"
        self.crate(src)
        rc, out = self.main("write")
        self.assertEqual(rc, 2, out)
        self.assertIn("no DET_MATH_FUNCTIONS", out)
        self.assertEqual(self.lib(), src)

    def test_two_declarations_are_an_input_error(self):
        src = self.FORMATTED + 'pub const DET_MATH_FUNCTIONS: &[&str] = &["cos"];\n'
        self.crate(src)
        rc, out = self.main("write")
        self.assertEqual(rc, 2, out)
        self.assertIn("2 declarations", out)
        self.assertEqual(self.lib(), src)

    def test_a_commented_out_declaration_is_left_and_the_real_one_written(self):
        src = "// pub const DET_MATH_FUNCTIONS: &[&str] = &[];\n" + self.FORMATTED
        self.crate(src)
        self.assertEqual(self.main("write")[0], 0)
        self.assertTrue(self.lib().startswith("// pub const DET_MATH_FUNCTIONS: &[&str] = &[];\n"))
        self.assertIn('&["cos", "sin"]', self.lib())

    def test_a_file_rustfmt_would_change_elsewhere_is_refused(self):
        # the write changes the declaration and nothing else
        src = self.FORMATTED + "fn   g( ) { }\n"
        self.crate(src)
        rc, out = self.main("write")
        self.assertEqual(rc, 2, out)
        self.assertIn("rustfmt", out)
        self.assertEqual(self.lib(), src)

    def test_no_use_compares_nothing(self):
        src = 'pub const DET_MATH_FUNCTIONS: &[&str] = &["sin"];\n'
        self.crate(src)
        rc, out = self.main("write")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.lib(), src)

    def test_rustfmt_failing_on_the_new_text_writes_nothing(self):
        fake = self.d / "fake-rustfmt"
        fake.write_text('#!/bin/sh\nt=$(cat)\ncase "$t" in *\\"cos\\"*) exit 1;; esac\nprintf "%s\\n" "$t"\n', encoding="utf-8")
        fake.chmod(0o755)
        real = fs.RUSTFMT
        fs.RUSTFMT = str(fake)
        self.addCleanup(setattr, fs, "RUSTFMT", real)
        self.crate(self.FORMATTED)
        rc, out = self.main("write")
        self.assertEqual(rc, 2, out)
        self.assertIn("rustfmt:", out)
        self.assertEqual(self.lib(), self.FORMATTED)

    def test_rustfmt_missing_writes_nothing(self):
        self.crate(self.FORMATTED)
        real = fs.RUSTFMT
        fs.RUSTFMT = "rustfmt-that-is-not-installed"
        self.addCleanup(setattr, fs, "RUSTFMT", real)
        rc, out = self.main("write")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.lib(), self.FORMATTED)

    def fake_rustfmt(self, body: str) -> None:
        fake = self.d / "fake-rustfmt"
        fake.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        fake.chmod(0o755)
        real = fs.RUSTFMT
        fs.RUSTFMT = str(fake)
        self.addCleanup(setattr, fs, "RUSTFMT", real)

    def test_rustfmt_failing_on_the_file_as_it_is_says_why_and_writes_nothing(self):
        self.fake_rustfmt('cat >/dev/null\necho "error: expected one of the things" >&2\nexit 1\n')
        self.crate(self.FORMATTED)
        rc, out = self.main("write")
        self.assertEqual(rc, 2, out)
        self.assertIn("rustfmt: error: expected one of the things", out)
        self.assertNotIn("would change", out)
        self.assertEqual(self.lib(), self.FORMATTED)

    def test_rustfmt_runs_in_the_file_directory_with_the_edition(self):
        seen = self.d / "seen"
        self.fake_rustfmt(f'pwd >> "{seen}"\necho "$@" >> "{seen}"\ncat\n')
        self.crate('//! a\npub mod inner;\n', extra={"src/inner/mod.rs": self.FORMATTED})
        rc, out = self.main("write")
        self.assertEqual(rc, 0, out)
        lines = seen.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], str((self.root / "src/inner").resolve()))
        self.assertEqual(lines[1], "--edition 2021")

    def test_a_crlf_file_keeps_its_line_ends(self):
        src = self.FORMATTED.replace("\n", "\r\n")
        self.crate("")
        (self.root / "src/lib.rs").write_bytes(src.encode())
        rc, out = self.main("write")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.root / "src/lib.rs").read_bytes().decode(), src.replace('&["tan"]', '&["cos", "sin"]'))

    def test_a_crlf_file_goes_to_rustfmt_as_lf(self):
        # a rustfmt that keeps what it is given would otherwise come back with \r\r\n
        self.fake_rustfmt("cat\n")
        src = self.FORMATTED.replace("\n", "\r\n")
        self.crate("")
        (self.root / "src/lib.rs").write_bytes(src.encode())
        rc, out = self.main("write")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.root / "src/lib.rs").read_bytes().decode(), src.replace('&["tan"]', '&["cos", "sin"]'))

    def test_a_file_with_both_line_ends_is_not_rewritten(self):
        src = self.FORMATTED.replace("\n", "\r\n", 1)
        self.crate("")
        (self.root / "src/lib.rs").write_bytes(src.encode())
        rc, out = self.main("write")
        self.assertEqual(rc, 2, out)
        self.assertIn("would change", out)
        self.assertEqual((self.root / "src/lib.rs").read_bytes().decode(), src)

    def test_a_comment_inside_the_list_is_not_lost(self):
        src = self.FORMATTED.replace('&["tan"]', '&[\n    // the old one\n    "tan",\n]')
        self.crate(src)
        rc, out = self.main("write")
        self.assertEqual(rc, 2, out)
        self.assertIn("a comment inside", out)
        self.assertEqual(self.lib(), src)

    def test_a_declaration_in_a_comment_or_a_string_is_not_one(self):
        for name, decl in (("line", '// pub const DET_MATH_FUNCTIONS: &[&str] = &["tan"];\n'),
                           ("block", '/* pub const DET_MATH_FUNCTIONS: &[&str] = &["tan"]; */\n'),
                           ("nested", '/* /* a */ DET_MATH_FUNCTIONS: &[&str] = &["tan"]; */\n'),
                           ("string", 'pub const D: &str = "DET_MATH_FUNCTIONS: &[&str] = &[]";\n'),
                           ("raw", 'pub const D: &str = r#"DET_MATH_FUNCTIONS: &[&str] = &[]"#;\n')):
            with self.subTest(name):
                src = decl + "pub fn f(x: f32) -> f32 {\n    alice_det_math::sin(x)\n}\n"
                self.crate(src)
                rc, out = self.main("write")
                self.assertEqual(rc, 2, out)
                self.assertIn("no DET_MATH_FUNCTIONS", out)
                self.assertEqual(self.lib(), src)
                rc, out = self.main("check")
                self.assertEqual(rc, 1, out)
                self.assertIn("no DET_MATH_FUNCTIONS", out)

    def test_a_url_before_the_declaration_on_its_line_does_not_hide_it(self):
        self.crate(GUARD + 'pub const U: &str = "a://b"; pub const DET_MATH_FUNCTIONS: &[&str] = &["sin"];\n'
                   'pub fn f(x: f32) -> f32 {\n    alice_det_math::sin(x)\n}\n')
        rc, out = self.main("check")
        self.assertEqual(rc, 0, out)

    def test_a_name_commented_out_in_the_list_is_not_declared(self):
        self.crate('pub const DET_MATH_FUNCTIONS: &[&str] = &[\n    // "sin",\n];\n'
                   'pub fn f(x: f32) -> f32 {\n    alice_det_math::sin(x)\n}\n')
        rc, out = self.main("check")
        self.assertEqual(rc, 1, out)
        self.assertIn("sin is called but is not in DET_MATH_FUNCTIONS", out)

    def test_what_the_list_holds_that_is_not_a_name_is_an_error_not_dropped(self):
        for name, item in (("upper case", '"COS"'), ("a comment in a string", '"//x"'), ("an empty name", '""'),
                           ("a space", '"s in"'), ("a constant", "OTHER"), ("a raw string", 'r"sin"')):
            for cmd in ("check", "write"):
                with self.subTest(name, cmd=cmd):
                    src = self.FORMATTED.replace('&["tan"]', f'&["cos", "sin", {item}]')
                    self.crate(src)
                    rc, out = self.main(cmd)
                    self.assertEqual(rc, 2, out)
                    self.assertIn("not a lower-case function id", out)
                    self.assertEqual(self.lib(), src)

    def generated(self, toml: bytes) -> None:
        (self.root / ".github").mkdir(exist_ok=True)
        (self.root / ".github" / "det-math-fold.toml").write_bytes(toml)

    def test_a_bad_generated_list_is_exit_2_and_writes_nothing(self):
        for name, toml, needle in (("a string", b'generated = "atan2"\n', "not a list"),
                                   ("not strings", b'generated = [1]\n', "not a list"),
                                   ("upper case", b'generated = ["Atan2"]\n', "not lower-case"),
                                   ("an empty name", b'generated = [""]\n', "not lower-case"),
                                   ("bad TOML", b'generated = [\n', "fold set:"),
                                   ("not UTF-8", b'generated = ["\xff"]\n', "fold set:"),
                                   ("too deep", b'generated = ' + b'[' * 100000, "fold set:")):
            for cmd in ("write", "check"):
                with self.subTest(name, cmd=cmd):
                    self.crate(self.FORMATTED)
                    self.generated(toml)
                    rc, out = self.main(cmd)
                    self.assertEqual(rc, 2, out)
                    self.assertIn(needle, out)
                    self.assertNotIn("Traceback", out)
                    self.assertEqual(self.lib(), self.FORMATTED)

    def test_a_source_that_is_not_utf8_is_exit_2(self):
        self.crate(self.FORMATTED)
        (self.root / "src/other.rs").write_bytes(b"\xff\xfe\n")
        rc, out = self.main("write")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.lib(), self.FORMATTED)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs file modes that bind")
    def test_a_read_only_source_is_exit_2(self):
        self.crate(self.FORMATTED)
        lib = self.root / "src/lib.rs"
        lib.chmod(0o444)
        self.addCleanup(lib.chmod, 0o644)
        rc, out = self.main("write")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.lib(), self.FORMATTED)


if __name__ == "__main__":
    unittest.main()
