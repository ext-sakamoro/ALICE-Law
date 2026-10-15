#!/usr/bin/env python3
"""Tests for conformance_roster_check.py (no network: a fake fetcher stands for `gh api`)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("crc", HERE / "conformance_roster_check.py")
crc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crc)

GATES = 'status_vocabulary = ["pass", "fail-known", "unverified", "n/a"]\n' + \
    "\n".join(f'[[gate]]\nid = "G{i}"\nname = "g{i}"\n' for i in (1, 2, 3))
ROSTER = '''[train]
github = "owner"

[crate.alpha]
version = "1.0.0"
state = "published"
order = 1
repo = "Alpha"

[crate.beta]
version = "1.0.0"
state = "published"
order = 2
repo = "Beta"

[crate.law]
version = "0.0.0"
state = "exempt"
order = 3
repo = "Law"
'''
PATHS = {"alice-law-conformance.toml", "src/lib.rs", "src/geom/mod.rs", "tests/golden.rs", ".github/workflows/ci.yml", "Cargo.toml", "README.md",
         "scripts/lib.rs", "docs/notes.md"}


def manifest(**over) -> str:
    entries = {
        "G1": 'status = "pass"\nevidence = ["src/lib.rs", "tests/golden.rs"]\nmutant = "swap the kernel"',
        "G2": 'status = "fail-known"\nevidence = []\nnote = "raw max at src/lib.rs:12, no helper"',
        "G3": 'status = "unverified"',
    }
    entries.update(over)
    return "\n\n".join(f"[{k}]\n{v}" for k, v in entries.items() if v is not None) + "\n"


class Check(unittest.TestCase):
    def run_check(self, repos: dict, missing: str = "", state: str | None = None, write=True, roster=ROSTER,
                  gates=GATES):
        """repos: {repo: manifest text or None}; returns (code, output, state text)"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "r.toml").write_text(roster, encoding="utf-8")
            (d / "g.toml").write_text(gates, encoding="utf-8")
            (d / "m.toml").write_text(missing, encoding="utf-8")
            st = d / "s.toml"
            if state is not None:
                st.write_text(state, encoding="utf-8")
            calls = []

            def fetch(repo):
                calls.append(repo)
                return repos.get(repo), set(PATHS)
            out = io.StringIO()
            args = ["--roster", str(d / "r.toml"), "--gates", str(d / "g.toml"), "--missing", str(d / "m.toml"),
                    "--state", str(st)] + (["--write"] if write else [])
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = crc.main(args, fetch=fetch)
            self.assertNotIn("Law", calls)  # an exempt crate is not read
            return code, out.getvalue(), st.read_text(encoding="utf-8") if st.exists() else None

    def green_state(self, repos):
        code, out, state = self.run_check(repos, write=True)
        self.assertEqual(code, 0, out)
        return state

    def test_every_manifest_complete_passes_and_the_state_is_pinned(self):
        repos = {"Alpha": manifest(), "Beta": manifest()}
        state = self.green_state(repos)
        self.assertIn('[manifest."Alpha"]\ncrates = ["alpha"]\n"pass" = 1\n"fail-known" = 1\n"unverified" = 1\n"n/a" = 0', state)
        code, out, _ = self.run_check(repos, state=state, write=False)
        self.assertEqual(code, 0, out)
        self.assertIn("2 manifests read for 2 crates (crates that share a repository share its manifest), 6 gates compared, 0 findings", out)

    def test_crates_that_share_a_repository_share_one_manifest_checked_once(self):
        roster = ROSTER + '\n[crate.gamma]\nversion = "1.0.0"\nstate = "published"\norder = 4\nrepo = "Alpha"\n'
        code, out, state = self.run_check({"Alpha": manifest(), "Beta": manifest()}, roster=roster)
        self.assertEqual(code, 0, out)
        self.assertIn('[manifest."Alpha"]\ncrates = ["alpha", "gamma"]\n"pass" = 1', state)
        self.assertEqual(state.count("[manifest."), 2)
        self.assertIn("2 manifests read for 3 crates", out)
        self.assertIn("6 gates compared", out)  # Alpha's 3 gates once, not once per crate
        code, out, state = self.run_check({"Alpha": manifest(G3=None), "Beta": manifest()}, roster=roster)
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count("gate G3 has no entry"), 1)

    def test_a_state_file_that_differs_from_main_is_a_finding(self):
        repos = {"Alpha": manifest(), "Beta": manifest()}
        state = self.green_state(repos)
        code, out, _ = self.run_check(repos, state=state.replace('"pass" = 1', '"pass" = 2', 1), write=False)
        self.assertEqual(code, 1, out)
        self.assertIn("s.toml differs from the manifests at main", out)
        code, out, _ = self.run_check(repos, state=None, write=False)
        self.assertEqual(code, 1, out)

    def test_a_missing_manifest_fails_with_the_crate_name(self):
        code, out, _ = self.run_check({"Alpha": manifest(), "Beta": None})
        self.assertEqual(code, 1, out)
        self.assertIn("found: beta: no alice-law-conformance.toml at the main of Beta", out)

    def test_a_listed_missing_manifest_is_expected_and_a_landed_one_is_stale(self):
        listed = '[missing."beta"]\nfixed-in = "Beta"\nwhy = "not written yet"\n'
        repos = {"Alpha": manifest(), "Beta": None}
        code, out, state = self.run_check(repos, missing=listed, write=True)
        self.assertEqual(code, 0, out)
        self.assertIn("note: beta: no alice-law-conformance.toml at main, as missing-manifests.toml expects", out)
        self.assertNotIn('"beta"', state)
        self.assertNotIn('"Beta"', state)
        code, out, _ = self.run_check({"Alpha": manifest(), "Beta": manifest()}, missing=listed)
        self.assertEqual(code, 1, out)
        self.assertIn("beta: alice-law-conformance.toml is at main, so its missing-manifests.toml entry is stale", out)

    def test_a_missing_list_entry_needs_its_repository_and_reason_and_a_train_crate(self):
        for listed in ('[missing."beta"]\nwhy = "x"\n', '[missing."beta"]\nfixed-in = "Beta"\n',
                       '[missing."gamma"]\nfixed-in = "G"\nwhy = "x"\n'):
            code, out, _ = self.run_check({"Alpha": manifest(), "Beta": None}, missing=listed)
            self.assertEqual(code, 2, (listed, out))

    def test_a_dropped_or_unknown_gate_fails(self):
        code, out, _ = self.run_check({"Alpha": manifest(G3=None), "Beta": manifest()})
        self.assertEqual(code, 1, out)
        self.assertIn("Alpha: alice-law-conformance.toml: gate G3 has no entry", out)
        code, out, _ = self.run_check({"Alpha": manifest(G4='status = "unverified"'), "Beta": manifest()})
        self.assertEqual(code, 1, out)
        self.assertIn("G4 is not a gate of conformance/gates.toml", out)

    def test_a_status_outside_the_vocabulary_or_no_status_fails(self):
        for g3, text in (('status = "green"', "status 'green' is not one of"), ('note = "x"', "G3 has no status")):
            code, out, _ = self.run_check({"Alpha": manifest(G3=g3), "Beta": manifest()})
            self.assertEqual(code, 1, out)
            self.assertIn(text, out)

    def test_the_legacy_status_is_read_as_unverified_with_a_note(self):
        repos = {"Alpha": manifest(G3='status = "missing"'), "Beta": manifest()}
        code, out, state = self.run_check(repos, write=True)
        self.assertEqual(code, 0, out)
        self.assertIn("G3 status 'missing' is the earlier vocabulary, read as 'unverified'", out)

    def test_fail_known_must_carry_a_note_that_locates_the_site(self):
        for g2, ok in (('status = "fail-known"\nnote = "raw max at src/lib.rs:12"', True),
                       ('status = "fail-known"\nnote = "no release leg in ci.yml (the aarch64 legs)"', True),
                       ('status = "fail-known"\nnote = "src/lib.rs line 40 calls f32::max"', True),
                       ('status = "fail-known"\nnote = "the helper is missing somewhere"', False),
                       ('status = "fail-known"\nnote = "raw max at src/other.rs:12"', False),
                       ('status = "fail-known"', False)):
            code, out, _ = self.run_check({"Alpha": manifest(G2=g2), "Beta": manifest()}, write=True)
            self.assertEqual(code, 0 if ok else 1, (g2, out))

    def test_evidence_naming_a_path_not_at_main_fails(self):
        g1 = 'status = "pass"\nevidence = ["src/gone.rs", ".github/workflows/ci.yml clippy job"]\nmutant = "m"'
        code, out, _ = self.run_check({"Alpha": manifest(G1=g1), "Beta": manifest()})
        self.assertEqual(code, 1, out)
        self.assertIn("G1 evidence names paths that are not at main: ['src/gone.rs']", out)

    def test_pass_without_evidence_or_mutant_counts_as_unverified(self):
        repos = {"Alpha": manifest(G1='status = "pass"\nevidence = ["src/lib.rs"]'), "Beta": manifest()}
        code, out, state = self.run_check(repos, write=True)
        self.assertEqual(code, 0, out)
        self.assertIn("G1 pass without a mutant, read as unverified", out)
        self.assertIn('[manifest."Alpha"]\ncrates = ["alpha"]\n"pass" = 0\n"fail-known" = 1\n"unverified" = 2', state)

    def test_an_evidence_path_in_any_written_form_must_be_at_main(self):
        # the first word of an entry, after backticks, a `label:`, `:<line>`, `::<name>` and `./`
        for entry in ("tests/nope.rs:10", "tests/nope.rs::t", "`tests/nope.rs` runs it", "test: tests/nope.rs",
                      "nope/", "./tests/nope.rs"):
            g1 = f'status = "pass"\nevidence = ["src/lib.rs", "{entry}"]\nmutant = "m"'
            code, out, _ = self.run_check({"Alpha": manifest(G1=g1), "Beta": manifest()})
            self.assertEqual(code, 1, (entry, out))
            self.assertIn("G1 evidence names paths that are not at main", out)
        for entry in ("tests/golden.rs:10", "tests/golden.rs::checker", "`tests/golden.rs`", "test: tests/golden.rs",
                      "./src/lib.rs", "src/", "ci.yml clippy job", "src/lib.rs (see ALICE-Law's scripts/x.py)"):
            g1 = f'status = "pass"\nevidence = ["{entry}"]\nmutant = "m"'
            code, out, state = self.run_check({"Alpha": manifest(G1=g1), "Beta": manifest()}, write=True)
            self.assertEqual(code, 0, (entry, out))
            self.assertIn('[manifest."Alpha"]\ncrates = ["alpha"]\n"pass" = 1', state)

    def test_pass_with_meaningless_evidence_or_mutant_is_not_counted_as_pass(self):
        for g1 in ('status = "pass"\nevidence = [""]\nmutant = "m"',
                   'status = "pass"\nevidence = ["trust me"]\nmutant = "m"',
                   'status = "pass"\nevidence = ["src/lib.rs"]\nmutant = "  "',
                   'status = "n/a"\nevidence = ["it does not apply"]\nmutant = "m"\nnote = "none here"'):
            code, out, state = self.run_check({"Alpha": manifest(G1=g1), "Beta": manifest()}, write=True)
            self.assertEqual(code, 0, (g1, out))
            self.assertIn('[manifest."Alpha"]\ncrates = ["alpha"]\n"pass" = 0', state, g1)
            self.assertIn("read as unverified", out)
        code, out, _ = self.run_check({"Alpha": manifest(G1='status = "pass"\nevidence = ["src/lib.rs"]\nmutant = 0'),
                                       "Beta": manifest()})
        self.assertEqual(code, 1, out)
        self.assertIn("G1 mutant is not a string", out)

    def test_fail_known_is_not_located_by_a_root_file_a_directory_or_an_unanchored_name(self):
        for note in ("see Cargo.toml", "tracked in README.md", "the helper in lib.rs is missing", "under src/geom",
                     "no step in ALICE-Law's .github/workflows/ci.yml", "see notes.md for the plan", "lib.rs:4",
                     "see alice-law-conformance.toml", "alice-law-conformance.toml:3"):
            code, out, _ = self.run_check({"Alpha": manifest(G2=f'status = "fail-known"\nnote = "{note}"'),
                                           "Beta": manifest()}, write=True)
            self.assertEqual(code, 1, (note, out))
            self.assertIn("G2 is fail-known without a located site", out)
        for note in ("raw max at src/geom/mod.rs", "src/lib.rs:12", "no release leg in ci.yml",
                     "mod.rs line 3 has the raw max"):
            code, out, _ = self.run_check({"Alpha": manifest(G2=f'status = "fail-known"\nnote = "{note}"'),
                                           "Beta": manifest()}, write=True)
            self.assertEqual(code, 0, (note, out))

    def test_a_gate_id_in_another_case_or_with_a_leading_zero_fails(self):
        for key, text in (("g15", "'g15' is not spelled as a gate id"), ("g1", "'g1' is not spelled as a gate id"),
                          ("G01", "G01 is not a gate of conformance/gates.toml")):
            code, out, _ = self.run_check({"Alpha": manifest() + f'\n[{key}]\nstatus = "unverified"\n',
                                           "Beta": manifest()})
            self.assertEqual(code, 1, (key, out))
            self.assertIn(text, out)

    def test_statuses_are_case_sensitive(self):
        for g3 in ('status = "Pass"', 'status = "PASS"', 'status = "Unverified"'):
            code, out, _ = self.run_check({"Alpha": manifest(G3=g3), "Beta": manifest()}, write=True)
            self.assertEqual(code, 1, (g3, out))
            self.assertIn("G3 status", out)
            self.assertIn("is not one of", out)

    def test_comparing_nothing_exits_2(self):
        listed = ('[missing."alpha"]\nfixed-in = "Alpha"\nwhy = "x"\n'
                  '[missing."beta"]\nfixed-in = "Beta"\nwhy = "x"\n')
        code, out, _ = self.run_check({"Alpha": None, "Beta": None}, missing=listed)
        self.assertEqual(code, 2, out)
        self.assertIn("nothing was compared (0 manifests", out)
        code, out, _ = self.run_check({}, roster='[train]\ngithub = "o"\n')
        self.assertEqual(code, 2, out)
        code, out, _ = self.run_check({"Alpha": manifest(), "Beta": manifest()},
                                      gates='status_vocabulary = ["pass"]\n')
        self.assertEqual(code, 2, out)


