#!/usr/bin/env python3
"""Tests of scripts/surface_claims_check.py (python3 scripts/test_surface_claims_check.py)

Each comparison of G15 has a case that must fail and the false forms it must not take for a surface
(a substring, a comment, a test module, wasi taken for a browser, a C# port taken for a binding, a
continue-on-error step); nothing compared and an input kind never read exit 2; the baseline only shrinks.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("scc", HERE / "surface_claims_check.py")
scc = importlib.util.module_from_spec(spec)
sys.modules["scc"] = scc
spec.loader.exec_module(scc)

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

CARGO = '[package]\nname = "alice-x"\nversion = "0.1.0"\n\n[package.metadata.alice]\nsurfaces = {surfaces}\n'
BROWSER_CI = (
    "on:\n  push:\n    branches: [main]\njobs:\n  web:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - run: cargo build --target wasm32-unknown-unknown && wasm-bindgen x.wasm\n"
    "      - run: node --test tests/js\n"
)
TABLE = "# alice-x\n\n<!-- surfaces -->\n| Surface | Where |\n|---|---|\n{rows}<!-- /surfaces -->\n"


def repo(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp(prefix="scc-"))
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    return d


def table(*surfaces: str) -> str:
    return TABLE.format(rows="".join(f"| {s} | x |\n" for s in surfaces))


def base(surfaces='["rust", "browser"]', readme=None, ci=BROWSER_CI, lib='#[wasm_bindgen]\npub fn f() {}\n', extra=None):
    files = {
        "Cargo.toml": CARGO.format(surfaces=surfaces),
        "src/lib.rs": lib,
        "README.md": table("rust", "browser") if readme is None else readme,
    }
    if ci is not None:
        files[".github/workflows/web.yml"] = ci
    files.update(extra or {})
    return repo(files)


def kinds(d: Path) -> list[str]:
    findings, compared = scc.check_repo(d)
    assert compared > 0
    return sorted(k for k, _ in findings)


def run(argv: list[str]) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = scc.main(argv)
    return code, out.getvalue() + err.getvalue()


class Comparisons(unittest.TestCase):
    def test_a_consistent_repository_passes(self):
        d = base()
        self.assertEqual(kinds(d), [])
        self.assertEqual(run(["--repo", str(d)])[0], 0)

    # 1. declared == detected
    def test_a_declared_surface_that_is_not_there_fails(self):
        d = base(surfaces='["rust", "browser", "python"]', readme=table("rust", "browser", "python"))
        self.assertIn("declared-not-found", kinds(d))

    def test_a_found_surface_that_is_not_declared_fails(self):
        d = base(lib='#[wasm_bindgen]\npub fn f() {}\n#[no_mangle]\npub extern "C" fn g() {}\n')
        self.assertIn("found-not-declared", kinds(d))

    def test_a_crate_with_bindings_and_no_declaration_fails(self):
        d = base(extra={"ffi/Cargo.toml": '[package]\nname = "alice-x-ffi"\n', "ffi/src/lib.rs": '#[no_mangle]\npub extern "C" fn g() {}\n'})
        self.assertIn("undeclared", kinds(d))

    def test_a_word_outside_the_vocabulary_fails(self):
        d = base(surfaces='["rust", "browser", "wasm"]')
        self.assertIn("vocabulary", kinds(d))

    # 2. the README table
    def test_a_readme_table_that_differs_fails(self):
        self.assertIn("readme", kinds(base(readme=table("rust", "browser", "python"))))

    def test_a_readme_without_the_table_fails_and_prose_is_not_read(self):
        self.assertIn("readme", kinds(base(readme="# alice-x\n\nRuns in the browser through WebAssembly.\n")))

    def test_both_readmes_must_hold_the_same_set(self):
        d = base(extra={"README_JP.md": table("rust")})
        self.assertIn("readme", kinds(d))

    # 3. CI builds and runs
    def test_a_declared_binding_surface_ci_does_not_build_fails(self):
        ci = "on:\n  push:\njobs:\n  j:\n    steps:\n      - run: cargo test\n"
        self.assertEqual(kinds(base(ci=ci)), ["ci-not-built", "ci-not-run"])

    def test_a_build_without_a_run_fails_for_browser(self):
        ci = "on:\n  push:\njobs:\n  j:\n    steps:\n      - run: cargo build --target wasm32-unknown-unknown\n"
        self.assertEqual(kinds(base(ci=ci)), ["ci-not-run"])

    def test_continue_on_error_steps_do_not_count(self):
        ci = BROWSER_CI.replace("      - run: node --test tests/js\n", "      - run: node --test tests/js\n        continue-on-error: true\n")
        self.assertEqual(kinds(base(ci=ci)), ["ci-not-run"])

    def test_a_schedule_only_workflow_does_not_count(self):
        ci = BROWSER_CI.replace("on:\n  push:\n    branches: [main]\n", "on:\n  schedule:\n    - cron: '0 0 * * *'\n")
        self.assertEqual(kinds(base(ci=ci)), ["ci-not-built", "ci-not-run"])

    def test_engine_surfaces_need_the_c_surface(self):
        d = base(surfaces='["rust", "browser", "unity"]', readme=table("rust", "browser", "unity"),
                 extra={"bindings/X.cs": 'class N { [DllImport("alice_x")] static extern void f(); }'})
        self.assertIn("engine-without-c", kinds(d))


class FalseForms(unittest.TestCase):
    def test_comments_and_test_modules_are_not_surfaces(self):
        lib = '// #[wasm_bindgen]\n/* #[no_mangle] extern "C" */\n#[wasm_bindgen]\npub fn f() {}\n#[cfg(test)]\nmod t {\n  #[pyfunction]\n  fn g() { let s = "}"; }\n}\n'
        self.assertEqual(kinds(base(lib=lib)), [])

    def test_wasi_is_not_browser(self):
        ci = "on:\n  push:\njobs:\n  j:\n    steps:\n      - run: cargo build --target wasm32-wasip1 && wasmtime run x.wasm\n"
        d = base(surfaces='["rust", "wasi"]', readme=table("rust", "wasi"), ci=ci, lib="pub fn f() {}\n")
        self.assertEqual(kinds(d), [])
        d = base(surfaces='["rust", "browser"]', ci=ci, lib="pub fn f() {}\n")
        self.assertIn("declared-not-found", kinds(d))

    def test_a_csharp_port_is_not_a_unity_binding(self):
        d = base(extra={"vrchat/Udon/T.cs": "class T : UdonSharpBehaviour { float Sdf(float x) { return x; } }",
                        "bindings/Other.cs": '[DllImport("alice_zip")] static extern void g();'})
        self.assertEqual(kinds(d), [])

    def test_a_unity_binding_through_a_constant_is_found(self):
        lib = '#[wasm_bindgen]\npub fn f() {}\n#[no_mangle]\npub extern "C" fn g() {}\n'
        ci = BROWSER_CI + "      - run: cargo rustc --crate-type cdylib --features ffi\n"
        d = base(surfaces='["rust", "browser", "c", "unity"]', readme=table("rust", "browser", "c", "unity"), ci=ci, lib=lib,
                 extra={"bindings/X.cs": 'class N { const string Lib = "libalice_x"; [DllImport(Lib)] static extern void g(); }'})
        self.assertEqual(kinds(d), [])


    def test_an_engine_binding_of_a_facade_takes_the_c_surface_of_the_engine_crate(self):
        ci = BROWSER_CI + "      - run: cargo rustc -p alice-x --crate-type cdylib --features ffi\n"
        d = repo({
            "Cargo.toml": CARGO.format(surfaces='["rust", "browser", "c"]').replace("alice-x", "alice-x-core"),
            "src/lib.rs": '#[wasm_bindgen]\npub fn f() {}\n#[no_mangle]\npub extern "C" fn g() {}\n',
            "facade/Cargo.toml": CARGO.format(surfaces='["rust", "unity"]'),
            "facade/src/lib.rs": "pub use alice_x_core::*;\n",
            "bindings/X.cs": '[DllImport("alice_x")] static extern void g();',
            ".github/workflows/web.yml": ci,
            "README.md": table("rust", "browser", "c", "unity"),
        })
        self.assertEqual(kinds(d), [])


class NothingCompared(unittest.TestCase):
    def test_no_declaration_anywhere_exits_2(self):
        # a README with a table, so that only the missing declaration can make this exit 2
        d = repo({"Cargo.toml": '[package]\nname = "alice-x"\n', "src/lib.rs": "pub fn f() {}\n", "README.md": table("rust")})
        code, out = run(["--repo", str(d)])
        self.assertEqual(code, 2, out)
        self.assertIn("no crate declares", out)

    def test_no_crate_manifest_exits_2(self):
        code, out = run(["--repo", str(repo({"README.md": "x", "src/lib.rs": "pub fn f() {}\n"}))])
        self.assertEqual(code, 2, out)
        self.assertIn("no crate manifest", out)

    def test_no_rust_source_exits_2(self):
        self.assertEqual(run(["--repo", str(repo({"Cargo.toml": CARGO.format(surfaces='["rust"]')}))])[0], 2)

    def test_a_declared_binding_surface_with_no_workflow_file_exits_2(self):
        self.assertEqual(run(["--repo", str(base(ci=None))])[0], 2)


class Baseline(unittest.TestCase):
    def test_more_findings_than_allowed_fail(self):
        self.assertTrue(scc.compare_baseline("R", {"readme": 2}, {"repository": {"R": {"readme": 1}}}))

    def test_fewer_findings_than_allowed_fail_as_stale(self):
        got = scc.compare_baseline("R", {"readme": 1}, {"repository": {"R": {"readme": 2}}})
        self.assertTrue(got and "stale" in got[0])

    def test_equal_counts_pass_and_a_new_kind_fails(self):
        self.assertEqual(scc.compare_baseline("R", {"readme": 1}, {"repository": {"R": {"readme": 1}}}), [])
        self.assertTrue(scc.compare_baseline("R", {"ci-not-built": 1}, {"repository": {"R": {"readme": 0}}}))
        self.assertTrue(scc.compare_baseline("S", {"readme": 1}, {"repository": {"R": {"readme": 1}}}))

    def test_the_baseline_is_generated_and_reads_back(self):
        good, bad = base(), base(readme=table("rust"))
        out = Path(tempfile.mkdtemp()) / "b.toml"
        code, _ = run(["--write-baseline", str(out), f"Good={good}", f"Bad={bad}"])
        self.assertEqual(code, 0)
        import tomllib
        data = tomllib.loads(out.read_text())
        self.assertEqual(data["repository"], {"Bad": {"readme": 1}})
        self.assertEqual(run(["--repo", str(bad), "--baseline", str(out), "--name", "Bad"])[0], 0)
        self.assertEqual(run(["--repo", str(good), "--baseline", str(out), "--name", "Bad"])[0], 1)  # shrank: stale

    def test_the_committed_baseline_is_generated_form(self):
        p = HERE.parent / "conformance" / "surface-claims-baseline.toml"
        text = p.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Generated by scripts/surface_claims_check.py --write-baseline"))
        import tomllib
        for name, row in tomllib.loads(text).get("repository", {}).items():
            self.assertTrue(row and all(isinstance(v, int) and v > 0 for v in row.values()), name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
