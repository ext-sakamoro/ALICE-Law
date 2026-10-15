#!/usr/bin/env python3
"""Tests of scripts/release_train.py (python3 scripts/test_release_train.py)

No network: crates.io is replaced by a fixture. The roster in the repository is also
read, so a malformed edit to it fails here.
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tarfile
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rt", HERE / "release_train.py")
rt = importlib.util.module_from_spec(spec)
sys.modules["rt"] = rt
spec.loader.exec_module(rt)
# the gate's consumer build runs cargo on the packaged crate; the fake packages of these
# tests are not crates, so the gate tests use a consumer that accepts them, and the real
# one is tested on its own (ConsumerBuild)
REAL_CONSUMER_BUILD = rt.consumer_build
# the gate reads the repository's secrets from GitHub; the gate tests use an empty inventory
# unless they pass their own (SecretInventory tests the real rules)
EMPTY_INVENTORY = {"repo": [], "environments": {}, "reviewers": {}}
_REAL_MAIN = rt.main


def _main(argv=None, **kw):
    kw.setdefault("inventory", lambda owner_repo: EMPTY_INVENTORY)
    # the gate's own tests do not run cargo semver-checks (class Semver tests that step)
    kw.setdefault("semver", lambda *a, **k: ([], 1, "not run in these tests"))
    return _REAL_MAIN(argv, **kw)


rt.main = _main
rt.consumer_build = lambda body, name, version, target_dir, entry=None: []

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


def crate_bytes(name: str, version: str, files: dict[str, bytes]) -> bytes:
    """a .crate (a gzipped tar under <name>-<version>/) holding files"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for rel, data in files.items():
            info = tarfile.TarInfo(f"{name}-{version}/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    # a fixed gzip timestamp, so the same files give the same bytes in every second
    return gzip.compress(raw.getvalue(), mtime=0)


def fake_packager(repo: Path, manifest_path: str, name: str, version: str, target_dir: Path) -> Path:
    out = target_dir / "package" / f"{name}-{version}.crate"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(crate_bytes(name, version, {"Cargo.toml": b"x", "src/lib.rs": b""}))
    return out


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
    # no background gc / maintenance writing into .git while the test removes it
    git(repo, "config", "gc.auto", "0")
    git(repo, "config", "maintenance.auto", "false")
    if not any(rel.startswith(".github/workflows/") for rel in files):
        files = {**files, ".github/workflows/ci.yml": "jobs:\n  test:\n    steps:\n      - run: cargo test\n"}
    if "CHANGELOG.md" not in files:
        # a release heading for every version a manifest of the fixture names (the gate requires one)
        versions = sorted({v for rel, text in files.items() if rel.endswith("Cargo.toml")
                           for v in re.findall(r'(?m)^version = "([^"]+)"', text)})
        files = {**files, "CHANGELOG.md": "# Changelog\n\n" + "".join(f"## [{v}] - 2026-01-02\n\n- x\n\n" for v in versions)}
    for rel, text in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x")
    return repo, git(repo, "rev-parse", "HEAD").strip()


def run(argv: list[str], text: str, fetch, manifest: str | None = None,
        files: dict[str, str] | None = None, manifest_path: str = "Cargo.toml",
        after=None, package=("Cargo.toml", "src/lib.rs")) -> tuple[int, str]:
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
            code = rt.main(["--roster", str(p), *argv, *extra], fetch=fetch,
                           lister=lambda repo, mp: list(package), packager=fake_packager)
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

    def test_an_optional_requirement_on_an_older_major_is_refused(self):
        # alice-edge 0.2.0 must not ship with `alice-sdf = "1.9"` once alice-sdf 6.0.0 is published
        roster_text = """
[crate.alice-sdf]
version = "6.0.0"
state = "published"
order = 0

[crate.alice-edge]
version = "0.2.0"
state = "prepared"
order = 1
"""
        edge = """[package]
name = "alice-edge"
version = "0.2.0"

[dependencies]
alice-sdf = { version = "1.9", optional = true, default-features = false }
"""
        fetch = fixture({("alice-sdf", "6.0.0"): False})
        problems, _ = rt.publish_gate(rt.load_roster(roster_text), "alice-edge", edge, fetch)
        self.assertTrue(any("alice-sdf '1.9', which does not accept the published 6.0.0" in p for p in problems), problems)
        problems, _ = rt.publish_gate(rt.load_roster(roster_text), "alice-edge", edge.replace('"1.9"', '"6"'), fetch)
        self.assertEqual(problems, [])

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

    def test_the_library_crates_of_the_roster_are_dep_consistency_core(self):
        repo = rt.load_roster((HERE.parent / "release-train.toml").read_text(encoding="utf-8"))
        self.assertTrue(rt.library_crates(repo))
        self.assertEqual(rt.library_crates(repo), sorted(rt.dc.CORE))

    def test_library_crates_leave_out_exempt_and_non_dependencies(self):
        r = roster(ROSTER.replace('state = "prepared-local"', 'state = "prepared-local"\nlaw-dependency = false'))
        self.assertEqual(rt.library_crates(r), ["alice-det-math", "alice-zip"])
        with self.assertRaises(ValueError):
            roster(ROSTER.replace('state = "prepared-local"', 'state = "prepared-local"\nlaw-dependency = "no"'))

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
                                "--ref", "0" * 40], fetch=fixture(PUBLISHED_DM),
                               lister=lambda repo, mp: ["Cargo.toml"], packager=fake_packager)
            self.assertEqual(code, 2)


class Package(unittest.TestCase):
    """the package carries the crate, not the repository's CI, tools or notes"""

    def gate(self, package):
        return run(["publish-gate", "alice-zip"], ROSTER, fixture(PUBLISHED_DM), ZIP, package=package)

    def test_a_package_of_the_crate_alone_goes(self):
        self.assertEqual(self.gate(("Cargo.toml", "src/lib.rs", "README.md", "tests/a.rs"))[0], 0)

    def test_ci_tools_docs_and_notes_are_refused(self):
        # private names are matched by hash; a made-up name stands in for them here
        saved = rt.PRIVATE_NAME_SHA256
        rt.PRIVATE_NAME_SHA256 = frozenset({hashlib.sha256(b".notes-x").hexdigest(),
                                            hashlib.sha256(b"notes-x.md").hexdigest()})
        try:
            for leak in (".github/workflows/ci.yml", "scripts/x.py", "docs/a.md", ".notes-x/s.json",
                         "Notes-X.md", "sub/notes-x.md", "scripts\\win.ps1"):
                code, out = self.gate(("Cargo.toml", "src/lib.rs", leak))
                self.assertEqual(code, 1, leak)
                self.assertIn("not the crate's", out)
            code, out = self.gate(("Cargo.toml", "src/notes-x.mdx", "src/x.notes-x"))
            self.assertEqual(code, 0, out)
        finally:
            rt.PRIVATE_NAME_SHA256 = saved

    def test_the_private_name_hashes_are_sha256(self):
        self.assertTrue(rt.PRIVATE_NAME_SHA256)
        self.assertTrue(all(len(h) == 64 and int(h, 16) >= 0 for h in rt.PRIVATE_NAME_SHA256))

    def test_the_refusal_counts_each_directory(self):
        code, out = self.gate(("Cargo.toml", ".github/a", ".github/b", "docs/c"))
        self.assertEqual(code, 1)
        self.assertIn(".github/ (2)", out)
        self.assertIn("docs/ (1)", out)
        self.assertIn("3 of 4 files", out)

    def test_a_name_that_only_starts_like_an_excluded_directory_goes(self):
        self.assertEqual(self.gate(("Cargo.toml", "src/docs/a.rs", "scripts.md", "src/scripts/b.rs"))[0], 0)

    def test_an_empty_package_list_exits_2(self):
        self.assertEqual(self.gate(())[0], 2)

    def test_a_failing_cargo_exits_2(self):
        def fail(repo, mp):
            raise subprocess.CalledProcessError(101, ["cargo", "package"], stderr="boom")
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP})
            p = Path(d, "r.toml")
            p.write_text(ROSTER, encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo),
                                "--ref", ref], fetch=fixture(PUBLISHED_DM), lister=fail, packager=fake_packager)
            self.assertEqual(code, 2)
            self.assertIn("cargo: boom", err.getvalue())


CHANGES = """# Changelog

## [Unreleased]

## [0.10.0] - 2026-10-12

- **Behavior change:** zeros
- other

## [0.9.0] - 2026-10-01

- **Security:** old
"""


class History(unittest.TestCase):
    """the roster can name commits the publish must contain and lines its CHANGELOG section must have"""

    def gate(self, extra: str, changes: str = CHANGES, contain: str | None = None, branch_off: bool = False):
        roster = ROSTER.replace('state = "prepared"\norder = 1', 'state = "prepared"\norder = 1\n' + extra)
        with tempfile.TemporaryDirectory() as d:
            repo, first = make_repo(d, {"Cargo.toml": ZIP, "CHANGELOG.md": changes})
            if branch_off:  # a commit that the publish commit does not contain
                git(repo, "checkout", "-q", "-b", "side")
                (repo / "side.txt").write_text("x", encoding="utf-8")
                git(repo, "add", "side.txt")
                git(repo, "commit", "-q", "-m", "side")
                side = git(repo, "rev-parse", "HEAD").strip()
                git(repo, "checkout", "-q", "-")
            else:
                side = None
            (repo / "later.txt").write_text("x", encoding="utf-8")
            git(repo, "add", "later.txt")
            git(repo, "commit", "-q", "-m", "later")
            ref = git(repo, "rev-parse", "HEAD").strip()
            subs = {"FIRST": first, "SIDE": side or "", "UNKNOWN": "1" * 40}
            for k, v in subs.items():
                roster = roster.replace(k, v)
            p = Path(d, "r.toml")
            p.write_text(roster, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo), "--ref", ref],
                               fetch=fixture(PUBLISHED_DM), lister=lambda r, m: ["Cargo.toml", "src/lib.rs"], packager=fake_packager)
            return code, out.getvalue()

    def test_an_ancestor_goes_and_counts(self):
        code, out = self.gate('must-contain = ["FIRST"]')
        self.assertEqual(code, 0, out)
        base = self.gate("")[1]
        self.assertNotEqual(out, base)  # one more check was compared

    def test_a_commit_on_another_branch_is_refused(self):
        code, out = self.gate('must-contain = ["SIDE"]', branch_off=True)
        self.assertEqual(code, 1)
        self.assertIn("does not contain", out)

    def test_an_unknown_commit_is_refused(self):
        code, out = self.gate('must-contain = ["UNKNOWN"]')
        self.assertEqual(code, 1)
        self.assertIn("not in the repository", out)

    def test_the_changelog_line_must_be_in_the_version_section(self):
        self.assertEqual(self.gate('changelog-has = ["**Behavior change:**"]')[0], 0)
        code, out = self.gate('changelog-has = ["**Security:**"]')  # only in [0.9.0]
        self.assertEqual(code, 1)
        self.assertIn("does not contain", out)

    def test_a_missing_version_section_is_refused(self):
        unreleased = CHANGES.replace("## [0.10.0] - 2026-10-12", "")
        code, out = self.gate('changelog-has = ["**Behavior change:**"]', changes=unreleased)
        self.assertEqual(code, 1)
        self.assertIn("no [0.10.0] section", out)

    def patch_gate(self, cherry_pick: bool):
        """a change made on a side branch; the publish commit has a rebased copy of it, or not"""
        with tempfile.TemporaryDirectory() as d:
            repo, _ = make_repo(d, {"Cargo.toml": ZIP, "CHANGELOG.md": CHANGES})
            git(repo, "checkout", "-q", "-b", "side")
            (repo / "fix.rs").write_text("fn f() {}\n", encoding="utf-8")
            git(repo, "add", "fix.rs")
            git(repo, "commit", "-q", "-m", "the change")
            side = git(repo, "rev-parse", "HEAD").strip()
            pid = subprocess.run(["git", "-C", str(repo), "patch-id", "--stable"],
                                 input=subprocess.run(["git", "-C", str(repo), "show", side], capture_output=True).stdout,
                                 capture_output=True).stdout.decode().split()[0]
            git(repo, "checkout", "-q", "-")
            (repo / "other.txt").write_text("x", encoding="utf-8")
            git(repo, "add", "other.txt")
            git(repo, "commit", "-q", "-m", "main moved")
            if cherry_pick:
                git(repo, "cherry-pick", side)
            (repo / "later.txt").write_text("x", encoding="utf-8")  # the copy is not the newest commit
            git(repo, "add", "later.txt")
            git(repo, "commit", "-q", "-m", "later")
            ref = git(repo, "rev-parse", "HEAD").strip()
            self.assertNotEqual(ref, side)
            roster = ROSTER.replace('state = "prepared"\norder = 1', f'state = "prepared"\norder = 1\nmust-contain-patch = ["{pid}"]')
            p = Path(d, "r.toml")
            p.write_text(roster, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo), "--ref", ref],
                               fetch=fixture(PUBLISHED_DM), lister=lambda r, m: ["Cargo.toml"], packager=fake_packager)
            return code, out.getvalue()

    def test_a_rebased_copy_of_the_change_counts(self):
        code, out = self.patch_gate(cherry_pick=True)
        self.assertEqual(code, 0, out)

    def test_without_the_change_the_patch_is_refused(self):
        code, out = self.patch_gate(cherry_pick=False)
        self.assertEqual(code, 1)
        self.assertIn("has the patch", out)

    def test_an_empty_or_malformed_list_exits_2(self):
        for extra in ("must-contain = []", 'must-contain = "x"', 'changelog-has = [""]', "changelog-has = [1]",
                      "must-contain-patch = []"):
            self.assertEqual(self.gate(extra)[0], 2, extra)


