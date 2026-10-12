#!/usr/bin/env python3
"""Tests of scripts/dep_consistency.py (python3 scripts/test_dep_consistency.py)

No network: crates.io is replaced by a fixture.
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


def lock(*pkgs: tuple[str, str]) -> str:
    return "version = 4\n\n" + "".join(
        f'[[package]]\nname = "{n}"\nversion = "{v}"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\n\n'
        for n, v in pkgs)


ALL_CORE = [(c, "1.0.0") for c in dc.CORE]
MANIFEST = "[package]\nname = \"x\"\n\n[dependencies]\n" + "".join(f'{c} = "1.0"\n' for c in dc.CORE)


def fixture(latest: dict[str, str] | None = None, deps: dict[tuple[str, str], list[tuple[str, str, bool]]] | None = None):
    latest = latest or {c: "1.0.0" for c in dc.CORE}
    deps = deps or {}

    def fetch(path: str) -> dict:
        parts = path.split("/")
        if len(parts) == 1:
            return {"crate": {"max_stable_version": latest[parts[0]], "max_version": latest[parts[0]]}}
        crate, version, _ = parts
        return {"dependencies": [
            {"crate_id": d, "req": r, "optional": o, "kind": "normal"} for d, r, o in deps.get((crate, version), [])
        ] + [{"crate_id": "serde", "req": "^1", "optional": False, "kind": "normal"},
             {"crate_id": "alice-zip", "req": "^0.1", "optional": False, "kind": "dev"}]}
    return fetch


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
    def run_check(self, lock_text, manifest=MANIFEST, fetch=None):
        return dc.check(lock_text, manifest, fetch or fixture())

    def test_a_consistent_set_passes(self):
        _, problems, tally = self.run_check(lock(*ALL_CORE, ("x", "0.0.0")))
        self.assertEqual(problems, [])
        n = len(dc.CORE)
        self.assertEqual(tally, {"one version per crate": n, "core crates present": n,
                                 "requirements accept the latest": n},
                         "one row per core crate (this repository's own package not counted)")

    def test_two_versions_of_a_crate_fail(self):
        _, problems, _ = self.run_check(lock(*ALL_CORE, ("alice-zip", "0.7.0")))
        self.assertEqual(problems, ["alice-zip is linked at 2 versions: 0.7.0, 1.0.0"])

    def test_a_non_core_alice_crate_is_also_checked(self):
        _, problems, _ = self.run_check(lock(*ALL_CORE, ("alice-cache", "0.1.0"), ("alice-cache", "0.2.0")))
        self.assertEqual(problems, ["alice-cache is linked at 2 versions: 0.1.0, 0.2.0"])

    def test_a_missing_core_crate_fails(self):
        _, problems, _ = self.run_check(lock(*[p for p in ALL_CORE if p[0] != "alice-crypto"]))
        self.assertEqual(problems, ["alice-crypto is not in Cargo.lock"])

    def test_a_requirement_that_refuses_the_latest_fails(self):
        latest = {c: "1.0.0" for c in dc.CORE} | {"alice-zip": "1.1.0"}
        deps = {("alice-db", "1.0.0"): [("alice-zip", "~1.0", False), ("alice-crypto", "^1", True)]}
        _, problems, tally = self.run_check(lock(*ALL_CORE), fetch=fixture(latest, deps))
        self.assertEqual(problems, ["alice-db 1.0.0 requires alice-zip ~1.0, which does not accept the latest release 1.1.0"])
        self.assertEqual(tally["requirements accept the latest"], len(dc.CORE) + 2, "non-alice and dev dependencies are not compared")

    def test_this_repository_must_follow_the_latest_too(self):
        latest = {c: "1.0.0" for c in dc.CORE} | {"alice-lol": "2.0.0"}
        _, problems, _ = self.run_check(lock(*ALL_CORE), fetch=fixture(latest))
        self.assertEqual(problems, ["this repository requires alice-lol 1.0, which does not accept the latest release 2.0.0"])

    def test_each_resolved_version_is_asked(self):
        asked = []
        base = fixture()

        def fetch(path):
            asked.append(path)
            return base(path)
        self.run_check(lock(*ALL_CORE, ("alice-zip", "0.7.0")), fetch=fetch)
        self.assertIn("alice-zip/0.7.0/dependencies", asked)
        self.assertIn("alice-zip/1.0.0/dependencies", asked)

    def test_latest_is_the_latest_stable_release_when_there_is_one(self):
        # crates.io's max_version includes pre-releases: 0.5.0-beta.1 published after 0.4.0
        # must not make every `^0.4` requirement "behind"; with no stable release (alice-db
        # today) the pre-release is the latest
        def fetch(path):
            if path == "alice-det-math":
                return {"crate": {"max_stable_version": "1.0.0", "max_version": "1.1.0-beta.1"}}
            if path == "alice-db":
                return {"crate": {"max_stable_version": None, "max_version": "1.0.0-beta.2"}}
            return fixture()(path)
        self.assertEqual(dc.latest_release(fetch, "alice-det-math"), "1.0.0")
        self.assertEqual(dc.latest_release(fetch, "alice-db"), "1.0.0-beta.2")
        _, problems, _ = self.run_check(lock(*ALL_CORE), fetch=fetch)
        self.assertEqual(problems, ["this repository requires alice-db 1.0, which does not accept the latest release 1.0.0-beta.2"])

    def test_manifest_requirements(self):
        text = ('[package]\nname = "x"\n[dependencies]\nalice-a = "0.3"\n'
                'alice-b = { version = "0.3.0-beta.2", features = ["c"] }\nserde = "1"\n'
                '[dependencies.alice-d]\nversion = "0.8"\nfeatures = ["e"]\n'
                '[dev-dependencies]\nalice-c = "9"\n')
        self.assertEqual(dc.manifest_requirements(text),
                         ("x", {"alice-a": "0.3", "alice-b": "0.3.0-beta.2", "alice-d": "0.8"}))

    def test_a_table_form_requirement_is_checked(self):
        # `[dependencies.alice-lol]` must not read as "no requirement": here it is behind
        manifest = MANIFEST.replace('alice-lol = "1.0"\n', "") + '[dependencies.alice-lol]\nversion = "0.9"\n'
        _, problems, tally = self.run_check(lock(*ALL_CORE), manifest=manifest)
        self.assertEqual(problems, ["this repository requires alice-lol 0.9, which does not accept the latest release 1.0.0"])
        self.assertEqual(tally["requirements accept the latest"], len(dc.CORE))

    def test_a_path_or_git_dependency_is_refused(self):
        for spec in ('{ path = "../ALICE-LOL" }', '{ path = "../ALICE-LOL", version = "1" }',
                     '{ git = "https://example.invalid/x" }'):
            with self.assertRaises(ValueError, msg=spec):
                dc.manifest_requirements(f'[package]\nname = "x"\n[dependencies]\nalice-lol = {spec}\n')


class Cli(unittest.TestCase):
    def run_main(self, lock_text, manifest=MANIFEST, fetch=None):
        with tempfile.TemporaryDirectory() as d:
            lp, mp = Path(d, "Cargo.lock"), Path(d, "Cargo.toml")
            if lock_text is not None:
                lp.write_text(lock_text, encoding="utf-8")
            mp.write_text(manifest, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dc.main(["--lock", str(lp), "--manifest", str(mp)], fetch=fetch or fixture())
            return code, out.getvalue(), err.getvalue()

    def test_exit_codes(self):
        self.assertEqual(self.run_main(lock(*ALL_CORE))[0], 0)
        code, out, _ = self.run_main(lock(*ALL_CORE, ("alice-zip", "0.7.0")))
        self.assertEqual(code, 1)
        self.assertIn("1 problems", out)

    def test_nothing_compared_fails(self):
        self.assertEqual(self.run_main("version = 4\n")[0], 2, "an empty lock file")
        self.assertEqual(self.run_main(None)[0], 2, "no lock file")

    def test_a_check_that_compares_nothing_fails(self):
        # no alice-* requirement anywhere: check 3 compares nothing, so the run is not a pass
        bare = '[package]\nname = "x"\n[dependencies]\nserde = "1"\n'
        code, out, err = self.run_main(lock(*ALL_CORE), manifest=bare)
        self.assertEqual(code, 2)
        self.assertIn("nothing was compared by: requirements accept the latest", err)
        self.assertIn("| requirements accept the latest | 0 |", out)

    def test_this_repositorys_package_is_not_a_row(self):
        code, out, _ = self.run_main(lock(*ALL_CORE, ("x", "0.0.0"), ("alice-law-x", "0.0.0")),
                                     manifest=MANIFEST.replace('name = "x"', 'name = "alice-law-x"'))
        self.assertEqual(code, 0)
        self.assertNotIn("| alice-law-x |", out)
        self.assertIn(f"| one version per crate | {len(dc.CORE)} |", out)

    def test_a_path_dependency_stops_the_run(self):
        code, _, err = self.run_main(lock(*ALL_CORE), manifest=MANIFEST + '[dependencies.alice-x]\npath = "../x"\n')
        self.assertEqual(code, 2)
        self.assertIn("not a crates.io dependency", err)

    def test_crates_io_failure_is_not_a_pass(self):
        def broken(path):
            raise dc.urllib.error.URLError("offline")
        self.assertEqual(self.run_main(lock(*ALL_CORE), fetch=broken)[0], 2)


ROSTER_BASE = """
[crate.alice-db]
version = "2.0.0"
state = "prepared"
order = 0
"""
# alice-zip linked twice (0.7.0 brought by alice-db 1.0.0, whose ^0.7 does not accept the latest)
TWO_ZIP = dict(lock_pkgs=(*ALL_CORE, ("alice-zip", "0.7.0")),
               deps={("alice-db", "1.0.0"): [("alice-zip", "^0.7", False)]})
PENDING = """
[pending.db-2]
until = "alice-db 2.0.0"
linked = ["alice-zip"]
requires = ["alice-db 1.0.0 -> alice-zip ^0.7"]
"""


class Pending(unittest.TestCase):
    """problems a coming release removes: named, counted, and time-boxed to that release"""

    def run_main(self, roster, lock_pkgs=ALL_CORE, deps=None, manifest=MANIFEST):
        with tempfile.TemporaryDirectory() as d:
            lp, mp, rp = Path(d, "Cargo.lock"), Path(d, "Cargo.toml"), Path(d, "r.toml")
            lp.write_text(lock(*lock_pkgs), encoding="utf-8")
            mp.write_text(manifest, encoding="utf-8")
            rp.write_text(roster, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dc.main(["--lock", str(lp), "--manifest", str(mp), "--roster", str(rp)],
                               fetch=fixture(None, deps))
            return code, out.getvalue(), err.getvalue()

    def test_listed_problems_are_expected_and_counted(self):
        code, out, err = self.run_main(ROSTER_BASE + PENDING, **TWO_ZIP)
        self.assertEqual(code, 0, err)
        self.assertIn("| db-2 | alice-db 2.0.0 | 2 |", out)
        self.assertIn("0 problems, expected (2) until a release", out)

    def test_without_the_roster_they_are_problems(self):
        with tempfile.TemporaryDirectory() as d:
            lp, mp = Path(d, "Cargo.lock"), Path(d, "Cargo.toml")
            lp.write_text(lock(*TWO_ZIP["lock_pkgs"]), encoding="utf-8")
            mp.write_text(MANIFEST, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = dc.main(["--lock", str(lp), "--manifest", str(mp)], fetch=fixture(None, TWO_ZIP["deps"]))
        self.assertEqual(code, 1)

    def test_an_unlisted_problem_is_still_a_problem(self):
        roster = ROSTER_BASE + PENDING.replace('requires = ["alice-db 1.0.0 -> alice-zip ^0.7"]\n', "")
        code, _, err = self.run_main(roster, **TWO_ZIP)
        self.assertEqual(code, 1)
        self.assertIn("alice-db 1.0.0 requires alice-zip ^0.7", err)

    def test_a_pending_entry_past_its_release_is_stale(self):
        roster = ROSTER_BASE.replace('state = "prepared"', 'state = "published"') + PENDING
        code, _, err = self.run_main(roster, **TWO_ZIP)
        self.assertEqual(code, 1)
        self.assertIn("is stale", err)

    def test_an_item_that_matches_no_problem_fails(self):
        roster = ROSTER_BASE + PENDING.replace('linked = ["alice-zip"]', 'linked = ["alice-zip", "alice-sdf"]')
        code, _, err = self.run_main(roster, **TWO_ZIP)
        self.assertEqual(code, 1)
        self.assertIn("('linked', 'alice-sdf'), which is not a problem any more", err)
        code, _, err = self.run_main(ROSTER_BASE + PENDING)  # nothing is linked twice at all
        self.assertEqual(code, 1)

    def test_the_requirement_must_match_exactly(self):
        roster = ROSTER_BASE + PENDING.replace("alice-zip ^0.7", "alice-zip ^0.6")
        self.assertEqual(self.run_main(roster, **TWO_ZIP)[0], 1)

    def test_malformed_entries_exit_2(self):
        for bad in (PENDING.replace('until = "alice-db 2.0.0"', 'until = "alice-db 9.9.9"'),  # not the roster version
                    PENDING.replace('until = "alice-db 2.0.0"', 'until = "alice-nope 1.0.0"'),
                    PENDING.replace('until = "alice-db 2.0.0"', 'until = "alice-db"'),
                    PENDING.replace("alice-db 1.0.0 -> alice-zip ^0.7", "alice-db 1.0.0 alice-zip ^0.7"),
                    PENDING.replace("alice-db 1.0.0 -> alice-zip ^0.7", "this repository -> alice-zip 0.7"),
                    PENDING.replace('linked = ["alice-zip"]', 'linked = ["alice-zip", "alice-zip"]'),
                    PENDING.replace('linked = ["alice-zip"]', 'linkd = ["alice-zip"]'),
                    '[pending.empty]\nuntil = "alice-db 2.0.0"\n',
                    PENDING + PENDING.replace("db-2", "db-2b")):  # the same problem in two entries
            self.assertEqual(self.run_main(ROSTER_BASE + bad, **TWO_ZIP)[0], 2, bad)

    def test_the_repository_roster_entries_are_well_formed(self):
        entries = dc.load_pending((HERE.parent / "release-train.toml").read_text(encoding="utf-8"))
        self.assertTrue(all(e["keys"] for e in entries))


if __name__ == "__main__":
    unittest.main()
