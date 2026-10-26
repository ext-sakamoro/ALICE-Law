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
from test_conformance_roster_check import GATES, PATHS, ROSTER, exact_line_baseline, manifest  # noqa: E402
from test_conformance_roster_check import poison_root  # noqa: E402

# this module's conformance_roster_check is not the one test_conformance_roster_check loads (it imports it by
# file), so the poisoned root is set here for this module's copy, the one land_law_state uses too; gen_law_status has
# its own root, poisoned the same way
_real_root, _real_gls_root = crc.ROOT, gls.ROOT


def setUpModule():
    assert lls.crc is crc and lls.gls is gls
    crc.ROOT = gls.ROOT = poison_root()


def tearDownModule():
    crc.ROOT, gls.ROOT = _real_root, _real_gls_root

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent"}
PASSED_G3 = manifest(G3='status = "pass"\nevidence = ["tests/golden.rs"]\nmutant = "drop the check"')


class LeakDetector(unittest.TestCase):
    def test_this_suite_runs_with_the_poisoned_root(self):
        # a call that leaves a path out must read the poisoned files, not this repository's
        self.assertEqual(crc.load_pending(crc.ROOT / "conformance" / "pending-state.toml"),
                         {"ALICE-Leak": {"sha": "0" * 40, "since": "2026-10-19"}})

    def test_a_land_call_without_root_reads_the_poisoned_root(self):
        # the default is bound at call time: the poisoned root has no release-train.toml, and the error names it
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = lls.main(["--today", "2026-10-19", "--candidate", "Alpha=/c@abc"], remote=lambda repo: (None, set()))
        self.assertNotEqual(code, 0, out.getvalue())
        self.assertIn(str(crc.ROOT / "release-train.toml"), out.getvalue())

    def test_a_status_call_without_root_reads_the_poisoned_root(self):
        # the poisoned root has no README.md; this repository's would pass --check
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = gls.main(["--check"])
        self.assertNotEqual(code, 0, out.getvalue())
        self.assertIn("README.md is missing", out.getvalue())


class LandBase(unittest.TestCase):
    """the Law root, the mains and the fakes the land tests share"""

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
        self.at_texts = {}
        self.history = {}
        # the baseline equals the mains' line-number references (manifest() holds one, G2's src/lib.rs:12)
        (self.root / "conformance" / "line-refs-baseline.toml").write_text(
            "# the baseline\n\n[baseline]\n" + "".join(f'"{r}" = {crc.line_refs(t)}\n' for r, t in sorted(self.mains.items())),
            encoding="utf-8")
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
            code = lls.main(list(args) if "--today" in args else ["--today", "2026-10-19", *args], remote=self.remote, local=local or self.fake_local({}), root=self.root,
                            resolve=lambda where, rev: (rev * 40)[:40], at=self.at)
        return code, out.getvalue()

    def state(self) -> str:
        return (self.root / "conformance" / "manifest-state.toml").read_text(encoding="utf-8")


