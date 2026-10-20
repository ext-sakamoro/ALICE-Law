#!/usr/bin/env python3
"""tests for scripts/g11_table_gen.py: a shared checker gets its row only where the crate's manifest names it
for a pass gate and the shared-files table lists it for the crate; the crate's own rows are copied; a
reference that does not resolve, a pass gate without a row and a table that is not the generated one are
gaps (exit 1); no pass gate, no row or an unreadable input is compared nothing (exit 2)

    python3 scripts/test_g11_table_gen.py
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
spec = importlib.util.spec_from_file_location("g11_table_gen", HERE / "g11_table_gen.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

REV = "b" * 40
NAME = "ALICE-Demo"

CRATE = {
    "alice-law-conformance.toml": '[G1]\nstatus = "pass"\nevidence = ["scripts/ban.py with scripts/test_ban.py"]\n\n'
                                  '[G4]\nstatus = "pass"\nevidence = ["tests/golden.rs through scripts/run_tests.sh"]\n\n'
                                  '[G9]\nstatus = "fail-known"\nevidence = ["scripts/ban.py"]\n\n'
                                  '[G11]\nstatus = "fail-known"\nevidence = ["scripts/ban.py"]\n',
    ".github/shared-files-pin": f"name {NAME}\nrev {REV}\n",
    ".github/workflows/ci.yml": "jobs:\n  t:\n    steps:\n      - run: scripts/run_tests.sh golden -- cargo test --test golden\n"
                                "      - run: |\n          python3 scripts/test_ban.py\n          python3 scripts/test_run_tests.py\n",
    "scripts/ban.py": "print()\n",
    "scripts/test_ban.py": "import unittest\nclass Ban(unittest.TestCase):\n"
                           "    def test_flags_the_canary(self):\n        pass\n"
                           "    def test_zero_files_fails(self):\n        pass\n",
    "scripts/run_tests.sh": "#!/bin/sh\n",
    "scripts/test_run_tests.py": "import unittest\nclass RunTests(unittest.TestCase):\n"
                                 "    def test_a_named_file_that_ran_zero_fails(self):\n        pass\n",
    "tests/golden.rs": "#[test]\nfn a_wrong_digest_is_refused() {}\n",
    "conformance/g11-own.toml": '[[row]]\ngate = "G4"\n'
                                'checker = ".github/workflows/ci.yml#run: scripts/run_tests.sh golden"\n'
                                'canary = "tests/golden.rs::a_wrong_digest_is_refused"\n'
                                'zero = "scripts/test_run_tests.py::RunTests.test_a_named_file_that_ran_zero_fails"\n'
                                'shared = "n/a: 黄金律の試験はこの crate のもの"\n',
}
LAW = {
    "conformance/shared-files.toml": f'[[file]]\npath = "scripts/ban.py"\nrepos = ["{NAME}"]\n\n'
                                     f'[[file]]\npath = "scripts/test_ban.py"\nrepos = ["{NAME}"]\n\n'
                                     f'[[file]]\npath = "scripts/other.py"\nrepos = ["{NAME}"]\n',
}
CATALOG = """[[checker]]
path = "scripts/ban.py"
canary = "scripts/test_ban.py::Ban.test_flags_the_canary"
zero = "scripts/test_ban.py::Ban.test_zero_files_fails"
shared = ["scripts/ban.py", "scripts/test_ban.py", "scripts/ban_data.toml"]

