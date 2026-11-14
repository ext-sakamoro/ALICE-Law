#!/usr/bin/env python3
"""Tests of scripts/release_train.py (python3 scripts/test_release_train.py)

No network: crates.io is replaced by a fixture. The roster in the repository is also
read, so a malformed edit to it fails here.
"""
from __future__ import annotations

import os
# a made-up name key: the package text check fails closed without one (the real key is never read here)
os.environ["ALICE_PRIVATE_NAME_HMAC_KEY"] = "4d" * 32
import contextlib
import gzip
import importlib.util
import io
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rt", HERE / "release_train.py")
rt = importlib.util.module_from_spec(spec)
sys.modules["rt"] = rt
spec.loader.exec_module(rt)

_REAL_MAIN = rt.main


def _main(argv=None, **kw):
    # read the features from the fixture's [features] table instead of cargo metadata
    # (class FeatureCoverage runs the real cargo metadata)
    kw.setdefault("metadata", fixture_metadata)
    return _REAL_MAIN(argv, **kw)


rt.main = _main


def fixture_metadata(manifest_path):
    """`cargo metadata --no-deps` of a fixture manifest, its [features] table taken as is"""
    m = tomllib.loads(Path(manifest_path).read_text(encoding="utf-8"))
    return {"packages": [{"name": m["package"]["name"], "manifest_path": str(manifest_path),
                          "features": m.get("features", {})}]}

ROSTER = """
[crate.alice-det-math]
version = "0.5.0"
sha = "1111111111111111111111111111111111111111"
state = "main"
order = 0

[crate.alice-zip]
version = "0.10.0"
sha = "2222222222222222222222222222222222222222"
state = "main"
order = 1

[crate.alice-analytics]
version = "0.5.0"
sha = "3333333333333333333333333333333333333333"
state = "main"
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
                               "HOME": str(repo)}, encoding="utf-8").stdout


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


FOLLOW_ROSTER = """
[crate.alice-zip]
version = "0.10.0"
sha = "4444444444444444444444444444444444444444"
state = "main"
order = 0
repo = "Z"

[crate.alice-db]
version = "0.4.0"
sha = "5555555555555555555555555555555555555555"
state = "main"
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


@unittest.skipIf(shutil.which("cargo") is None, "cargo is not installed")


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


COVERED = ROSTER.replace('order = 1\n', 'order = 1\nrepo = "ALICE-Zip"\nconsumer-features = [["fft", "std"]]\n'
                         'consumer-features-why = "w"\nexclude-features = { hw = "a board-only feature" }\n', 1)
ZIP_FEATURES = ZIP + '\n[features]\ndefault = ["std"]\nstd = []\nfft = []\nhw = []\n'


def named_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    """a git repository root/name holding `files`, committed"""
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    return repo