class LandLawState(LandBase):
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
            code = crc.main(["--today", "2026-10-19", "--roster", str(self.root / "release-train.toml"),
                             "--gates", str(self.root / "conformance" / "gates.toml"),
                             "--missing", str(self.root / "conformance" / "missing-manifests.toml"),
                             "--state", str(self.root / "conformance" / "manifest-state.toml"),
                             "--pending", str(self.root / "conformance" / "pending-state.toml"),
                             "--line-baseline", exact_line_baseline(self.root, self.mains)], fetch=self.remote)
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
                             "--today", "2026-10-19", "--line-baseline",
                             exact_line_baseline(self.root, self.mains), *flags],
                            fetch=self.remote, at=self.at, history=lambda repo: self.history.get(repo, []))
        return code, out.getvalue()

    def pending(self) -> str | None:
        p = self.root / "conformance" / "pending-state.toml"
        return p.read_text(encoding="utf-8") if p.exists() else None

    def test_law_first_then_the_crate_stays_green_and_the_entry_is_dropped_after(self):
        sha = "abc" * 13 + "a"
        self.at_texts[("Alpha", sha)] = PASSED_G3
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
        code, out = self.live()
        self.assertEqual(code, 0, out)
        self.assertIn("so it has landed", out)
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

    def test_a_rebased_candidate_already_recorded_does_not_move(self):
        # Law recorded Alpha's ci commit (abc); the landing rebased it to another sha with the same manifest: the
        # recorded state already holds, so the landing gate must not ask for another Law change
        self.run_land("--candidate", "Alpha=/c@abc", "--write", local=self.fake_local({"abc": PASSED_G3}))
        before = self.pending()
        code, out = self.run_land("--candidate", "Alpha=/c@abd", local=self.fake_local({"abd": PASSED_G3}))
        self.assertEqual(code, 0, out)
        self.assertIn("state moves: no", out)
        # --write with no move leaves the recorded entry as it is
        self.run_land("--candidate", "Alpha=/c@abd", "--write", local=self.fake_local({"abd": PASSED_G3}))
        self.assertEqual(self.pending(), before)

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

    def baseline(self) -> str:
        return (self.root / "conformance" / "line-refs-baseline.toml").read_text(encoding="utf-8")

    def test_a_candidate_adding_a_line_reference_is_refused(self):
        added = manifest(G3='status = "unverified"\nnote = "see src/lib.rs#L40"')
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": added}))
        self.assertEqual(code, 2, out)
        self.assertIn("Alpha: alice-law-conformance.toml would hold 2 line-number reference(s), the baseline allows 1",
                      out)
        self.assertNotIn("state moves", out)

    def test_a_candidate_removing_a_line_reference_moves_nothing_and_the_next_landing_lowers_it(self):
        anchored = manifest(G2='status = "fail-known"\nevidence = []\nnote = "raw max at src/lib.rs#kernel"')
        code, out = self.run_land("--candidate", "Alpha=/c@abc", "--write", local=self.fake_local({"abc": anchored}))
        # the Law commit comes first: a lowered baseline would be below Alpha's main until it lands
        self.assertEqual(code, 0, out)
        self.assertIn("Alpha: the landing lowers its line-number references from 1 to 0", out)
        self.assertIn('"Alpha" = 1', self.baseline())
        # Alpha has landed; the next landing (of Beta) lowers Alpha's baseline
        self.mains["Alpha"] = anchored
        local = self.fake_local({"b1": manifest()})
        code, out = self.run_land("--candidate", "Beta=/b@b1", local=local)
        self.assertEqual(code, 1, out)
        self.assertIn("Alpha: line-reference baseline 1 -> 0", out)
        self.assertIn('"Alpha" = 1', self.baseline())  # not written without --write
        self.assertEqual(self.run_land("--candidate", "Beta=/b@b1", "--write", local=local)[0], 1)
        self.assertEqual(self.baseline(), '# the baseline\n\n[baseline]\n"Alpha" = 0\n"Beta" = 1\n')
        self.assertEqual(self.run_land("--candidate", "Beta=/b@b1", local=local)[0], 0)

    def test_a_main_below_its_baseline_is_lowered_by_any_landing(self):
        self.mains["Beta"] = manifest(G2='status = "fail-known"\nevidence = []\nnote = "raw max at src/lib.rs#k"')
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": manifest()}))
        self.assertEqual(code, 1, out)
        self.assertIn("Beta: line-reference baseline 1 -> 0", out)

    def test_live_holds_a_fresh_drop_as_a_note_and_an_old_one_or_strict_as_a_finding(self):
        import datetime
        lb = Path(exact_line_baseline(self.root, self.mains))
        self.mains["Beta"] = manifest(G2='status = "fail-known"\nevidence = []\nnote = "raw max at src/lib.rs#k"')
        self.at_texts[("Beta", "b2")] = self.mains["Beta"]
        self.at_texts[("Beta", "b1")] = manifest()
        self.history["Beta"] = [("b2", datetime.date(2026, 10, 17)), ("b1", datetime.date(2026, 10, 1))]

        def live(*flags):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = crc.main(["--today", "2026-10-19", "--roster", str(self.root / "release-train.toml"),
                                 "--gates", str(self.root / "conformance" / "gates.toml"),
                                 "--missing", str(self.root / "conformance" / "missing-manifests.toml"),
                                 "--state", str(self.root / "conformance" / "manifest-state.toml"),
                                 "--pending", str(self.root / "conformance" / "pending-state.toml"),
                                 "--line-baseline", str(lb), *flags],
                                fetch=self.remote, at=self.at, history=lambda repo: self.history.get(repo, []))
            return code, out.getvalue()
        code, out = live("--today", "2026-10-19")
        self.assertEqual(code, 0, out)
        self.assertIn("slack: Beta: baseline 1, count 0, below since 2026-10-17", out)
        self.assertEqual(live("--today", "2026-10-21")[0], 1)
        self.assertEqual(live("--today", "2026-10-19", "--strict")[0], 1)

    def test_a_real_clone_at_a_commit(self):
        repo = Path(self.tmp.name) / "alpha"

        def git(*a):
            return subprocess.run(["git", *a], cwd=repo, env=ENV, check=True, capture_output=True, text=True, encoding="utf-8").stdout
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
                    code = lls.main(["--today", "2026-10-19", "--candidate", f"Alpha={repo}@{rev}"], remote=self.remote, root=self.root,
                                    at=self.at)
                self.assertEqual(code, want, out.getvalue())



