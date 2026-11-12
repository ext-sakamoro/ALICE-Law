#!/usr/bin/env python3
"""Tests for scripts/g1_coverage_live.py.

* the Law-bearing derivation on facts given in memory (a definer; a consumer by [dependencies] plus a use in
  production code; not by a dev dependency alone, not by a use without the dependency; the det-math repository and
  a repository without production code out of scope);
* the comparison (an unlisted lacking repository, a listed one that has the evidence or is no longer Law-bearing,
  definers that drifted) and the baseline (round trip, a hashed entry and an unknown key refused);
* facts read from a git repository written here: a definition in a comment or under tests/ does not count, a
  clippy list missing one banned method or a CI clippy run without the deny is not G1's evidence;
* the modes: --only against a baseline, --write-own refusing the shared baseline, 0 production files exit 2.

Usage: python3 scripts/test_g1_coverage_live.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import g1_coverage_live as g  # noqa: E402
import measure_conformance as mc  # noqa: E402

BANNED = g.banned()
# every temporary tree of these tests lives under one directory, removed when the module is done
SCRATCH = tempfile.mkdtemp(prefix="g1-coverage-test-")


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


def f(**kw) -> dict:
    base = {"files": 2, "packages": set(), "deps": set(), "names": set(), "defines": False, "g1": False,
            "detmath": False}
    base.update(kw)
    return base


class Derivation(unittest.TestCase):
    def test_a_definer(self):
        self.assertEqual(g.law_bearing({"A": f(defines=True)}, set()), {"A": "defines a Law"})

    def test_a_consumer_needs_the_dependency_and_a_use(self):
        pk = {"alice-zip"}
        self.assertIn("computes through alice-zip",
                      g.law_bearing({"B": f(deps={"alice-zip"}, names={"alice_zip"})}, pk)["B"])
        self.assertEqual(g.law_bearing({"B": f(deps={"alice-zip"})}, pk), {})
        self.assertEqual(g.law_bearing({"B": f(names={"alice_zip"})}, pk), {})
        self.assertEqual(g.law_bearing({"B": f(deps={"alice-other"}, names={"alice_other"})}, pk), {})

    def test_out_of_scope(self):
        self.assertEqual(g.law_bearing({"D": f(defines=True, detmath=True)}, set()), {})
        self.assertEqual(g.law_bearing({"E": f(defines=True, files=0)}, set()), {})


class Compare(unittest.TestCase):
    def test_clean(self):
        read = {"A": f(defines=True, g1=True), "B": f(defines=True)}
        self.assertEqual(g.compare(read, {"A": "x", "B": "x"}, ["B"], ["A", "B"]), [])

    def test_an_unlisted_lacking_repository_is_red(self):
        out = g.compare({"B": f(defines=True)}, {"B": "x"}, [], ["B"])
        self.assertEqual(len(out), 1)
        self.assertIn("not in lacking", out[0])

    def test_a_listed_repository_with_the_evidence_is_red(self):
        out = g.compare({"B": f(defines=True, g1=True)}, {"B": "x"}, ["B"], ["B"])
        self.assertIn("drop it from lacking", out[0])

    def test_a_listed_repository_no_longer_law_bearing_is_red(self):
        out = g.compare({"C": f()}, {}, ["C"], [])
        self.assertIn("not Law-bearing", out[0])

    def test_definers_that_drifted_are_red(self):
        out = g.compare({"A": f(defines=True, g1=True), "Z": f(defines=True, g1=True)}, {"A": "x", "Z": "x"}, [],
                        ["A", "Y"])
        self.assertEqual(len(out), 2)
        self.assertTrue(any("Z: defines a Law but is not in definers" in x for x in out))
        self.assertTrue(any("Y: in definers but defines no Law" in x for x in out))


class Baseline(unittest.TestCase):
    def write(self, text: str) -> Path:
        d = Path(tempfile.mkdtemp(dir=SCRATCH))
        p = d / "b.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_round_trip(self):
        p = self.write(g.render(["A"], {"alice-a"}, ["B"]))
        self.assertEqual(g.load_baseline(p), (["A"], ["B"]))
        self.assertEqual(g.load_definer_packages(p), {"alice-a"})

    def test_refused(self):
        for text in ('lacking = ["sha256:00ff"]\n', 'definers = ["sha256:00ff"]\n', 'other = []\n',
                     'lacking = "ALICE-A"\n'):
            with self.assertRaises(ValueError, msg=text):
                g.load_baseline(self.write(text))

    def test_the_committed_baseline(self):
        definers, lacking = g.load_baseline(g.BASELINE)
        self.assertTrue(definers)
        self.assertTrue(g.load_definer_packages(g.BASELINE))
        self.assertFalse([x for x in definers + lacking if not x.startswith("ALICE-")])


CLIPPY_FULL = "disallowed-methods = [\n" + "".join(
    f'  {{ path = "{w}::{m}", reason = "G1" }},\n' for m in sorted(BANNED) for w in ("f32", "f64")) + "]\n"
CI_DENY = """name: ci
on: [push]
jobs:
  lint:
    runs-on: ubuntu-24.04
    steps:
      - run: cargo clippy --all-targets -- -D warnings
