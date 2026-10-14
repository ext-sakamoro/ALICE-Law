#!/usr/bin/env python3
"""Tests of scripts/gen-law-readme-block.py (python3 scripts/test_gen_law_readme_block.py)"""
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
spec = importlib.util.spec_from_file_location("glrb", HERE / "gen-law-readme-block.py")
glrb = importlib.util.module_from_spec(spec)
sys.modules["glrb"] = glrb
spec.loader.exec_module(glrb)

SID = "ab" * 32
DIGEST = "cd" * 32
ROSTER = """
[crate.alice-det-math]
version = "0.5.0"
state = "published"
order = 0
repo = "ALICE-DetMath"

[crate.alice-foo]
version = "1.2.0"
state = "published"
order = 1
repo = "ALICE-Foo"

[crate.alice-bar]
version = "0.1.0"
state = "planned"
order = 2
repo = "ALICE-Bar"
law-dependency = false
"""
GATES = """
schema_version = 0
[[gate]]
id = "G1"
name = "no-libm-direct"
[[gate]]
id = "G2"
name = "ieee-max-min"
[[gate]]
id = "G3"
name = "no-outer-fneg-of-fma"
"""
E2E_TOML = '[package]\nname = "e2e"\n[dependencies]\nalice-foo = { version = "1", features = ["x"] }\nalice-det-math = "0.5"\n'
E2E_TEST = f'const SEMANTICS_HEX: &str = "{SID}";\n'
WORKFLOW = ("jobs:\n  digest:\n    strategy:\n      matrix:\n        include:\n          - runner: macos-15\n"
            "            arch: arm64\n          - runner: macos-15-intel\n            arch: x86_64\n"
            "  digest-compare:\n    needs: digest\n")
