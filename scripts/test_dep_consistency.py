#!/usr/bin/env python3
"""Tests of scripts/dep_consistency.py (python3 scripts/test_dep_consistency.py)

No network: the lock file and the manifest are fixtures.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("dc", HERE / "dep_consistency.py")
dc = importlib.util.module_from_spec(spec)
sys.modules["dc"] = dc
spec.loader.exec_module(dc)

SHA = "0123456789abcdef0123456789abcdef01234567"
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"


def main_source(crate: str) -> str:
    return f"git+https://github.com/ext-sakamoro/{crate.upper()}?branch=main#{SHA}"


def lock(*pkgs: tuple, source=None) -> str:
    """packages (name, version) or (name, version, source); by default from the repository's main branch"""
    out = "version = 4\n\n"
    for p in pkgs:
        n, v = p[0], p[1]
        src = p[2] if len(p) > 2 else (source or main_source(n))
        out += f'[[package]]\nname = "{n}"\nversion = "{v}"\n' + (f'source = "{src}"\n' if src else "") + "\n"
    return out


ALL_CORE = [(c, "1.0.0") for c in dc.CORE]


def dep(crate: str, extra: str = "") -> str:
    return f'{crate} = {{ git = "https://github.com/ext-sakamoro/{crate.upper()}", branch = "main", version = "1.0"{extra} }}\n'


MANIFEST = "[package]\nname = \"x\"\n\n[dependencies]\n" + "".join(dep(c) for c in dc.CORE) + 'serde = "1"\n'


class Matches(unittest.TestCase):
    """Cargo's requirement rules (https://doc.rust-lang.org/cargo/reference/specifying-dependencies.html)"""

    def test_caret(self):
        for req, yes, no in [
            ("^1.2.3", ["1.2.3", "1.9.0"], ["1.2.2", "2.0.0"]),
            ("^1.2", ["1.2.0", "1.99.0"], ["1.1.9", "2.0.0"]),
            ("^1", ["1.0.0", "1.9.9"], ["0.9.0", "2.0.0"]),
            ("^0.2.3", ["0.2.3", "0.2.9"], ["0.2.2", "0.3.0"]),
            ("^0.2", ["0.2.0", "0.2.9"], ["0.1.9", "0.3.0"]),
            ("^0.0.3", ["0.0.3"], ["0.0.4", "0.0.2"]),
            ("^0.0", ["0.0.0", "0.0.9"], ["0.1.0"]),
            ("^0", ["0.0.0", "0.9.9"], ["1.0.0"]),
            ("0.4", ["0.4.0", "0.4.7"], ["0.5.0", "0.3.9"]),  # a bare requirement is a caret
        ]:
            for v in yes:
                self.assertTrue(dc.matches(req, v), f"{req} should accept {v}")
            for v in no:
                self.assertFalse(dc.matches(req, v), f"{req} should refuse {v}")

    def test_tilde_exact_wildcard_and_ranges(self):
        for req, yes, no in [
            ("~1.2.3", ["1.2.3", "1.2.9"], ["1.3.0"]),
            ("~1.2", ["1.2.0", "1.2.9"], ["1.3.0"]),
            ("~1", ["1.0.0", "1.9.0"], ["2.0.0"]),
            ("=1.2.3", ["1.2.3"], ["1.2.4"]),
            ("=1.2", ["1.2.0", "1.2.9"], ["1.3.0"]),
            ("*", ["0.0.1", "9.9.9"], []),
            ("1.*", ["1.0.0", "1.9.9"], ["2.0.0"]),
            (">= 1.2, < 1.5", ["1.2.0", "1.4.9"], ["1.5.0", "1.1.9"]),
            ("> 1.2", ["1.3.0"], ["1.2.9"]),
            ("> 1.2.3", ["1.2.4"], ["1.2.3"]),
            ("<= 1.2", ["1.2.9"], ["1.3.0"]),
            # wildcards stand for omitted parts, and a bare wildcard requirement is exact on the
            # parts it gives (not a caret): `1.2.*` is `=1.2`, so 1.3.0 and 1.13.0 are refused
            ("1.2.*", ["1.2.0", "1.2.9"], ["1.3.0", "1.13.0", "1.1.9", "1.2.0-beta.1"]),
            ("1.2.x", ["1.2.5"], ["1.3.0"]),
            ("1.X.*", ["1.0.0", "1.13.0"], ["2.0.0", "0.9.0"]),
            ("0.*", ["0.0.0", "0.9.0"], ["1.0.0"]),
            ("x", ["0.0.1", "9.0.0"], ["1.0.0-beta.1"]),
            ("^1.2.*", ["1.2.0", "1.13.0"], ["2.0.0", "1.1.0"]),
            (">1.2.*", ["1.3.0"], ["1.2.9"]),
            ("<=1.*", ["1.9.9", "0.1.0"], ["2.0.0"]),
            # a full version with a pre-release bounds the range exactly (found by the
            # differential check against the semver crate: it was read as unbounded)
            ("<=0.1.1-beta.1", ["0.1.1-beta.1", "0.1.0"], ["1.2.1", "0.1.1"]),
            (">=0.3.0-beta.2", ["0.3.0", "1.0.0"], ["0.2.9"]),
        ]:
            for v in yes:
                self.assertTrue(dc.matches(req, v), f"{req} should accept {v}")
            for v in no:
                self.assertFalse(dc.matches(req, v), f"{req} should refuse {v}")

    def test_pre_releases(self):
        # a pre-release satisfies a requirement only when it names the same x.y.z with a pre-release
        self.assertTrue(dc.matches("^0.3.0-beta.1", "0.3.0-beta.2"))
        self.assertTrue(dc.matches("^0.3.0-beta.1", "0.3.0"))
        self.assertTrue(dc.matches("^0.3.0-beta.1", "0.3.5"))
        self.assertFalse(dc.matches("^0.3.0-beta.2", "0.3.0-beta.1"))
        self.assertFalse(dc.matches("^0.3", "0.3.1-beta.1"), "no pre-release named in the requirement")
        self.assertFalse(dc.matches("^0.3.0-beta.1", "0.3.1-beta.1"), "another x.y.z's pre-release")
        self.assertFalse(dc.matches("^0.14.0-preview.4", "2.2.0"))
        self.assertTrue(dc.matches("=0.3.0-beta.2", "0.3.0-beta.2"))
        self.assertFalse(dc.matches("=0.3.0-beta.2", "0.3.0"))
        self.assertTrue(dc.Version("1.0.0-alpha") < dc.Version("1.0.0-alpha.1") < dc.Version("1.0.0-beta.2")
                        < dc.Version("1.0.0-beta.11") < dc.Version("1.0.0-rc.1") < dc.Version("1.0.0"))

    def test_unreadable_input_is_an_error(self):
        for req in ("", "^q", "1.2.3.4", "1.*.3", "*.2", "1.2.*-beta", "^x", "^*", ">=*", "~X", "*.*", "x.X",
                    "<=1, *", ">=1.0, <2, x"):
            with self.assertRaises(ValueError, msg=req):
                dc.matches(req, "1.0.0")