class GateOutput(unittest.TestCase):
    def test_go_prints_the_commit_the_crate_sha256_and_the_commands(self):
        code, out = run(["publish-gate", "alice-zip"], ROSTER, fixture(PUBLISHED_DM), ZIP)
        self.assertEqual(code, 0, out)
        body = crate_bytes("alice-zip", "0.10.0", {"Cargo.toml": b"x", "src/lib.rs": b""})
        self.assertIn(hashlib.sha256(body).hexdigest(), out)
        self.assertIn("(2 files, built by cargo package)", out)
        self.assertIn("cargo publish --manifest-path Cargo.toml", out)  # no Cargo.lock committed: no --locked
        self.assertIn("roster-mark published alice-zip", out)

    def test_a_committed_lock_adds_locked(self):
        code, out = run(["publish-gate", "alice-zip"], ROSTER, fixture(PUBLISHED_DM),
                        files={"Cargo.toml": ZIP, "Cargo.lock": "# lock\n"})
        self.assertEqual(code, 0, out)
        self.assertIn("cargo publish --locked", out)

    def test_a_refused_gate_does_not_package(self):
        def boom(*args):
            raise AssertionError("packaged after a refusal")
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP})
            p = Path(d, "r.toml")
            p.write_text(ROSTER.replace('state = "prepared"', 'state = "planned"'), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo), "--ref", ref],
                               fetch=fixture(PUBLISHED_DM), lister=lambda r, m: ["Cargo.toml"], packager=boom)
        self.assertEqual(code, 1)


    def test_cargo_package_is_locked_when_a_lock_is_committed(self):
        real = subprocess.run
        for files, locked in (({"Cargo.toml": ZIP}, False), ({"Cargo.toml": ZIP, "Cargo.lock": "#\n"}, True)):
            seen = []

            def spy(cmd, *args, **kw):
                if cmd[0] == "cargo":
                    seen.append(cmd)
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                return real(cmd, *args, **kw)

            with tempfile.TemporaryDirectory() as d:
                repo, _ = make_repo(d, files)
                rt.subprocess.run = spy
                try:
                    out = rt.cargo_package(repo, "Cargo.toml", "alice-zip", "0.10.0", Path(d, "t"))
                finally:
                    rt.subprocess.run = real
            self.assertEqual(len(seen), 1)
            self.assertEqual("--locked" in seen[0], locked, files)
            self.assertEqual(out, Path(d, "t", "package", "alice-zip-0.10.0.crate"))


ZIP_LICENSED = ZIP.replace('version = "0.10.0"\n', 'version = "0.10.0"\nlicense = "Apache-2.0"\n', 1)
OWNED = "\n[train]\nowners = [\"owner-a\"]\n"


class Verify(unittest.TestCase):
    """after `cargo publish`: the release on crates.io is the commit the gate checked"""

    def world(self, d: str, **over):
        repo, ref = make_repo(d, {"Cargo.toml": ZIP_LICENSED})
        vcs = over.get("vcs", {"git": {"sha1": ref}, "path_in_vcs": ""})
        files = {"Cargo.toml": b"x"}
        if vcs is not None:
            files[".cargo_vcs_info.json"] = json.dumps(vcs).encode()
        body = over.get("body", crate_bytes("alice-zip", "0.10.0", files))
        listed = over.get("checksum", hashlib.sha256(body).hexdigest())
        version = {"num": "0.10.0", "yanked": over.get("yanked", False), "checksum": listed,
                   "license": over.get("license", "Apache-2.0")}
        owners = over.get("owners", ["owner-a"])

        def fetch(path):
            if path == "alice-zip/0.10.0":
                if over.get("missing"):
                    raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
                return {"version": version}
            if path == "alice-zip/owners":
                return {"users": [{"login": o} for o in owners]}
            raise AssertionError(path)

        tag_at = over.get("tag", ref)
        return repo, ref, listed, fetch, (lambda url: body), (lambda r, remote, tag: tag_at if tag == "v0.10.0" else None)

    def verify(self, cmd="publish-verify", roster_text=ROSTER + OWNED, expect=None, **over):
        with tempfile.TemporaryDirectory() as d:
            repo, ref, listed, fetch, download, tags = self.world(d, **over)
            p = Path(d, "r.toml")
            p.write_text(roster_text, encoding="utf-8")
            argv = ["--roster", str(p), *(["roster-mark", "published"] if cmd == "roster-mark" else [cmd]),
                    "alice-zip", "--repo", str(repo), "--ref", ref, "--expect-sha256", expect or listed]
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = rt.main(argv, fetch=fetch, download=download, tags=tags,
                               docs=lambda c, v: {"doc_status": True, "version": v})
            return code, out.getvalue(), p.read_text(encoding="utf-8")

    def test_the_release_of_the_gated_commit_passes(self):
        code, out, _ = self.verify()
        self.assertEqual(code, 0, out)
        self.assertIn("(7 checks)", out)
        self.assertIn("doc_status=True (information, not a check)", out)

    def test_each_difference_is_refused(self):
        cases = {
            "not on crates.io": dict(missing=True),
            "is yanked": dict(yanked=True),
            "the gate packaged": dict(expect="0" * 64),
            "license": dict(license="MIT"),
            "owners": dict(owners=["owner-a", "someone"]),
            "the downloaded .crate": dict(checksum="f" * 64, expect="f" * 64),
            "packaged from 1111": dict(vcs={"git": {"sha1": "1" * 40}, "path_in_vcs": ""}),
            "dirty checkout": dict(vcs={"git": {"sha1": None, "dirty": True}, "path_in_vcs": ""}),
            "no .cargo_vcs_info.json": dict(vcs=None),
            "not the remote": dict(tag=None),
            "names 2222": dict(tag="2" * 40),
        }
        for needle, over in cases.items():
            code, out, _ = self.verify(**over)
            self.assertEqual(code, 1, needle)
            self.assertIn(needle.replace("not the remote", "not on the remote"), out)

    def test_the_vcs_sha1_must_be_the_commit_and_the_path_the_manifest_directory(self):
        code, out, _ = self.verify(vcs={"git": {"sha1": "x"}, "path_in_vcs": "sub"})
        self.assertEqual(code, 1)
        self.assertIn("'sub'", out)

    def test_without_train_owners_exits_2(self):
        self.assertEqual(self.verify(roster_text=ROSTER)[0], 2)

    def test_mark_writes_published_only_after_a_passing_verify(self):
        code, out, text = self.verify(cmd="roster-mark")
        self.assertEqual(code, 0, out)
        self.assertEqual(rt.load_roster(text)["alice-zip"]["state"], "published")
        self.assertEqual(rt.load_roster(text)["alice-analytics"]["state"], "prepared-local")
        code, out, text = self.verify(cmd="roster-mark", yanked=True)
        self.assertEqual(code, 1)
        self.assertEqual(rt.load_roster(text)["alice-zip"]["state"], "prepared")

    def test_mark_changes_one_line_of_one_table(self):
        new = rt.mark_published(ROSTER, "alice-zip")
        diff = [(a, b) for a, b in zip(ROSTER.splitlines(), new.splitlines()) if a != b]
        self.assertEqual(diff, [('state = "prepared"', 'state = "published"')])
        with self.assertRaises(rt.RosterError):
            rt.mark_published(ROSTER, "alice-analytics")  # prepared-local, not prepared

    def test_the_tag_of_the_entry_names_the_tag(self):
        self.assertEqual(rt.tag_name({"version": "1.2.3", "tag": "alice-foo-v{version}"}), "alice-foo-v1.2.3")
        self.assertEqual(rt.tag_name({"version": "1.2.3"}), "v1.2.3")


FOLLOW_ROSTER = """
[crate.alice-zip]
version = "0.10.0"
state = "published"
order = 0
repo = "Z"

[crate.alice-db]
version = "0.4.0"
state = "prepared"
order = 1
repo = "D"
"""