FIRST_ROSTER = ROSTER + '''
[crate.gamma]
version = "0.1.0"
state = "planned"
order = 4
repo = "Gamma"

[crate.delta]
version = "0.1.0"
state = "planned"
order = 5
repo = "Delta"
'''
FIRST = manifest(G2='status = "fail-known"\nevidence = []\nnote = "raw max, no helper (src/lib.rs)"')
FIRST_G3 = manifest(G2='status = "fail-known"\nevidence = []\nnote = "raw max, no helper (src/lib.rs)"',
                    G3='status = "pass"\nevidence = ["tests/golden.rs"]\nmutant = "drop the check"')
FIRST_MISSING = ("[missing.gamma]\nfixed-in = \"Gamma\"\nwhy = \"not written yet\"\n\n"
                 "[missing.delta]\nfixed-in = \"Delta\"\nwhy = \"not written yet\"\n")


class FirstManifest(LandBase):
    """two repositories that join the roster bring their first manifest: the state records each before it lands"""

    def setUp(self):
        super().setUp()
        (self.root / "release-train.toml").write_text(FIRST_ROSTER, encoding="utf-8")
        self.missing(FIRST_MISSING)
        self.mains.update({"Gamma": None, "Delta": None})
        self.local = self.fake_local({"e": FIRST, "d": FIRST_G3})
        self.at_texts.update({("Gamma", "e" * 40): FIRST, ("Delta", "d" * 40): FIRST_G3})

    def missing(self, text: str):
        (self.root / "conformance" / "missing-manifests.toml").write_text(text, encoding="utf-8")

    def pending_entries(self) -> dict:
        return crc.load_pending(self.root / "conformance" / "pending-state.toml")

    def roster_check(self, *extra: str):
        out = io.StringIO()
        texts = {r: t for r, t in self.mains.items()}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = crc.main(["--today", "2026-10-19", "--roster", str(self.root / "release-train.toml"),
                             "--gates", str(self.root / "conformance" / "gates.toml"),
                             "--missing", str(self.root / "conformance" / "missing-manifests.toml"),
                             "--state", str(self.root / "conformance" / "manifest-state.toml"),
                             "--pending", str(self.root / "conformance" / "pending-state.toml"),
                             "--line-baseline", exact_line_baseline(self.root, texts), *extra],
                            fetch=self.remote, at=self.at)
        return code, out.getvalue()

    def land_both(self, *extra: str):
        return self.run_land("--candidate", "Gamma=/c@e", "--candidate", "Delta=/c@d", *extra, local=self.local)

    def test_two_first_manifests_are_recorded_and_the_roster_check_accepts_them_before_they_land(self):
        self.missing("")  # the Law change that records them removes their missing-manifests entries
        code, out = self.land_both("--write")
        self.assertEqual(code, 1, out)
        self.assertIn("state moves: yes", out)
        self.assertIn('[manifest."Gamma"]\ncrates = ["gamma"]', self.state())
        self.assertIn('[manifest."Delta"]\ncrates = ["delta"]', self.state())
        self.assertEqual(set(self.pending_entries()), {"Gamma", "Delta"})
        self.assertEqual(self.pending_entries()["Delta"]["sha"], "d" * 40)
        # the mains still have no manifest: the roster check reads each at its pending commit
        code, out = self.roster_check()
        self.assertEqual(code, 0, out)
        self.assertIn("gamma: no alice-law-conformance.toml at main yet: pending-state.toml records its first manifest", out)
        self.assertIn("Delta: pending: the state records dddddddddddd (its first manifest)", out)
        # once both land, the entries have landed (a note, and the next --write drops them)
        self.mains.update({"Gamma": FIRST, "Delta": FIRST_G3})
        code, out = self.roster_check()
        self.assertEqual(code, 0, out)
        self.assertIn("Gamma: pending entry eeeeeeeeeeee: main's counts are the recorded ones, so it has landed", out)
        self.assertEqual(self.roster_check("--strict")[0], 1)

    def test_a_missing_manifest_without_its_entry_or_a_pending_record_is_a_finding(self):
        self.missing("")
        code, out = self.roster_check()
        self.assertEqual(code, 1, out)
        self.assertIn("gamma: no alice-law-conformance.toml at the main of Gamma", out)
        code, out = self.run_land("--candidate", "Alpha=/c@abc", local=self.fake_local({"abc": manifest()}))
        self.assertEqual(code, 2, out)
        self.assertIn("delta: no alice-law-conformance.toml at the main of Delta", out)

    def test_the_entry_is_removed_in_the_change_that_records_the_first_manifest(self):
        code, out = self.land_both()
        self.assertEqual(code, 2, out)
        self.assertIn("gamma: alice-law-conformance.toml is at main, so its missing-manifests.toml entry is stale", out)

    def test_a_recorded_first_manifest_stays_when_another_candidate_is_recorded(self):
        self.missing(FIRST_MISSING.split("\n\n")[1])  # delta's entry stays: only gamma is recorded now
        code, out = self.run_land("--candidate", "Gamma=/c@e", "--write", local=self.local)
        self.assertEqual(code, 1, out)
        self.assertEqual(set(self.pending_entries()), {"Gamma"})
        self.missing("")
        code, out = self.run_land("--candidate", "Delta=/c@d", "--write", local=self.local)
        self.assertEqual(code, 1, out)
        self.assertEqual(set(self.pending_entries()), {"Gamma", "Delta"})
        self.assertIn('[manifest."Gamma"]', self.state())
        self.assertEqual(self.roster_check()[0], 0)

    def test_a_first_manifest_the_state_does_not_match_is_a_finding(self):
        self.missing("")
        self.assertEqual(self.land_both("--write")[0], 1)
        self.at_texts[("Delta", "d" * 40)] = FIRST  # the recorded counts are no longer the commit's
        code, out = self.roster_check()
        self.assertEqual(code, 1, out)
        self.assertIn("Delta: the state records counts that are neither main's nor those of pending entry", out)


if __name__ == "__main__":
    unittest.main()
