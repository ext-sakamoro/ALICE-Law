#!/usr/bin/env python3
"""Tests of scripts/land_law_state.py (python3 scripts/test_land_law_state.py).

The other repositories' main is a fake fetcher (no network); a candidate is a fake or a real git clone."""
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import conformance_roster_check as crc  # noqa: E402
import land_law_state as lls  # noqa: E402
from test_conformance_roster_check import GATES, PATHS, ROSTER, manifest  # noqa: E402
from test_conformance_roster_check import poison_root  # noqa: E402

# this module's conformance_roster_check is not the one test_conformance_roster_check loads (it imports it by
# file), so the poisoned root is set here for this module's copy, the one land_law_state uses too
_real_root = crc.ROOT


def setUpModule():
    assert lls.crc is crc
    crc.ROOT = poison_root()


def tearDownModule():
    crc.ROOT = _real_root


ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent"}
PASSED_G3 = manifest(G3='status = "pass"\nevidence = ["tests/golden.rs"]\nmutant = "drop the check"')


class LeakDetector(unittest.TestCase):
    def test_a_land_call_without_root_reads_the_poisoned_root(self):
        # the default is bound at call time: the poisoned root has no release-train.toml, and the error names it
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = lls.main(["--candidate", "Alpha=/c@abc"], remote=lambda repo: (None, set()))
        self.assertEqual(code, 2, out.getvalue())
        self.assertIn(str(crc.ROOT / "release-train.toml"), out.getvalue())