class Checks(unittest.TestCase):
    def test_a_consistent_set_passes(self):
        _, problems, tally = dc.check(lock(*ALL_CORE, ("x", "0.0.0", None)), MANIFEST)
        self.assertEqual(problems, [])
        n = len(dc.CORE)
        self.assertEqual(tally, {"one version per crate": n, "core crates present": n, "from the main branches": 2 * n},
                         "each locked alice-* package and each of this repository's alice-* dependencies (own package not counted)")

    def test_two_versions_of_a_crate_fail(self):
        _, problems, _ = dc.check(lock(*ALL_CORE, ("alice-zip", "0.7.0")), MANIFEST)
        self.assertEqual(problems, ["alice-zip is linked at 2 versions: 0.7.0, 1.0.0"])

    def test_a_non_core_alice_crate_is_also_checked(self):
        _, problems, _ = dc.check(lock(*ALL_CORE, ("alice-cache", "0.1.0"), ("alice-cache", "0.2.0")), MANIFEST)
        self.assertEqual(problems, ["alice-cache is linked at 2 versions: 0.1.0, 0.2.0"])

    def test_a_missing_core_crate_fails(self):
        _, problems, _ = dc.check(lock(*[p for p in ALL_CORE if p[0] != "alice-crypto"]), MANIFEST)
        self.assertEqual(problems, ["alice-crypto is not in Cargo.lock"])

    def test_a_crates_io_source_in_the_lock_fails(self):
        _, problems, _ = dc.check(lock(*[p for p in ALL_CORE if p[0] != "alice-zip"], ("alice-zip", "1.0.0", REGISTRY)),
                                  MANIFEST)
        self.assertEqual(problems, [f"alice-zip 1.0.0 comes from {REGISTRY}, not from its repository's main branch"])

    def test_a_commit_or_tag_source_in_the_lock_fails(self):
        for src in (f"git+https://github.com/ext-sakamoro/ALICE-ZIP?rev={SHA}#{SHA}",
                    f"git+https://github.com/ext-sakamoro/ALICE-ZIP?tag=v1.0.0#{SHA}",
                    f"git+https://github.com/ext-sakamoro/ALICE-ZIP?branch=dev#{SHA}",
                    f"git+https://example.invalid/ALICE-ZIP?branch=main#{SHA}"):
            _, problems, _ = dc.check(lock(*[p for p in ALL_CORE if p[0] != "alice-zip"], ("alice-zip", "1.0.0", src)),
                                      MANIFEST)
            self.assertEqual(len(problems), 1, src)
            self.assertIn("not from its repository's main branch", problems[0], src)

    def test_a_non_alice_package_is_not_judged_by_source(self):
        _, problems, tally = dc.check(lock(*ALL_CORE, ("serde", "1.0.0", REGISTRY)), MANIFEST)
        self.assertEqual(problems, [])
        self.assertEqual(tally["from the main branches"], 2 * len(dc.CORE))

    def test_this_repository_must_name_the_main_branches_too(self):
        for spec, why in (('"1.0"', "crates.io"), (f'{{ git = "https://github.com/ext-sakamoro/ALICE-LOL", rev = "{SHA}", version = "1.0" }}', "rev"),
                          ('{ git = "https://github.com/ext-sakamoro/ALICE-LOL", version = "1.0" }', "no branch"),
                          ('{ git = "https://github.com/ext-sakamoro/ALICE-LOL", branch = "dev", version = "1.0" }', "dev")):
            manifest = MANIFEST.replace(dep("alice-lol"), f"alice-lol = {spec}\n")
            _, problems, _ = dc.check(lock(*ALL_CORE), manifest)
            self.assertEqual(len(problems), 1, why)
            self.assertIn("this repository names alice-lol", problems[0], why)

    def test_manifest_dependencies(self):
        text = ('[package]\nname = "x"\n[dependencies]\n' + dep("alice-a") + 'serde = "1"\n'
                '[dependencies.alice-d]\ngit = "https://github.com/ext-sakamoro/ALICE-D"\nbranch = "main"\nversion = "0.8"\n'
                '[dev-dependencies]\nalice-c = "9"\n')
        name, deps = dc.manifest_dependencies(text)
        self.assertEqual(name, "x")
        self.assertEqual(sorted(deps), ["alice-a", "alice-d"])
        self.assertEqual(deps["alice-d"]["branch"], "main")

    def test_a_path_dependency_or_one_without_a_version_is_refused(self):
        for spec in ('{ path = "../ALICE-LOL" }', '{ path = "../ALICE-LOL", version = "1" }',
                     '{ git = "https://github.com/ext-sakamoro/ALICE-LOL", branch = "main" }'):
            with self.assertRaises(ValueError, msg=spec):
                dc.manifest_dependencies(f'[package]\nname = "x"\n[dependencies]\nalice-lol = {spec}\n')