class Followups(unittest.TestCase):
    def run_followups(self, repos: dict[str, dict[str, str]], crate="alice-zip", roster_text=FOLLOW_ROSTER):
        with tempfile.TemporaryDirectory() as d:
            for name, files in repos.items():
                sub = Path(d, name)
                sub.mkdir()
                make_repo(str(sub), files)
                (sub / "repo").rename(sub / "tmp")
                for child in (sub / "tmp").iterdir():
                    child.rename(sub / child.name)
                (sub / "tmp").rmdir()
            p = Path(d, "r.toml")
            p.write_text(roster_text, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = rt.main(["--roster", str(p), "followups", crate, "--repos-root", d, "--at", "HEAD"])
            return code, out.getvalue()

    def test_requirements_that_do_not_accept_the_version_are_listed(self):
        code, out = self.run_followups({
            "Z": {"Cargo.toml": ZIP, "fuzz/Cargo.toml": '[dependencies]\nalice-zip = { path = ".." }\n'},
            "D": {"Cargo.toml": '[package]\nname = "alice-db"\nversion = "0.4.0"\n[dependencies]\nalice-zip = "0.9"\n',
                  "e2e/Cargo.toml": '[package]\nname = "e"\nversion = "0.0.0"\n[dev-dependencies]\nalice_zip = { version = "0.10" }\n',
                  "w/Cargo.toml": '[workspace.dependencies]\nalice-zip = { path = "../z" }\n'
                                  '[target.\'cfg(unix)\'.build-dependencies]\nalice-zip = "=0.9.1"\n'},
        })
        self.assertEqual(code, 1, out)
        self.assertIn("D/Cargo.toml [dependencies] alice-zip = '0.9' does not accept 0.10.0", out)
        self.assertIn("ok:   D/e2e/Cargo.toml [dev-dependencies]", out)
        self.assertIn("[workspace.dependencies] alice-zip: no version requirement", out)
        self.assertIn("[target.cfg(unix).build-dependencies] alice-zip = '=0.9.1'", out)
        self.assertIn("3 to bump, 1 accept it (5 manifests", out)
        self.assertNotIn("fuzz", out)  # a path in the crate's own repository

    def test_nothing_to_bump_exits_0(self):
        code, out = self.run_followups({"Z": {"Cargo.toml": ZIP}, "D": {"Cargo.toml": '[dependencies]\nalice-zip = "0.10"\n'}})
        self.assertEqual(code, 0, out)

    def test_a_missing_checkout_exits_2(self):
        self.assertEqual(self.run_followups({"Z": {"Cargo.toml": ZIP}})[0], 2)


class RequiredCi(unittest.TestCase):
    """the jobs the roster names, in the workflow it names, with the legs it expects, all green"""

    REQ = ('required-ci = [{ workflow = "CI", job = "Geometry digests", legs = ["Geometry digests (ubuntu)", '
           '"Geometry digests (windows)"] }, { workflow = "CI", job = "Clippy" }, '
           '{ workflow = "Wheel", job = "Build", legs = 2 }]')

    def gate(self, by_workflow, extra=None, train='\n[train]\ngithub = "owner"\n', repo='repo = "R"\n'):
        extra = self.REQ if extra is None else extra
        roster = ROSTER.replace('state = "prepared"\norder = 1', 'state = "prepared"\norder = 1\n' + repo + extra) + train
        seen = []

        def actions(owner_repo, sha):
            seen.append((owner_repo, sha))
            if isinstance(by_workflow, Exception):
                raise by_workflow
            return by_workflow
        with tempfile.TemporaryDirectory() as d:
            repo_dir, ref = make_repo(d, {"Cargo.toml": ZIP})
            p = Path(d, "r.toml")
            p.write_text(roster, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo_dir), "--ref", ref],
                               fetch=fixture(PUBLISHED_DM), lister=lambda r, m: ["Cargo.toml"],
                               packager=fake_packager, actions=actions)
        return code, out.getvalue(), seen, ref

    @staticmethod
    def ok(name):
        return {"name": name, "status": "completed", "conclusion": "success"}

    def green(self):
        return {"CI": [self.ok("Geometry digests (ubuntu)"), self.ok("Geometry digests (windows)"), self.ok("Clippy"),
                       {"name": "Other", "status": "completed", "conclusion": "failure"}],
                "Wheel": [self.ok("Build (3.11)"), self.ok("Build (3.12)")],
                "Lint": [{"name": "Clippy", "status": "completed", "conclusion": "failure"}]}

    def test_green_jobs_go_and_the_commit_is_asked_for(self):
        code, out, seen, ref = self.gate(self.green())
        self.assertEqual(code, 0, out)
        self.assertEqual(seen, [("owner/R", ref)])

    def test_a_missing_leg_or_an_unexpected_one_is_refused(self):
        w = self.green()
        w["CI"] = [j for j in w["CI"] if j["name"] != "Geometry digests (windows)"]
        code, out, _, _ = self.gate(w)
        self.assertEqual(code, 1)
        self.assertIn("legs missing ['Geometry digests (windows)']", out)
        w = self.green()
        w["CI"].append(self.ok("Geometry digests (macos)"))
        code, out, _, _ = self.gate(w)
        self.assertEqual(code, 1)
        self.assertIn("not in the roster ['Geometry digests (macos)']", out)

    def test_the_leg_count_must_match(self):
        w = self.green()
        w["Wheel"] = w["Wheel"][:1]
        code, out, _, _ = self.gate(w)
        self.assertEqual(code, 1)
        self.assertIn("1 legs ran, the roster expects 2", out)

    def test_a_job_of_another_workflow_does_not_count(self):
        w = self.green()
        w["CI"] = [j for j in w["CI"] if j["name"] != "Clippy"]  # only the failing Clippy of "Lint" is left
        code, out, _, _ = self.gate(w)
        self.assertEqual(code, 1)
        self.assertIn("0 legs ran, the roster expects 1", out)
        w = self.green()
        del w["Wheel"]
        code, out, _, _ = self.gate(w)
        self.assertEqual(code, 1)
        self.assertIn("the workflow has no run", out)

    def test_one_red_skipped_or_running_leg_is_refused(self):
        for status, conclusion in (("completed", "failure"), ("completed", "skipped"), ("in_progress", None),
                                   ("completed", "cancelled")):
            w = self.green()
            w["CI"][1] = {"name": "Geometry digests (windows)", "status": status, "conclusion": conclusion}
            code, out, _, _ = self.gate(w)
            self.assertEqual(code, 1, (status, conclusion))
            self.assertIn("not green: Geometry digests (windows)", out)

    def test_a_job_the_run_skipped_asks_for_a_dispatch_on_the_commit(self):
        # a path filter the commit did not match: named jobs skipped, or a matrix left unexpanded
        w = self.green()
        w["CI"][2] = {"name": "Clippy", "status": "completed", "conclusion": "skipped"}
        code, out, _, ref = self.gate(w)
        self.assertEqual(code, 1, out)
        self.assertIn(f"required CI job 'Clippy' of workflow 'CI' on {ref[:12]}: skipped by the run (not green): "
                      f"dispatch CI on {ref}", out)
        w = self.green()
        w["Wheel"] = [{"name": "Gate", "status": "completed", "conclusion": "success"},
                      {"name": "Build (${{ matrix.python }})", "status": "completed", "conclusion": "skipped"}]
        code, out, _, ref = self.gate(w)
        self.assertEqual(code, 1, out)
        self.assertIn(f"skipped by the run (not green): dispatch Wheel on {ref}", out)
        self.assertNotIn("dispatch CI", out)
        # a job named per matrix value, where the skipped run shows the matrix unexpanded
        fuzz = {"Fuzz": [{"name": "Gate", "status": "completed", "conclusion": "success"},
                         {"name": "Fuzz — ${{ matrix.target }}", "status": "completed", "conclusion": "skipped"}]}
        code, out, _, ref = self.gate(fuzz, extra='required-ci = [{ workflow = "Fuzz", job = "Fuzz — parse" }]')
        self.assertEqual(code, 1, out)
        self.assertIn(f"required CI job 'Fuzz — parse' of workflow 'Fuzz' on {ref[:12]}: skipped by the run "
                      f"(not green): dispatch Fuzz on {ref}", out)

    def test_the_go_record_names_the_jobs_from_a_dispatched_run(self):
        w = self.green()
        w["Wheel"] = [dict(j, event="workflow_dispatch") for j in w["Wheel"]]
        code, out, _, _ = self.gate(w)
        self.assertEqual(code, 0, out)
        self.assertIn("  dispatched:    required CI from a dispatched run: Wheel: Build (2 legs)", out)
        # one leg from a push run and one from a dispatched run: not a dispatched job
        w = self.green()
        w["Wheel"] = [dict(w["Wheel"][0], event="push"), dict(w["Wheel"][1], event="workflow_dispatch")]
        code, out, _, _ = self.gate(w)
        self.assertEqual(code, 0, out)
        self.assertNotIn("dispatched:", out)
        code, out, _, _ = self.gate(self.green())
        self.assertEqual(code, 0, out)
        self.assertNotIn("dispatched:", out)

    def test_a_name_that_only_starts_like_the_job_does_not_count(self):
        w = self.green()
        w["CI"] = [j for j in w["CI"] if j["name"] != "Clippy"] + [self.ok("Clippy-lib")]
        self.assertEqual(self.gate(w)[0], 1)

    def test_an_api_error_is_a_refusal_not_a_traceback(self):
        for err in (subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 404"), OSError("no gh"),
                    KeyError("workflow_runs")):
            code, out, _, _ = self.gate(err)
            self.assertEqual(code, 1, err)
            self.assertIn("could not be read", out)
            self.assertNotIn("Traceback", out)

    def test_without_the_owner_or_the_repo_exits_2(self):
        self.assertEqual(self.gate(self.green(), train="")[0], 2)
        self.assertEqual(self.gate(self.green(), repo="")[0], 2)

    def test_malformed_entries_exit_2(self):
        for extra in ("required-ci = []", 'required-ci = ["Clippy"]', 'required-ci = [{ workflow = "CI" }]',
                      'required-ci = [{ workflow = "CI", job = "C", legs = 0 }]',
                      'required-ci = [{ workflow = "CI", job = "C", legs = [] }]',
                      'required-ci = [{ workflow = "CI", job = "C", leg = 1 }]'):
            self.assertEqual(self.gate(self.green(), extra=extra)[0], 2, extra)

    def test_an_entry_since_a_later_version_is_not_asked_yet(self):
        later = self.REQ.replace('{ workflow = "CI", job = "Clippy" }', '{ workflow = "CI", job = "Clippy", since = "0.10.1" }')
        w = self.green()
        w["CI"] = [j for j in w["CI"] if j["name"] != "Clippy"]
        self.assertEqual(self.gate(w, extra=later)[0], 0)  # alice-zip is at 0.10.0 in the roster
        self.assertEqual(self.gate(w, extra=later.replace('since = "0.10.1"', 'since = "0.10.0"'))[0], 1)
        self.assertEqual(self.gate(w, extra=later.replace('since = "0.10.1"', 'since = "0.9.0"'))[0], 1)

    def test_a_malformed_since_is_a_roster_error_for_every_command(self):
        roster = ROSTER.replace('state = "prepared"\norder = 1', 'state = "prepared"\norder = 1\nrequired-ci = [{ workflow = "CI", job = "C", since = "x" }]')
        with self.assertRaises(ValueError):
            rt.load_roster(roster)

    def test_a_malformed_since_exits_2(self):
        for since in ('since = "x"', "since = 3"):
            extra = self.REQ.replace('{ workflow = "CI", job = "Clippy" }', '{ workflow = "CI", job = "Clippy", ' + since + ' }')
            self.assertEqual(self.gate(self.green(), extra=extra)[0], 2, since)

    def test_no_required_ci_asks_nothing(self):
        code, _, seen, _ = self.gate(self.green(), extra="")
        self.assertEqual((code, seen), (0, []))


def tiny_crate(name: str, version: str, build_rs: str | None = None, extra: str = "",
               lib: str = "pub fn f() -> u32 { 1 }\n", main: str | None = None) -> bytes:
    """a real .crate (optionally with a build script, more manifest text, or only a binary)"""
    manifest = f'[package]\nname = "{name}"\nversion = "{version}"\nedition = "2021"\n' + extra
    files = {"Cargo.toml": manifest.encode()}
    if main is None:
        files["src/lib.rs"] = lib.encode()
    else:
        files["src/main.rs"] = main.encode()
    if build_rs is not None:
        files["build.rs"] = build_rs.encode()
    return crate_bytes(name, version, files)


