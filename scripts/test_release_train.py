#!/usr/bin/env python3
"""Tests of scripts/release_train.py (python3 scripts/test_release_train.py)

No network: crates.io is replaced by a fixture. The roster in the repository is also
read, so a malformed edit to it fails here.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rt", HERE / "release_train.py")
rt = importlib.util.module_from_spec(spec)
sys.modules["rt"] = rt
spec.loader.exec_module(rt)

ROSTER = """
[crate.alice-det-math]
version = "0.5.0"
state = "published"
order = 0
requires = {}

[crate.alice-zip]
version = "0.10.0"
state = "prepared"
order = 1
requires = { alice-det-math = "0.5" }

[crate.alice-analytics]
version = "0.5.0"
state = "prepared-local"
order = 2
requires = { alice-det-math = "0.5", alice-zip = "0.10" }

[crate.alice-law-integration]
version = "0.0.0"
state = "exempt"
order = 3
requires = { alice-zip = "0.10" }
"""


def fixture(present: dict[tuple[str, str], bool]):
    """present[(crate, version)] = yanked; anything else is a 404"""

    def fetch(path: str) -> dict:
        crate, version = path.split("/")
        if (crate, version) not in present:
            raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
        return {"version": {"num": version, "yanked": present[(crate, version)]}}

    return fetch


PUBLISHED_DM = {("alice-det-math", "0.5.0"): False}


def roster(text: str = ROSTER) -> dict:
    return rt.load_roster(text)


def run(argv: list[str], text: str, fetch) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d, "release-train.toml")
        p.write_text(text, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = rt.main(["--roster", str(p), *argv], fetch=fetch)
        return code, out.getvalue()


class Next(unittest.TestCase):
    def test_the_first_crate_whose_upstreams_are_published(self):
        self.assertEqual(rt.next_crate(roster()), ("alice-zip", 1))

    def test_a_crate_waits_until_its_upstream_is_published(self):
        r = roster(ROSTER.replace('order = 0\nrequires = {}', 'order = 0\nrequires = {}').replace(
            '[crate.alice-zip]\nversion = "0.10.0"\nstate = "prepared"', '[crate.alice-zip]\nversion = "0.10.0"\nstate = "published"'))
        self.assertEqual(rt.next_crate(r)[0], "alice-analytics")

    def test_a_crate_that_requires_a_never_published_crate_is_never_next(self):
        # `order` already puts upstreams first, so the only upstream that can stay
        # unpublished ahead of a crate is an exempt one
        text = (
            "[crate.tool]\nversion = \"0.0.0\"\nstate = \"exempt\"\norder = 0\nrequires = {}\n\n"
            "[crate.a]\nversion = \"1.0.0\"\nstate = \"prepared\"\norder = 1\nrequires = { tool = \"0\" }\n"
        )
        self.assertEqual(rt.next_crate(rt.load_roster(text)), (None, 1))
        code, out = run(["next"], text, fixture({}))
        self.assertEqual(code, 1)
        self.assertIn("waits for an upstream", out)

    def test_nothing_to_publish_compares_nothing_and_says_no(self):
        text = "[crate.a]\nversion = \"1.0.0\"\nstate = \"published\"\norder = 0\nrequires = {}\n"
        code, out = run(["next"], text, fixture({}))
        self.assertEqual(code, 1)
        self.assertIn("nothing to publish", out)

    def test_main_prints_the_next_crate(self):
        code, out = run(["next"], ROSTER, fixture(PUBLISHED_DM))
        self.assertEqual((code, out.strip()), (0, "next: alice-zip 0.10.0 (state prepared)"))


class PublishGate(unittest.TestCase):
    def test_go_when_upstreams_are_published_and_accepted(self):
        problems, compared = rt.publish_gate(roster(), "alice-zip", fixture(PUBLISHED_DM))
        self.assertEqual((problems, compared), ([], 2))

    def test_refused_when_the_upstream_is_not_published_in_the_roster(self):
        r = roster(ROSTER.replace('order = 1\nrequires', 'order = 1\nrequires').replace(
            'version = "0.5.0"\nstate = "published"\norder = 0', 'version = "0.5.0"\nstate = "prepared"\norder = 0'))
        problems, _ = rt.publish_gate(r, "alice-zip", fixture(PUBLISHED_DM))
        self.assertTrue(any("not published" in p for p in problems), problems)

    def test_refused_when_the_upstream_is_missing_on_crates_io(self):
        problems, _ = rt.publish_gate(roster(), "alice-zip", fixture({}))
        self.assertTrue(any("missing on crates.io" in p for p in problems), problems)

    def test_refused_when_the_upstream_is_yanked(self):
        problems, _ = rt.publish_gate(roster(), "alice-zip", fixture({("alice-det-math", "0.5.0"): True}))
        self.assertTrue(any("yanked on crates.io" in p for p in problems), problems)

    def test_refused_when_the_requirement_does_not_accept_the_published_version(self):
        r = roster(ROSTER.replace('requires = { alice-det-math = "0.5" }', 'requires = { alice-det-math = "0.4" }'))
        problems, _ = rt.publish_gate(r, "alice-zip", fixture(PUBLISHED_DM))
        self.assertTrue(any("does not accept the published 0.5.0" in p for p in problems), problems)

    def test_refused_when_already_published(self):
        f = fixture({**PUBLISHED_DM, ("alice-zip", "0.10.0"): False})
        problems, _ = rt.publish_gate(roster(), "alice-zip", f)
        self.assertTrue(any("already on crates.io" in p for p in problems), problems)

    def test_refused_when_not_prepared(self):
        problems, _ = rt.publish_gate(roster(), "alice-analytics", fixture(PUBLISHED_DM))
        self.assertTrue(any("needs 'prepared'" in p for p in problems), problems)
        self.assertTrue(any("alice-zip 0.10.0 is 'prepared', not published" in p for p in problems), problems)

    def test_refused_for_an_unknown_crate_or_an_unknown_upstream(self):
        self.assertEqual(rt.publish_gate(roster(), "alice-nope", fixture({}))[0], ["alice-nope is not in the roster"])
        r = roster(ROSTER.replace('requires = { alice-det-math = "0.5" }', 'requires = { alice-det-math = "0.5", alice-other = "1" }'))
        problems, _ = rt.publish_gate(r, "alice-zip", fixture(PUBLISHED_DM))
        self.assertTrue(any("alice-other" in p and "not in the roster" in p for p in problems), problems)

    def test_main_exit_codes(self):
        self.assertEqual(run(["publish-gate", "alice-zip"], ROSTER, fixture(PUBLISHED_DM))[0], 0)
        self.assertEqual(run(["publish-gate", "alice-zip"], ROSTER, fixture({}))[0], 1)

        def offline(path: str) -> dict:
            raise urllib.error.URLError("offline")

        self.assertEqual(run(["publish-gate", "alice-zip"], ROSTER, offline)[0], 2)


class Roster(unittest.TestCase):
    def test_a_roster_with_no_crate_is_refused(self):
        with self.assertRaises(rt.RosterError):
            rt.load_roster("")
        self.assertEqual(run(["next"], "", fixture({}))[0], 2)

    def test_a_crate_listed_before_its_upstream_is_refused(self):
        with self.assertRaises(rt.RosterError):
            rt.load_roster(ROSTER.replace('state = "prepared"\norder = 1', 'state = "prepared"\norder = -1'))

    def test_an_unknown_state_or_a_malformed_version_is_refused(self):
        with self.assertRaises(rt.RosterError):
            rt.load_roster(ROSTER.replace('state = "prepared-local"', 'state = "ready"'))
        with self.assertRaises(ValueError):
            rt.load_roster(ROSTER.replace('version = "0.10.0"', 'version = "0.10"'))

    def test_the_repository_roster_is_well_formed(self):
        r = rt.load_roster((HERE.parent / "release-train.toml").read_text(encoding="utf-8"))
        self.assertGreater(len(r), 0)
        self.assertTrue(any(c["state"] == "published" for c in r.values()))


if __name__ == "__main__":
    unittest.main()
