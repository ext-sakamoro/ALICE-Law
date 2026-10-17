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
        text, paths, read = self.fetch_with({
            "git/trees": (0, {"tree": [{"path": "alice-law-conformance.toml", "type": "blob"},
                                       {"path": "src", "type": "tree"}, {"path": "src/lib.rs", "type": "blob"}]}),
            "contents/alice-law": (0, {"content": base64.b64encode(b"[G1]\n").decode()}),
            "contents/src/lib.rs": (0, {"content": base64.b64encode(b"fn tie() {}\n").decode()})})
        self.assertEqual(text, "[G1]\n")
        self.assertEqual(paths, {"alice-law-conformance.toml", "src/lib.rs"})
        self.assertEqual(read("src/lib.rs"), "fn tie() {}\n")
        self.assertIsNone(read("src/absent.rs"))

    def test_no_manifest_in_the_tree_is_none(self):
        text, paths, read = self.fetch_with({"git/trees": (0, {"tree": [{"path": "src/lib.rs", "type": "blob"}]})})
        self.assertIsNone(text)
        self.assertIsNone(read)

    def test_a_truncated_tree_or_an_api_error_is_an_input_error(self):
        with self.assertRaises(crc.InputError):
            self.fetch_with({"git/trees": (0, {"truncated": True, "tree": []})})
        with self.assertRaises(crc.InputError):
            self.fetch_with({"git/trees": (1, "HTTP 500: server error")})
        with self.assertRaises(crc.InputError):
            self.fetch_with({"git/trees": (1, "gh: Not Found (HTTP 404)")})


class CheckManifest(unittest.TestCase):
    """--check-manifest: one member's manifest at a git ref of a local clone"""

    def repo(self, files: dict, commit=True) -> Path:
        import subprocess
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d)
        git = lambda *a: subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)
        git("init", "-q")
        for rel, text in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text, encoding="utf-8")
        if commit:
            git("add", "-A")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
        return d

    def run_cm(self, d: Path, ref="HEAD"):
        with tempfile.TemporaryDirectory() as t:
            g = Path(t) / "g.toml"
            g.write_text(GATES, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = crc.main(["--check-manifest", str(d), "--ref", ref, "--gates", str(g)])
        return code, out.getvalue()

    def files(self, text=None):
        f = {rel: "x\n" for rel in ("src/lib.rs", "tests/golden.rs", "Cargo.toml")}
        f[crc.MANIFEST] = manifest() if text is None else text
        return f

    def test_a_clean_manifest_passes_with_the_counts_it_would_record(self):
        code, out = self.run_cm(self.repo(self.files()))
        self.assertEqual(code, 0, out)
        self.assertIn("3 gates compared against 4 files, 0 findings (pass 1, fail-known 1, unverified 1, n/a 0)", out)

    def test_an_evidence_path_missing_at_the_ref_is_a_finding_naming_the_ref(self):
        text = manifest(G1='status = "pass"\nevidence = ["tools/x/{a.rs,b.rs}"]\nmutant = "m"')
        code, out = self.run_cm(self.repo(self.files(text)))
        self.assertEqual(code, 1, out)
        self.assertIn("G1 evidence names paths that are not at HEAD", out)

    def test_the_ref_is_read_not_the_working_tree(self):
        d = self.repo(self.files(manifest(G1='status = "pass"\nevidence = ["src/new.rs"]\nmutant = "m"')))
        (d / "src" / "new.rs").write_text("uncommitted\n", encoding="utf-8")
        code, out = self.run_cm(d)
        self.assertEqual(code, 1, out)
        self.assertIn("src/new.rs", out)

    def test_the_manifest_text_is_read_at_the_ref_not_from_the_working_tree(self):
        bad = manifest(G1='status = "pass"\nevidence = ["src/gone.rs"]\nmutant = "m"')
        # committed clean, working tree bad: the ref is clean
        d = self.repo(self.files())
        (d / crc.MANIFEST).write_text(bad, encoding="utf-8")
        code, out = self.run_cm(d)
        self.assertEqual(code, 0, out)
        # committed bad, working tree clean: the ref is bad
        d = self.repo(self.files(bad))
        (d / crc.MANIFEST).write_text(manifest(), encoding="utf-8")
        code, out = self.run_cm(d)
        self.assertEqual(code, 1, out)
        self.assertIn("src/gone.rs", out)

    def test_the_label_is_the_repository_name_from_origin(self):
        import subprocess
        d = self.repo(self.files())
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", "git@github.com:owner/ALICE-Example.git"],
                       check=True)
        code, out = self.run_cm(d)
        self.assertEqual(code, 0, out)
        self.assertIn("manifest check: ALICE-Example@HEAD:", out)
        d = self.repo(self.files(manifest(G1='status = "pass"\nevidence = ["src/gone.rs"]\nmutant = "m"')))
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", "https://github.com/owner/ALICE-Example"],
                       check=True)
        code, out = self.run_cm(d)
        self.assertEqual(code, 1, out)
        self.assertIn("found: ALICE-Example: alice-law-conformance.toml: G1 evidence", out)

    def test_no_manifest_at_the_ref_or_an_unknown_ref_compares_nothing(self):
        f = self.files()
        del f[crc.MANIFEST]
        code, out = self.run_cm(self.repo(f))
        self.assertEqual(code, 2, out)
        self.assertIn("no alice-law-conformance.toml", out)
        code, out = self.run_cm(self.repo(self.files()), ref="no-such-ref")
        self.assertEqual(code, 2, out)
        self.assertIn("ref 'no-such-ref' not found", out)

    def test_a_manifest_with_no_gate_entry_compared_nothing(self):
        code, out = self.run_cm(self.repo(self.files("# nothing\n")))
        self.assertEqual(code, 2, out)
        self.assertIn("compared nothing", out)