UNPUBLISHED_DEP = '\n[dependencies]\ngate-probe-not-published-9f3c = "9.9"\n'


# a build script that, like one reading the consumer's lock, needs a Cargo.lock above the crate
NEEDS_LOCK_ABOVE = """
use std::path::Path;
fn main() {
    let dir = std::env::var("CARGO_MANIFEST_DIR").unwrap();
    let found = Path::new(&dir).ancestors().skip(1).any(|d| d.join("Cargo.lock").exists());
    assert!(found, "no Cargo.lock above the crate");
}
"""


@unittest.skipIf(rt.shutil.which("cargo") is None, "cargo is not installed")
class ConsumerBuild(unittest.TestCase):
    """the packaged crate is built as a crates.io consumer builds it"""

    def build(self, body: bytes, name: str = "gate-probe", version: str = "0.1.0") -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            return REAL_CONSUMER_BUILD(body, name, version, Path(d))

    def test_a_plain_crate_builds(self):
        self.assertEqual(self.build(tiny_crate("gate-probe", "0.1.0")), [])

    def test_a_build_script_that_needs_a_lock_above_the_crate_is_refused(self):
        problems = self.build(tiny_crate("gate-probe", "0.1.0", NEEDS_LOCK_ABOVE))
        self.assertEqual(len(problems), 2, problems)  # default and no default features
        self.assertTrue(all("does not build" in p for p in problems), problems)

    def test_the_same_build_script_passes_inside_a_tree_with_a_lock(self):
        # the teeth of the check: inside the repository (where `cargo package` verifies)
        # the same build script finds a lock and passes
        with tempfile.TemporaryDirectory() as d:
            Path(d, "Cargo.lock").write_text("", encoding="utf-8")
            crate = Path(d, "c")
            (crate / "src").mkdir(parents=True)
            (crate / "Cargo.toml").write_text('[package]\nname = "gate-probe"\nversion = "0.1.0"\nedition = "2021"\n[workspace]\n', encoding="utf-8")
            (crate / "src" / "lib.rs").write_text("", encoding="utf-8")
            (crate / "build.rs").write_text(NEEDS_LOCK_ABOVE, encoding="utf-8")
            r = subprocess.run(["cargo", "build", "-q", "--target-dir", str(Path(d, "t"))], cwd=crate, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_crate_named_otherwise_compiles_nothing_and_is_refused(self):
        # the .crate holds gate-other; asked for gate-probe there is no manifest to build
        problems = self.build(tiny_crate("gate-other", "0.1.0"))
        self.assertEqual(len(problems), 1, problems)

    def test_no_library_artifact_is_refused(self):
        self.assertFalse(rt.compiled_lib('{"reason":"build-finished","success":true}', "gate-probe", "0.1.0"))
        self.assertFalse(rt.compiled_lib("", "gate-probe", "0.1.0"))
        other = json.dumps({"reason": "compiler-artifact", "package_id": "path+file:///x#gate-other@0.1.0",
                            "target": {"kind": ["lib"]}})
        self.assertFalse(rt.compiled_lib(other, "gate-probe", "0.1.0"))
        ours = other.replace("gate-other", "gate-probe")
        self.assertTrue(rt.compiled_lib(ours, "gate-probe", "0.1.0"))
        old_form = json.dumps({"reason": "compiler-artifact", "package_id": "gate-probe 0.1.0 (path+file:///x)",
                               "target": {"kind": ["proc-macro"]}})
        self.assertTrue(rt.compiled_lib(old_form, "gate-probe", "0.1.0"))
        build_script = ours.replace('"lib"', '"custom-build"')
        self.assertFalse(rt.compiled_lib(build_script, "gate-probe", "0.1.0"))

    def test_a_lock_or_configuration_above_the_build_directory_is_found(self):
        for rel in ("Cargo.lock", "Cargo.toml", ".cargo/config.toml", ".cargo/config"):
            with tempfile.TemporaryDirectory() as d:
                Path(d, rel).parent.mkdir(parents=True, exist_ok=True)
                Path(d, rel).write_text("", encoding="utf-8")
                self.assertTrue(rt.manifests_above(Path(d, "a", "b")), rel)

    def with_env(self, **env):
        saved = {k: rt.os.environ.get(k) for k in env}
        rt.os.environ.update(env)
        self.addCleanup(lambda: [rt.os.environ.pop(k, None) if v is None else rt.os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])

    def test_an_unpublished_dependency_is_refused_even_with_an_ambient_patch(self):
        self.assertEqual(len(self.build(tiny_crate("gate-probe", "0.1.0", extra=UNPUBLISHED_DEP))), 2)
        with tempfile.TemporaryDirectory() as d:
            dep = Path(d, "dep")
            (dep / "src").mkdir(parents=True)
            (dep / "Cargo.toml").write_text('[package]\nname = "gate-probe-not-published-9f3c"\nversion = "9.9.0"\n'
                                            'edition = "2021"\n', encoding="utf-8")
            (dep / "src" / "lib.rs").write_text("", encoding="utf-8")
            home = Path(d, "cargo-home")
            home.mkdir()
            (home / "config.toml").write_text(f'[patch.crates-io]\ngate-probe-not-published-9f3c = '
                                              f'{{ path = "{dep.as_posix()}" }}\n', encoding="utf-8")
            self.with_env(CARGO_HOME=str(home))
            problems = self.build(tiny_crate("gate-probe", "0.1.0", extra=UNPUBLISHED_DEP))
        self.assertEqual(len(problems), 2, problems)

    def test_ambient_rustflags_do_not_reach_the_build(self):
        hidden = '#[cfg(not(gate_hidden))]\ncompile_error!("needs a flag from the environment");\n'
        self.with_env(RUSTFLAGS="--cfg gate_hidden", CARGO_ENCODED_RUSTFLAGS="--cfg\x1fgate_hidden")
        problems = self.build(tiny_crate("gate-probe", "0.1.0", lib=hidden))
        self.assertEqual(len(problems), 2, problems)  # default and no default features

    def test_a_feature_that_does_not_build_is_refused(self):
        lib = '#[cfg(feature = "x")]\ncompile_error!("feature x is broken");\n'
        problems = self.build(tiny_crate("gate-probe", "0.1.0", extra="\n[features]\nx = []\n", lib=lib))
        self.assertTrue(problems)
        self.assertTrue(any("feature" in p for p in problems), problems)

    def test_a_no_default_build_that_does_not_build_is_refused(self):
        lib = '#[cfg(not(feature = "std"))]\ncompile_error!("needs std");\n'
        problems = self.build(tiny_crate("gate-probe", "0.1.0", extra='\n[features]\ndefault = ["std"]\nstd = []\n',
                                         lib=lib))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no default features", problems[0])

    def test_declared_feature_sets_are_built(self):
        lib = '#[cfg(all(feature = "a", feature = "b"))]\ncompile_error!("a with b");\n'
        crate = tiny_crate("gate-probe", "0.1.0", extra="\n[features]\na = []\nb = []\n", lib=lib)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(REAL_CONSUMER_BUILD(crate, "gate-probe", "0.1.0", Path(d), {"consumer-features": [["a"], ["b"]]}), [])
            self.assertEqual(len(REAL_CONSUMER_BUILD(crate, "gate-probe", "0.1.0", Path(d), {"consumer-features": [["a", "b"]]})), 1)

    def test_features_that_break_together_are_refused_unless_the_roster_names_the_sets(self):
        lib = '#[cfg(all(feature = "a", feature = "b"))]\ncompile_error!("a with b");\n'
        crate = tiny_crate("gate-probe", "0.1.0", extra="\n[features]\na = []\nb = []\n", lib=lib)
        problems = self.build(crate)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("all features", problems[0])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(REAL_CONSUMER_BUILD(crate, "gate-probe", "0.1.0", Path(d), {"consumer-features": [["a"], ["b"]]}), [])

    def test_a_crate_carrying_cargo_configuration_is_refused(self):
        body = crate_bytes("gate-probe", "0.1.0", {
            "Cargo.toml": b'[package]\nname = "gate-probe"\nversion = "0.1.0"\nedition = "2021"\n',
            "src/lib.rs": b"", ".cargo/config.toml": b"[build]\n"})
        problems = self.build(body)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(".cargo/", problems[0])
        # another tool's file under .cargo/ is not Cargo configuration
        other = crate_bytes("gate-probe", "0.1.0", {
            "Cargo.toml": b'[package]\nname = "gate-probe"\nversion = "0.1.0"\nedition = "2021"\n',
            "src/lib.rs": b"", ".cargo/mutants.toml": b"timeout_multiplier = 2\n"})
        self.assertEqual(self.build(other), [])

    def test_a_binary_crate_is_built_from_outside_its_directory(self):
        # a [patch] in a configuration next to the manifest must not be read (the .crate check
        # above refuses a packaged .cargo/; here the binary build itself is checked)
        seen = []
        real = rt.subprocess.run

        def spy(cmd, *a, **kw):
            if cmd[0] == "cargo":
                seen.append((cmd, kw.get("cwd")))
            return real(cmd, *a, **kw)
        rt.subprocess.run = spy
        try:
            self.assertEqual(self.build(tiny_crate("gate-probe", "0.1.0", main="fn main() {}\n")), [])
        finally:
            rt.subprocess.run = real
        cmd, cwd = seen[-1]
        self.assertIn("--manifest-path", cmd)
        pkg = Path(cmd[cmd.index("--manifest-path") + 1]).parent
        self.assertNotIn(pkg, [Path(cwd), *Path(cwd).parents])

    def test_a_binary_crate_is_built_on_its_own(self):
        self.assertEqual(self.build(tiny_crate("gate-probe", "0.1.0", main="fn main() {}\n")), [])
        broken = self.build(tiny_crate("gate-probe", "0.1.0", main='compile_error!("x");\nfn main() {}\n'))
        self.assertEqual(len(broken), 1, broken)

    def test_feature_specs(self):
        self.assertEqual([l for l, *_ in rt.feature_specs({}, None)], ["default features", "no default features"])
        self.assertEqual([l for l, *_ in rt.feature_specs({"features": {"default": [], "x": []}}, None)][-1],
                         "all features")
        specs = rt.feature_specs({"features": {"x": []}}, {"consumer-features": [["x"], []],
                                                           "consumer-check-features": [["py"]]})
        self.assertEqual([(fs, cmd) for _, _, fs, cmd in specs][2:], [(["x"], "build"), ([], "build"), (["py"], "check")])

    def test_a_check_set_is_compiled_but_not_linked(self):
        # a cdylib whose symbol only another tool provides at link time: build fails, check passes
        # (a native library that does not exist: linking fails on every platform, while a cdylib with
        # an undefined symbol links on Linux, which allows those in shared libraries)
        lib = '#[cfg(feature = "py")]\n#[link(name = "gate_probe_host_provided")]\nextern "C" { fn provided_by_the_host(); }\n' \
              '#[cfg(feature = "py")]\n#[no_mangle]\npub extern "C" fn entry() { unsafe { provided_by_the_host() } }\n'
        extra = '\n[lib]\ncrate-type = ["rlib", "cdylib"]\n[features]\npy = []\n'
        crate = tiny_crate("gate-probe", "0.1.0", extra=extra, lib=lib)
        with tempfile.TemporaryDirectory() as d:
            linked = REAL_CONSUMER_BUILD(crate, "gate-probe", "0.1.0", Path(d), {"consumer-features": [["py"]]})
            checked = REAL_CONSUMER_BUILD(crate, "gate-probe", "0.1.0", Path(d), {"consumer-check-features": [["py"]]})
        self.assertEqual(len(linked), 1, linked)
        self.assertEqual(checked, [])

    def test_a_check_set_with_a_compile_error_is_refused(self):
        lib = '#[cfg(feature = "py")]\ncompile_error!("py broken");\n'
        crate = tiny_crate("gate-probe", "0.1.0", extra="\n[features]\npy = []\n", lib=lib)
        with tempfile.TemporaryDirectory() as d:
            problems = REAL_CONSUMER_BUILD(crate, "gate-probe", "0.1.0", Path(d), {"consumer-check-features": [["py"]]})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("cargo check", problems[0])

    def expected(self, version: str, lib: str, until: str = "gate-probe 0.2.0", expect=None):
        crate = tiny_crate("gate-probe", version, extra="\n[features]\npy = []\n", lib=lib)
        entry = {"consumer-check-features": [["py"]],
                 "consumer-expected-failure": [{"features": ["py"], "until": until, "why": "w",
                                                "expect": expect or dict(self.EXPECT)}]}
        with tempfile.TemporaryDirectory() as d:
            return REAL_CONSUMER_BUILD(crate, "gate-probe", version, Path(d), entry)

    HELD = '#[cfg(feature = "py")]\npub fn held() { let _x: u8 = "known"; }\n'
    EXPECT = {"code": "E0308", "file": "src/lib.rs", "text": '"known"', "count": 1}

    def test_an_expected_failure_passes_until_its_release(self):
        self.assertEqual(self.expected("0.1.0", self.HELD), [])
        stale = self.expected("0.2.0", self.HELD)
        self.assertEqual(len(stale), 1, stale)
        self.assertIn("was until", stale[0])

    def test_an_expected_failure_that_builds_is_refused(self):
        fixed = self.expected("0.1.0", "pub fn f() {}\n")
        self.assertEqual(len(fixed), 1, fixed)
        self.assertIn("builds", fixed[0])

    def test_a_new_error_next_to_the_held_one_is_refused(self):
        lib = self.HELD + '#[cfg(feature = "py")]\npub fn new() { undefined_thing(); }\n'
        problems = self.expected("0.1.0", lib)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("E0425", problems[0])

    def test_the_same_code_elsewhere_or_with_another_message_is_refused(self):
        other_file = self.expected("0.1.0", self.HELD, expect=dict(self.EXPECT, file="src/other.rs"))
        self.assertEqual(len(other_file), 1, other_file)
        other_message = self.expected("0.1.0", self.HELD, expect=dict(self.EXPECT, message="expected `bool`"))
        self.assertEqual(len(other_message), 1, other_message)
        self.assertEqual(self.expected("0.1.0", self.HELD, expect=dict(self.EXPECT, message="mismatched types")), [])

    def test_another_error_with_the_same_code_in_the_same_file_is_refused(self):
        # the held error fixed and a new type mismatch in the same file: same code and file, other text
        lib = '#[cfg(feature = "py")]\npub fn other() { let _y: bool = 3; }\n'
        problems = self.expected("0.1.0", lib)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("E0308", problems[0])

    def test_the_label_pins_the_held_error_where_the_text_alone_does_not(self):
        # the held error fixed and a new mismatch on the same text in another function
        lib = '#[cfg(feature = "py")]\npub fn other() { let _q: i64 = "known"; }\n'
        self.assertEqual(self.expected("0.1.0", lib), [])  # text and count alone accept it
        held_label = dict(self.EXPECT, label="expected `u8`, found `&str`")
        self.assertEqual(self.expected("0.1.0", self.HELD, expect=held_label), [])
        problems = self.expected("0.1.0", lib, expect=held_label)
        self.assertEqual(len(problems), 1, problems)

    def test_one_more_of_the_held_error_is_refused_by_the_count(self):
        lib = self.HELD + '#[cfg(feature = "py")]\npub fn again() { let _x: u8 = "known"; }\n'
        problems = self.expected("0.1.0", lib)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("count is 1", problems[0])
        self.assertEqual(self.expected("0.1.0", lib, expect=dict(self.EXPECT, count=2)), [])

    def test_the_code_in_a_comment_of_another_error_is_not_the_error(self):
        lib = '#[cfg(feature = "py")]\npub fn other() { undefined_thing(); } // E0308\n'
        problems = self.expected("0.1.0", lib)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("E0425", problems[0])

    def test_crate_errors_reads_the_crates_own_json_diagnostics(self):
        mine = {"reason": "compiler-message", "package_id": "path+file:///x#gate-probe@0.1.0",
                "message": {"level": "error", "code": {"code": "E0308"}, "message": "mismatched types",
                            "spans": [{"file_name": "/tmp/x/src/lib.rs", "is_primary": True,
                                       "text": [{"text": 'let _x: u8 = "known";', "highlight_start": 14,
                                                 "highlight_end": 21}]}]}}
        dep = dict(mine, package_id="registry+https://x#other@1.0.0")
        abort = {"reason": "compiler-message", "package_id": mine["package_id"],
                 "message": {"level": "error", "code": None, "message": "aborting due to 1 previous error", "spans": []}}
        warn = {"reason": "compiler-message", "package_id": mine["package_id"],
                "message": {"level": "warning", "code": None, "message": "unused", "spans": []}}
        out = "\n".join(json.dumps(m) for m in (mine, dep, abort, warn)) + "\nnot json\n"
        errs = rt.crate_errors(out, "gate-probe", "0.1.0")
        self.assertEqual(errs, [{"code": "E0308", "file": "/tmp/x/src/lib.rs", "text": '"known"', "label": "",
                                 "message": "mismatched types"}])
        ex = {"code": "E0308", "file": "src/lib.rs", "text": '"known"'}
        self.assertTrue(rt.matches_expected(errs[0], ex))
        self.assertFalse(rt.matches_expected(errs[0], dict(ex, file="b.rs")))  # not a suffix of a name
        self.assertFalse(rt.matches_expected(dict(errs[0], file="/tmp/x/src/xlib.rs"), dict(ex, file="lib.rs")))
        self.assertFalse(rt.matches_expected(errs[0], dict(ex, text="3")))

    def test_an_entry_for_one_command_does_not_hold_the_other(self):
        e = {"consumer-expected-failure": [{"features": ["py"], "command": "check", "expect": {}, "until": "x 1.0.0", "why": "w"}]}
        self.assertIsNotNone(rt.expected_failure(e, "x", "0.1.0", ["py"], "check"))
        self.assertIsNone(rt.expected_failure(e, "x", "0.1.0", ["py"], "build"))

    def test_consumer_features_in_the_roster_are_checked(self):
        why = 'consumer-features-why = "python is built by maturin"\n'
        rt.load_roster(ROSTER.replace('order = 1\n', 'order = 1\nconsumer-features = [["a"], ["a", "b"]]\n' + why, 1))
        for bad in ('[]', '["a"]', '[[1]]', '[[""]]'):
            with self.assertRaises(rt.RosterError, msg=bad):
                rt.load_roster(ROSTER.replace('order = 1\n', f'order = 1\nconsumer-features = {bad}\n' + why, 1))
        for bad in ('consumer-features = [["a"]]\n', 'consumer-features = [["a"]]\nconsumer-features-why = " "\n', why):
            with self.assertRaises(rt.RosterError, msg=bad):
                rt.load_roster(ROSTER.replace('order = 1\n', 'order = 1\n' + bad, 1))
        ok = ('consumer-check-features = [["py"]]\n' + why +
              'consumer-expected-failure = [{features = ["py"], expect = {code = "E0308", file = "src/lib.rs", text = "arr", count = 1}, '
              'until = "alice-zip 0.10.1", why = "w"}]\n')
        rt.load_roster(ROSTER.replace('order = 1\n', 'order = 1\n' + ok, 1))
        for old, bad in (('until = "alice-zip 0.10.1"', 'until = "alice-db 0.10.1"'),
                         ('until = "alice-zip 0.10.1"', 'until = "alice-zip"'),
                         ('{features = ["py"]', '{features = ["other"]'),
                         ('expect = {code = "E0308", file = "src/lib.rs", text = "arr", count = 1}', 'expect = "E0308"'),
                         ('file = "src/lib.rs",', 'file = "",'),
                         ('count = 1}', 'count = 1, line = 3}'),
                         ('count = 1}', 'count = 0}'),
                         (', text = "arr"', ''),
                         ('why = "w"}', 'why = "w", command = "link"}')):
            text = ok.replace(old, bad, 1)
            self.assertNotEqual(text, ok)
            with self.assertRaises(rt.RosterError, msg=bad):
                rt.load_roster(ROSTER.replace('order = 1\n', 'order = 1\n' + text, 1))


class GateConsumer(unittest.TestCase):
    def test_a_consumer_refusal_refuses_the_gate_after_packaging(self):
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP})
            p = Path(d, "r.toml")
            p.write_text(ROSTER, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo), "--ref", ref],
                               fetch=fixture(PUBLISHED_DM), lister=lambda r, m: ["Cargo.toml"], packager=fake_packager,
                               consumer=lambda body, name, version, t, entry=None: [f"{name} {version}: does not build"])
        self.assertEqual(code, 1)
        self.assertIn("refused: alice-zip 0.10.0: does not build", out.getvalue())
        self.assertNotIn("go for", out.getvalue())

    def test_the_roster_feature_sets_reach_the_consumer_build(self):
        seen = []
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP})
            p = Path(d, "r.toml")
            p.write_text(ROSTER.replace('order = 1\n', 'order = 1\nconsumer-features = [["svo"]]\nconsumer-features-why = "x"\n', 1), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo), "--ref", ref],
                               fetch=fixture(PUBLISHED_DM), lister=lambda r, m: ["Cargo.toml"], packager=fake_packager,
                               consumer=lambda body, name, version, t, entry=None: seen.append(entry["consumer-features"]) or [])
        self.assertEqual((code, seen), (0, [[["svo"]]]))