"""


def git_repo(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp(dir=SCRATCH))
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"}
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "t"]):
        subprocess.run(["git", "-C", str(d), *cmd], check=True, capture_output=True, env=env)
    return d


MANIFEST = '[package]\nname = "alice-a"\nversion = "0.1.0"\n\n[dependencies]\nalice-zip = "1"\n\n[dev-dependencies]\nalice-sdf = "1"\n'


class Facts(unittest.TestCase):
    def facts(self, files: dict[str, str]) -> dict:
        return g.facts(mc.Repo(git_repo(files), "HEAD"), BANNED)

    def test_definition_in_code_counts(self):
        r = self.facts({"Cargo.toml": MANIFEST, "src/lib.rs": "pub const A_SEMANTICS_ID: [u8; 32] = [0; 32];\n"})
        self.assertTrue(r["defines"])
        self.assertEqual(r["packages"], {"alice-a"})
        self.assertEqual(r["deps"], {"alice-zip"})

    def test_definition_in_a_comment_or_a_test_does_not_count(self):
        r = self.facts({"Cargo.toml": MANIFEST, "src/lib.rs": "// pub const A_SEMANTICS_ID: u8 = 0;\npub fn f() {}\n",
                        "tests/t.rs": "pub fn law_id() {}\n", "examples/src/e.rs": "pub fn law_id() {}\n"})
        self.assertFalse(r["defines"])
        self.assertEqual(r["files"], 1)

    def test_g1_evidence_needs_the_full_list_and_the_deny(self):
        base = {"Cargo.toml": MANIFEST, "src/lib.rs": "pub fn f() {}\n"}
        self.assertTrue(self.facts({**base, "clippy.toml": CLIPPY_FULL, ".github/workflows/ci.yml": CI_DENY})["g1"])
        short = CLIPPY_FULL.replace('  { path = "f64::sin", reason = "G1" },\n', "")
        self.assertFalse(self.facts({**base, "clippy.toml": short, ".github/workflows/ci.yml": CI_DENY})["g1"])
        no_deny = CI_DENY.replace(" -- -D warnings", "")
        self.assertFalse(self.facts({**base, "clippy.toml": CLIPPY_FULL, ".github/workflows/ci.yml": no_deny})["g1"])


class Modes(unittest.TestCase):
    def test_only_against_an_own_file(self):
        d = git_repo({"Cargo.toml": MANIFEST, "src/lib.rs": "use alice_zip::x;\n"})
        own = d / "own.toml"
        pk = Path(tempfile.mkdtemp(dir=SCRATCH)) / "b.toml"
        pk.write_text(g.render(["ALICE-Zip"], {"alice-zip"}, []), encoding="utf-8")
        with mock.patch.object(g, "BASELINE", pk):
            self.assertEqual(g.main(["--write-own", f"ALICE-P={d}", "--baseline", str(own)]), 0)
            self.assertEqual(g.load_baseline(own), ([], ["ALICE-P"]))
            self.assertEqual(g.main(["--only", f"ALICE-P={d}", "--baseline", str(own)]), 0)
            own.write_text(g.render([], set(), []), encoding="utf-8")
            self.assertEqual(g.main(["--only", f"ALICE-P={d}", "--baseline", str(own)]), 1)

    def test_write_own_refuses_the_shared_baseline(self):
        d = git_repo({"Cargo.toml": MANIFEST, "src/lib.rs": "pub fn f() {}\n"})
        fake = Path(tempfile.mkdtemp(dir=SCRATCH)) / "b.toml"
        # a valid shared baseline, so only the target guard can refuse
        shared = g.render(["ALICE-Zip"], {"alice-zip"}, [])
        fake.write_text(shared, encoding="utf-8")
        with mock.patch.object(g, "BASELINE", fake):
            self.assertEqual(g.main(["--write-own", f"ALICE-P={d}", "--baseline", str(fake)]), 2)
        self.assertEqual(fake.read_text(encoding="utf-8"), shared)

    def test_no_production_code_is_exit_2(self):
        d = git_repo({"Cargo.toml": MANIFEST, "tests/t.rs": "fn x() {}\n"})
        self.assertEqual(g.main(["--only", f"ALICE-P={d}"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