class Cli(unittest.TestCase):
    def run_main(self, lock_text, manifest=MANIFEST):
        with tempfile.TemporaryDirectory() as d:
            lp, mp = Path(d, "Cargo.lock"), Path(d, "Cargo.toml")
            if lock_text is not None:
                lp.write_text(lock_text, encoding="utf-8")
            mp.write_text(manifest, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dc.main(["--lock", str(lp), "--manifest", str(mp)])
            return code, out.getvalue(), err.getvalue()

    def test_exit_codes(self):
        self.assertEqual(self.run_main(lock(*ALL_CORE))[0], 0)
        code, out, _ = self.run_main(lock(*ALL_CORE, ("alice-zip", "0.7.0")))
        self.assertEqual(code, 1)
        self.assertIn("1 problems", out)
        self.assertEqual(self.run_main(lock(*ALL_CORE, source=REGISTRY))[0], 1, "everything from crates.io")

    def test_nothing_compared_fails(self):
        self.assertEqual(self.run_main("version = 4\n")[0], 2, "an empty lock file")
        self.assertEqual(self.run_main(None)[0], 2, "no lock file")

    def test_a_check_that_compares_nothing_fails(self):
        # no alice-* package in the lock and no alice-* dependency: nothing is from main or not
        bare = '[package]\nname = "x"\n[dependencies]\nserde = "1"\n'
        code, out, err = self.run_main(lock(("serde", "1.0.0", REGISTRY)), manifest=bare)
        self.assertEqual(code, 2)
        self.assertIn("nothing was compared by: one version per crate, from the main branches", err)

    def test_this_repositorys_package_is_not_a_row(self):
        code, out, _ = self.run_main(lock(*ALL_CORE, ("x", "0.0.0", None), ("alice-law-x", "0.0.0", None)),
                                     manifest=MANIFEST.replace('name = "x"', 'name = "alice-law-x"'))
        self.assertEqual(code, 0)
        self.assertNotIn("| alice-law-x |", out)
        self.assertIn(f"| one version per crate | {len(dc.CORE)} |", out)

    def test_a_path_dependency_stops_the_run(self):
        code, _, err = self.run_main(lock(*ALL_CORE), manifest=MANIFEST + '[dependencies.alice-x]\npath = "../x"\n')
        self.assertEqual(code, 2)
        self.assertIn("path", err)


# the roster records alice-db at 1.9.0 on main; an entry until 2.0.0 is pending, and stale once the
# roster records 2.0.0 or later
ROSTER_BASE = """
[crate.alice-db]
version = "1.9.0"
state = "main"
order = 0
"""
# alice-zip linked twice, the second copy a commit pin some core crate still names
OLD_ZIP = f"git+https://github.com/ext-sakamoro/ALICE-ZIP?rev={SHA}#{SHA}"
TWO_ZIP = dict(lock_pkgs=(*ALL_CORE, ("alice-zip", "0.7.0", OLD_ZIP)))
PENDING = """
[pending.db-2]
until = "alice-db 2.0.0"
linked = ["alice-zip"]
source = ["alice-zip"]
"""


class Pending(unittest.TestCase):
    """problems a coming change removes: named, counted, and time-boxed"""

    def run_main(self, roster, lock_pkgs=ALL_CORE, manifest=MANIFEST):
        with tempfile.TemporaryDirectory() as d:
            lp, mp, rp = Path(d, "Cargo.lock"), Path(d, "Cargo.toml"), Path(d, "r.toml")
            lp.write_text(lock(*lock_pkgs), encoding="utf-8")
            mp.write_text(manifest, encoding="utf-8")
            rp.write_text(roster, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dc.main(["--lock", str(lp), "--manifest", str(mp), "--roster", str(rp)])
            return code, out.getvalue(), err.getvalue()

    def test_listed_problems_are_expected_and_counted(self):
        code, out, err = self.run_main(ROSTER_BASE + PENDING, **TWO_ZIP)
        self.assertEqual(code, 0, err)
        self.assertIn("| db-2 | alice-db 2.0.0 | 2 |", out)
        self.assertIn("0 problems, expected (2) until a release", out)

    def test_an_unlisted_problem_is_still_a_problem(self):
        code, _, err = self.run_main(ROSTER_BASE + PENDING.replace('source = ["alice-zip"]\n', ""), **TWO_ZIP)
        self.assertEqual(code, 1)
        self.assertIn("alice-zip 0.7.0 comes from", err)

    def test_a_pending_entry_past_its_version_is_stale(self):
        for recorded in ("2.0.0", "2.1.0"):
            roster = ROSTER_BASE.replace('version = "1.9.0"', f'version = "{recorded}"') + PENDING
            code, _, err = self.run_main(roster, **TWO_ZIP)
            self.assertEqual(code, 1, recorded)
            self.assertIn("is stale", err)

    def test_an_item_that_matches_no_problem_fails(self):
        roster = ROSTER_BASE + PENDING.replace('linked = ["alice-zip"]', 'linked = ["alice-zip", "alice-sdf"]')
        code, _, err = self.run_main(roster, **TWO_ZIP)
        self.assertEqual(code, 1)
        self.assertIn("('linked', 'alice-sdf'), which is not a problem any more", err)
        self.assertEqual(self.run_main(ROSTER_BASE + PENDING)[0], 1)  # nothing is wrong at all

    def test_this_repositorys_own_dependency_cannot_be_listed(self):
        manifest = MANIFEST.replace(dep("alice-lol"), 'alice-lol = "1.0"\n')
        roster = ROSTER_BASE + PENDING.replace('source = ["alice-zip"]', 'source = ["alice-zip", "alice-lol"]')
        code, _, err = self.run_main(roster, manifest=manifest, **TWO_ZIP)
        self.assertEqual(code, 1)
        self.assertIn("this repository names alice-lol", err)

    def test_malformed_entries_exit_2(self):
        for bad in (PENDING.replace('until = "alice-db 2.0.0"', 'until = "alice-db 2.x"'),  # not a version
                    PENDING.replace('until = "alice-db 2.0.0"', 'until = "alice-nope 1.0.0"'),
                    PENDING.replace('until = "alice-db 2.0.0"', 'until = "alice-db"'),
                    PENDING.replace('linked = ["alice-zip"]', 'linked = ["alice-zip", "alice-zip"]'),
                    PENDING.replace('linked = ["alice-zip"]', 'linkd = ["alice-zip"]'),
                    PENDING.replace('source = ["alice-zip"]', 'requires = ["alice-db 1.0.0 -> alice-zip ^0.7"]'),
                    '[pending.empty]\nuntil = "alice-db 2.0.0"\n',
                    PENDING + PENDING.replace("db-2", "db-2b")):  # the same problem in two entries
            self.assertEqual(self.run_main(ROSTER_BASE + bad, **TWO_ZIP)[0], 2, bad)

    def test_the_repository_roster_entries_are_well_formed(self):
        entries = dc.load_pending((HERE.parent / "release-train.toml").read_text(encoding="utf-8"))
        self.assertTrue(all(e["keys"] for e in entries))


if __name__ == "__main__":
    unittest.main()