class ReleaseHeading(unittest.TestCase):
    def test_a_dated_heading_passes_and_others_are_refused(self):
        ok = "# C\n\n## [Unreleased]\n\n## [0.3.0] - 2026-10-13\n\n- x\n"
        self.assertIsNone(rt.release_heading_problem(ok, "0.3.0"))
        for text in ("## [0.3.0] - 未公開 (公開日を入れる)\n", "## [0.3.0]\n", "## [0.3.0] - 2026-13-01\n",
                     "## [0.3.0] - 2999-01-01\n", "## [0.3.0] - 2026-10-13 (yanked)\n", "## [0.2.0] - 2026-01-01\n"):
            self.assertIsNotNone(rt.release_heading_problem(text, "0.3.0"), text)

    def test_the_gate_refuses_an_undated_heading_without_changelog_has(self):
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP, "CHANGELOG.md": "## [0.10.0] - 未公開 (公開日を入れる)\n\n- x\n"})
            p = Path(d, "r.toml")
            p.write_text(ROSTER, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo), "--ref", ref],
                               fetch=fixture(PUBLISHED_DM), lister=lambda r, m: ["Cargo.toml"], packager=fake_packager)
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("is not `## [0.10.0] - YYYY-MM-DD`", out.getvalue())


class PackagingLeavesTheTree(unittest.TestCase):
    def test_a_packager_that_changes_a_tracked_file_is_refused(self):
        def rewriting(repo, manifest_path, name, version, target_dir):
            (repo / "Cargo.lock").write_text("# rewritten\n", encoding="utf-8")
            return fake_packager(repo, manifest_path, name, version, target_dir)
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP, "Cargo.lock": "# lock\n"})
            p = Path(d, "r.toml")
            p.write_text(ROSTER, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo), "--ref", ref],
                               fetch=fixture(PUBLISHED_DM), lister=lambda r, m: ["Cargo.toml"], packager=rewriting)
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("after cargo package", out.getvalue())
        self.assertNotIn("go for", out.getvalue())