class FeatureCoverage(unittest.TestCase):
    """the coverage of the declared consumer sets; the features are the `features` map of
    cargo metadata (the command and gate tests run the real cargo metadata on fixture crates)"""

    def test_the_roster_validates_exclude_features(self):
        rt.load_roster(COVERED)
        for bad in ('exclude-features = { hw = "" }', 'exclude-features = ["hw"]', 'exclude-features = {}',
                    'exclude-features = { fft = "in a set too" }', 'exclude-features = { default = "not a feature" }'):
            with self.assertRaises(rt.RosterError, msg=bad):
                rt.load_roster(COVERED.replace('exclude-features = { hw = "a board-only feature" }', bad, 1))
        with self.assertRaises(rt.RosterError):
            rt.load_roster(ROSTER.replace('order = 1\n', 'order = 1\nexclude-features = { hw = "x" }\n', 1))

    def test_every_feature_must_be_enabled_by_a_set_or_excluded(self):
        entry = rt.load_roster(COVERED)["alice-zip"]
        f = {"default": ["std"], "std": [], "fft": [], "hw": []}
        self.assertEqual(rt.feature_coverage_problems("alice-zip", entry, f), ([], 1))
        # a feature added to the manifest later, in no set and not excluded
        p, n = rt.feature_coverage_problems("alice-zip", entry, {**f, "foo": []})
        self.assertEqual((n, len(p)), (1, 1))
        self.assertIn("['foo'] are enabled by no consumer set and not in exclude-features", p[0])
        # names the crate does not have: a set member, or an exclude left behind
        p, _ = rt.feature_coverage_problems("alice-zip", entry, {"std": [], "hw": []})
        self.assertIn("['fft']", p[0])
        p, _ = rt.feature_coverage_problems("alice-zip", entry, {"std": [], "fft": []})
        self.assertIn("['hw']", p[0])
        # check sets count as covered
        e2 = {**entry, "consumer-check-features": [["foo"]]}
        self.assertEqual(rt.feature_coverage_problems("alice-zip", e2, {**f, "foo": []}), ([], 1))

    def test_an_implicit_feature_counts_and_is_covered_through_the_feature_that_enables_it(self):
        entry = {"consumer-features": [["std", "python"]]}
        # cargo metadata: serde and pyo3 are optional dependencies; python enables pyo3 (its
        # implicit feature) and nothing enables serde
        f = {"std": [], "python": ["pyo3", "numpy/extra"], "pyo3": ["dep:pyo3"], "numpy": ["dep:numpy"],
             "serde": ["dep:serde"], "weak": ["serde?/derive"]}
        p, _ = rt.feature_coverage_problems("x", entry, f)
        self.assertEqual(len(p), 1, p)
        self.assertIn("['serde', 'weak']", p[0])
        # "serde?/derive" does not enable serde
        p, _ = rt.feature_coverage_problems("x", {"consumer-features": [["std", "python", "weak"]]}, f)
        self.assertIn("['serde']", p[0])
        self.assertEqual(rt.feature_coverage_problems("x", {"consumer-features": [["std", "python", "weak"], ["serde"]]},
                                                      f), ([], 1))

    def test_a_crate_without_declared_sets_is_not_compared(self):
        entry = rt.load_roster(ROSTER)["alice-zip"]
        self.assertEqual(rt.feature_coverage_problems("alice-zip", entry, {"anything": []}), ([], 0))

    def run_cov(self, roster_text: str, repos: dict[str, dict[str, str]], dirty: str | None = None):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d, "repos")
            root.mkdir()
            for name, files in repos.items():
                named_repo(root, name, files)
            if dirty:
                (root / dirty / "Cargo.toml").write_text("x", encoding="utf-8")
            p = Path(d, "r.toml")
            p.write_text(roster_text, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = rt.main(["--roster", str(p), "feature-coverage", "--repos-root", str(root)],
                               metadata=rt.sg.cargo_metadata)
        return code, out.getvalue() + err.getvalue()

    def test_the_command_reads_each_checkout_with_cargo_metadata(self):
        crate = {"Cargo.toml": ZIP_FEATURES, "src/lib.rs": ""}
        code, out = self.run_cov(COVERED, {"ALICE-Zip": crate})
        self.assertEqual(code, 0, out)
        self.assertIn("1 crate(s) with consumer sets compared", out)
        # the mutant of the requirement: a feature added outside every set and exclude
        code, out = self.run_cov(COVERED, {"ALICE-Zip": {**crate, "Cargo.toml": ZIP_FEATURES + "foo = []\n"}})
        self.assertEqual(code, 1, out)
        self.assertIn("refused: alice-zip: the crate's feature(s) ['foo']", out)
        # an optional dependency no feature names as dep: is a feature cargo metadata reports
        opt = ZIP_FEATURES.replace('serde = "1"', 'serde = { version = "1", optional = true }', 1)
        code, out = self.run_cov(COVERED, {"ALICE-Zip": {**crate, "Cargo.toml": opt}})
        self.assertEqual(code, 1, out)
        self.assertIn("['serde']", out)

    def test_the_manifest_path_is_honoured(self):
        text = COVERED.replace('repo = "ALICE-Zip"\n', 'repo = "ALICE-Zip"\nmanifest-path = "lib/Cargo.toml"\n', 1)
        # the root manifest is another package: reading it instead finds no alice-zip
        outer = '[package]\nname = "outer"\nversion = "0.1.0"\nedition = "2021"\n'
        code, out = self.run_cov(text, {"ALICE-Zip": {"Cargo.toml": outer, "src/lib.rs": "",
                                                      "lib/Cargo.toml": ZIP_FEATURES, "lib/src/lib.rs": ""}})
        self.assertEqual(code, 0, out)
        self.assertIn("covered: alice-zip: ALICE-Zip/lib/Cargo.toml", out)

    def test_a_missing_or_changed_checkout_is_refused(self):
        crate = {"Cargo.toml": ZIP_FEATURES, "src/lib.rs": ""}
        code, out = self.run_cov(COVERED, {"Other": crate})
        self.assertEqual(code, 1, out)
        self.assertIn("is not a checkout", out)
        code, out = self.run_cov(COVERED, {"ALICE-Zip": crate}, dirty="ALICE-Zip")
        self.assertEqual(code, 1, out)
        self.assertIn("has changes to tracked files", out)

    def test_cargo_metadata_failing_exits_2_and_never_falls_back(self):
        # another package at the path, and a manifest cargo cannot read
        for files, needle in (({"Cargo.toml": ZIP_FEATURES.replace('"alice-zip"', '"other"', 1), "src/lib.rs": ""},
                               "0 package(s) named alice-zip"),
                              ({"Cargo.toml": ZIP_FEATURES + "[[[", "src/lib.rs": ""}, "cargo metadata failed")):
            code, out = self.run_cov(COVERED, {"ALICE-Zip": files})
            self.assertEqual(code, 2, out)
            self.assertIn(needle, out)
            self.assertNotIn("covered:", out)

    def test_no_crate_with_sets_compared_exits_2(self):
        code, out = self.run_cov(ROSTER, {})
        self.assertEqual(code, 2, out)
        self.assertIn("no crate with consumer sets was compared", out)


SEMVER_WF = ("on:\n  push:\n    branches: [main, 'ci/**']\njobs:\n  semver:\n    steps:\n      - name: run scripts/semver_gate.py (shared)\n"
             "        run: python3 scripts/semver_gate.py --config .github/semver-gate.toml --target-dir t\n")
GATE_CONFIG = ('consumer-features = [["std", "fft"]]\nconsumer-features-why = "w"\n'
               'exclude-features = { hw = "a board-only feature" }\nequal-version = "next-smallest-step"\n')


class DriftHarness(unittest.TestCase):
    """runs semver-config-drift over fixture repositories (no tests of its own)"""

    def run_drift(self, roster_text: str, repos: dict[str, dict[str, str]]):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d, "repos")
            root.mkdir()
            for name, files in repos.items():
                named_repo(root, name, files)
            p = Path(d, "r.toml")
            p.write_text(roster_text, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = rt.main(["--roster", str(p), "semver-config-drift", "--repos-root", str(root), "--at", "HEAD"])
        return code, out.getvalue() + err.getvalue()

    def zip_repo(self, config: str | None = GATE_CONFIG, workflow: str = SEMVER_WF, extra=None) -> dict:
        files = {"Cargo.toml": ZIP_FEATURES, ".github/workflows/semver.yml": workflow, **(extra or {})}
        if config is not None:
            files[".github/semver-gate.toml"] = config
        return files


class SemverConfigDrift(DriftHarness):
    def test_a_copy_of_the_roster_entry_passes_and_the_step_name_is_not_a_run(self):
        code, out = self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo()})
        self.assertEqual(code, 0, out)
        self.assertIn("1 semver_gate run(s) compared", out)

    def test_order_does_not_matter(self):
        cfg = GATE_CONFIG.replace('[["std", "fft"]]', '[["fft", "std"]]')
        self.assertEqual(self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(cfg)})[0], 0)

    def test_each_copied_key_that_differs_is_refused(self):
        for old, new in (('[["std", "fft"]]', '[["std"]]'), ('consumer-features-why = "w"', 'consumer-features-why = "x"'),
                         ('{ hw = "a board-only feature" }', '{ hw = "another reason" }')):
            code, out = self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(GATE_CONFIG.replace(old, new))})
            self.assertEqual(code, 1, (old, out))
            self.assertIn("is not a copy of [crate.alice-zip]", out)
        # check sets present only in the config
        code, out = self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(GATE_CONFIG + 'consumer-check-features = [["hw"]]\n')})
        self.assertEqual(code, 1, out)
        self.assertIn("consumer-check-features", out)

    def test_a_run_without_a_tracked_config_is_refused(self):
        wf = SEMVER_WF.replace(" --config .github/semver-gate.toml", "")
        plain = ROSTER.replace('order = 1\n', 'order = 1\nrepo = "ALICE-Zip"\n', 1)
        for roster_text in (COVERED, plain):
            code, out = self.run_drift(roster_text, {"ALICE-Zip": self.zip_repo(None, wf)})
            self.assertEqual(code, 1, out)
            self.assertIn("reads no --config, which is not a tracked .github/semver-gate*.toml", out)
        # a config path outside .github/semver-gate*.toml
        wf = SEMVER_WF.replace(".github/semver-gate.toml", "cfg/gate.toml")
        code, out = self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(None, wf, extra={"cfg/gate.toml": GATE_CONFIG})})
        self.assertEqual(code, 1, out)
        self.assertIn("reads cfg/gate.toml, which is not a tracked", out)

    def test_each_tracked_config_is_read_by_exactly_one_run(self):
        # a tracked config no run reads
        code, out = self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(extra={".github/semver-gate-extra.toml": GATE_CONFIG})})
        self.assertEqual(code, 1, out)
        self.assertIn("semver-gate-extra.toml: read by 0 direct workflow run(s)", out)
        # one config read by two runs
        code, out = self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(workflow=SEMVER_WF + SEMVER_WF.split("steps:\n", 1)[1])})
        self.assertEqual(code, 1, out)
        self.assertIn("read by 2 direct workflow run(s)", out)

    def test_a_wrapper_or_a_gate_with_no_direct_run_is_refused(self):
        wrapper_wf = "jobs:\n  semver:\n    steps:\n      - run: bash scripts/sv.sh\n"
        files = self.zip_repo(workflow=wrapper_wf, extra={
            "scripts/semver_gate.py": "# the gate\n",
            "scripts/sv.sh": "#!/bin/sh\npython3 scripts/semver_gate.py --config .github/other.toml\n"})
        code, out = self.run_drift(COVERED, {"ALICE-Zip": files})
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/sv.sh: runs semver_gate.py outside the workflows", out)
        self.assertIn("scripts/semver_gate.py is tracked but no workflow runs it directly", out)
        self.assertIn("semver-gate.toml: read by 0 direct workflow run(s)", out)
        # a wrapper beside a direct run is refused too; scripts/preflight.sh may run it
        both = self.zip_repo(extra={"scripts/semver_gate.py": "# the gate\n",
                                    "scripts/sv.sh": "python3 scripts/semver_gate.py --config x.toml\n"})
        self.assertEqual(self.run_drift(COVERED, {"ALICE-Zip": both})[0], 1)
        pre = self.zip_repo(extra={"scripts/semver_gate.py": "# the gate\n",
                                   "scripts/preflight.sh": "python3 scripts/semver_gate.py --config .github/semver-gate.toml\n"})
        self.assertEqual(self.run_drift(COVERED, {"ALICE-Zip": pre})[0], 0)

    def test_the_integration_repository_uses_the_gate_as_a_library(self):
        # its roster crates are all exempt: the gate file without a direct run is fine there
        text = COVERED.replace('[crate.alice-law-integration]\nversion = "0.0.0"\nstate = "exempt"\n',
                               '[crate.alice-law-integration]\nversion = "0.0.0"\nstate = "exempt"\nrepo = "ALICE-Law"\n', 1)
        self.assertIn('repo = "ALICE-Law"', text)
        law = {"Cargo.toml": '[package]\nname = "alice-law-integration"\nversion = "0.0.0"\n',
               "scripts/semver_gate.py": "# the gate\n", ".github/workflows/ci.yml": "jobs: {}\n"}
        code, out = self.run_drift(text, {"ALICE-Zip": self.zip_repo(), "ALICE-Law": law})
        self.assertEqual(code, 0, out)

    def test_the_manifest_of_the_run_names_the_crate(self):
        wf = SEMVER_WF.replace("semver_gate.py --config", "semver_gate.py --manifest lib/Cargo.toml --config")
        files = self.zip_repo(workflow=wf, extra={"Cargo.toml": "[workspace]\nmembers = [\"lib\"]\n",
                                                  "lib/Cargo.toml": ZIP_FEATURES})
        self.assertEqual(self.run_drift(COVERED, {"ALICE-Zip": files})[0], 0)
        # a roster crate of another repository, run from this one
        other = self.zip_repo(extra={"Cargo.toml": ZIP_FEATURES.replace('"alice-zip"', '"alice-det-math"', 1)})
        code, out = self.run_drift(COVERED, {"ALICE-Zip": other})
        self.assertEqual(code, 1, out)
        self.assertIn("which is not a roster crate of ALICE-Zip", out)

    def test_a_continued_line_is_one_run(self):
        wf = SEMVER_WF.replace("semver_gate.py --config", "semver_gate.py \\\n          --config")
        self.assertEqual(self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(workflow=wf)})[0], 0)
        bad = SEMVER_WF.replace("semver_gate.py --config", "semver_gate.py \\\n          --config").replace(
            ".github/semver-gate.toml", ".github/other.toml")
        code, out = self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(workflow=bad, extra={
            ".github/other.toml": 'consumer-features = [["std"]]\nconsumer-features-why = "w"\n'})})
        self.assertEqual(code, 1, out)

    def test_any_interpreter_or_block_form_is_a_run(self):
        other = {".github/semver-gate-probe.toml": 'consumer-features = [["std"]]\nconsumer-features-why = "w"\n'}
        forms = ("        run: python3 -u scripts/semver_gate.py --config .github/semver-gate-probe.toml\n",
                 "        run: python3.11 scripts/semver_gate.py --config .github/semver-gate-probe.toml\n",
                 '        run: |\n          PY=python3\n          "$PY" scripts/semver_gate.py --config .github/semver-gate-probe.toml\n',
                 "        run: >-\n          python3 scripts/semver_gate.py\n          --config .github/semver-gate-probe.toml\n",
                 "        run: cd x && ./scripts/semver_gate.py --config .github/semver-gate-probe.toml && echo done\n")
        for form in forms:
            wf = SEMVER_WF + "      - name: probe\n" + form
            code, out = self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(workflow=wf, extra=other)})
            self.assertEqual(code, 1, (form, out))
            self.assertIn("--config .github/semver-gate-probe.toml) at HEAD is not a copy", out, form)
            self.assertIn("2 semver_gate run(s) compared", out, form)

    def test_a_shell_separator_ends_the_run(self):
        # the second command's --config is not the gate's
        wf = SEMVER_WF.replace("--target-dir t\n", "--target-dir t && tool --config .github/other.toml\n")
        code, out = self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(workflow=wf)})
        # refused for the list (the gate is a simple command), not as another config
        self.assertEqual(code, 1, out)
        self.assertIn("`&&` after the run", out)
        self.assertNotIn("is not a copy", out)

    def test_an_unknown_form_fails_closed(self):
        for form, needle in (("        run: python3 scripts/semver_gate.py --config $CONFIG\n", "is not a literal path"),
                             ('        run: python3 scripts/semver_gate.py --config "unclosed\n', "cannot split"),
                             ("        run: echo semver_gate.py,next\n", "outside a run of it")):
            wf = SEMVER_WF + "      - name: probe\n" + form
            code, out = self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(workflow=wf)})
            self.assertEqual(code, 1, (form, out))
            self.assertIn(needle, out, form)
        # path filters, step names and comments are not runs
        wf = SEMVER_WF.replace("    branches: [main, 'ci/**']\n", "    branches: [main, 'ci/**']\n    paths:\n"
                               "      - 'scripts/semver_gate.py'\n      - scripts/test_semver_gate.py\n"
                               "      - '.github/semver-gate.toml'\n") + "      # runs scripts/semver_gate.py above\n"
        self.assertEqual(self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(workflow=wf)})[0], 0)

    def test_a_missing_checkout_is_refused_and_nothing_compared_exits_2(self):
        code, out = self.run_drift(COVERED, {"Other": self.zip_repo()})
        self.assertEqual(code, 1, out)
        self.assertIn("is not a checkout", out)
        code, out = self.run_drift(COVERED, {"ALICE-Zip": {"Cargo.toml": ZIP_FEATURES,
                                                           ".github/workflows/ci.yml": "jobs: {}\n"}})
        self.assertEqual(code, 2, out)
        self.assertIn("nothing was compared", out)


