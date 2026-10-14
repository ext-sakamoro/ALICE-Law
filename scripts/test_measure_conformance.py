#!/usr/bin/env python3
"""Tests of scripts/measure_conformance.py (python3 scripts/test_measure_conformance.py)"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mc", HERE / "measure_conformance.py")
mc = importlib.util.module_from_spec(spec)
sys.modules["mc"] = mc
spec.loader.exec_module(mc)

BANNED = {"sin", "cos", "sin_cos"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def make(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q")
    for path, text in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    return repo


CLIPPY = 'disallowed-methods = [\n  { path = "f32::sin" }, { path = "f64::sin" }, { path = "f32::cos" }, { path = "f64::cos" },\n' \
         '  { path = "std::time::SystemTime::now" },\n]\n'
WORKFLOW = "jobs:\n  t:\n    runs-on: macos-latest\n    steps:\n      - run: cargo clippy -- -D warnings\n" \
           "      - run: cargo test --release\n"


class Measure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def cells(self, files: dict[str, str]) -> dict[str, str]:
        repo = make(self.root, f"r{len(list(self.root.iterdir()))}", files)
        return mc.measure(mc.Repo(repo, "HEAD"), BANNED)

    def test_g1_names_the_banned_methods_a_clippy_list_lacks(self):
        c = self.cells({"clippy.toml": CLIPPY, ".github/workflows/ci.yml": WORKFLOW, "src/lib.rs": ""})
        self.assertIn("clippy.toml: missing sin_cos", c["G1"])
        self.assertIn("CI clippy -D found", c["G1"])
        self.assertIn("no clippy configuration", self.cells({"src/lib.rs": ""})["G1"])

    def test_g1_reports_a_pending_method_banned_by_default_apart_from_the_banned_ones(self):
        repo = make(self.root, "rp", {"clippy.toml": CLIPPY.replace('{ path = "f32::cos" }, ', '') + "", "src/lib.rs": ""})
        c = mc.measure(mc.Repo(repo, "HEAD"), BANNED, {"powi"})
        self.assertIn("not banned powi (pending, banned by default: allowed only if", c["G1"])
        listed = make(self.root, "rq", {"clippy.toml": CLIPPY.replace("]\n", '  { path = "f32::powi" }, { path = "f64::powi" },\n]\n', 1),
                                        "src/lib.rs": ""})
        self.assertNotIn("not banned powi", mc.measure(mc.Repo(listed, "HEAD"), BANNED, {"powi"})["G1"])

    def test_g6_tells_an_own_identifier_from_a_re_export(self):
        own = self.cells({"src/lib.rs": "pub const FOO_SEMANTICS_ID: [u8; 32] = [0; 32];\n"})
        self.assertIn("own identifier src/lib.rs:1 (FOO_SEMANTICS_ID)", own["G6"])
        re_export = self.cells({"src/lib.rs": "pub use alice_det_math::SEMANTICS_ID;\n"})
        self.assertIn("re-export only", re_export["G6"])
        self.assertIn("not found", self.cells({"src/lib.rs": "fn f() {}\n"})["G6"])

    def test_g10_counts_library_sources_but_not_binaries(self):
        c = self.cells({"src/lib.rs": "fn t() { let _ = std::time::Instant::now(); }\n",
                        "src/bin/tool.rs": "fn main() { let _ = std::time::Instant::now(); }\n"})
        self.assertIn("1 uses in library sources", c["G10"])
        self.assertIn("lint bans std::time::SystemTime::now", self.cells({"clippy.toml": CLIPPY, "src/lib.rs": ""})["G10"])

    def test_g10_sees_each_listed_form_in_a_workspace_member(self):
        for form in mc.CLOCK_RNG:
            c = self.cells({"member/src/key.rs": f"fn f() {{ let _ = {form}; }}\n"})
            self.assertIn("1 uses in library sources", c["G10"], form)
        none = self.cells({"src/lib.rs": "fn f() {}\n"})["G10"]
        self.assertIn(f"none of the forms in library sources (lexical scan of {len(mc.CLOCK_RNG)} forms)", none)
        for form in ("Utc::now", "OsRng", "rand::rng()", "getrandom", "Uuid::now_v7"):
            self.assertIn(form, mc.CLOCK_RNG)

    def test_g14_tells_build_configuration_from_a_workflow_leg(self):
        c = self.cells({".cargo/config.toml": '[build]\nrustflags = ["-C", "target-cpu=native"]\n', "src/lib.rs": ""})
        self.assertIn("flag in build configuration .cargo/config.toml", c["G14"])
        w = self.cells({".github/workflows/ci.yml": "env:\n  RUSTFLAGS: -C target-cpu=native\n", "src/lib.rs": ""})
        self.assertIn("no flag in build configuration", w["G14"])
        self.assertIn("(test leg or shipped build: audit)", w["G14"])

    def test_g5_finds_the_release_leg_on_an_aarch64_runner(self):
        self.assertIn("release test found, aarch64 runner macos-latest",
                      self.cells({".github/workflows/ci.yml": WORKFLOW, "src/lib.rs": ""})["G5"])
        self.assertIn("aarch64 runner not found",
                      self.cells({".github/workflows/ci.yml": "jobs:\n  t:\n    runs-on: ubuntu-latest\n"
                                  "    steps:\n      - run: cargo test --release\n", "src/lib.rs": ""})["G5"])

    def test_a_gate_list_without_banned_methods_measures_nothing(self):
        law = self.root / "law"
        (law / "conformance").mkdir(parents=True)
        (law / "conformance" / "gates.toml").write_text('[[gate]]\nid = "G1"\nname = "x"\n', encoding="utf-8")
        make(self.root, "ALICE-X", {"src/lib.rs": ""})
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(mc.main(["--repos", str(self.root), "--crates", "ALICE-X", "--law", str(law)]), 2)
        self.assertIn("no banned G1 method", out.getvalue())

    def test_an_unreadable_repository_or_no_banned_list_is_an_error(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(mc.main(["--repos", str(self.root), "--crates", "nope", "--law",
                                      str(HERE.parent)]), 2)


if __name__ == "__main__":
    unittest.main()
