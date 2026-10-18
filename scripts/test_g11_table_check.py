#!/usr/bin/env python3
"""tests for scripts/g11_table_check.py: a crate whose table is complete passes; each finding F1-F6 is
raised by the edit that should raise it; no row or no pass gate is compared nothing (exit 2)

    python3 scripts/test_g11_table_check.py
"""
from __future__ import annotations

import importlib.util
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("g11_table_check", HERE / "g11_table_check.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

REV = "a" * 40
NAME = "ALICE-Demo"

CRATE = {
    "alice-law-conformance.toml": '[G1]\nstatus = "pass"\n\n[G4]\nstatus = "pass"\n\n[G9]\nstatus = "fail-known"\n\n'
                                  '[G11]\nstatus = "unverified"\n',
    ".github/shared-files-pin": f"name {NAME}\nrev {REV}\ntable-sha256 {'0' * 64}\nchecker-sha256 {'0' * 64}\n",
    "Cargo.toml": '[package]\nname = "demo"\n\n[features]\ndefault = []\nextra = []\nsvo = []\n',
    ".github/workflows/ci.yml": "jobs:\n  t:\n    steps:\n      - run: scripts/run_tests.sh golden -- cargo test --test golden\n"
                                "      - run: cargo test --features extra --lib\n"
                                "      # cargo test --features svo --lib (a comment builds nothing)\n"
                                "      - run: cargo clippy --features svo --lib\n"
                                "      - run: cargo test --features svo --test other_target\n"
                                "      - run: |\n          python3 scripts/test_ban.py\n          python3 scripts/test_run_tests.py\n",
    "scripts/run_tests.sh": "#!/bin/sh\n",
    "scripts/test_run_tests.py": "import unittest\nclass RunTests(unittest.TestCase):\n"
                                 "    def test_no_test_ran_fails(self):\n        pass\n"
                                 "    def test_ok(self):\n        pass\n"
                                 "class Other(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
    "scripts/ban.py": "print()\n",
    "scripts/test_ban.py": "def test_flags_the_canary():\n    pass\n\ndef test_zero_files_fails():\n    pass\n",
    "tests/golden.rs": "#[test]\nfn a_wrong_digest_is_refused() {}\n\n#[test]\n#[ignore]\nfn ignored() {}\n",
    "src/lib.rs": "#[cfg(test)]\n#[cfg(feature = \"extra\")]\nmod tests {\n    #[test]\n    fn gated_built() {}\n}\n"
                  "#[cfg(test)]\n#[cfg(feature = \"svo\")]\nmod more {\n    #[test]\n    fn gated_unbuilt() {}\n}\n",
}
TABLE = """[[row]]
gate = "G1"
checker = "scripts/ban.py"
canary = "scripts/test_ban.py::test_flags_the_canary"
zero = "scripts/test_ban.py::test_zero_files_fails"
shared = "n/a: this crate's own ban list"

[[row]]
gate = "G4"
checker = ".github/workflows/ci.yml#run: scripts/run_tests.sh golden"
canary = "tests/golden.rs::a_wrong_digest_is_refused"
zero = "scripts/test_run_tests.py::RunTests.test_no_test_ran_fails"
shared = ["scripts/run_tests.sh", "scripts/test_run_tests.py"]
"""
LAW = {
    "conformance/shared-files.toml": f'[[file]]\npath = "scripts/run_tests.sh"\nrepos = ["{NAME}"]\n\n'
                                     f'[[file]]\npath = "scripts/test_run_tests.py"\nrepos = ["{NAME}"]\n\n'
                                     f'[[file]]\npath = "scripts/other.py"\nrepos = ["ALICE-Else"]\n',
    "scripts/test_shared_files_check.py": "import unittest\nclass Shared(unittest.TestCase):\n"
                                          "    def test_compared_nothing_exits_2(self):\n        pass\n",
    ".github/workflows/law.yml": "jobs:\n  t:\n    steps:\n      - run: python3 scripts/test_shared_files_check.py\n",
}


def write(root: Path, files: dict):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class Table(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="g11-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.crate, self.law = self.tmp / "crate", self.tmp / "law"
        write(self.crate, CRATE)
        write(self.crate, {"conformance/g11.toml": TABLE})
        write(self.law, LAW)

    def edit(self, rel, old, new, root=None):
        p = (root or self.crate) / rel
        s = p.read_text()
        self.assertEqual(s.count(old), 1, old)
        p.write_text(s.replace(old, new))

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = g.main(["--repo", str(self.crate), "--name", NAME, "--law", f"{REV}={self.law}"])
        return rc, out.getvalue() + err.getvalue()

    def assertFinding(self, code, text):
        rc, out = self.run_main()
        self.assertEqual(rc, 1, out)
        self.assertTrue(any(line.startswith(f"error: {code} ") and text in line for line in out.splitlines()), out)

    def test_a_complete_table_passes(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn("2 rows, 2 pass gates, 0 findings", out)

    def test_f1_a_pass_gate_without_a_row(self):
        self.edit("alice-law-conformance.toml", '[G9]\nstatus = "fail-known"', '[G9]\nstatus = "pass"')
        self.assertFinding("F1", "G9 is pass")

    def test_g11_itself_needs_no_row(self):
        self.edit("alice-law-conformance.toml", '[G11]\nstatus = "unverified"', '[G11]\nstatus = "pass"')
        self.assertEqual(self.run_main()[0], 0)

    def test_f2_a_row_for_a_gate_that_is_not_pass(self):
        self.edit("alice-law-conformance.toml", '[G4]\nstatus = "pass"', '[G4]\nstatus = "unverified"')
        self.assertFinding("F2", "G4 is not pass")

    def test_f3_a_missing_checker_path(self):
        self.edit("conformance/g11.toml", 'checker = "scripts/ban.py"', 'checker = "scripts/gone.py"')
        self.assertFinding("F3", "scripts/gone.py is not in the tree")

    def test_f3_an_anchor_that_is_not_there(self):
        self.edit("conformance/g11.toml", "#run: scripts/run_tests.sh golden", "#run: scripts/run_tests.sh silver")
        self.assertFinding("F3", "occurs 0 times")

    def test_f3_an_anchor_that_occurs_twice(self):
        self.edit("conformance/g11.toml", "#run: scripts/run_tests.sh golden", "#run:")
        self.assertFinding("F3", "an anchor names one place")

    def test_f4_a_renamed_rust_test(self):
        self.edit("tests/golden.rs", "fn a_wrong_digest_is_refused", "fn renamed")
        self.assertFinding("F4", "0 fn named a_wrong_digest_is_refused")

    def test_f4_an_ignored_rust_test(self):
        self.edit("conformance/g11.toml", "golden.rs::a_wrong_digest_is_refused", "golden.rs::ignored")
        self.assertFinding("F4", "#[ignore]d")

    def test_f4_a_feature_gated_test_no_ci_step_builds(self):
        self.edit("conformance/g11.toml", "tests/golden.rs::a_wrong_digest_is_refused", "src/lib.rs::gated_unbuilt")
        self.assertFinding("F4", "with feature(s) ['svo']")

    def test_a_feature_gated_test_a_ci_step_builds_resolves(self):
        self.edit("conformance/g11.toml", "tests/golden.rs::a_wrong_digest_is_refused", "src/lib.rs::gated_built")
        self.assertEqual(self.run_main()[0], 0)

    def test_f4_the_feature_only_on_a_clippy_line_a_comment_or_another_target(self):
        # src/lib.rs::gated_unbuilt needs svo: the fixture's CI names svo on a comment, a clippy line and a
        # `cargo test --test other_target` line, none of which runs the lib's unit tests with svo
        self.edit("conformance/g11.toml", "tests/golden.rs::a_wrong_digest_is_refused", "src/lib.rs::gated_unbuilt")
        self.assertFinding("F4", "lib's unit tests of demo with feature(s) ['svo']")

    def test_a_lib_test_line_with_the_feature_resolves(self):
        self.edit("conformance/g11.toml", "tests/golden.rs::a_wrong_digest_is_refused", "src/lib.rs::gated_unbuilt")
        self.edit(".github/workflows/ci.yml", "      - run: cargo test --features extra --lib\n",
                  "      - run: cargo test --features extra --lib\n      - run: cargo test --lib --features svo gated\n")
        self.assertEqual(self.run_main()[0], 0)

    def test_a_lib_test_line_for_another_package_does_not_count(self):
        self.edit("conformance/g11.toml", "tests/golden.rs::a_wrong_digest_is_refused", "src/lib.rs::gated_unbuilt")
        self.edit(".github/workflows/ci.yml", "      - run: cargo test --features extra --lib\n",
                  "      - run: cargo test --features extra --lib\n      - run: cargo test -p other --lib --features svo\n")
        self.assertFinding("F4", "with feature(s) ['svo']")

    def lib_svo(self, line):
        self.edit("conformance/g11.toml", "tests/golden.rs::a_wrong_digest_is_refused", "src/lib.rs::gated_unbuilt")
        self.edit(".github/workflows/ci.yml", "      - run: cargo test --features extra --lib\n",
                  f"      - run: cargo test --features extra --lib\n      - run: {line}\n")

    def test_a_filter_that_selects_the_test_resolves(self):
        self.lib_svo("cargo test --lib --features svo more::")
        self.assertEqual(self.run_main()[0], 0)

    def test_a_filter_that_does_not_select_the_test(self):
        self.lib_svo("cargo test --lib --features svo nomatch_filter::")
        self.assertFinding("F4", "with feature(s) ['svo']")

    def test_a_skip_of_the_test(self):
        self.lib_svo("cargo test --lib --features svo -- --skip gated_unbuilt")
        self.assertFinding("F4", "with feature(s) ['svo']")

    def test_exact_needs_the_whole_path(self):
        self.lib_svo("cargo test --lib --features svo -- --exact gated_unbuilt")
        self.assertFinding("F4", "with feature(s) ['svo']")

    def test_exact_with_the_whole_path_resolves(self):
        self.lib_svo("cargo test --lib --features svo -- --exact more::gated_unbuilt")
        self.assertEqual(self.run_main()[0], 0)

    def test_a_filter_where_the_module_path_is_unknown_is_refused(self):
        # a #[path] attribute makes the file's module unknowable here; a filtered command then does not count
        self.edit("src/lib.rs", "#[cfg(test)]\n#[cfg(feature = \"extra\")]", '#[path = "y.rs"]\nmod y;\n#[cfg(test)]\n#[cfg(feature = "extra")]')
        self.lib_svo("cargo test --lib --features svo more::")
        self.assertFinding("F4", "with feature(s) ['svo']")

    def test_a_filter_through_cargo_test_nonzero(self):
        self.lib_svo("scripts/cargo_test_nonzero.sh -p demo --lib --features svo other::")
        self.assertFinding("F4", "with feature(s) ['svo']")

    def test_module_paths(self):
        tree = g.Tree(lambda p: "", set())
        code = "mod a {\n    mod b {\n        fn t() {}\n    }\n}\n"
        self.assertEqual(g.module_path(tree, "x/src/m/n.rs", code, code.index("fn t")), "m::n::a::b::t")
        self.assertEqual(g.module_path(tree, "x/src/m/mod.rs", code, code.index("fn t")), "m::a::b::t")
        self.assertEqual(g.module_path(tree, "x/src/lib.rs", "fn t() {}", 0), "t")
        self.assertEqual(g.module_path(tree, "x/tests/golden.rs", "fn t() {}", 0), "t")
        self.assertIsNone(g.module_path(tree, "x/src/lib.rs", '#[path = "y.rs"]\nmod y;\nfn t() {}', 22))

    def test_an_integration_test_needs_a_run_selecting_it(self):
        self.edit(".github/workflows/ci.yml", "      - run: scripts/run_tests.sh golden -- cargo test --test golden\n", "")
        self.assertFinding("F4", "the integration test golden")

    def test_required_features_of_a_test_target_count(self):
        self.edit("Cargo.toml", "svo = []\n", 'svo = []\n\n[[test]]\nname = "golden"\nrequired-features = ["svo"]\n')
        self.assertFinding("F4", "the integration test golden of demo with feature(s) ['svo']")

    def test_a_python_test_no_command_runs(self):
        self.edit(".github/workflows/ci.yml", "          python3 scripts/test_ban.py\n", "")
        self.assertFinding("F4", "scripts/test_ban.py: no workflow or scripts/preflight.sh command runs it")

    def test_a_python_test_named_only_in_a_comment(self):
        self.edit(".github/workflows/ci.yml", "          python3 scripts/test_ban.py\n", "          # python3 scripts/test_ban.py\n")
        self.assertFinding("F4", "scripts/test_ban.py: no workflow")

    def test_f4_a_missing_python_test(self):
        self.edit("scripts/test_ban.py", "def test_flags_the_canary", "def test_other")
        self.assertFinding("F4", "no test_flags_the_canary")

    def test_f4_an_ambiguous_bare_python_name(self):
        self.edit("conformance/g11.toml", "RunTests.test_no_test_ran_fails", "test_ok")
        self.assertFinding("F4", "ambiguous")

    def test_f4_a_python_name_that_is_not_a_test(self):
        self.edit("scripts/test_ban.py", "def test_flags_the_canary", "def flags_the_canary")
        self.edit("conformance/g11.toml", "::test_flags_the_canary", "::flags_the_canary")
        self.assertFinding("F4", "is not a test")

    def test_a_law_reference_resolves_at_the_pinned_revision(self):
        self.edit("conformance/g11.toml", 'zero = "scripts/test_ban.py::test_zero_files_fails"',
                  'zero = "law@shared-files-pin:scripts/test_shared_files_check.py::Shared.test_compared_nothing_exits_2"')
        self.assertEqual(self.run_main()[0], 0)

    def test_f4_a_law_reference_missing_at_the_pinned_revision(self):
        self.edit("conformance/g11.toml", 'zero = "scripts/test_ban.py::test_zero_files_fails"',
                  'zero = "law@shared-files-pin:scripts/test_shared_files_check.py::Shared.test_gone"')
        self.assertFinding("F4", "no Shared.test_gone")

    def test_f4_a_law_reference_to_a_pin_the_crate_lacks(self):
        self.edit("conformance/g11.toml", 'zero = "scripts/test_ban.py::test_zero_files_fails"',
                  'zero = "law@public-text-pin:scripts/x.py::test_x"')
        self.assertFinding("F4", ".github/public-text-pin is not in the tree")

    def test_f5_canary_equal_to_zero(self):
        self.edit("conformance/g11.toml", 'zero = "scripts/test_ban.py::test_zero_files_fails"',
                  'zero = "scripts/test_ban.py::test_flags_the_canary"')
        self.assertFinding("F5", "same reference")

    def test_f6_a_shared_path_the_table_does_not_list_for_the_crate(self):
        self.edit("conformance/g11.toml", '"scripts/test_run_tests.py"]', '"scripts/other.py"]')
        self.assertFinding("F6", "scripts/other.py is not a shared-files row")

    def test_f6_n_a_while_the_table_lists_the_checker(self):
        self.edit("conformance/g11.toml", 'checker = "scripts/ban.py"', 'checker = "scripts/run_tests.sh"')
        self.assertFinding("F6", "shared is n/a but the shared-files table lists scripts/run_tests.sh")

    def test_f6_shared_must_be_a_list_or_a_reason(self):
        self.edit("conformance/g11.toml", 'shared = "n/a: this crate\'s own ban list"', 'shared = "n/a:"')
        self.assertFinding("F6", "non-empty list")

    def test_no_row_is_compared_nothing(self):
        (self.crate / "conformance/g11.toml").write_text("")
        self.assertEqual(self.run_main()[0], 2)

    def test_no_pass_gate_is_compared_nothing(self):
        (self.crate / "alice-law-conformance.toml").write_text('[G1]\nstatus = "unverified"\n')
        (self.crate / "conformance/g11.toml").write_text("")
        self.assertEqual(self.run_main()[0], 2)

    def test_a_missing_table_exits_2(self):
        (self.crate / "conformance/g11.toml").unlink()
        self.assertEqual(self.run_main()[0], 2)


if __name__ == "__main__":
    unittest.main()
