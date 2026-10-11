#!/usr/bin/env python3
"""Tests of scripts/release_train.py (python3 scripts/test_release_train.py)

No network: crates.io is replaced by a fixture. The roster in the repository is also
read, so a malformed edit to it fails here.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
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

[crate.alice-zip]
version = "0.10.0"
state = "prepared"
order = 1

[crate.alice-analytics]
version = "0.5.0"
state = "prepared-local"
order = 2

[crate.alice-law-integration]
version = "0.0.0"
state = "exempt"
order = 3
"""

ZIP = """[package]
name = "alice-zip"
version = "0.10.0"

[dependencies]
alice-det-math = { version = "0.5", default-features = false }
serde = "1"
"""


def fixture(present: dict[tuple[str, str], bool], latest: dict[str, str] | None = None):
    """present[(crate, version)] = yanked; any other version is a 404"""
    latest = latest or {}

    def fetch(path: str) -> dict:
        parts = path.split("/")
        if len(parts) == 1:
            return {"crate": {"max_stable_version": latest[parts[0]], "max_version": latest[parts[0]]}}
        crate, version = parts
        if (crate, version) not in present:
            raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
        return {"version": {"num": version, "yanked": present[(crate, version)]}}

    return fetch


PUBLISHED_DM = {("alice-det-math", "0.5.0"): False}


def roster(text: str = ROSTER) -> dict:
    return rt.load_roster(text)


def gate(manifest: str = ZIP, fetch=None, crate: str = "alice-zip", text: str = ROSTER):
    return rt.publish_gate(roster(text), crate, manifest, fetch or fixture(PUBLISHED_DM))


