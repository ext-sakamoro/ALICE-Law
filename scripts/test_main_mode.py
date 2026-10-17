#!/usr/bin/env python3
"""Tests for main_mode.py: the requirement raise of the CI copy and what verify counts as a problem."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("main_mode", HERE / "main_mode.py")
mm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm)

MANIFEST = '''[package]
name = "alice-law-e2e"
version = "0.0.0"

[dependencies]
alice-db = "0.5"
alice-zip = "0.10"
alice-sdf = "6"
alice-edge = { version = ">=0.2, <0.4", default-features = false, features = ["std", "law"] }
serde = "1"

[features]
main-mode = []

[workspace]
'''


def pkg(name: str, version: str, source: str | None) -> dict:
    return {"name": name, "version": version, "source": source}


REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"


class Accepts(unittest.TestCase):
    def test_cargo_rules_for_the_forms_e2e_uses(self):
        cases = [
            ("0.5", "0.5.1", True), ("0.5", "0.6.0", False), ("3", "3.9.0", True), ("3", "4.0.0", False),
            ("^0.0.3", "0.0.3", True), ("^0.0.3", "0.0.4", False), ("0.4.1", "0.4.0", False),
            (">=0.4.1, <0.6", "0.5.0", True), (">=0.4.1, <0.6", "0.6.0", False), (">=0.2, <0.4", "0.4.0", False),
            ("=0.4.1", "0.4.1", True), ("=0.4.1", "0.4.2", False), (">0.4", "0.4.1", True), ("<=1", "1.0.0", True),
        ]
        for req, version, want in cases:
            self.assertEqual(mm.accepts(req, version), want, f"{req!r} accepts {version}")

    def test_a_comparator_this_does_not_read_is_refused_not_guessed(self):
        with self.assertRaises(ValueError):
            mm.accepts("~1.2", "1.2.3")
        with self.assertRaises(ValueError):
            mm.accepts("*", "1.2.3")


class Raise(unittest.TestCase):
    def test_only_the_requirements_that_reject_main_are_raised_and_the_feature_is_on(self):
        versions = {"alice-db": "0.6.0", "alice-zip": "0.11.0", "alice-sdf": "6.1.0", "alice-edge": "0.4.0"}
        out, changed = mm.raise_requirements(MANIFEST, versions)
        self.assertEqual(changed, ['alice-db: "0.5" -> "0.6.0"', 'alice-zip: "0.10" -> "0.11.0"',
                                   'alice-edge: ">=0.2, <0.4" -> "0.4.0"'])
        self.assertIn('alice-db = "0.6.0"\n', out)
        self.assertIn('alice-sdf = "6"\n', out)  # 6.1.0 accepted: left as it is
        self.assertIn('alice-edge = { version = "0.4.0", default-features = false, features = ["std", "law"] }', out)
        self.assertIn('serde = "1"\n', out)  # not a core crate
        self.assertIn('[features]\ndefault = ["main-mode"]\nmain-mode = []', out)

    def test_nothing_to_raise_still_turns_the_feature_on(self):
        out, changed = mm.raise_requirements(MANIFEST, {"alice-db": "0.5.1"})
        self.assertEqual(changed, [])
        self.assertIn('default = ["main-mode"]', out)

    def test_a_manifest_without_the_feature_is_refused(self):
        with self.assertRaises(ValueError):
            mm.raise_requirements(MANIFEST.replace("main-mode = []\n", ""), {"alice-db": "0.6.0"})


class Problems(unittest.TestCase):
    def all_from_main(self) -> list[dict]:
        return [pkg(c, "1.0.0", None) for c in mm.CRATES]

    def test_every_core_crate_from_its_checkout_is_clean(self):
        _, problems, compared = mm.problems_of(self.all_from_main())
        self.assertEqual(problems, [])
        self.assertEqual(compared, len(mm.CRATES))

    def test_a_member_rejecting_another_members_new_major_stays_red(self):
        # alice-db main still requires alice-zip 0.10 while alice-zip main is 0.11: the patch is used
        # for the e2e crate's own (raised) requirement and the registry release comes in for alice-db
        packages = self.all_from_main() + [pkg("alice-zip", "0.10.0", REGISTRY)]
        _, problems, _ = mm.problems_of(packages)
        self.assertEqual(problems, [f"alice-zip 0.10.0: from {REGISTRY}, not from main"])

    def test_a_core_crate_never_resolved_from_main_is_a_problem(self):
        packages = [p for p in self.all_from_main() if p["name"] != "alice-edge"]
        _, problems, _ = mm.problems_of(packages)
        self.assertEqual(problems, ["alice-edge: not resolved from main (0 times)"])
        packages.append(pkg("alice-edge", "0.3.0", REGISTRY))
        _, problems, _ = mm.problems_of(packages)
        self.assertIn("alice-edge: not resolved from main (0 times)", problems)
        self.assertIn(f"alice-edge 0.3.0: from {REGISTRY}, not from main", problems)

    def test_no_core_crate_in_the_graph_compares_nothing(self):
        _, problems, compared = mm.problems_of([pkg("serde", "1.0.0", REGISTRY)])
        self.assertEqual(compared, 0)
        self.assertEqual(len(problems), len(mm.CRATES))


class Verify(unittest.TestCase):
    def test_without_the_patch_it_says_so_instead_of_reading_every_crate_as_from_crates_io(self):
        import contextlib
        import io
        import tempfile
        saved = mm.CONFIG
        with tempfile.TemporaryDirectory() as d:
            mm.CONFIG = Path(d) / ".cargo" / "config.toml"
            self.addCleanup(setattr, mm, "CONFIG", saved)
            mm_root = mm.ROOT
            mm.ROOT = Path(d)
            self.addCleanup(setattr, mm, "ROOT", mm_root)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = mm.verify()
        self.assertEqual(code, 2)
        self.assertIn("the patch did not run", err.getvalue())


if __name__ == "__main__":
    unittest.main()