class LawEvidence(unittest.TestCase):
    """a G2 / G8 pass counts only with the evidence the gate text asks for, named and present at the ref"""
    GATES = 'status_vocabulary = ["pass", "fail-known", "unverified", "n/a"]\n' + \
        "\n".join(f'[[gate]]\nid = "G{i}"\nname = "g{i}"\n' for i in (2, 8))
    SRC = {"src/lib.rs": "pub fn write_v2(x: u8) {}\npub enum Pinning {\n    Unpinned,\n    Pinned,\n}\n"
                         "pub struct RequirePinned;\n",
           "tests/ties.rs": "#[test]\nfn max_ties_through_the_api() {}\n\n#[test]\n#[ignore = \"slow\"]\nfn ignored_tie() {}\n\n"
                            "fn helper_tie() {}\n\n#[cfg(any())]\n#[test]\nfn never_built_tie() {}\n\n"
                            "#[test]\n#[cfg_attr(\n    miri,\n    ignore\n)]\nfn multi_line_attr_tie() {}\n",
           "src/bare.rs": "#[test]\nfn outside_tie() {}\n",
           "src/inner.rs": "fn not_a_test_tie() {}\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn unit_tie() {}\n}\n"}
    PASS = 'status = "pass"\nevidence = ["src/lib.rs"]\nmutant = "m"'

    def counts(self, g2: str, g8: str):
        files = dict(self.SRC)
        files[crc.MANIFEST] = f"[G2]\n{self.PASS}\n{g2}\n\n[G8]\n{self.PASS}\n{g8}\n"
        d = CheckManifest.repo(self, files)
        with tempfile.TemporaryDirectory() as t:
            g = Path(t) / "g.toml"
            g.write_text(self.GATES, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = crc.main(["--check-manifest", str(d), "--gates", str(g)])
        self.assertEqual(code, 0, out.getvalue())
        text = out.getvalue()
        return text[text.index("findings (") + len("findings ("):].split(")")[0], text

    G2_OK = 'tie_tests = ["max of a field: tests/ties.rs::max_ties_through_the_api"]'
    G8_OK = ('writers = ["src/lib.rs::write_v2"]\nlegacy_status = "src/lib.rs::Unpinned"\n'
             'legacy_policy = "src/lib.rs::RequirePinned"')

    def test_both_passes_with_their_evidence_count_as_pass(self):
        c, _ = self.counts(self.G2_OK, self.G8_OK)
        self.assertEqual(c, "pass 2, fail-known 0, unverified 0, n/a 0")
        # a class label with ": " in it
        c, _ = self.counts('tie_tests = ["reductions (max: fold, lanes): tests/ties.rs::max_ties_through_the_api"]',
                           self.G8_OK)
        self.assertEqual(c, "pass 2, fail-known 0, unverified 0, n/a 0")
        c, _ = self.counts("tie_tests = []", 'writers = ["src/lib.rs::write_v2"]\nlegacy = "none"')
        self.assertEqual(c, "pass 2, fail-known 0, unverified 0, n/a 0")

    def test_a_g2_pass_without_existing_tie_tests_is_unverified(self):
        for g2, why in [("", "G2 pass without tie_tests"),
                        ('tie_tests = ["max: tests/ties.rs::no_such_test"]', "does not define no_such_test"),
                        ('tie_tests = ["max: tests/gone.rs::max_ties_through_the_api"]', "tests/gone.rs is not in the tree"),
                        ('tie_tests = ["tests/ties.rs::max_ties_through_the_api"]', "is not `<class>:"),
                        ('tie_tests = [" : tests/ties.rs::max_ties_through_the_api"]', "is not `<class>:"),
                        ('tie_tests = ["max: tests/ties.rs::ignored_tie"]', "is #[ignore]d"),
                        ('tie_tests = ["max: tests/ties.rs::helper_tie"]', "has no #[test]"),
                        ('tie_tests = ["max: tests/ties.rs::never_built_tie"]', "never built"),
                        ('tie_tests = ["max: src/inner.rs::not_a_test_tie"]', "has no #[test]"),
                        ('tie_tests = ["max: src/bare.rs::outside_tie"]', "outside tests/")]:
            c, out = self.counts(g2, self.G8_OK)
            self.assertEqual(c, "pass 1, fail-known 0, unverified 1, n/a 0", g2)
            self.assertIn(why, out)

    def test_a_unit_test_in_a_cfg_test_module_and_a_multi_line_attribute_count(self):
        c, _ = self.counts('tie_tests = ["max: src/inner.rs::unit_tie", "min: tests/ties.rs::multi_line_attr_tie"]',
                           self.G8_OK)
        self.assertEqual(c, "pass 2, fail-known 0, unverified 0, n/a 0")

    def test_a_g8_pass_without_writers_or_the_legacy_status_and_policy_is_unverified(self):
        for g8, why in [("legacy = \"none\"", "G8 pass without writers"),
                        ('writers = []\nlegacy = "none"', "an empty list compared nothing"),
                        ('writers = ["src/lib.rs::write_v9"]\nlegacy = "none"', "does not define write_v9"),
                        ('writers = ["src/lib.rs::write_v2"]', "legacy_status"),
                        ('writers = ["src/lib.rs::write_v2"]\nlegacy_status = "src/lib.rs::Unpinned"',
                         "legacy_policy"),
                        ('writers = ["src/lib.rs::write_v2"]\nlegacy_status = "src/lib.rs::Unpinned"\n'
                         'legacy_policy = "src/lib.rs::Strict"', "does not define Strict")]:
            c, out = self.counts(self.G2_OK, g8)
            self.assertEqual(c, "pass 1, fail-known 0, unverified 1, n/a 0", g8)
            self.assertIn(why, out)

    def test_symbol_lookup_reads_the_ref_not_the_name_in_a_comment(self):
        paths = {"src/a.rs"}
        read = {"src/a.rs": "// fn ghost() appears only here\nfn real() {}\n"}.get
        self.assertIsNone(crc.symbol_defined("src/a.rs::real", paths, read))
        self.assertIsNotNone(crc.symbol_defined("src/a.rs::ghost", paths, read))
        self.assertIsNotNone(crc.symbol_defined("src/a.rs::real", paths, None))


    def test_a_quote_in_a_char_literal_or_a_raw_string_does_not_blank_the_code_after_it(self):
        # the two forms that hid real writers: a `'"'` match arm (ALICE-Edge src/mqtt_bridge.rs) and a
        # `strip_prefix('"')` call (libalice src/residual.rs), each followed by a pub fn; and a raw string
        # holding quotes; each is followed by a string literal, as in the real files, so a quote counted
        # as opening a string pairs with it and swallows the fn between
        paths = {"src/a.rs", "src/b.rs", "src/c.rs"}
        read = {"src/a.rs": 'fn esc(c: char, out: &mut String) {\n    match c {\n        \'"\' => out.push_str("q"),\n        _ => out.push(c),\n    }\n}\npub fn to_binary_ext() -> u8 { 1 }\nconst TAIL: &str = "tail";\n', "src/b.rs": 'fn key(raw: &str) -> Option<&str> {\n    raw.strip_prefix(\'"\').and_then(|r| r.strip_suffix(b\'\\\'\' as char))\n}\npub fn compress_residual_delta() {}\nconst TAIL: &str = "tail";\n', "src/c.rs": 'fn json<\'a>(x: &\'a str) -> String {\n    format!(r#"{} says "hi"#, x)\n}\npub fn after_raw() {}\nconst TAIL: &str = "tail";\n// fn in_comment() {}\nconst S: &str = "fn in_string() {}";\n'}.get
        self.assertIsNone(crc.symbol_defined("src/a.rs::to_binary_ext", paths, read))
        self.assertIsNone(crc.symbol_defined("src/b.rs::compress_residual_delta", paths, read))
        self.assertIsNone(crc.symbol_defined("src/c.rs::after_raw", paths, read))
        self.assertIsNone(crc.symbol_defined("src/c.rs::json", paths, read))
        # still blanked: a name in a comment or a string is not a definition
        self.assertIsNotNone(crc.symbol_defined("src/c.rs::in_comment", paths, read))
        self.assertIsNotNone(crc.symbol_defined("src/c.rs::in_string", paths, read))

class Internal(unittest.TestCase):
    """--internal: the committed state file alone, no other repository read"""
    GOOD = ('[manifest."Alpha"]\ncrates = ["alpha"]\n"pass" = 1\n"fail-known" = 1\n"unverified" = 1\n"n/a" = 0\n\n'
            '[manifest."Beta"]\ncrates = ["beta"]\n"pass" = 0\n"fail-known" = 0\n"unverified" = 3\n"n/a" = 0\n')

    def run_internal(self, state: str, missing: str = "", roster=ROSTER):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "r.toml").write_text(roster, encoding="utf-8")
            (d / "g.toml").write_text(GATES, encoding="utf-8")
            (d / "m.toml").write_text(missing, encoding="utf-8")
            (d / "s.toml").write_text(state, encoding="utf-8")

            def fetch(repo):
                raise AssertionError(f"--internal read {repo}")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = crc.main(["--roster", str(d / "r.toml"), "--gates", str(d / "g.toml"), "--missing",
                                 str(d / "m.toml"), "--state", str(d / "s.toml"), "--internal"], fetch=fetch)
            return code, out.getvalue()

    def test_a_consistent_state_passes_without_reading_any_repository(self):
        code, out = self.run_internal(self.GOOD)
        self.assertEqual(code, 0, out)
        self.assertIn("2 repositories, 0 findings", out)

    def test_a_state_that_omits_a_crate_fails(self):
        state = self.GOOD.split('\n\n[manifest."Beta"]')[0] + "\n"
        code, out = self.run_internal(state)
        self.assertEqual(code, 1, out)
        self.assertIn("beta (Beta) has no entry", out)

    def test_an_omitted_crate_listed_as_missing_is_expected(self):
        state = self.GOOD.split('\n\n[manifest."Beta"]')[0] + "\n"
        code, out = self.run_internal(state, missing='[missing.beta]\nfixed-in = "Beta"\nwhy = "not yet"\n')
        self.assertEqual(code, 0, out)

    def test_counts_that_do_not_sum_to_the_gates_fail(self):
        code, out = self.run_internal(self.GOOD.replace('"n/a" = 0\n\n[manifest."Beta"]', '"n/a" = 1\n\n[manifest."Beta"]'))
        self.assertEqual(code, 1, out)
        self.assertIn("sum to 4, not to the 3 gates", out)

    def test_a_negative_or_non_integer_count_fails(self):
        for bad in ('"pass" = -1', '"pass" = "1"', '"pass" = true'):
            code, out = self.run_internal(self.GOOD.replace('"pass" = 1', bad, 1))
            self.assertEqual(code, 1, (bad, out))

    def test_a_crate_under_the_wrong_repository_or_twice_or_unknown_fails(self):
        for state, want in ((self.GOOD.replace('crates = ["beta"]', 'crates = ["alpha"]'), "listed under both"),
                            (self.GOOD.replace('crates = ["beta"]', 'crates = ["beta", "gamma"]'), "gamma is not"),
                            (self.GOOD.replace('[manifest."Beta"]', '[manifest."Other"]'), "belongs to Beta"),
                            (self.GOOD.replace('crates = ["beta"]', 'crates = ["law"]'), "law is not")):
            code, out = self.run_internal(state)
            self.assertEqual(code, 1, (want, out))
            self.assertIn(want, out)

    def test_an_unparseable_or_empty_state_fails(self):
        code, out = self.run_internal("[manifest.\n")
        self.assertEqual(code, 1, out)
        self.assertIn("does not parse", out)
        code, out = self.run_internal("")
        self.assertNotEqual(code, 0, out)

    def test_the_stale_message_names_the_regeneration_command(self):
        check = Check()
        code, out, _ = check.run_check({"Alpha": manifest(), "Beta": manifest()}, state="# stale\n", write=False)
        self.assertEqual(code, 1, out)
        self.assertIn("regenerate manifest-state: run python3 scripts/conformance_roster_check.py --write", out)


if __name__ == "__main__":
    unittest.main()