CHANGES_WF = """on:
  push:
    branches: [main, 'ci/**']
  pull_request:
    branches: [main]
    paths:
      - 'src/**'
      - 'scripts/semver_gate.py'
      - '.github/semver-gate.toml'
jobs:
  changes:
    outputs:
      run: ${{ steps.decide.outputs.run }}
    steps:
      - name: decide
        id: decide
        run: >-
          python3 scripts/changed_paths.py decide --event "$EVENT"
          --patterns-file .github/paths/semver-checks.txt
  semver-checks:
    needs: changes
    if: needs.changes.outputs.run == 'true'
    steps:
      - name: run
        run: python3 scripts/semver_gate.py --config .github/semver-gate.toml --target-dir t
  gate:
    if: always()
    needs: [changes, semver-checks]
    steps:
      - name: gate
        run: python3 scripts/changed_paths.py gate --needs-json "$NEEDS"
"""
PATTERNS = "# the paths\nsrc/**\nscripts/semver_gate.py\n.github/semver-gate.toml\n"


class SemverGateBlocks(DriftHarness):
    """the run of the gate can fail the workflow, and a push to main that changes the gate
    reaches it"""

    def refused(self, workflow: str, needle: str, extra=None):
        code, out = self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(workflow=workflow, extra=extra)})
        self.assertEqual(code, 1, (needle, out))
        self.assertIn(needle, out)

    def test_a_plain_run_on_push_to_main_blocks(self):
        self.assertEqual(self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo()})[0], 0)
        # the event conditions are allowed on the step and the job
        wf = SEMVER_WF.replace("      - name: run scripts", "      - if: github.event_name == 'push'\n        name: run scripts")
        wf = wf.replace("  semver:\n", "  semver:\n    if: ${{ github.event_name == 'pull_request' }}\n")
        self.assertEqual(self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(workflow=wf)})[0], 0)

    def test_a_soft_command_is_refused(self):
        for tail, needle in (("--target-dir t || true\n", "`||` after the run"),
                             ("--target-dir t||:\n", "`||` after the run"),
                             ("--target-dir t | tee log\n", "`|` after the run"),
                             ("--target-dir t &\n", "`&` after the run")):
            self.refused(SEMVER_WF.replace("--target-dir t\n", tail), needle)
        block = SEMVER_WF.replace("        run: python3 scripts/semver_gate.py",
                                  "        run: |\n          set +e\n          python3 scripts/semver_gate.py")
        self.refused(block, "the step's run has 2 commands")

    def test_the_gate_is_a_simple_command_outside_any_condition(self):
        run = "        run: python3 scripts/semver_gate.py --config .github/semver-gate.toml --target-dir t\n"
        for repl, needle in (
                ("        run: |\n          python3 scripts/semver_gate.py --config .github/semver-gate.toml && echo ok\n          echo end\n",
                 "the step's run has 2 commands"),
                ("        run: python3 scripts/semver_gate.py --config .github/semver-gate.toml && echo ok\n",
                 "`&&` after the run"),
                ("        run: python3 scripts/semver_gate.py --config .github/semver-gate.toml; exit 0\n", "`;` after the run"),
                ("        run: |\n          if ! python3 scripts/semver_gate.py --config .github/semver-gate.toml; then echo skipped; fi\n",
                 "`if` before the run"),
                ("        run: |\n          while python3 scripts/semver_gate.py --config .github/semver-gate.toml; do break; done\n",
                 "`while` before the run"),
                ("        run: ! python3 scripts/semver_gate.py --config .github/semver-gate.toml\n", "`!` before the run"),
                ("        run: echo $(python3 scripts/semver_gate.py --config .github/semver-gate.toml)\n", "before the run"),
                ("        run: echo `python3 scripts/semver_gate.py --config .github/semver-gate.toml --target-dir t`\n",
                 "before the run"),
                ("        run: |\n          if\n            python3 scripts/semver_gate.py --config .github/semver-gate.toml\n"
                 "          then\n            echo ok\n          fi\n", "the step's run has 5 commands"),
                # a group, a function and a subshell whose operator sits on a later line
                ("        run: |\n          {\n            python3 scripts/semver_gate.py --config .github/semver-gate.toml\n"
                 "          } || true\n", "the step's run has 3 commands"),
                ("        run: |\n          g() {\n            python3 scripts/semver_gate.py --config .github/semver-gate.toml\n"
                 "          }\n          g || true\n", "the step's run has 4 commands"),
                ("        run: |\n          (\n            python3 scripts/semver_gate.py --config .github/semver-gate.toml\n"
                 "          ) || true\n", "the step's run has 3 commands"),
                ("        run: |\n          if true; then\n            python3 scripts/semver_gate.py --config .github/semver-gate.toml\n"
                 "          fi\n", "the step's run has 3 commands")):
            self.refused(SEMVER_WF.replace(run, repl), needle)
        # the gate in an env value the run expands: the run itself is soft
        env = ("        env:\n          CMD: python3 scripts/semver_gate.py --config .github/semver-gate.toml\n"
               "        run: $CMD || true\n")
        self.refused(SEMVER_WF.replace(run, env), "does not run semver_gate.py itself")
        # redirections, assignments, an interpreter with flags, a `|` block of one command with
        # a comment, and a folded block are fine
        for repl in ("        run: python3 -u scripts/semver_gate.py --config .github/semver-gate.toml > log 2>&1\n",
                     "        run: RUST_LOG=info python3 scripts/semver_gate.py --config .github/semver-gate.toml\n",
                     "        run: |\n          # the gate\n          python3 scripts/semver_gate.py \\\n"
                     "            --config .github/semver-gate.toml\n",
                     "        run: >-\n          python3 scripts/semver_gate.py\n          --config .github/semver-gate.toml\n"):
            code, out = self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(workflow=SEMVER_WF.replace(run, repl))})
            self.assertEqual(code, 0, (repl, out))

    def test_continue_on_error_shell_and_if_are_refused(self):
        step = "      - name: run scripts/semver_gate.py (shared)\n"
        self.refused(SEMVER_WF.replace(step, step + "        continue-on-error: true\n"), "continue-on-error on the step")
        self.refused(SEMVER_WF.replace("  semver:\n", "  semver:\n    continue-on-error: true\n"),
                     "continue-on-error on the job semver")
        self.refused(SEMVER_WF.replace(step, step + "        shell: python\n"), "shell 'python' on the step")
        self.refused(SEMVER_WF.replace(step, step + "        if: false\n"), "the step has `if: false`")
        self.refused(SEMVER_WF.replace("  semver:\n", "  semver:\n    if: github.ref != 'refs/heads/main'\n"),
                     "job semver has `if: github.ref != 'refs/heads/main'`")
        # continue-on-error: false is not soft
        self.assertEqual(self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(
            workflow=SEMVER_WF.replace(step, step + "        continue-on-error: false\n"))})[0], 0)

    def test_a_push_to_main_with_a_change_to_the_gate_reaches_the_run(self):
        self.refused(SEMVER_WF.replace("  push:\n    branches: [main, 'ci/**']\n", "  workflow_dispatch:\n"),
                     "does not run on push")
        self.refused(SEMVER_WF.replace("[main, 'ci/**']", "['ci/**']"), "does not include main")
        paths = "  push:\n    branches: [main]\n    paths:\n      - 'src/**'\n      - '.github/semver-gate.toml'\n"
        self.refused(SEMVER_WF.replace("  push:\n    branches: [main, 'ci/**']\n", paths),
                     "on.push.paths does not list ['scripts/semver_gate.py']")
        ok = paths.replace("      - 'src/**'\n", "      - 'src/**'\n      - 'scripts/semver_gate.py'\n")
        self.assertEqual(self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(
            workflow=SEMVER_WF.replace("  push:\n    branches: [main, 'ci/**']\n", ok))})[0], 0)
        self.refused(SEMVER_WF.replace("    branches: [main, 'ci/**']\n", "    branches: [main]\n    paths-ignore:\n      - 'scripts/**'\n"),
                     "paths-ignore")

    def test_a_job_the_changes_job_decides_blocks_through_the_gate_job(self):
        files = {".github/paths/semver-checks.txt": PATTERNS}
        self.assertEqual(self.run_drift(COVERED, {"ALICE-Zip": self.zip_repo(workflow=CHANGES_WF, extra=files)})[0], 0)
        gate = CHANGES_WF[CHANGES_WF.index("  gate:\n"):]
        for wf, needle in (
                (CHANGES_WF.replace(gate, ""), "no `if: always()` job that needs both changes and semver-checks"),
                (CHANGES_WF.replace("needs: [changes, semver-checks]", "needs: [changes]"), "no `if: always()` job"),
                (CHANGES_WF.replace("  gate:\n    if: always()\n", "  gate:\n    if: always()\n    continue-on-error: true\n"),
                 "continue-on-error on the gate job gate"),
                (CHANGES_WF.replace('--needs-json "$NEEDS"', '--needs-json "$NEEDS" || true'), "`||` after changed_paths.py gate"),
                (CHANGES_WF.replace("changed_paths.py gate", "changed_paths.py report"), "does not run changed_paths.py gate"),
                (CHANGES_WF.replace("      - name: gate\n", "      - name: gate\n        if: false\n"),
                 "a step of the gate job gate has `if: false`"),
                (CHANGES_WF.replace("--patterns-file .github/paths/semver-checks.txt", "--pattern 'src/**'"),
                 "lists patterns inline"),
                (CHANGES_WF.replace("  changes:\n", "  changed:\n").replace("needs: changes\n", "needs: changed\n"),
                 "there is no changes job")):
            self.refused(wf, needle, files)
        self.refused(CHANGES_WF, "does not list ['scripts/semver_gate.py']",
                     {".github/paths/semver-checks.txt": PATTERNS.replace("scripts/semver_gate.py\n", "")})
        self.refused(CHANGES_WF.replace("      - 'src/**'\n", ""), "on.pull_request.paths is not the patterns", files)


if __name__ == "__main__":
    unittest.main()