[[checker]]
path = "scripts/other.py"
canary = "scripts/test_other.py::Other.test_flags"
zero = "scripts/test_other.py::Other.test_zero"
shared = ["scripts/other.py"]
"""


def write(root: Path, files: dict):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))


class Gen(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="g11gen-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.crate, self.law, self.catalog = self.tmp / "crate", self.tmp / "law", self.tmp / "catalog.toml"
        write(self.crate, CRATE)
        write(self.law, LAW)
        self.catalog.write_bytes(CATALOG.encode("utf-8"))

    def edit(self, path: Path, old, new):
        s = path.read_bytes().decode("utf-8")
        self.assertEqual(s.count(old), 1, old)
        path.write_bytes(s.replace(old, new).encode("utf-8"))

    def run_main(self, mode="--write"):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = gen.main(["--repo", str(self.crate), "--name", NAME, "--catalog", str(self.catalog),
                           "--law", f"{REV}={self.law}", mode])
        return rc, out.getvalue() + err.getvalue()

    def table(self):
        return (self.crate / "conformance/g11.toml").read_bytes().decode("utf-8")

    def assertGap(self, text, mode="--write"):
        rc, out = self.run_main(mode)
        self.assertEqual(rc, 1, out)
        self.assertTrue(any(line.startswith("error: ") and text in line for line in out.splitlines()), out)

    def test_write_then_check_passes(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn("2 rows (1 from the catalog), 2 pass gates, 0 gaps", out)
        self.assertEqual(self.run_main("--check")[0], 0)

    def test_the_generated_table_passes_the_g11_check(self):
        self.assertEqual(self.run_main()[0], 0)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = gen.chk.main(["--repo", str(self.crate), "--name", NAME, "--law", f"{REV}={self.law}"])
        self.assertEqual(rc, 0, out.getvalue() + err.getvalue())

    def test_a_shared_row_keeps_the_paths_the_table_lists_for_the_crate(self):
        self.run_main()
        self.assertIn('checker = "scripts/ban.py"\ncanary = "scripts/test_ban.py::Ban.test_flags_the_canary"\n'
                      'zero = "scripts/test_ban.py::Ban.test_zero_files_fails"\n'
                      'shared = ["scripts/ban.py", "scripts/test_ban.py"]\n', self.table())

    def test_rows_are_ordered_by_gate_number(self):
        self.edit(self.crate / "alice-law-conformance.toml", '[G4]\nstatus = "pass"', '[G10]\nstatus = "pass"')
        self.edit(self.crate / "conformance/g11-own.toml", 'gate = "G4"', 'gate = "G10"')
        self.assertEqual(self.run_main()[0], 0)
        t = self.table()
        self.assertLess(t.index('gate = "G1"'), t.index('gate = "G10"'))

    def test_no_row_where_the_shared_table_does_not_list_the_checker_for_this_crate(self):
        self.edit(self.law / "conformance/shared-files.toml", 'path = "scripts/ban.py"\nrepos = ["ALICE-Demo"]',
                  'path = "scripts/ban.py"\nrepos = ["ALICE-Else"]')
        self.assertGap("G1 is pass and has no row")
        self.assertNotIn("scripts/ban.py", self.table())

    def test_no_row_where_the_gate_evidence_does_not_name_the_checker(self):
        self.edit(self.crate / "alice-law-conformance.toml", 'evidence = ["scripts/ban.py with', 'evidence = ["a ban with')
        self.assertGap("G1 is pass and has no row")

    def test_a_longer_path_that_ends_with_the_checker_does_not_name_it(self):
        self.edit(self.crate / "alice-law-conformance.toml", 'evidence = ["scripts/ban.py with',
                  'evidence = ["old/scripts/ban.py with')
        self.assertGap("G1 is pass and has no row")

    def test_a_longer_path_that_starts_with_the_checker_does_not_name_it(self):
        self.edit(self.crate / "alice-law-conformance.toml", 'evidence = ["scripts/ban.py with',
                  'evidence = ["scripts/ban.py.bak with')
        self.assertGap("G1 is pass and has no row")

    def test_no_row_for_a_gate_that_is_not_pass(self):
        self.run_main()
        self.assertNotIn('gate = "G9"', self.table())
        self.assertNotIn('gate = "G11"', self.table())

    def test_a_catalog_entry_whose_canary_is_missing_is_a_gap(self):
        self.edit(self.catalog, "Ban.test_flags_the_canary", "Ban.test_gone")
        self.assertGap("G1 scripts/ban.py: canary")

    def test_a_catalog_entry_whose_zero_is_missing_is_a_gap(self):
        self.edit(self.catalog, "Ban.test_zero_files_fails", "Ban.test_gone")
        self.assertGap("G1 scripts/ban.py: zero")

    def test_an_own_row_that_does_not_resolve_is_a_gap(self):
        self.edit(self.crate / "tests/golden.rs", "fn a_wrong_digest_is_refused", "fn renamed")
        self.assertGap("G4 .github/workflows/ci.yml#run: scripts/run_tests.sh golden: canary")

    def test_a_run_tests_leg_whose_canary_is_in_another_binary_is_a_gap(self):
        write(self.crate, {"tests/other.rs": "#[test]\nfn refused() {}\n"})
        self.edit(self.crate / ".github/workflows/ci.yml", "cargo test --test golden\n",
                  "cargo test --test golden\n      - run: cargo test --test other\n")
        self.edit(self.crate / "conformance/g11-own.toml", "tests/golden.rs::a_wrong_digest_is_refused",
                  "tests/other.rs::refused")
        self.assertGap("is not a test of a binary this run_tests.sh line names (golden)")

    def test_a_run_tests_leg_with_a_minimum_names_its_binary(self):
        write(self.crate, {"tests/x.rs": "#[test]\nfn x() {}\n"})
        self.edit(self.crate / ".github/workflows/ci.yml", "run_tests.sh golden --", "run_tests.sh x,golden=1 --")
        self.edit(self.crate / "conformance/g11-own.toml", "run_tests.sh golden", "run_tests.sh x,golden=1")
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)

    def test_a_folded_run_tests_line_that_ends_with_the_separator_is_checked(self):
        write(self.crate, {"tests/other.rs": "#[test]\nfn refused() {}\n"})
        self.edit(self.crate / ".github/workflows/ci.yml", "run: scripts/run_tests.sh golden -- cargo test --test golden\n",
                  "run: >-\n          scripts/run_tests.sh golden --\n          cargo test --test golden --test other\n")
        self.edit(self.crate / "conformance/g11-own.toml", "#run: scripts/run_tests.sh golden", "#scripts/run_tests.sh golden --")
        self.edit(self.crate / "conformance/g11-own.toml", "tests/golden.rs::a_wrong_digest_is_refused",
                  "tests/other.rs::refused")
        self.assertGap("is not a test of a binary this run_tests.sh line names (golden)")

    def test_a_run_tests_line_without_a_separator_is_a_gap(self):
        self.edit(self.crate / ".github/workflows/ci.yml", "run: scripts/run_tests.sh golden -- cargo test --test golden\n",
                  "run: scripts/run_tests.sh golden\n")
        self.assertGap("has no `--` after its names on that line")

    def test_a_run_tests_line_with_the_separator_only_on_a_continued_line_is_a_gap(self):
        self.edit(self.crate / ".github/workflows/ci.yml", "run: scripts/run_tests.sh golden -- cargo test --test golden\n",
                  "run: |\n          scripts/run_tests.sh golden \\\n            -- cargo test --test golden\n")
        self.edit(self.crate / "conformance/g11-own.toml", "#run: scripts/run_tests.sh golden", "#scripts/run_tests.sh golden")
        self.assertGap("has no `--` after its names on that line")

    def test_an_own_row_for_a_generated_checker_is_a_gap(self):
        own = self.crate / "conformance/g11-own.toml"
        own.write_bytes((own.read_bytes().decode("utf-8") + '\n[[row]]\ngate = "G1"\nchecker = "scripts/ban.py"\n'
                         'canary = "scripts/test_ban.py::Ban.test_flags_the_canary"\n'
                         'zero = "scripts/test_ban.py::Ban.test_zero_files_fails"\nshared = ["scripts/ban.py"]\n')
                        .encode("utf-8"))
        self.assertGap("is a shared checker; its row is generated")

    def test_an_own_row_without_a_field_is_a_gap(self):
        self.edit(self.crate / "conformance/g11-own.toml", 'canary = "tests/golden.rs::a_wrong_digest_is_refused"\n', "")
        self.assertGap("row 1: no canary")

    def test_check_fails_on_an_edited_table(self):
        self.run_main()
        self.edit(self.crate / "conformance/g11.toml", "Ban.test_flags_the_canary", "Ban.test_zero_files_fails")
        self.assertGap("is not the generated table", "--check")

    def test_check_fails_when_the_table_is_missing(self):
        self.assertGap("is not the generated table", "--check")

    def test_non_ascii_text_is_written_and_compared_as_utf8(self):
        self.assertEqual(self.run_main()[0], 0)
        self.assertIn("n/a: 黄金律の試験はこの crate のもの", self.table())
        self.assertEqual(self.run_main("--check")[0], 0)


class CompareNothing(unittest.TestCase):
    setUp = Gen.setUp
    edit = Gen.edit
    run_main = Gen.run_main

    def test_no_pass_gate_exits_2(self):
        p = self.crate / "alice-law-conformance.toml"
        p.write_bytes(p.read_bytes().replace(b'status = "pass"', b'status = "unverified"'))
        rc, out = self.run_main()
        self.assertEqual(rc, 2, out)
        self.assertIn("compared nothing", out)

    def test_no_row_exits_2(self):
        (self.crate / "conformance/g11-own.toml").unlink()
        self.edit(self.crate / "alice-law-conformance.toml", 'evidence = ["scripts/ban.py with', 'evidence = ["a ban with')
        rc, out = self.run_main()
        self.assertEqual(rc, 2, out)
        self.assertIn("compared nothing", out)

    def test_a_missing_shared_files_pin_exits_2(self):
        (self.crate / ".github/shared-files-pin").unlink()
        rc, out = self.run_main()
        self.assertEqual(rc, 2, out)
        self.assertIn(".github/shared-files-pin is not in the tree", out)

    def test_an_unreadable_law_revision_exits_2(self):
        rc, out = self.run_main_with_law(f"{'c' * 40}={self.law}")
        self.assertEqual(rc, 2, out)

    def run_main_with_law(self, spec_):
        self.edit(self.crate / ".github/shared-files-pin", f"rev {REV}", f"rev {'d' * 40}")
        saved = gen.chk.git_law
        self.addCleanup(setattr, gen.chk, "git_law", saved)
        gen.chk.git_law = lambda rev, url=gen.chk.LAW_URL: None
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = gen.main(["--repo", str(self.crate), "--name", NAME, "--catalog", str(self.catalog),
                           "--law", spec_, "--write"])
        return rc, out.getvalue() + err.getvalue()


class Catalog(unittest.TestCase):
    def test_the_repository_catalog_loads(self):
        entries = gen.load_catalog(gen.CATALOG.read_text(encoding="utf-8"))
        self.assertGreater(len(entries), 0)
        for e in entries:
            self.assertNotEqual(e["canary"], e["zero"], e["path"])

    def test_a_path_listed_twice_is_refused(self):
        with self.assertRaisesRegex(ValueError, "listed twice"):
            gen.load_catalog(CATALOG + CATALOG.split("\n\n")[0] + "\n")

    def test_shared_without_the_checker_is_refused(self):
        with self.assertRaisesRegex(ValueError, "does not list the checker itself"):
            gen.load_catalog(CATALOG.replace('shared = ["scripts/other.py"]', 'shared = ["scripts/x.py"]'))

    def test_an_entry_without_a_zero_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no zero"):
            gen.load_catalog(CATALOG.replace('zero = "scripts/test_other.py::Other.test_zero"\n', ""))


if __name__ == "__main__":
    unittest.main()