CARGO = '[package]\nname = "alice-foo"\nversion = "1.2.0"\n[features]\ndefault = ["std"]\nstd = []\nsimd = []\n[dependencies]\nalice-det-math = { version = "0.5" }\n'
README_EN = "# alice-foo\n\nIntro.\n\n## Usage\n\ntext\n\n## License\n\nMIT\n"
README_JA = "# alice-foo\n\n概要\n\n## 使い方\n\n本文\n\n## ライセンス\n\nMIT\n"
MANIFEST = """
[G1]
status = "pass"
evidence = ["clippy.toml"]
mutant = "x.sin() in src/lib.rs fails the lint"
note = "type-resolved ban"
[G2]
status = "fail-known"
note = "no per-width test yet"
[features.std]
guarantee = "inside"
evidence = ["tests/oracle.rs"]
note = "same arithmetic generation"
[features.simd]
guarantee = "outside"
note = "lane order differs"
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    return git(repo, "rev-parse", "HEAD").strip()


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = glrb.main(list(argv))
    return rc, out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.law, self.crate = Path(self.tmp.name, "law"), Path(self.tmp.name, "crate")
        for r in (self.law, self.crate):
            r.mkdir()
            git(r, "init", "-q")
        self.sha = commit(self.law, {"release-train.toml": ROSTER, "conformance/gates.toml": GATES,
                                     "e2e/Cargo.toml": E2E_TOML, "e2e/tests/law_to_storage_roundtrip.rs": E2E_TEST,
                                     "e2e/golden/run.digest": DIGEST + "\n", ".github/workflows/e2e.yml": WORKFLOW})
        commit(self.crate, {"Cargo.toml": CARGO, "README.md": README_EN, "README_JP.md": README_JA,
                            "clippy.toml": "", "tests/oracle.rs": ""})

    def render(self, lang="en", crate="alice-foo"):
        return run("render", "--law", str(self.law), "--law-ref", self.sha, "--crate", crate,
                   "--crate-repo", str(self.crate), "--lang", lang)

    def write_and_commit(self, crate="alice-foo"):
        rc, out = run("write", "--law", str(self.law), "--law-ref", self.sha, "--crate", crate,
                      "--crate-repo", str(self.crate))
        self.assertEqual(rc, 0, out)
        git(self.crate, "add", "-A")
        git(self.crate, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "block")

    def check(self, *extra, crate="alice-foo"):
        return run("check", "--law", str(self.law), "--crate", crate, "--crate-repo", str(self.crate),
                   "--law-main", "HEAD", *extra)


class Render(Base):
    def test_no_manifest_shows_every_gate_and_feature_as_not_declared_never_pass(self):
        rc, out = self.render()
        self.assertEqual(rc, 0, out)
        self.assertIn("no `alice-law-conformance.toml`: every gate is not declared", out)
        for g in ("G1 no-libm-direct", "G2 ieee-max-min", "G3 no-outer-fneg-of-fma"):
            self.assertIn(f"| {g} | not declared |  |", out)
        self.assertNotIn("| pass |", out)
        self.assertIn("| `simd` | not declared |  |", out)
        self.assertIn("| `std` | not declared |  |", out)
        rc, ja = self.render("ja")
        self.assertNotIn("| 適合 |", ja)
        self.assertIn("| G1 no-libm-direct | 未申告 |  |", ja)

    def test_the_facts_come_from_the_pinned_revision_and_the_manifest(self):
        commit(self.crate, {"alice-law-conformance.toml": MANIFEST})
        rc, out = self.render()
        self.assertEqual(rc, 0, out)
        self.assertIn(f"<!-- law-integration:begin law={self.sha} -->", out)
        self.assertIn("| Release train | alice-foo 1.2.0 (published), law dependency: yes |", out)
        self.assertIn(f"alice-det-math 0.5.0 in the train, arithmetic identifier `{SID}`", out)
        self.assertIn('this crate requires `alice-det-math = "0.5"`', out)
        self.assertIn(f'covered (`alice-foo = "1"`), digest `{DIGEST}`, the same on aarch64 and x86_64', out)
        self.assertIn("| G1 no-libm-direct | pass | type-resolved ban |", out)
        self.assertIn("| G2 ieee-max-min | known unmet | no per-width test yet |", out)
        self.assertIn("| G3 no-outer-fneg-of-fma | not declared |  |", out)  # absent from the manifest
        self.assertIn("| `std` | inside | same arithmetic generation |", out)
        self.assertIn("| `simd` | outside | lane order differs |", out)
        self.assertTrue(out.rstrip().endswith(glrb.END))

    def test_a_crate_inside_a_workspace_is_read_at_its_manifest_path(self):
        self.sha = commit(self.law, {"release-train.toml": ROSTER.replace(
            'repo = "ALICE-Foo"', 'repo = "ALICE-Foo"\nmanifest-path = "alice-foo/Cargo.toml"')})
        commit(self.crate, {"Cargo.toml": '[workspace]\nmembers = ["alice-foo"]\n[workspace.dependencies]\n'
                                          'alice-det-math = { version = "0.5.0" }\n',
                            "alice-foo/Cargo.toml": '[package]\nname = "alice-foo"\n[features]\ngpu = []\n'
                                                    '[dependencies]\nalice-det-math = { workspace = true }\n'})
        out = self.render()[1]
        self.assertIn('this crate requires `alice-det-math = "0.5.0"`', out)
        self.assertIn("| `gpu` | not declared |  |", out)

    def test_alice_det_math_itself_is_named_as_such(self):
        out = self.render(crate="alice-det-math")[1]
        self.assertIn("arithmetic identifier `" + SID + "`; this crate is alice-det-math |", out)
        self.assertNotIn("requires", out.split("Arithmetic", 1)[1].split("\n", 1)[0])
        ja = self.render("ja", crate="alice-det-math")[1]
        self.assertIn("この crate が alice-det-math |", ja)

    def test_a_crate_outside_the_end_to_end_check_and_without_law_dependency(self):
        rc, out = self.render(crate="alice-bar")
        self.assertEqual(rc, 0, out)
        self.assertIn("alice-bar 0.1.0 (planned), law dependency: no", out)
        self.assertIn("| End to end check | not part of the end to end check |", out)

    def test_the_two_architectures_are_stated_only_when_the_workflow_compares_them(self):
        for wf in ("jobs:\n  a:\n    runs-on: ubuntu-24.04\n",
                   WORKFLOW.replace("          - runner: macos-15\n            arch: arm64\n", ""),  # Intel leg only
                   WORKFLOW.replace("  digest-compare:\n    needs: digest\n", "")):              # no compare job
            self.sha = commit(self.law, {".github/workflows/e2e.yml": wf})
            out = self.render()[1]
            self.assertIn(f"digest `{DIGEST}` |", out, wf)
            self.assertNotIn("aarch64", out, wf)

    def test_the_block_is_deterministic_and_carries_no_date(self):
        commit(self.crate, {"alice-law-conformance.toml": MANIFEST})
        a, b = self.render()[1], self.render()[1]
        self.assertEqual(a, b)
        import re
        self.assertIsNone(re.search(r"20\d\d-\d\d-\d\d", a))

    def test_japanese_has_no_full_stop(self):
        commit(self.crate, {"alice-law-conformance.toml": MANIFEST})
        rc, ja = self.render("ja")
        self.assertEqual(rc, 0, ja)
        self.assertNotIn("。", ja)
        self.assertIn("## ALICE-Law との統合", ja)

    def test_a_manifest_the_block_must_not_render_is_refused(self):
        for bad in ('[G1]\nstatus = "fail-known"\n',                                 # fail-known without a note
                    '[G1]\nstatus = "n/a"\nevidence = ["clippy.toml"]\nmutant = "m"\n',  # n/a without a note
                    '[G1]\nstatus = "done"\nnote = "x"\n',                           # unknown status
                    '[features.std]\nguarantee = "inside"\nnote = "x"\n',            # inside without evidence
                    '[features.gpu]\nguarantee = "outside"\nnote = "x"\n',           # not a feature of the crate
                    '[G1]\nstatus = "pass"\nevidence = ["tests/gone.rs"]\n',          # evidence path not in the crate
                    '[G1]\nstatus = "pass"\nevidence = "tests/gone.rs"\n',            # a string, not a list
                    '[G1]\nstatus = "pass"\nevidence = "clippy.toml"\n',              # a string, even an existing path
                    '[G1]\nstatus = "pass"\nevidence = [1]\n',                        # not strings
                    '[G1]\nstatus = "pass"\nevidence = ["src/law"]\n',                # a directory without a slash
                    '[features.std]\nguarantee = "inside"\nevidence = ["src/none/"]\nnote = "x"\n'):
            commit(self.crate, {"alice-law-conformance.toml": bad})
            self.assertEqual(self.render()[0], 2, bad)

    def test_a_pass_without_evidence_or_mutant_is_shown_as_unverified_with_a_warning(self):
        for m in ('[G1]\nstatus = "pass"\nmutant = "m"\n',                               # no evidence key
                  '[G1]\nstatus = "pass"\nevidence = []\nmutant = "m"\n',                # empty list
                  '[G1]\nstatus = "pass"\nevidence = ["", "  "]\nmutant = "m"\n',        # empty strings
                  '[G1]\nstatus = "pass"\nevidence = ["clippy.toml"]\n',                # no mutant
                  '[G1]\nstatus = "n/a"\nevidence = ["clippy.toml"]\nnote = "x"\n'):   # n/a, no mutant
            commit(self.crate, {"alice-law-conformance.toml": m})
            rc, out = self.render()
            self.assertEqual(rc, 0, m)
            self.assertIn("| G1 no-libm-direct | unverified |", out, m)
            self.assertIn("shown as unverified", out, m)
            self.assertNotIn("| pass |", out, m)

    def test_the_four_statuses_and_the_legacy_missing(self):
        commit(self.crate, {"alice-law-conformance.toml":
                            '[G1]\nstatus = "n/a"\nevidence = ["clippy.toml"]\nmutant = "m"\nnote = "no float"\n'
                            '[G2]\nstatus = "missing"\nnote = "old"\n[G3]\nstatus = "unverified"\n'})
        rc, out = self.render()
        self.assertEqual(rc, 0, out)
        self.assertIn("| G1 no-libm-direct | n/a | no float |", out)
        self.assertIn("| G2 ieee-max-min | unverified | old |", out)
        self.assertIn("'missing' is the earlier vocabulary, read as 'unverified'", out)
        self.assertIn("| G3 no-outer-fneg-of-fma | unverified |  |", out)
        ja = self.render("ja")[1]
        self.assertIn("| G1 no-libm-direct | 対象外 | no float |", ja)
        self.assertIn("| G2 ieee-max-min | 未確認 | old |", ja)

    def test_free_text_evidence_and_existing_paths_are_accepted(self):
        commit(self.crate, {"alice-law-conformance.toml":
                            '[G1]\nstatus = "pass"\nevidence = ["clippy.toml (repository root)", "tests/", "tests", '
                            '"the determinism job in ci"]\n'})
        self.assertEqual(self.render()[0], 0)

    def test_renamed_and_target_dependencies_and_implicit_features(self):
        commit(self.crate, {"Cargo.toml": '[package]\nname = "alice-foo"\n[features]\nfast = ["dep:extra"]\n'
                                          '[dependencies]\nextra = { version = "1", optional = true }\n'
                                          'plot = { version = "1", optional = true }\n'
                                          '[target.\'cfg(unix)\'.dependencies]\n'
                                          'dm = { package = "alice-det-math", version = "0.5.1" }\n'})
        out = self.render()[1]
        self.assertIn('this crate requires `alice-det-math = "0.5.1"`', out)
        self.assertIn("| `plot` | not declared |  |", out)   # an optional dependency is a feature of its own
        self.assertIn("| `fast` | not declared |  |", out)
        self.assertNotIn("| `extra` |", out)                  # named as dep:extra, so no implicit feature

    def test_raw_html_in_a_note_is_removed(self):
        commit(self.crate, {"alice-law-conformance.toml": '[G2]\nstatus = "missing"\nnote = "a <b>bold</b> <img src=x> b"\n'})
        line = next(l for l in self.render()[1].splitlines() if l.startswith("| G2"))
        self.assertIn("| a bold b |", line)

    def test_a_long_note_is_cut_and_a_pipe_escaped(self):
        commit(self.crate, {"alice-law-conformance.toml":
                            '[G2]\nstatus = "missing"\nnote = "a|b ' + "word " * 60 + '"\n'})
        out = self.render()[1]
        line = next(l for l in out.splitlines() if l.startswith("| G2"))
        self.assertIn("a\\|b", line)
        self.assertTrue(line.endswith("… |"))
        self.assertLess(len(line), 220)

    def test_an_unknown_crate_or_revision_is_an_input_error(self):
        self.assertEqual(self.render(crate="alice-none")[0], 2)
        self.assertEqual(run("render", "--law", str(self.law), "--law-ref", "0" * 40, "--crate", "alice-foo",
                             "--crate-repo", str(self.crate), "--lang", "en")[0], 2)


class Write(Base):
    def test_the_block_goes_before_the_license_section_and_rewriting_is_idempotent(self):
        self.write_and_commit()
        en = (self.crate / "README.md").read_text(encoding="utf-8")
        ja = (self.crate / "README_JP.md").read_text(encoding="utf-8")
        self.assertLess(en.index("## Usage"), en.index("## ALICE-Law integration"))
        self.assertLess(en.index("## ALICE-Law integration"), en.index("## License"))
        self.assertLess(ja.index("## ALICE-Law との統合"), ja.index("## ライセンス"))
        rc, _ = run("write", "--law", str(self.law), "--law-ref", self.sha, "--crate", "alice-foo",
                    "--crate-repo", str(self.crate))
        self.assertEqual(rc, 0)
        self.assertEqual((self.crate / "README.md").read_text(encoding="utf-8"), en)

    def test_write_refuses_uncommitted_inputs(self):
        (self.crate / "Cargo.toml").write_text(CARGO + "# edit\n", encoding="utf-8")
        self.assertEqual(run("write", "--law", str(self.law), "--law-ref", self.sha, "--crate", "alice-foo",
                             "--crate-repo", str(self.crate))[0], 2)

    def test_the_japanese_readme_may_be_named_readme_ja(self):
        git(self.crate, "mv", "README_JP.md", "README_ja.md")
        git(self.crate, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "rename")
        self.write_and_commit()
        self.assertIn("## ALICE-Law との統合", (self.crate / "README_ja.md").read_text(encoding="utf-8"))
        self.assertFalse((self.crate / "README_JP.md").exists())
        rc, out = self.check()
        self.assertEqual(rc, 0, out)
        self.assertIn("2 blocks compared", out)

    def test_two_japanese_readmes_are_refused(self):
        commit(self.crate, {"README_ja.md": README_JA})
        self.assertEqual(run("write", "--law", str(self.law), "--law-ref", self.sha, "--crate", "alice-foo",
                             "--crate-repo", str(self.crate))[0], 2)
        self.assertEqual(self.check()[0], 2)

    def test_a_missing_japanese_readme_is_refused(self):
        (self.crate / "README_JP.md").unlink()
        self.assertEqual(run("write", "--law", str(self.law), "--law-ref", self.sha, "--crate", "alice-foo",
                             "--crate-repo", str(self.crate))[0], 2)


class Claims(unittest.TestCase):
    def test_the_closed_list(self):
        for claim in ("passes G7", "G14 is met", "Law-conformant", "ALICE-Law compliant", "conforms to the Law",
                      "conforming with ALICE-Law", "Law に準拠する", "ALICE-Law 適合",
                      "fully conformant with ALICE Law", "complies with the Law", "satisfies every Law gate",
                      "in compliance with the ALICE-Law", "Law 準拠済み"):
            self.assertEqual(glrb.outside_claims(claim), [1], claim)
        for prose in ("IEEE 準拠", "メモリ適合性", "a conformance claim", "`alice-law-conformance.toml`",
                      "IEEE compliant", "complies with IEEE 754", "conforms to the spec", "a law of cooling gate",
                      "G15", "G0", "G21", "law of cooling"):
            self.assertEqual(glrb.outside_claims(prose), [], prose)

    def test_only_a_link_only_line_to_the_gate_definitions_has_its_target_ignored(self):
        url = "https://github.com/ext-sakamoro/ALICE-Law/blob/main/docs/CONFORMANCE.md"
        for line in (f"[Conformance]({url}#g12)", f"- [gate definitions]({url})", f"* [docs/CONFORMANCE.md]({url}#g1)  "):
            self.assertEqual(glrb.outside_claims(line), [], line)
        for line in (f"[passes every Law gate]({url})",       # the link text is a claim
                     f"- [G12]({url}#g12)",                   # so is a gate as link text
                     f"See [the gates]({url}) for G1.",       # text outside the link
                     f"G12 pass, see CONFORMANCE.md",         # a plain mention is no link
                     f"[Conformance]({url}#g12) and [more]({url})",
                     f"[Conformance](https://example.org/g12#G12)"):  # not the gate definitions
            self.assertEqual(glrb.outside_claims(line), [1], line)
        self.assertEqual(glrb.outside_claims("```\nG3 passes\n```\n"), [])  # a code fence stays skipped

    def test_claims_in_a_line_that_names_the_law_or_its_gate_definitions(self):
        for line in ("This crate passes every ALICE-Law conformance gate.", "Fully conformant (docs/CONFORMANCE.md)",
                     "[Fully conformant](docs/CONFORMANCE.md)", "[x](docs/CONFORMANCE.md) passes every gate",
                     "<a href='docs/CONFORMANCE.md'>passes every gate</a>", "ALICE-Law: all gates pass",
                     "Law-compliant build", "passed the Law gates",
                     "Meets every Law requirement.", "100% Law-clean.", "Law: \u2705 all green",
                     "Passes all of the Law's checks."):
            self.assertEqual(glrb.outside_claims(line), [1], line)
        for line in ("passes every test gate", "fully conformant to IEEE 754", "all gates of the FPGA pass",
                     "See [the gate definitions](docs/CONFORMANCE.md)", "a law of cooling gate passes", "the Law's scope",
                     "meets every timing requirement", "all green LEDs", "\u2705 tests pass", "passes the type checks",
                     "the Law's requirements are listed in docs/CONFORMANCE.md"):
            self.assertEqual(glrb.outside_claims(line), [], line)


class Check(Base):
    def test_a_current_block_passes(self):
        self.write_and_commit()
        rc, out = self.check()
        self.assertEqual(rc, 0, out)
        self.assertIn("2 blocks compared, 0 problems", out)

    def test_no_block_compares_nothing(self):
        rc, out = self.check()
        self.assertEqual(rc, 2, out)
        self.assertIn("0 law-integration blocks", out)

    def test_a_manifest_change_without_regenerating_is_red(self):
        self.write_and_commit()
        commit(self.crate, {"alice-law-conformance.toml": MANIFEST})
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("README.md: the block differs", out)

    def test_a_hand_edited_block_is_red(self):
        self.write_and_commit()
        p = self.crate / "README.md"
        p.write_text(p.read_text(encoding="utf-8").replace("| not declared |", "| pass |", 1), encoding="utf-8")
        commit(self.crate, {})
        self.assertEqual(self.check()[0], 1)

    def test_a_later_law_change_does_not_make_the_pinned_block_stale(self):
        self.write_and_commit()
        commit(self.law, {"e2e/golden/run.digest": "ef" * 32 + "\n"})  # the next revision re-blesses
        self.assertEqual(self.check()[0], 0)

    def test_a_pin_off_the_law_main_line_is_red(self):
        git(self.law, "checkout", "-q", "-b", "side")
        self.sha = commit(self.law, {"e2e/golden/run.digest": "ef" * 32 + "\n"})
        git(self.law, "checkout", "-q", "-")
        self.write_and_commit()
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn(f"the pinned ALICE-Law revision {self.sha[:12]} is not on HEAD", out)

    def test_an_unknown_pin_is_an_input_error(self):
        self.write_and_commit()
        for name in ("README.md", "README_JP.md"):
            p = self.crate / name
            p.write_text(p.read_text(encoding="utf-8").replace(self.sha, "1" * 40), encoding="utf-8")
        commit(self.crate, {})
        self.assertEqual(self.check()[0], 2)

    def test_the_two_readmes_must_pin_the_same_revision(self):
        self.write_and_commit()
        old = self.sha
        self.sha = commit(self.law, {"e2e/golden/run.digest": "ef" * 32 + "\n"})
        en = (self.crate / "README.md").read_text(encoding="utf-8")
        self.write_and_commit()
        (self.crate / "README.md").write_text(en, encoding="utf-8")  # README.md back at the old pin
        commit(self.crate, {})
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("pin different ALICE-Law revisions", out)
        self.assertIn(old[:12], out)

    def test_a_missing_japanese_readme_or_two_blocks_are_red(self):
        self.write_and_commit()
        (self.crate / "README_JP.md").unlink()
        commit(self.crate, {})
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("README_JP.md: missing", out)
        en = (self.crate / "README.md").read_text(encoding="utf-8")
        block = en[en.index("<!-- law-integration:begin"):en.index(glrb.END) + len(glrb.END)]
        (self.crate / "README.md").write_text(en + "\n" + block + "\n", encoding="utf-8")
        commit(self.crate, {"README_JP.md": README_JA})
        rc, out = self.check()
        self.assertIn("README.md: 2 law-integration blocks", out)

    def test_conformance_wording_outside_the_block_warns_then_fails_with_error(self):
        self.write_and_commit()
        p = self.crate / "README.md"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "Intro.", "Intro. Fully conformant with G2.\n\n```\nG3 in a fence is fine\n```\n\n"
                      "See [the gates](https://example.org/docs/CONFORMANCE.md) for G1.\n\n"
                      "IEEE 準拠のどの target でも同じ. A conformance claim goes through stages. "
                      "See `alice-law-conformance.toml`. メモリ適合性.\n\nThis crate is Law-conformant."), encoding="utf-8")
        commit(self.crate, {})
        rc, out = self.check()
        self.assertEqual(rc, 0, out)
        # line 9 links the gate definitions but also says "for G1" outside the link: a claim
        self.assertIn("warning: README.md: conformance wording outside the block on lines [3, 9, 13]", out)
        rc, out = self.check("--lint", "error")
        self.assertEqual(rc, 1, out)

    def test_the_crate_ref_is_read_not_the_working_tree(self):
        self.write_and_commit()
        (self.crate / "README.md").write_text(README_EN, encoding="utf-8")  # uncommitted removal
        self.assertEqual(self.check()[0], 0)


if __name__ == "__main__":
    unittest.main()
