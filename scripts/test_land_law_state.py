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
import gen_law_status as gls  # noqa: E402
import land_law_state as lls  # noqa: E402
from test_conformance_roster_check import GATES, PATHS, ROSTER, manifest  # noqa: E402

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent"}
PASSED_G3 = manifest(G3='status = "pass"\nevidence = ["tests/golden.rs"]\nmutant = "drop the check"')


class LandLawState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "law"
        (self.root / "conformance").mkdir(parents=True)
        (self.root / "e2e" / "golden").mkdir(parents=True)
        (self.root / "release-train.toml").write_text(ROSTER, encoding="utf-8")
        (self.root / "conformance" / "gates.toml").write_text(GATES, encoding="utf-8")
        (self.root / "e2e" / "golden" / "run.digest").write_text("0.5.1 " + "0" * 64 + "\n", encoding="utf-8")
        for name in gls.READMES.values():
            (self.root / name).write_text(f"# t\n\n{gls.BEGIN}\n{gls.END}\n", encoding="utf-8")
        self.mains = {"Alpha": manifest(), "Beta": manifest()}
        self.at_texts, self.statuses = {}, {}
        # the committed state is what the mains say
        states = crc.check(crc.load_toml(self.root / "release-train.toml"),
                           crc.load_toml(self.root / "conformance" / "gates.toml"), {}, self.remote)[2]
        (self.root / "conformance" / "manifest-state.toml").write_text(crc.render_state(states), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gls.main([], root=self.root), 0)

    def tearDown(self):
        self.tmp.cleanup()

    def at(self, repo, sha):
        return self.at_texts[(repo, sha)], set(PATHS)

    def compare(self, repo, sha):
        return self.statuses[(repo, sha)]

    def remote(self, repo):
        self.assertNotEqual(repo, "Law")  # an exempt crate is not read
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

    def run_land(self, *args: str, local=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = lls.main(list(args), remote=self.remote, local=local or self.fake_local({}), root=self.root,
                            resolve=lambda where, rev: (rev * 40)[:40], at=self.at, compare=self.compare)
        return code, out.getvalue()

    def state(self) -> str:
        return (self.root / "conformance" / "manifest-state.toml").read_text(encoding="utf-8")

    def test_a_candidate_with_the_same_statuses_does_not_move_the_state(self):
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": manifest()}))
        self.assertEqual(code, 0, out)
        self.assertIn("state moves: no", out)
        self.assertIn("2 manifests read for 2 crates, 6 gates compared (candidates: Alpha)", out)

    def test_a_candidate_that_flips_a_gate_moves_the_state_and_names_the_counts(self):
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": PASSED_G3}))
        self.assertEqual(code, 1, out)
        self.assertIn("state moves: yes", out)
        self.assertIn("Alpha: pass 1 -> 2, unverified 1 -> 0", out)
        self.assertNotIn("Beta:", out)

    def test_the_candidate_is_read_at_its_rev_not_at_main(self):
        # main and the table have the flip; the candidate takes it back: the state follows the candidate's rev
        self.mains["Alpha"] = PASSED_G3
        states = crc.check(crc.load_toml(self.root / "release-train.toml"),
                           crc.load_toml(self.root / "conformance" / "gates.toml"), {}, self.remote)[2]
        (self.root / "conformance" / "manifest-state.toml").write_text(crc.render_state(states), encoding="utf-8")
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": manifest()}))
        self.assertEqual(code, 1, out)
        self.assertIn("Alpha: pass 2 -> 1, unverified 0 -> 1", out)

    def test_write_records_the_state_and_the_readme_blocks_the_landing_makes_true(self):
        before = (self.root / "README.md").read_text(encoding="utf-8")
        local = self.fake_local({"abc": PASSED_G3})
        code, out = self.run_land("--candidate", "Alpha=/c@abc", "--write", local=local)
        self.assertEqual(code, 1, out)
        self.assertIn('[manifest."Alpha"]\ncrates = ["alpha"]\n"pass" = 2\n"fail-known" = 1\n"unverified" = 0', self.state())
        self.assertNotEqual((self.root / "README.md").read_text(encoding="utf-8"), before)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gls.main(["--check"], root=self.root), 0)
        # once written, the same candidate moves nothing
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=local)
        self.assertEqual(code, 0, out)
        # and the written state is the roster check's own output for those manifests
        self.mains["Alpha"] = PASSED_G3
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(out2):
            code = crc.main(["--roster", str(self.root / "release-train.toml"),
                             "--gates", str(self.root / "conformance" / "gates.toml"),
                             "--missing", str(self.root / "conformance" / "missing-manifests.toml"),
                             "--state", str(self.root / "conformance" / "manifest-state.toml")], fetch=self.remote)
        self.assertEqual(code, 0, out2.getvalue())

    def test_write_without_a_move_leaves_the_files_alone(self):
        before = self.state()
        code, out = self.run_land("--candidate", "Alpha=/c@abc", "--write", local=self.fake_local({"abc": manifest()}))
        self.assertEqual(code, 0, out)
        self.assertNotIn("wrote", out)
        self.assertEqual(self.state(), before)

    def test_two_candidates_are_both_read(self):
        local = self.fake_local({"a1": PASSED_G3, "b1": PASSED_G3})
        code, out = self.run_land("--candidate", "Alpha=/a@a1", "--candidate", "Beta=/b@b1", local=local)
        self.assertEqual(code, 1, out)
        self.assertIn("Alpha: pass 1 -> 2", out)
        self.assertIn("Beta: pass 1 -> 2", out)
        self.assertIn("(candidates: Alpha, Beta)", out)

    def test_a_candidate_manifest_with_findings_is_not_computed(self):
        bad = manifest(G1='status = "maybe"')  # a status outside the vocabulary
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": bad}))
        self.assertEqual(code, 2, out)
        self.assertIn("state not computed", out)
        self.assertNotIn("state moves", out)

    def test_bad_candidates_are_refused(self):
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
        self.assertIn("nothing was compared (0 manifests, 0 gates)", out)
        self.assertNotIn("state moves", out)

    def live(self, *flags: str) -> tuple[int, str]:
        """this repository's live check of the committed state against the fake mains"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = crc.main(["--roster", str(self.root / "release-train.toml"),
                             "--gates", str(self.root / "conformance" / "gates.toml"),
                             "--missing", str(self.root / "conformance" / "missing-manifests.toml"),
                             "--state", str(self.root / "conformance" / "manifest-state.toml"),
                             "--pending", str(self.root / "conformance" / "pending-state.toml"),
                             "--today", "2026-10-19", *flags],
                            fetch=self.remote, at=self.at, compare=self.compare)
        return code, out.getvalue()

    def pending(self) -> str | None:
        p = self.root / "conformance" / "pending-state.toml"
        return p.read_text(encoding="utf-8") if p.exists() else None

    def test_law_first_then_the_crate_stays_green_and_the_entry_is_dropped_after(self):
        sha = "abc" * 13 + "a"
        self.at_texts[("Alpha", sha)] = PASSED_G3
        self.statuses[("Alpha", sha)] = "ahead"
        local = self.fake_local({"abc": PASSED_G3})
        code, out = self.run_land("--candidate", "Alpha=/c@abc", "--write", "--today", "2026-10-18", local=local)
        self.assertEqual(code, 1, out)
        self.assertIn(f"Alpha: pending -> {sha[:12]} (since 2026-10-18)", out)
        self.assertIn(f'[pending."Alpha"]\nsha = "{sha}"\nsince = "2026-10-18"', self.pending())
        # Law has the new state; Alpha's main has not moved yet: green, the difference explained
        code, out = self.live()
        self.assertEqual(code, 0, out)
        self.assertIn(f"Alpha: pending: the state records {sha[:12]}, not landed yet", out)
        # Alpha lands: green in live (a note), a finding under --strict, and the next Law change drops it
        self.mains["Alpha"] = PASSED_G3
        self.statuses[("Alpha", sha)] = "identical"
        code, out = self.live()
        self.assertEqual(code, 0, out)
        self.assertIn("has landed on main", out)
        self.assertEqual(self.live("--strict")[0], 1)
        code, out = self.run_land("--candidate", "Beta=/b@b1", local=self.fake_local({"b1": manifest()}))
        self.assertEqual(code, 1, out)
        self.assertIn(f"Alpha: pending {sha[:12]} -> dropped (landed)", out)
        self.run_land("--candidate", "Beta=/b@b1", "--write", local=self.fake_local({"b1": manifest()}))
        self.assertNotIn("Alpha", self.pending())
        code, out = self.live("--strict")
        self.assertEqual(code, 0, out)

    def test_a_stale_table_moves_even_when_no_entry_changes(self):
        # Beta's main flipped after the last state commit; the candidate (Alpha) moves nothing itself
        self.mains["Beta"] = PASSED_G3
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": manifest()}))
        self.assertEqual(code, 1, out)
        self.assertIn("Beta: pass 1 -> 2, unverified 1 -> 0", out)
        self.assertNotIn("pending", out)

    def test_a_candidate_that_does_not_move_records_no_entry(self):
        self.run_land("--candidate", "Alpha=/c@abc", "--write", local=self.fake_local({"abc": manifest()}))
        self.assertIsNone(self.pending())

    def test_a_re_rolled_candidate_keeps_the_date_its_entry_was_recorded(self):
        self.run_land("--candidate", "Alpha=/c@abc", "--write", "--today", "2026-10-17",
                      local=self.fake_local({"abc": PASSED_G3}))
        both = manifest(G2='status = "pass"\nevidence = ["tests/golden.rs"]\nmutant = "m"',
                        G3='status = "pass"\nevidence = ["tests/golden.rs"]\nmutant = "drop the check"')
        code, out = self.run_land("--candidate", "Alpha=/c@abd", "--write", "--today", "2026-10-19",
                                  local=self.fake_local({"abd": both}))
        self.assertEqual(code, 1, out)
        self.assertIn('sha = "' + ("abd" * 14)[:40] + '"\nsince = "2026-10-17"', self.pending())

    def test_a_real_clone_at_a_commit(self):
        repo = Path(self.tmp.name) / "alpha"

        def git(*a):
            return subprocess.run(["git", *a], cwd=repo, env=ENV, check=True, capture_output=True, text=True).stdout
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
        out = io.StringIO()
        for rev, want in ((old, 0), (new, 1)):
            with self.subTest(rev=rev[:7]):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    code = lls.main(["--candidate", f"Alpha={repo}@{rev}"], remote=self.remote, root=self.root,
                                    at=self.at, compare=self.compare)
                self.assertEqual(code, want, out.getvalue())


if __name__ == "__main__":
    unittest.main()
