#!/usr/bin/env python3
"""Tests of scripts/line_refs_ratchet.py and the roster's line-number reference counts
(python3 scripts/test_line_refs_ratchet.py)."""
from __future__ import annotations

import contextlib
import datetime
import io
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import conformance_roster_check as crc  # noqa: E402
import line_refs_ratchet as lrr  # noqa: E402


def baseline(**counts: int) -> str:
    return "[baseline]\n" + "".join(f'"ALICE-{k}" = {v}\n' for k, v in counts.items())


class Ratchet(unittest.TestCase):
    def run_ratchet(self, new: str | None, old: str | None) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as d:
            n, o = Path(d) / "new.toml", Path(d) / "old.toml"
            if new is not None:
                n.write_text(new, encoding="utf-8")
            if old is not None:
                o.write_text(old, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = lrr.main(["--baseline", str(n), "--base", str(o)])
            return code, out.getvalue()

    def test_an_unchanged_or_lowered_baseline_passes(self):
        for new in (baseline(A=5, B=0), baseline(A=3, B=0)):
            with self.subTest(new=new):
                code, out = self.run_ratchet(new, baseline(A=5, B=0))
                self.assertEqual(code, 0, out)
                self.assertIn("compared 2, 0 finding(s)", out)

    def test_a_raised_baseline_is_refused(self):
        code, out = self.run_ratchet(baseline(A=6, B=0), baseline(A=5, B=0))
        self.assertEqual(code, 1, out)
        self.assertIn("ALICE-A: the baseline rose from 5 to 6", out)

    def test_a_new_entry_starts_at_zero(self):
        self.assertEqual(self.run_ratchet(baseline(A=5, C=0), baseline(A=5))[0], 0)
        code, out = self.run_ratchet(baseline(A=5, C=2), baseline(A=5))
        self.assertEqual(code, 1, out)
        self.assertIn("a new baseline entry must start at 0, not 2", out)

    def test_the_file_is_never_removed(self):
        for old in (baseline(A=1, B=0), baseline(A=0, B=0)):
            with self.subTest(old=old):
                code, out = self.run_ratchet(None, old)
                self.assertEqual(code, 1, out)
                self.assertIn("the baseline file is removed: keep it", out)

    def test_the_file_is_introduced_when_main_has_none(self):
        code, out = self.run_ratchet(baseline(A=100, B=0), None)
        self.assertEqual(code, 0, out)
        self.assertIn("the baseline file is introduced (2 entries)", out)
        self.assertEqual(self.run_ratchet(None, None)[0], 2)

    def test_bad_or_empty_input_is_not_a_pass(self):
        self.assertEqual(self.run_ratchet(baseline(A=1), "[baseline]\n")[0], 2)
        self.assertEqual(self.run_ratchet('[baseline]\n"ALICE-A" = -1\n', baseline(A=1))[0], 2)
        self.assertEqual(self.run_ratchet('[other]\n', baseline(A=1))[0], 2)


class Counts(unittest.TestCase):
    MANIFEST = ('[G1]\nstatus = "pass"\nevidence = ["src/lib.rs:12 the kernel", "tests/a.rs (lines 3-9)"]\n'
                'note = "see line 40 and src/x.rs#kernel"\n[G2]\nstatus = "n/a"\nmutant = "anchors only: src/x.rs#f"\n')

    def test_the_line_number_forms_are_counted_in_the_gate_tables(self):
        self.assertEqual(crc.line_refs(self.MANIFEST), 3)  # src/lib.rs:12, (lines 3-9), line 40
        self.assertEqual(crc.line_refs('[meta]\nnote = "line 5"\n[G1]\nnote = "src/x.rs#f"\n'), 0)

    def test_exactly_the_baseline_passes_and_above_or_below_is_a_finding(self):
        texts = {"ALICE-A": self.MANIFEST, "ALICE-DB": '[G1]\nnote = "src/x.rs#f"\n'}
        f, notes = crc.line_ref_findings(texts, {"ALICE-A": 3, "ALICE-DB": 0})
        self.assertEqual(f, [])
        self.assertIn("ALICE-DB: 0 line-number reference(s) in alice-law-conformance.toml, baseline 0", notes)
        f, _ = crc.line_ref_findings(texts, {"ALICE-A": 2, "ALICE-DB": 0})
        self.assertIn("ALICE-A: alice-law-conformance.toml has 3 line-number reference(s), the baseline allows 2: "
                      "name the place by an anchor `path#token` (scripts/line_refs_propose.py proposes one)", f)
        f, _ = crc.line_ref_findings(texts, {"ALICE-A": 4, "ALICE-DB": 0})
        self.assertIn("ALICE-A: alice-law-conformance.toml has 3 line-number reference(s), below its baseline 4: "
                      "lower the baseline in conformance/line-refs-baseline.toml to 3", f)

    def test_the_github_anchor_any_extension_and_a_path_then_l_form_count(self):
        for text in ("src/foo.rs#L42", "src/foo.rs#L3-L9", "src/foo.rs#L3-9", "src/foo.rs L42",
                     "shaders/sdf.wgsl:12", "include/alice.h:30", "bindings/x.ts:9", "ci.yaml:40", "src/lib.c:5",
                     "Cargo.lock:12", "src/foo.rs:42:7", "(src/a.rs, line 12)"):
            with self.subTest(text=text):
                self.assertEqual(crc.line_refs(f'[G1]\nnote = "{text}"\n'), 1)

    def test_versions_dates_ratios_ports_and_anchors_do_not_count(self):
        for text in ("0.4.2", "2026-10-20", "a 1.5:1 ratio", "https://example.org:8443/x", "src/x.rs#kernel",
                     "L2 norm", "the L1 cache", "f64::sin", "std::f32::consts"):
            with self.subTest(text=text):
                self.assertEqual(crc.line_refs(f'[G1]\nnote = "{text}"\n'), 0)

    def test_below_the_baseline_is_a_note_while_fresh_and_a_finding_after_or_under_strict(self):
        texts = {"ALICE-A": self.MANIFEST}
        today = datetime.date(2026, 10, 19)
        calls = []

        def since(days):
            def f(repo, allowed):
                calls.append((repo, allowed))
                return None if days is None else today - datetime.timedelta(days=days)
            return f
        f, notes = crc.line_ref_findings(texts, {"ALICE-A": 5}, False, since(3), today)
        self.assertEqual(f, [])
        self.assertIn("slack: ALICE-A: baseline 5, count 3, below since 2026-10-16 (a finding after 3 days and "
                      "under --strict)", notes)
        self.assertEqual(calls, [("ALICE-A", 5)])
        f, _ = crc.line_ref_findings(texts, {"ALICE-A": 5}, False, since(4), today)
        self.assertEqual(f, ["ALICE-A: alice-law-conformance.toml has 3 line-number reference(s), below its baseline 5: "
                             "lower the baseline in conformance/line-refs-baseline.toml to 3 (below since 2026-10-15, "
                             "4 days)"])
        f, _ = crc.line_ref_findings(texts, {"ALICE-A": 5}, False, since(None), today)
        self.assertIn("(the date it went below is not known)", f[0])
        f, _ = crc.line_ref_findings(texts, {"ALICE-A": 5}, True, since(0), today)
        self.assertEqual(len(f), 1)
        # above the baseline is a finding whatever the age
        f, _ = crc.line_ref_findings(texts, {"ALICE-A": 2}, False, since(0), today)
        self.assertEqual(len(f), 1)

    def test_below_since_walks_the_manifest_history_to_the_run_below(self):
        hist = [("c3", datetime.date(2026, 10, 18)), ("c2", datetime.date(2026, 10, 17)),
                ("c1", datetime.date(2026, 10, 10))]
        texts = {"c3": '[G1]\nnote = "a.rs:1"\n', "c2": '[G1]\nnote = "a.rs:1 b.rs:2"\n',
                 "c1": '[G1]\nnote = "a.rs:1 b.rs:2 c.rs:3"\n'}
        at = lambda repo, sha: (texts[sha], set())  # noqa: E731
        bs = crc.below_since_from(lambda repo: hist, at)
        self.assertEqual(bs("A", 3), datetime.date(2026, 10, 17))  # c2 went below 3, c1 held 3
        self.assertEqual(bs("A", 2), datetime.date(2026, 10, 18))
        self.assertIsNone(bs("A", 4))  # below through the whole history read: unknown, so a finding

    def test_no_baseline_file_bans_every_reference_and_a_missing_entry_is_zero(self):
        f, _ = crc.line_ref_findings({"ALICE-A": self.MANIFEST}, None)
        self.assertEqual(len(f), 1)
        self.assertIn("has 3 line-number reference(s), the baseline allows 0", f[0])
        f, _ = crc.line_ref_findings({"ALICE-A": self.MANIFEST}, {})
        self.assertIn("the baseline allows 0", f[0])

    def test_a_baseline_entry_with_no_manifest_is_a_finding(self):
        f, _ = crc.line_ref_findings({}, {"ALICE-Gone": 0})
        self.assertIn("ALICE-Gone: in conformance/line-refs-baseline.toml but no manifest was read for it", f)

    def test_the_repository_baseline_reads(self):
        got = crc.load_line_baseline(HERE.parent / "conformance" / "line-refs-baseline.toml")
        self.assertTrue(got, "the committed baseline file has entries")


if __name__ == "__main__":
    unittest.main()