class Fetcher(unittest.TestCase):
    """gh_fetcher against a stubbed `gh api`"""

    def fetch_with(self, answers):
        import json
        import subprocess
        import types
        saved = crc.subprocess.run

        def run(cmd, capture_output, text):
            path = cmd[2]
            for key, (code, body) in answers.items():
                if key in path:
                    return types.SimpleNamespace(returncode=code, stdout=json.dumps(body) if code == 0 else "",
                                                 stderr="" if code == 0 else body)
            raise AssertionError(path)
        crc.subprocess.run = run
        self.addCleanup(setattr, crc.subprocess, "run", saved)
        return crc.gh_fetcher("owner")("Repo")

    def test_the_tree_and_the_manifest_are_read(self):
        import base64
        text, paths = self.fetch_with({
            "git/trees": (0, {"tree": [{"path": "alice-law-conformance.toml", "type": "blob"},
                                       {"path": "src", "type": "tree"}, {"path": "src/lib.rs", "type": "blob"}]}),
            "contents/": (0, {"content": base64.b64encode(b"[G1]\n").decode()})})
        self.assertEqual(text, "[G1]\n")
        self.assertEqual(paths, {"alice-law-conformance.toml", "src/lib.rs"})

    def test_no_manifest_in_the_tree_is_none(self):
        text, paths = self.fetch_with({"git/trees": (0, {"tree": [{"path": "src/lib.rs", "type": "blob"}]})})
        self.assertIsNone(text)

    def test_a_truncated_tree_or_an_api_error_is_an_input_error(self):
        with self.assertRaises(crc.InputError):
            self.fetch_with({"git/trees": (0, {"truncated": True, "tree": []})})
        with self.assertRaises(crc.InputError):
            self.fetch_with({"git/trees": (1, "HTTP 500: server error")})
        with self.assertRaises(crc.InputError):
            self.fetch_with({"git/trees": (1, "gh: Not Found (HTTP 404)")})


if __name__ == "__main__":
    unittest.main()