def run(argv: list[str], text: str, fetch, manifest: str | None = None) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d, "release-train.toml")
        p.write_text(text, encoding="utf-8")
        extra = []
        if manifest is not None:
            m = Path(d, "Cargo.toml")
            m.write_text(manifest, encoding="utf-8")
            extra = ["--manifest", str(m)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = rt.main(["--roster", str(p), *argv, *extra], fetch=fetch)
        return code, out.getvalue()


class Next(unittest.TestCase):
    def test_the_first_unpublished_crate_in_order(self):
        self.assertEqual(rt.next_crate(roster()), ("alice-zip", 2))

    def test_nothing_to_publish_says_no(self):
        text = "[crate.a]\nversion = \"1.0.0\"\nstate = \"published\"\norder = 0\n"
        code, out = run(["next"], text, fixture({}))
        self.assertEqual(code, 1)
        self.assertIn("nothing to publish", out)

    def test_main_prints_the_next_crate(self):
        code, out = run(["next"], ROSTER, fixture(PUBLISHED_DM))
        self.assertEqual(code, 0)
        self.assertIn("next: alice-zip 0.10.0", out)


class Manifest(unittest.TestCase):
    def test_every_table_cargo_resolves_is_read(self):
        text = """[package]
name = "x"
version = "1.0.0"
[dependencies]
alice-a = "0.1"
alice_b = { package = "alice-b", path = "../b", version = "0.2" }
alice-c = { version = "0.3", optional = true }
[build-dependencies]
alice-d = "0.4"
[target.'cfg(unix)'.dependencies]
alice-e = { version = "0.5" }
[dev-dependencies]
alice-f = "0.6"
"""
        name, version, reqs = rt.manifest_alice_requirements(text)
        self.assertEqual((name, version), ("x", "1.0.0"))
        self.assertEqual(reqs, {"alice-a": "0.1", "alice-b": "0.2", "alice-c": "0.3",
                                "alice-d": "0.4", "alice-e": "0.5"})

    def test_a_dependency_without_a_crates_io_requirement_is_an_error(self):
        for spec in ['{ path = "../x" }', '{ workspace = true }', '{ git = "https://x", version = "1" }']:
            text = f'[package]\nname = "x"\nversion = "1.0.0"\n[dependencies]\nalice-a = {spec}\n'
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                rt.manifest_alice_requirements(text)


class PublishGate(unittest.TestCase):
    def test_go_when_upstreams_are_published_and_accepted(self):
        self.assertEqual(gate(), ([], 2))

    def test_the_requirements_come_from_the_manifest(self):
        # an alice dependency the roster knows nothing about is still checked
        m = ZIP + 'alice-codec = { version = "0.1.2", optional = true }\n'
        problems, compared = gate(m, fixture({**PUBLISHED_DM, ("alice-codec", "0.1.2"): False}, {"alice-codec": "0.1.2"}))
        self.assertEqual((problems, compared), ([], 3))
        problems, _ = gate(m, fixture(PUBLISHED_DM, {"alice-codec": "0.2.0"}))
        self.assertTrue(any("alice-codec" in p for p in problems), problems)

    def test_refused_when_the_manifest_is_another_crate_or_version(self):
        problems, _ = gate(ZIP.replace('version = "0.10.0"', 'version = "0.10.1"'))
        self.assertTrue(any("the manifest is alice-zip 0.10.1" in p for p in problems), problems)

    def test_refused_when_the_upstream_is_not_published_in_the_roster(self):
        text = ROSTER.replace('version = "0.5.0"\nstate = "published"', 'version = "0.5.0"\nstate = "prepared"', 1)
        problems, _ = gate(text=text)
        self.assertTrue(any("not published" in p for p in problems), problems)

    def test_refused_when_the_upstream_is_missing_or_yanked(self):
        self.assertTrue(any("missing on crates.io" in p for p in gate(fetch=fixture({}))[0]))
        yanked = fixture({("alice-det-math", "0.5.0"): True})
        self.assertTrue(any("yanked on crates.io" in p for p in gate(fetch=yanked)[0]))

    def test_refused_when_the_requirement_does_not_accept_the_published_version(self):
        problems, _ = gate(ZIP.replace('version = "0.5"', 'version = "0.4"'))
        self.assertTrue(any("does not accept the published 0.5.0" in p for p in problems), problems)

    def test_refused_when_already_published(self):
        problems, _ = gate(fetch=fixture({**PUBLISHED_DM, ("alice-zip", "0.10.0"): False}))
        self.assertTrue(any("already on crates.io" in p for p in problems), problems)

    def test_refused_when_not_prepared_or_not_in_the_roster(self):
        m = ZIP.replace("alice-zip", "alice-analytics").replace("0.10.0", "0.5.0")
        problems, _ = gate(m, crate="alice-analytics")
        self.assertTrue(any("needs 'prepared'" in p for p in problems), problems)
        self.assertEqual(gate(crate="alice-nope")[0], ["alice-nope is not in the roster"])


class Registry(unittest.TestCase):
    """crates.io answers other than the version asked for are not 'missing' (exit 2)"""

    def answers(self, body):
        def fetch(path: str) -> dict:
            if body is OSError:
                raise ConnectionResetError("reset")
            if body is json.JSONDecodeError:
                raise json.JSONDecodeError("bad", "x", 0)
            return body
        return fetch

    def test_a_200_without_a_version_is_an_error_not_missing(self):
        for body in ({}, {"errors": [{"detail": "boom"}]}):
            with self.subTest(body=body):
                self.assertRaises(rt.RegistryError, rt.release_state, self.answers(body), "alice-zip", "0.10.0")
                self.assertEqual(run(["publish-gate", "alice-zip"], ROSTER, self.answers(body), ZIP)[0], 2)

    def test_a_200_for_another_version_is_an_error(self):
        body = {"version": {"num": "0.4.0", "yanked": False}}
        self.assertRaises(rt.RegistryError, rt.release_state, self.answers(body), "alice-det-math", "0.5.0")
        self.assertEqual(run(["publish-gate", "alice-zip"], ROSTER, self.answers(body), ZIP)[0], 2)

    def test_only_a_404_is_missing(self):
        self.assertEqual(rt.release_state(fixture({}), "alice-zip", "0.10.0"), "missing")

        def server_error(path: str) -> dict:
            raise urllib.error.HTTPError(path, 500, "Server Error", None, None)

        self.assertEqual(run(["publish-gate", "alice-zip"], ROSTER, server_error, ZIP)[0], 2)

    def test_a_reset_connection_or_a_broken_body_exits_2(self):
        for body in (OSError, json.JSONDecodeError):
            with self.subTest(body=body):
                self.assertEqual(run(["publish-gate", "alice-zip"], ROSTER, self.answers(body), ZIP)[0], 2)

    def test_main_exit_codes(self):
        self.assertEqual(run(["publish-gate", "alice-zip"], ROSTER, fixture(PUBLISHED_DM), ZIP)[0], 0)
        self.assertEqual(run(["publish-gate", "alice-zip"], ROSTER, fixture({}), ZIP)[0], 1)
        self.assertEqual(run(["publish-gate", "alice-zip"], ROSTER, fixture(PUBLISHED_DM), "not toml [")[0], 2)


class Roster(unittest.TestCase):
    def test_a_roster_with_no_crate_is_refused(self):
        with self.assertRaises(rt.RosterError):
            rt.load_roster("")
        self.assertEqual(run(["next"], "", fixture({}))[0], 2)

    def test_requires_in_the_roster_is_refused(self):
        with self.assertRaises(rt.RosterError):
            rt.load_roster(ROSTER.replace('order = 1\n', 'order = 1\nrequires = { alice-det-math = "0.5" }\n'))

    def test_an_unknown_state_a_repeated_order_or_a_malformed_version_is_refused(self):
        for bad in (ROSTER.replace('state = "prepared-local"', 'state = "ready"'),
                    ROSTER.replace("order = 2", "order = 1"),
                    ROSTER.replace('version = "0.10.0"', 'version = "0.10"'),
                    ROSTER.replace('version = "0.10.0"', 'version = "00.10.0"')):
            with self.subTest(bad=bad[:40]), self.assertRaises(ValueError):
                rt.load_roster(bad)

    def test_the_repository_roster_is_well_formed(self):
        r = rt.load_roster((HERE.parent / "release-train.toml").read_text(encoding="utf-8"))
        self.assertGreater(len(r), 0)
        self.assertTrue(any(c["state"] == "published" for c in r.values()))


if __name__ == "__main__":
    unittest.main()