class PackageText(unittest.TestCase):
    def gate(self, files: dict):
        def packager(repo, manifest_path, name, version, target_dir):
            out = target_dir / "package" / f"{name}-{version}.crate"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(crate_bytes(name, version, files))
            return out
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP})
            p = Path(d, "r.toml")
            p.write_text(ROSTER, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo), "--ref", ref],
                               fetch=fixture(PUBLISHED_DM), lister=lambda r, m: ["Cargo.toml"], packager=packager)
        return code, out.getvalue()

    def test_internal_text_in_the_package_is_refused(self):
        # (the fixture words are joined at run time: this file is public text too)
        code, out = self.gate({"Cargo.toml": b"x", "src/lib.rs": b"// a " + b"wor" + b"ker wrote this\n"})
        self.assertEqual(code, 1, out)
        self.assertIn("the package carries internal text: src/lib.rs:1: agent process", out)
        code, out = self.gate({"Cargo.toml": b"x", "README.md": b"see [" + b"[feedback_x_y]" + b"]\n"})
        self.assertIn("note link", out)

    def test_a_clean_package_goes_and_the_check_is_named(self):
        code, out = self.gate({"Cargo.toml": b"x", "src/lib.rs": b"pub fn f() {}\n"})
        self.assertEqual(code, 0, out)
        self.assertIn("text:          every file of the package", out)

    def test_a_build_path_compiled_into_a_packaged_binary_is_refused(self):
        # (the home directory is spelled in pieces: this file is public text too)
        blob = b"\x7fELF\0\0" + b"/ho" + b"me/someuser/.cargo/registry/src/x/lib.rs" + b"\0\xff"
        code, out = self.gate({"Cargo.toml": b"x", "jni/libfoo.so": blob})
        self.assertEqual(code, 1, out)
        self.assertIn("the package carries internal text: jni/libfoo.so: 1 build paths: binary home path", out)
        self.assertNotIn("someuser", out)
        code, out = self.gate({"Cargo.toml": b"x", "jni/libfoo.so": b"\x7fELF\0\0symbol_table\0\xff"})
        self.assertEqual(code, 0, out)

    def test_a_package_with_no_file_compares_nothing(self):
        problems, n = rt.package_text_problems(crate_bytes("alice-zip", "0.10.0", {}), "alice-zip", "0.10.0",
                                               Path("."), "HEAD", None, "")
        self.assertEqual(n, 0)
        self.assertTrue(any("compared nothing" in p for p in problems))

    def test_a_package_read_with_an_empty_private_set_is_refused(self):
        load = rt.load_public_text

        def emptied():
            m = load()
            m.PRIVATE_REPO_HASHES = frozenset()
            return m
        self.addCleanup(setattr, rt, "load_public_text", load)
        rt.load_public_text = emptied
        problems, n = rt.package_text_problems(crate_bytes("alice-zip", "0.10.0", {"Cargo.toml": b"x"}), "alice-zip",
                                               "0.10.0", Path("."), "HEAD", None, "")
        self.assertEqual(n, 0)
        self.assertEqual(len(problems), 1)
        self.assertIn("private-repository set of this check is empty", problems[0])


class WorkflowPublisher(unittest.TestCase):
    def problems(self, workflow: str, reviewers=lambda o, n: 0, owner_repo="o/r"):
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP, ".github/workflows/release.yml": workflow})
            return rt.workflow_publish_problems(repo, ref, "alice-zip", owner_repo, reviewers)

    def job(self, body: str, head: str = "") -> str:
        return head + "jobs:\n  p:\n    runs-on: x\n" + body

    def test_crates_io_publishers_are_refused(self):
        for body in ("    steps:\n      - run: cargo publish\n",
                     "    steps:\n      - run: cargo +stable publish\n",
                     "    steps:\n      - run: cargo publish --token $T  # not --dry-run\n",
                     "    steps:\n      - run: |\n          cargo \\\n            publish\n",
                     "    steps:\n      - run: |\n          cargo\n          publish --locked\n",
                     "    steps:\n      - uses: MarcoIeni/release-plz-action@v0.5\n",
                     "    steps:\n      - run: cargo workspaces publish --yes\n",
                     "    steps:\n      - run: cargo release publish --execute\n",
                     "    steps:\n      - run: ./scripts/publish.sh\n        env:\n          CARGO_REGISTRY_TOKEN: ${{ secrets.T }}\n",
                     "    steps:\n      - run: ./x.sh\n        env:\n          CARGO_REGISTRIES_MINE_TOKEN: ${{ secrets.X }}\n",
                     "    steps:\n      - uses: rust-lang/crates-io-auth-action@v1\n",
                     "    permissions:\n      id-token: write\n    steps:\n      - run: cargo build\n",
                     "    environment: release\n    steps:\n      - run: cargo publish\n"):
            problems, n = self.problems(self.job(body), reviewers=lambda o, e: 5)
            self.assertEqual((len(problems), n), (1, 1), body)
            self.assertIn("crates.io", problems[0])
        problems, _ = self.problems("env:\n  CARGO_REGISTRY_TOKEN: ${{ secrets.T }}\n" + self.job("    steps: []\n"))
        self.assertTrue(problems)  # a workflow-level credential

    def test_dry_runs_comments_and_other_commands_pass(self):
        for body in ("    steps:\n      - run: cargo publish --dry-run\n",
                     "    steps:\n      - run: echo hello # cargo publish\n",
                     "    steps:\n      - run: cargo test\n"):
            self.assertEqual(self.problems(self.job(body))[0], [], body)

    def test_another_registry_needs_an_environment_with_reviewers(self):
        pypi = "    environment: pypi\n    permissions:\n      id-token: write\n    steps:\n      - uses: pypa/gh-action-pypi-publish@abc\n"
        self.assertEqual(self.problems(self.job(pypi), reviewers=lambda o, e: 1)[0], [])
        problems, _ = self.problems(self.job(pypi), reviewers=lambda o, e: 0)
        self.assertIn("without an environment that has required reviewers (pypi)", problems[0])
        named = "    environment:\n      name: pypi\n      url: x\n    steps:\n      - run: twine upload d/*\n"
        seen = []
        self.assertEqual(self.problems(self.job(named), reviewers=lambda o, e: seen.append(e) or 1)[0], [])
        self.assertEqual(seen, ["pypi"])
        bare = "    steps:\n      - run: npm publish\n        env:\n          NODE_AUTH_TOKEN: ${{ secrets.N }}\n"
        self.assertEqual(len(self.problems(self.job(bare), reviewers=lambda o, e: 1)[0]), 1)
        self.assertEqual(len(self.problems(self.job(pypi), reviewers=lambda o, e: 1, owner_repo=None)[0]), 1)

    def test_the_environment_of_another_job_does_not_count(self):
        wf = "jobs:\n  a:\n    environment: pypi\n    steps: []\n  b:\n    steps:\n      - run: twine upload x\n"
        self.assertEqual(len(self.problems(wf, reviewers=lambda o, e: 1)[0]), 1)

    def test_no_workflow_directory_compares_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP, ".github/workflows/": ""})
            problems, n = rt.workflow_publish_problems(repo, ref, "alice-zip")
        self.assertEqual(n, 0)
        self.assertIn("compared nothing", problems[0])

    def test_the_gate_refuses_a_repository_with_a_publishing_workflow(self):
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP, ".github/workflows/r.yml": "jobs:\n  p:\n    steps:\n      - run: cargo publish\n"})
            p = Path(d, "r.toml")
            p.write_text(ROSTER, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo), "--ref", ref],
                               fetch=fixture(PUBLISHED_DM), lister=lambda r, m: ["Cargo.toml"], packager=fake_packager)
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("can publish to crates.io outside this gate", out.getvalue())


TAG = "on:\n  push:\n    tags: ['v*']\n"
SHAPES = {
    "script with a renamed secret": "  p:\n    runs-on: x\n    steps:\n      - run: ./scripts/ship.sh\n        env:\n          TOK: ${{ secrets.CRATES }}\n",
    "local composite action": "  p:\n    runs-on: x\n    steps:\n      - uses: ./.github/actions/ship\n        with:\n          token: ${{ secrets.CRATES }}\n",
    "login then make": "  p:\n    runs-on: x\n    steps:\n      - run: cargo login ${{ secrets.X }}\n      - run: make release\n",
    "assembled env name": "  p:\n    runs-on: x\n    steps:\n      - run: echo \"CARGO_REGISTRY_${KIND}=${{ secrets.X }}\" >> $GITHUB_ENV\n      - run: make release\n",
    "xargs": "  p:\n    runs-on: x\n    steps:\n      - run: echo publish | xargs cargo --token ${{ secrets.X }}\n",
    "local reusable workflow, inherited": "  p:\n    uses: ./.github/workflows/inner.yml\n    secrets: inherit\n",
}
INNER = "on:\n  workflow_call:\njobs:\n  q:\n    runs-on: x\n    steps:\n      - run: ./ship.sh ${{ secrets.CRATES }}\n"