class LandBase(unittest.TestCase):
    """the Law root, the mains and the fakes the land tests share"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "law"
        (self.root / "conformance").mkdir(parents=True)
        (self.root / "release-train.toml").write_text(ROSTER, encoding="utf-8")
        (self.root / "conformance" / "gates.toml").write_text(GATES, encoding="utf-8")
        self.mains = {"Alpha": manifest(), "Beta": manifest()}
        # the baseline equals the mains' line-number references (manifest() holds one, G2's src/lib.rs:12)
        (self.root / "conformance" / "line-refs-baseline.toml").write_text(
            "# the baseline\n\n[baseline]\n" + "".join(f'"{r}" = {crc.line_refs(t)}\n' for r, t in sorted(self.mains.items())),
            encoding="utf-8")
        self.reads = []

    def tearDown(self):
        self.tmp.cleanup()

    def remote(self, repo):
        self.assertNotEqual(repo, "Law")  # an exempt crate is not read
        self.reads.append(repo)
        return self.mains.get(repo), set(PATHS)

    def fake_local(self, texts: dict):
        """a stand-in for crc.git_fetcher: <dir>@<rev> -> the manifest text recorded for that rev"""
        def factory(where: Path, rev: str):
            def fetch():
                if rev not in texts:
                    raise crc.InputError(f"{where}: ref {rev!r} not found")
                return texts[rev], set(PATHS)
            return fetch
        return factory

    def files(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}

    def run_land(self, *args: str, local=None):
        out = io.StringIO()
        before = self.files()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = lls.main(list(args), remote=self.remote, local=local or self.fake_local({}), root=self.root,
                            resolve=lambda where, rev: (rev * 40)[:40])
        self.assertEqual(self.files(), before)  # a report: nothing in this repository is written
        return code, out.getvalue()


class LandLawState(LandBase):
    def test_a_candidate_with_the_same_statuses_does_not_move_the_state(self):
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": manifest()}))
        self.assertEqual(code, 0, out)
        self.assertIn("land_law_state: state moves: no", out.splitlines())
        self.assertIn("2 manifests read for 2 crates, 6 gates compared, 0 findings (candidates: Alpha@abcabcabcabc)", out)

    def test_a_candidate_that_flips_a_gate_moves_the_state_names_the_counts_and_still_exits_0(self):
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": PASSED_G3}))
        self.assertEqual(code, 0, out)
        self.assertIn("land_law_state: state moves: yes", out.splitlines())
        self.assertIn("  Alpha: pass 1 -> 2, unverified 1 -> 0", out.splitlines())
        self.assertFalse([x for x in out.splitlines() if x.startswith("  Beta:")], out)

    def test_the_comparison_is_with_the_mains_now_not_a_recorded_state(self):
        # main has the flip; the candidate takes it back: the move is computed against main as it is
        self.mains["Alpha"] = PASSED_G3
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": manifest()}))
        self.assertEqual(code, 0, out)
        self.assertIn("  Alpha: pass 2 -> 1, unverified 0 -> 1", out.splitlines())
        self.assertIn("Alpha", self.reads)  # the candidate's own main is read for the comparison

    def test_two_candidates_are_both_read(self):
        local = self.fake_local({"abc": PASSED_G3, "def": PASSED_G3})
        code, out = self.run_land("--candidate", "Alpha=/c@abc", "--candidate", "Beta=/d@def", local=local)
        self.assertEqual(code, 0, out)
        self.assertIn("  Alpha: pass 1 -> 2, unverified 1 -> 0", out.splitlines())
        self.assertIn("  Beta: pass 1 -> 2, unverified 1 -> 0", out.splitlines())

    def test_a_candidate_manifest_with_findings_is_reported_not_refused(self):
        bad = manifest(G1='status = "maybe"')  # a status outside the vocabulary
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": bad}))
        self.assertEqual(code, 0, out)
        self.assertIn("(Law's live check will show it; this does not refuse the landing)", out)
        self.assertIn("status 'maybe' is not one of", out)
        self.assertIn("  Alpha: leaves the state", out.splitlines())
        self.assertIn("1 findings", out)

    def test_bad_candidates_are_no_result(self):
        for args, why in ((["--candidate", "Gamma=/c@abc"], "not a release-train repository"),
                          (["--candidate", "Alpha=/c"], "needs ALICE-X=<clone dir>@<rev>"),
                          (["--candidate", "Alpha@abc"], "needs ALICE-X=<clone dir>@<rev>"),
                          (["--candidate", "Alpha=/c@abc", "--candidate", "Alpha=/d@abc"], "given twice"),
                          ([], "no --candidate given"),
                          (["--candidate", "Alpha=/c@nope"], "ref 'nope' not found")):
            with self.subTest(args=args):
                code, out = self.run_land(*args, local=self.fake_local({"abc": manifest()}))
                self.assertEqual(code, 2, out)
                self.assertIn(why, out)
                self.assertNotIn("state moves", out)

    def test_nothing_compared_is_not_a_pass(self):
        (self.root / "conformance" / "gates.toml").write_text('status_vocabulary = ["pass"]\n', encoding="utf-8")
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": manifest()}))
        self.assertEqual(code, 2, out)

    def test_no_manifest_read_is_not_a_pass(self):
        # every crate's manifest is missing as missing-manifests.toml expects: nothing is compared
        (self.root / "conformance" / "missing-manifests.toml").write_text(
            '[missing.alpha]\nfixed-in = "Alpha"\nwhy = "not yet written"\n'
            '[missing.beta]\nfixed-in = "Beta"\nwhy = "not yet written"\n', encoding="utf-8")
        self.mains = {"Alpha": None, "Beta": None}
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": None}))
        self.assertEqual(code, 2, out)
        self.assertIn("nothing was compared", out)

    def test_a_candidate_adding_a_line_reference_is_reported(self):
        more = manifest(G3='status = "unverified"\nnote = "see src/lib.rs:40"')
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": more}))
        self.assertEqual(code, 0, out)
        self.assertIn("Alpha: alice-law-conformance.toml has 2 line-number reference(s), the baseline allows 1", out)

    def test_a_candidate_removing_a_line_reference_is_a_note(self):
        fewer = manifest(G2='status = "fail-known"\nevidence = []\nnote = "raw max, no helper (src/lib.rs)"')
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": fewer}))
        self.assertEqual(code, 0, out)
        self.assertIn("note: slack: Alpha: baseline 1, count 0", out)
        self.assertNotIn("found:", out)

    def test_a_first_manifest_is_new_in_the_state_and_its_stale_missing_entry_is_reported(self):
        roster = ROSTER + '\n[crate.gamma]\nversion = "0.1.0"\nstate = "planned"\norder = 4\nrepo = "Gamma"\n'
        (self.root / "release-train.toml").write_text(roster, encoding="utf-8")
        (self.root / "conformance" / "missing-manifests.toml").write_text(
            '[missing.gamma]\nfixed-in = "Gamma"\nwhy = "not written yet"\n', encoding="utf-8")
        self.mains["Gamma"] = None
        code, out = self.run_land("--candidate", "Gamma=/g@abc", local=self.fake_local({"abc": manifest()}))
        self.assertEqual(code, 0, out)
        self.assertIn("  Gamma: new in the state", out.splitlines())
        self.assertIn("gamma: alice-law-conformance.toml is at main, so its missing-manifests.toml entry is stale", out)

    def test_a_real_clone_at_a_commit(self):
        repo = Path(self.tmp.name) / "alpha"

        def git(*a):
            return subprocess.run(["git", *a], cwd=repo, env=ENV, check=True, capture_output=True, text=True,
                                  encoding="utf-8").stdout
        repo.mkdir()
        git("init", "-q", "-b", "main")
        for p in PATHS:
            (repo / p).parent.mkdir(parents=True, exist_ok=True)
            (repo / p).write_text("x\n", encoding="utf-8")
        (repo / crc.MANIFEST).write_text(manifest(), encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "a")
        old = git("rev-parse", "HEAD").strip()
        (repo / crc.MANIFEST).write_text(PASSED_G3, encoding="utf-8")
        git("commit", "-q", "-am", "b")
        new = git("rev-parse", "HEAD").strip()
        for rev, verdict in ((old, "no"), (new, "yes")):
            with self.subTest(rev=rev[:7]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    code = lls.main(["--candidate", f"Alpha={repo}@{rev}"], remote=self.remote, root=self.root)
                self.assertEqual(code, 0, out.getvalue())
                self.assertIn(f"land_law_state: state moves: {verdict}", out.getvalue().splitlines())
                self.assertIn(f"Alpha@{rev[:12]}", out.getvalue())


if __name__ == "__main__":
    unittest.main()
