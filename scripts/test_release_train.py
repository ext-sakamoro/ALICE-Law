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
import subprocess
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


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                               "HOME": str(repo)}).stdout


def make_repo(d: str, files: dict[str, str]) -> tuple[Path, str]:
    """a git repository in d holding `files`, committed; (path, commit)"""
    repo = Path(d, "repo")
    repo.mkdir()
    git(repo, "init", "-q")
    for rel, text in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x")
    return repo, git(repo, "rev-parse", "HEAD").strip()


def run(argv: list[str], text: str, fetch, manifest: str | None = None,
        files: dict[str, str] | None = None, manifest_path: str = "Cargo.toml",
        after=None) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d, "release-train.toml")
        p.write_text(text, encoding="utf-8")
        extra = []
        if manifest is not None or files is not None:
            repo, ref = make_repo(d, files if files is not None else {manifest_path: manifest})
            if after:
                after(repo)
            extra = ["--repo", str(repo), "--ref", ref, "--manifest-path", manifest_path]
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

    def test_underscore_names_are_alice_crates(self):
        text = """[package]\nname = "x"\nversion = "1.0.0"\n[dependencies]
alice_det_math = "0.4"
z = { package = "alice_zip", version = "0.9" }
"""
        self.assertEqual(rt.manifest_alice_requirements(text)[2], {"alice-det-math": "0.4", "alice-zip": "0.9"})

    def test_another_registry_is_an_error(self):
        for spec in ['{ version = "0.5", registry = "other" }', '{ version = "0.5", registry-index = "https://x" }']:
            text = f'[package]\nname = "x"\nversion = "1.0.0"\n[dependencies]\nalice-a = {spec}\n'
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                rt.manifest_alice_requirements(text)

    def test_overrides_of_alice_crates_are_found(self):
        text = """[patch.crates-io]
alice-det-math = { path = "../dm" }
serde = { path = "../serde" }
[patch.'https://github.com/x/y']
alice_zip = { path = "../z" }
[replace]
"alice-det-math:0.5.0" = { path = "../dm" }
"""
        self.assertEqual(rt.alice_overrides(text), ["[patch.crates-io] alice-det-math",
                                                    "[patch.https://github.com/x/y] alice-zip",
                                                    "[replace] alice-det-math:0.5.0"])
        self.assertEqual(rt.alice_overrides("[patch.crates-io]\nserde = { path = \"x\" }\n"), [])

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

    def test_refused_when_the_manifest_or_the_root_overrides_an_alice_crate(self):
        patched = ZIP + '\n[patch.crates-io]\nalice-det-math = { path = "../dm" }\n'
        self.assertTrue(any("overrides an ALICE crate" in p for p in gate(patched)[0]))
        root = '[workspace]\nmembers = ["x"]\n[replace]\n"alice-det-math:0.5.0" = { path = "../dm" }\n'
        problems, _ = rt.publish_gate(roster(), "alice-zip", ZIP, fixture(PUBLISHED_DM), root)
        self.assertTrue(any("[replace]" in p for p in problems), problems)
        code, out = run(["publish-gate", "alice-zip"], ROSTER, fixture(PUBLISHED_DM),
                        files={"sub/Cargo.toml": ZIP, "Cargo.toml": root}, manifest_path="sub/Cargo.toml")
        self.assertEqual(code, 1, out)

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

    def test_a_latest_release_body_without_versions_exits_2(self):
        m = ZIP + 'alice-codec = { version = "0.1", optional = true }\n'

        def fetch(path: str) -> dict:
            if path == "alice-codec":
                return {}
            return fixture(PUBLISHED_DM)(path)

        self.assertEqual(run(["publish-gate", "alice-zip"], ROSTER, fetch, m)[0], 2)

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


class Checkout(unittest.TestCase):
    """the gate reads the committed manifest and refuses a checkout that is not that commit"""

    def test_a_dirty_checkout_is_refused(self):
        def dirty(repo: Path):
            (repo / "Cargo.toml").write_text(ZIP + "# changed\n", encoding="utf-8")
        code, out = run(["publish-gate", "alice-zip"], ROSTER, fixture(PUBLISHED_DM), ZIP, after=dirty)
        self.assertEqual(code, 1)
        self.assertIn("changes to tracked files", out)

    def test_a_checkout_at_another_commit_is_refused(self):
        def move(repo: Path):
            (repo / "README").write_text("x", encoding="utf-8")
            git(repo, "add", "README")
            git(repo, "commit", "-q", "-m", "later")
        code, out = run(["publish-gate", "alice-zip"], ROSTER, fixture(PUBLISHED_DM), ZIP, after=move)
        self.assertEqual(code, 1)
        self.assertIn("not ", out)

    def test_the_committed_manifest_is_read_not_the_working_tree(self):
        # an untracked file does not make the checkout dirty, and the gate reads the commit
        def untracked(repo: Path):
            (repo / "notes.txt").write_text("x", encoding="utf-8")
        self.assertEqual(run(["publish-gate", "alice-zip"], ROSTER, fixture(PUBLISHED_DM), ZIP, after=untracked)[0], 0)

    def test_an_unknown_ref_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            repo, _ = make_repo(d, {"Cargo.toml": ZIP})
            p = Path(d, "r.toml")
            p.write_text(ROSTER, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo),
                                "--ref", "0" * 40], fetch=fixture(PUBLISHED_DM))
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