class SecretInventory(unittest.TestCase):
    """a publishing credential must not be reachable by a workflow outside an approved environment"""

    def check(self, job: str, declared: dict, reviewers: dict | None = None, head: str = TAG, env: str | None = None):
        if env:
            job = job.replace("    runs-on: x\n", f"    runs-on: x\n    environment: {env}\n", 1) if "runs-on" in job else job
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP, ".github/workflows/release.yml": head + "jobs:\n" + job,
                                      ".github/workflows/inner.yml": INNER})
            problems, _ = rt.workflow_publish_problems(repo, ref, "alice-zip", "o/r", lambda o, e: 0, declared, reviewers or {})
        return problems

    def test_an_undeclared_or_publishing_repository_secret_is_refused(self):
        inv = {"repo": ["CRATES"], "environments": {"release": ["X"]}, "reviewers": {"release": 0}}
        problems = rt.secret_problems("alice-zip", "R", {}, inv)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all("not declared" in p for p in problems))
        declared = {"CRATES": {"purpose": "crates.io", "publishing": True}, "release:X": {"purpose": "x", "publishing": True}}
        problems = rt.secret_problems("alice-zip", "R", declared, inv)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all("publishing credential" in p for p in problems))
        inv["reviewers"]["release"] = 1
        self.assertEqual(len(rt.secret_problems("alice-zip", "R", declared, inv)), 1)  # the repository one stays
        harmless = {"CRATES": {"purpose": "coverage", "publishing": False}, "release:X": {"purpose": "x", "publishing": False}}
        self.assertEqual(rt.secret_problems("alice-zip", "R", harmless, inv), [])
        stale = dict(harmless, GONE={"purpose": "removed", "publishing": True})
        problems = rt.secret_problems("alice-zip", "R", stale, inv)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("does not have", problems[0])

    def test_each_indirect_shape_needs_an_approved_environment_for_a_publishing_secret(self):
        publishing = {"CRATES": {"purpose": "crates.io", "publishing": True}, "X": {"purpose": "crates.io", "publishing": True}}
        harmless = {"CRATES": {"purpose": "a licence", "publishing": False}, "X": {"purpose": "a licence", "publishing": False}}
        for name, job in SHAPES.items():
            refused = [p for p in self.check(job, publishing) if "receives" in p]
            self.assertEqual(len(refused), 1, (name, refused))
            self.assertEqual([p for p in self.check(job, harmless) if "receives" in p], [], name)
            self.assertEqual([p for p in self.check(job, publishing, head="on:\n  push:\n    branches: [main]\n") if "receives" in p], [], name)
            if "runs-on" in job:
                self.assertEqual([p for p in self.check(job, publishing, {"release": 1}, env="release") if "receives" in p], [], name)
                self.assertEqual(len([p for p in self.check(job, publishing, {"release": 0}, env="release") if "receives" in p]), 1, name)

    def test_inheriting_secrets_into_an_external_workflow_is_refused(self):
        job = "  p:\n    uses: someorg/tools/.github/workflows/publish.yml@main\n    secrets: inherit\n"
        for declared in ({}, {"X": {"purpose": "x", "publishing": False}}):
            problems = self.check(job, declared)
            self.assertTrue(any("not a local workflow" in p for p in problems), problems)

    def test_an_oidc_token_on_a_release_trigger_needs_an_approved_environment(self):
        job = "  p:\n    runs-on: x\n    permissions:\n      id-token: write\n    steps:\n      - run: ./upload.sh\n"
        self.assertTrue(any("OIDC" in p for p in self.check(job, {})))
        self.assertEqual([p for p in self.check(job, {}, {"release": 1}, env="release") if "receives" in p], [])

    def test_the_roster_declares_secrets_with_a_purpose(self):
        rt.load_roster(ROSTER + '\n[secrets."R"]\nX = { purpose = "coverage upload", publishing = false }\n')
        for bad in ('X = "coverage"', 'X = { purpose = "" , publishing = false }', 'X = { purpose = "p" }',
                    'X = { purpose = "p", publishing = "no" }'):
            with self.assertRaises(rt.RosterError, msg=bad):
                rt.load_roster(ROSTER + f'\n[secrets."R"]\n{bad}\n')

    def test_the_gate_refuses_when_the_secrets_cannot_be_read(self):
        def broken(owner_repo):
            raise RuntimeError("gh api: 404")
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP})
            p = Path(d, "r.toml")
            p.write_text(ROSTER, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo), "--ref", ref],
                               fetch=fixture(PUBLISHED_DM), lister=lambda r, m: ["Cargo.toml"], packager=fake_packager,
                               inventory=broken)
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("the secrets of the repository could not be read", out.getvalue())
        with self.assertRaises(RuntimeError):
            rt.secret_inventory(None)


class VariablesAndHosts(unittest.TestCase):
    def test_variables_are_declared_with_a_purpose_and_never_as_credentials(self):
        inv = {"repo": [], "environments": {"pypi": []}, "reviewers": {}, "variables": ["A"], "env_variables": {"pypi": ["B"]}}
        self.assertEqual(len(rt.variable_problems("c", "R", {}, inv)), 2)
        ok = {"A": {"purpose": "a flag"}, "pypi:B": {"purpose": "x"}}
        self.assertEqual(rt.variable_problems("c", "R", ok, inv), [])
        stale = dict(ok, GONE={"purpose": "y"})
        self.assertIn("does not have", rt.variable_problems("c", "R", stale, inv)[0])
        rt.load_roster(ROSTER + '\n[variables."R"]\nA = { purpose = "a flag" }\n')
        for bad in ('A = { purpose = "t", publishing = true }', 'A = "x"', 'A = { purpose = "" }'):
            with self.assertRaises(rt.RosterError, msg=bad):
                rt.load_roster(ROSTER + f'\n[variables."R"]\n{bad}\n')

    def test_dependabot_secrets_are_inventoried(self):
        inv = {"repo": [], "environments": {}, "reviewers": {}, "dependabot": ["D"]}
        self.assertIn("not declared", rt.secret_problems("c", "R", {}, inv)[0])
        self.assertIn("publishing credential", rt.secret_problems("c", "R", {"dependabot:D": {"purpose": "x", "publishing": True}}, inv)[0])
        self.assertEqual(rt.secret_problems("c", "R", {"dependabot:D": {"purpose": "x", "publishing": False}}, inv), [])

    def test_every_list_is_read_page_by_page_and_counted(self):
        pages = {}

        def fake_run(cmd, *a, **kw):
            path = cmd[2]
            base, _, query = path.partition("?")
            page = int(dict(x.split("=") for x in query.split("&")).get("page", "1")) if query else 1
            key = "variables" if "variables" in base else "environments" if base.endswith("environments") else "secrets"
            if base.endswith("actions/secrets"):
                total, names = 150, [f"S{i}" for i in range(150)]
            elif base.endswith("actions/variables") and pages.get("short"):
                total, names = 3, ["V0", "V1"]  # GitHub counts 3, lists 2
            else:
                total, names = 0, []
            chunk = names[(page - 1) * 100: page * 100]
            body = {"total_count": total, key: [{"name": n} for n in chunk]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")
        real = rt.subprocess.run
        rt.subprocess.run = fake_run
        try:
            inv = rt.secret_inventory("o/r")
            self.assertEqual(len(inv["repo"]), 150)
            pages["short"] = True
            with self.assertRaises(RuntimeError):
                rt.secret_inventory("o/r")
        finally:
            rt.subprocess.run = real

    def hosts(self, runs_on: str, head: str = TAG, hosts=None, env=None, reviewers=None, extra: str = "", runners=None):
        job = f"  p:\n    runs-on: {runs_on}\n" + (f"    environment: {env}\n" if env else "") + extra + "    steps:\n      - run: make\n"
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP, ".github/workflows/w.yml": head + "jobs:\n" + job})
            problems, _ = rt.workflow_publish_problems(repo, ref, "c", "o/r", lambda o, e: 0, {}, reviewers or {}, hosts or {},
                                                       runners or [])
        return [p for p in problems if "self-hosted" in p]

    def test_a_self_hosted_job_on_a_release_trigger_needs_an_approved_environment_or_a_declaration(self):
        sdf_runner = [{"self-hosted", "Windows", "X64", "unity", "windows-unity", "ue5"}]
        for runs_on in ("[self-hosted, windows, ue5]", "self-hosted", "my-box", "${{ vars.RUNNER }}", "[linux, gpu]",
                        "windows-unity", "[windows-unity]", "ubuntu-latest-gpu", "${{ inputs.runner }}",
                        "${{ github.event.inputs.x }}", "${{ needs.setup.outputs.runner }}",
                        "${{ fromJSON(needs.x.outputs.matrix) }}", "{ group: big }", "${{ matrix.os }}"):
            self.assertEqual(len(self.hosts(runs_on, runners=sdf_runner)), 1, runs_on)
        self.assertEqual(len(self.hosts("${{ matrix.os }}", extra="    strategy:\n      matrix:\n        os: [self-hosted]\n")), 1)
        self.assertEqual(len(self.hosts("${{ matrix.os }}", extra="    strategy:\n      matrix:\n        os: [ubuntu-latest, ${{ inputs.x }}]\n")), 1)
        self.assertEqual(len(self.hosts("", extra="")), 1)  # no label at all
        multi = "\n      - self-hosted\n      - linux"
        self.assertEqual(len(self.hosts(multi)), 1)
        for runs_on in ("ubuntu-latest", "windows-2022", "macos-15-intel", "[ubuntu-24.04-arm]"):
            self.assertEqual(self.hosts(runs_on, runners=sdf_runner), [], runs_on)
        hosted_matrix = "    strategy:\n      matrix:\n        os: [ubuntu-latest, windows-latest, macos-15]\n"
        self.assertEqual(self.hosts("${{ matrix.os }}", extra=hosted_matrix), [])
        include = "    strategy:\n      matrix:\n        include:\n          - os: ubuntu-latest\n          - os: macos-14\n"
        self.assertEqual(self.hosts("${{ matrix.os }}", extra=include), [])
        flow = "    strategy:\n      matrix:\n        include:\n          - { os: ubuntu-latest, target: x }\n          - { os: macos-14, target: y }\n"
        self.assertEqual(self.hosts("${{ matrix.os }}", extra=flow), [])
        flow_custom = flow.replace("macos-14", "windows-unity")
        self.assertEqual(len(self.hosts("${{ matrix.os }}", extra=flow_custom)), 1)
        chosen = ("    strategy:\n      matrix:\n        os: ${{ fromJSON(github.event_name == 'schedule' && '[\"macos-15-intel\"]' "
                  "|| '[\"ubuntu-latest\", \"windows-latest\"]') }}\n")
        self.assertEqual(self.hosts("${{ matrix.os }}", extra=chosen), [])
        self.assertEqual(len(self.hosts("${{ matrix.os }}", extra=chosen.replace("macos-15-intel", "my-box"))), 1)
        self.assertEqual(len(self.hosts("${{ matrix.os }}", extra=chosen.replace("'[\"ubuntu-latest\", \"windows-latest\"]'", "inputs.os"))), 1)
        self.assertEqual(self.hosts("self-hosted", head="on:\n  push:\n    branches: [main]\n"), [])
        self.assertEqual(self.hosts("self-hosted", env="build", reviewers={"build": 1}), [])
        self.assertEqual(len(self.hosts("self-hosted", env="build", reviewers={"build": 0})), 1)
        declared = {"w.yml:p": {"purpose": "the engine build", "registry-credentials-on-host": False}}
        self.assertEqual(self.hosts("self-hosted", hosts=declared), [])

    def test_a_label_of_a_registered_runner_is_self_hosted_even_if_it_looks_hosted(self):
        # a runner registered with the label "ubuntu-latest" would take the job: the registered labels win
        self.assertEqual(len(self.hosts("ubuntu-latest", runners=[{"self-hosted", "ubuntu-latest"}])), 1)

    def test_the_roster_declares_self_hosted_jobs_without_host_credentials(self):
        rt.load_roster(ROSTER + '\n[self-hosted."R"]\n"w.yml:p" = { purpose = "x", registry-credentials-on-host = false }\n')
        for bad in ('"w.yml:p" = { purpose = "x", registry-credentials-on-host = true }', '"w.yml:p" = { purpose = "x" }',
                    '"p" = { purpose = "x", registry-credentials-on-host = false }'):
            with self.assertRaises(rt.RosterError, msg=bad):
                rt.load_roster(ROSTER + f'\n[self-hosted."R"]\n{bad}\n')


def semver_output(step: str, total: int, failed: int, lints=(), from_v="1.0.0", to_v="1.1.0") -> str:
    """what cargo semver-checks 0.50 prints for one run (the lines the gate reads); with no
    failure it prints no "fail" count at all"""
    counts = f"{total - failed} pass, {failed} fail, 0 warn, 3 skip" if failed else f"{total} pass, 3 skip"
    out = [f"     Building x v{to_v} (current)",
           f"    Checking x v{from_v} -> v{to_v} ({step} change)",
           f"     Checked [   0.200s] {total} checks: {counts}"]
    out += [f"\n--- failure {lint}: some description ---\n" for lint in lints]
    out.append(f"     Summary {'semver requires new major version: 1 major and 0 minor checks failed' if failed else 'no semver update required'}")
    return "\n".join(out)


def latest_fixture(crate: str, version: str | None):
    def fetch(path: str) -> dict:
        if version is None:
            raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
        assert path == crate, path
        return {"crate": {"max_stable_version": version, "max_version": version}}
    return fetch


class Semver(unittest.TestCase):
    def test_the_step_follows_cargo_compatibility(self):
        for old, new, step in (("3.0.1", "3.1.0", "minor"), ("3.0.1", "3.0.2", "patch"), ("3.0.1", "4.0.0", "major"),
                               ("0.5.0", "0.5.1", "minor"), ("0.5.1", "0.6.0", "major"), ("0.0.1", "0.0.2", "major"),
                               ("1.9.9", "1.10.0", "minor")):
            self.assertEqual(rt.version_step(old, new), step, (old, new))

    def test_a_clean_minor_run_counts_and_a_breaking_one_is_refused(self):
        self.assertEqual(rt.semver_verdict(0, semver_output("minor", 196, 0), "minor", "l"), (None, 1))
        p, n = rt.semver_verdict(1, semver_output("minor", 196, 1, ["enum_discriminants_undefined_non_unit_variant"]),
                                 "minor", "alice-physics 3.1.0 (default features)")
        self.assertEqual(n, 0)
        self.assertIn("a minor version does not allow enum_discriminants_undefined_non_unit_variant", p)
        self.assertIn("semver requires new major version", p)

    def test_zero_checks_a_wrong_step_a_silent_failure_and_no_run_are_refused(self):
        self.assertIn("compared 0 checks", rt.semver_verdict(0, semver_output("minor", 0, 0), "minor", "l")[0])
        self.assertIn("compared a patch step, the versions make a minor step",
                      rt.semver_verdict(0, semver_output("patch", 10, 0), "minor", "l")[0])
        # a nonzero exit with no failed check counted is still refused
        self.assertIsNotNone(rt.semver_verdict(1, semver_output("minor", 10, 0), "minor", "l")[0])
        self.assertIn("did not run (exit 127)", rt.semver_verdict(127, "No such file: cargo", "minor", "l")[0])
        self.assertIn("printed no count", rt.semver_verdict(0, "    Checking x v1.0.0 -> v1.1.0 (minor change)", "minor", "l")[0])
        # colour codes do not hide the lines the gate reads
        coloured = semver_output("minor", 5, 0).replace("Checking", "\x1b[1m\x1b[32mChecking\x1b[0m")
        self.assertEqual(rt.semver_verdict(0, coloured, "minor", "l"), (None, 1))

    def test_a_major_step_is_not_compared(self):
        calls = []
        p, n, note = rt.semver_problems(Path("."), "Cargo.toml", "x", "2.0.0", {}, None, latest_fixture("x", "1.4.0"),
                                        run=lambda *a: calls.append(a) or (0, ""))
        self.assertEqual((p, n, calls), ([], 0, []))
        self.assertIn("major step", note)

    def test_a_crate_with_no_release_has_nothing_to_compare(self):
        p, n, note = rt.semver_problems(Path("."), "Cargo.toml", "x", "0.1.0", {}, None, latest_fixture("x", None),
                                        run=lambda *a: self.fail("ran"))
        self.assertEqual((p, n), ([], 0))
        self.assertIn("no release", note)

    def test_a_version_not_newer_than_the_latest_is_refused(self):
        for latest_v, version in (("1.1.0", "1.1.0"), ("1.2.0", "1.1.0"), ("1.0.0", "1.0.0-rc.1"),
                                  ("1.0.0-rc.2", "1.0.0-rc.1"), ("1.0.0+build.2", "1.0.0+build.1")):
            p, _, _ = rt.semver_problems(Path("."), "Cargo.toml", "x", version, {}, None, latest_fixture("x", latest_v),
                                         run=lambda *a: self.fail("ran"))
            self.assertEqual(len(p), 1, (latest_v, version))

    def test_a_pre_release_of_a_later_version_is_compared(self):
        p, n, note = rt.semver_problems(Path("."), "Cargo.toml", "x", "1.1.0-rc.1", {}, None, latest_fixture("x", "1.0.0"),
                                        run=lambda *a: (0, semver_output("minor", 5, 0)))
        self.assertEqual((p, n), ([], 2))
        self.assertIn("(minor)", note)

    def test_precedence_follows_semver(self):
        order = ["0.9.9", "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
                 "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.10.0"]
        keys = [rt.version_precedence(v) for v in order]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(rt.version_precedence("1.0.0+a"), rt.version_precedence("1.0.0"))

    def test_every_feature_set_of_the_consumer_build_is_compared(self):
        seen = []

        def run(repo, manifest_path, baseline, features, target_dir):
            seen.append((tuple(baseline), None if features is None else tuple(features)))
            return 0, semver_output("minor", 50, 0)
        entry = {"consumer-features": [["a", "std"]], "consumer-check-features": [["py"]]}
        p, n, note = rt.semver_problems(Path("."), "Cargo.toml", "x", "1.1.0", {"features": {"a": [], "py": []}}, entry,
                                        latest_fixture("x", "1.0.0"), run=run)
        self.assertEqual((p, n), ([], 4))
        base = ("--baseline-version", "1.0.0")
        self.assertEqual(seen, [(base, None), (base, ()), (base, ("a", "std")), (base, ("py",))])
        self.assertEqual(rt.semver_feature_args(None), ["--default-features"])
        self.assertEqual(rt.semver_feature_args([]), ["--only-explicit-features"])
        self.assertEqual(rt.semver_feature_args(["a", "b"]), ["--only-explicit-features", "--features", "a,b"])

    def test_one_refused_feature_set_refuses_the_release(self):
        runs = iter([(0, semver_output("minor", 50, 0)),
                     (1, semver_output("minor", 50, 1, ["enum_variant_added"]))])
        p, n, _ = rt.semver_problems(Path("."), "Cargo.toml", "x", "1.1.0", {}, None, latest_fixture("x", "1.0.0"),
                                     run=lambda *a: next(runs))
        self.assertEqual((len(p), n), (1, 1), p)
        self.assertIn("(no default features)", p[0])

    def test_the_gate_refuses_a_breaking_change_under_a_minor_version(self):
        roster_text = ROSTER.replace('version = "0.10.0"', 'version = "0.10.1"', 1)
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP.replace('version = "0.10.0"', 'version = "0.10.1"', 1)})
            p = Path(d, "r.toml")
            p.write_text(roster_text, encoding="utf-8")
            fetch = fixture(PUBLISHED_DM, {"alice-zip": "0.10.0"})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo), "--ref", ref],
                               fetch=fetch, lister=lambda r, m: ["Cargo.toml"], packager=fake_packager,
                               semver=lambda *a: rt.semver_problems(*a, run=lambda *r: (
                                   1, semver_output("minor", 9, 1, ["inherent_method_missing"], "0.10.0", "0.10.1"))))
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("refused: alice-zip 0.10.1 (default features): a minor version does not allow inherent_method_missing",
                      out.getvalue())
        self.assertNotIn("go for", out.getvalue())


class GateGoOutput(unittest.TestCase):
    def test_the_go_lines_name_every_step_in_one_column(self):
        with tempfile.TemporaryDirectory() as d:
            repo, ref = make_repo(d, {"Cargo.toml": ZIP})
            p = Path(d, "r.toml")
            p.write_text(ROSTER, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = rt.main(["--roster", str(p), "publish-gate", "alice-zip", "--repo", str(repo), "--ref", ref],
                               fetch=fixture(PUBLISHED_DM), lister=lambda r, m: ["Cargo.toml"], packager=fake_packager,
                               semver=lambda *a: ([], 3, "0.9.0 -> 0.10.0 (major step)"))
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        labels = re.findall(r"^  ([a-z. 0-9]+):( +)\S", text, re.M)
        self.assertEqual([name for name, _ in labels],
                         ["commit", ".crate sha256", "text", "consumer", "semver", "publish", "then"])
        self.assertEqual({len(name) + len(gap) for name, gap in labels}, {14}, labels)
        self.assertIn("  semver:        0.9.0 -> 0.10.0 (major step)", text)


class SemverEndToEnd(unittest.TestCase):
    """the real cargo semver-checks on two small crates: the baseline 1.0.0 has a
    #[non_exhaustive] unit-only enum, as alice-physics 3.0.1's LawCheck has"""

    BASE = "#[derive(Clone, Copy, Debug)]\n#[non_exhaustive]\npub enum Check {\n    A,\n    B,\n}\n"

    def crate(self, root: Path, name: str, version: str, src: str) -> Path:
        d = root / name
        (d / "src").mkdir(parents=True)
        (d / "Cargo.toml").write_text(f'[package]\nname = "semverprobe"\nversion = "{version}"\nedition = "2021"\n'
                                      f'publish = false\n\n[workspace]\n', encoding="utf-8")
        (d / "src" / "lib.rs").write_text(src, encoding="utf-8")
        return d

    def gate(self, version: str, src: str) -> tuple[list[str], int, str]:
        if shutil.which("cargo-semver-checks") is None:
            self.fail("cargo-semver-checks is not installed (CI installs it; locally: cargo install cargo-semver-checks)")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            base = self.crate(root, "base", "1.0.0", self.BASE)
            new = self.crate(root, "new", version, src)
            return rt.semver_problems(
                new, "Cargo.toml", "semverprobe", version, {}, None, latest_fixture("semverprobe", "1.0.0"),
                run=lambda repo, mp, baseline, features, td: rt.run_semver_checks(
                    repo, mp, ["--baseline-root", str(base)], features, td),
                target_dir=root / "target")

    def test_a_field_variant_under_a_minor_version_is_refused(self):
        src = self.BASE.replace("    B,\n", "    B,\n    C { id: [u8; 32] },\n")
        p, n, _ = self.gate("1.1.0", src)
        self.assertEqual(n, 0)
        self.assertEqual(len(p), 2, p)  # default features and none
        self.assertIn("enum_discriminants_undefined_non_unit_variant", p[0])

    def test_a_unit_variant_under_a_minor_version_passes(self):
        p, n, note = self.gate("1.1.0", self.BASE.replace("    B,\n", "    B,\n    C,\n"))
        self.assertEqual((p, n), ([], 2), p)
        self.assertIn("1.0.0 -> 1.1.0 (minor)", note)

    def test_an_unchanged_api_under_a_patch_version_passes(self):
        self.assertEqual(self.gate("1.0.1", self.BASE)[:2], ([], 2))

    def test_the_same_change_under_a_major_version_is_not_compared(self):
        p, n, note = self.gate("2.0.0", self.BASE.replace("    B,\n", "    B,\n    C { id: [u8; 32] },\n"))
        self.assertEqual((p, n), ([], 0))
        self.assertIn("major step", note)


if __name__ == "__main__":
    unittest.main()
