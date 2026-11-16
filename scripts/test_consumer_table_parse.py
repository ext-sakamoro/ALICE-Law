#!/usr/bin/env python3
"""Tests of scripts/consumer_table_parse.py (python3 scripts/test_consumer_table_parse.py)

Two small consumers stand for the crates' scripts/test_shared_files.py: STRICT reads only rows with a
repos line (as the copies before the own rows did), OWN_AWARE also reads own rows. Each fetches the
pinned revision as the real one does, so the probe's url rewrite is exercised; no test reaches the
network. The real target the table plans (conformance/planned/<blob>) is run on the real table too."""
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import consumer_table_parse as ctp  # noqa: E402
import shared_files_check as sfc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

CONSUMER = '''import hashlib, re, subprocess, tempfile, unittest
from pathlib import Path
PIN = Path(__file__).resolve().parent.parent / ".github/shared-files-pin"
def pv(k):
    return [l.split()[1] for l in PIN.read_text(encoding="utf-8").splitlines() if l.startswith(k + " ")][0]
class SharedFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as t:
            subprocess.run(["git", "init", "-q", t], check=True)
            subprocess.run(["git", "-C", t, "fetch", "-q", "--depth", "1",
                            "https://github.com/ext-sakamoro/ALICE-Law", pv("rev")], check=True)
            data = subprocess.run(["git", "-C", t, "show", "FETCH_HEAD:conformance/shared-files.toml"],
                                  check=True, capture_output=True).stdout
        if hashlib.sha256(data).hexdigest() != pv("table-sha256"):
            raise RuntimeError("not the pinned table")
        for b in data.decode("utf-8").split("[[file]]")[1:]:
            if not re.search(r"^repos = \\[", b, re.M) and not (OWN and re.search(r"^own = ", b, re.M)):
                raise RuntimeError("a table row without a repos line")
'''
STRICT = ("OWN = False\n" + CONSUMER).encode()
OWN_AWARE = ("OWN = True\n" + CONSUMER).encode()

SHARED_ROW = b'[[file]]\npath = "scripts/a.py"\nsha256 = "' + b"a" * 64 + b'"\nrepos = ["ALICE-X", "ALICE-Y"]\n'
OWN_ROW = b'\n[[file]]\npath = ".github/workflows/ci.yml"\nown = "each repository\'s own"\n'


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Reads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def work(self) -> Path:
        self.n += 1
        return Path(self.tmp.name) / f"w{self.n}"

    def check(self, table, base, target, held_bytes, moving=None):
        """check() with versions given as {bytes: [holders]} and the target given by its bytes; the row moves from
        every non-target version unless `moving` (bytes) says otherwise"""
        held = {sha(b): repos for b, repos in held_bytes.items()}
        by = {sha(b): b for b in held_bytes}
        mv = frozenset(sha(b) for b in moving) if moving is not None else frozenset(set(held) - {sha(target)})
        return ctp.check(table, base, sha(target), held, lambda s: by.get(s, "no holder"), self.work(), mv)

    def test_a_strict_consumer_reads_a_table_without_own_rows(self):
        self.assertEqual(ctp.reads(STRICT, SHARED_ROW, self.work())[0], True)

    def test_a_strict_consumer_cannot_read_an_own_row(self):
        ok, last = ctp.reads(STRICT, SHARED_ROW + OWN_ROW, self.work())
        self.assertFalse(ok)
        self.assertIn("without a repos line", last)

    def test_an_own_aware_target_reads_the_table(self):
        findings, unreadable, n = self.check(SHARED_ROW + OWN_ROW, SHARED_ROW, OWN_AWARE, {OWN_AWARE: ["ALICE-X"]})
        self.assertEqual((findings, unreadable, n), ([], [], 1))

    def test_a_target_that_cannot_read_the_table_is_a_finding(self):
        findings, _, n = self.check(SHARED_ROW + OWN_ROW, SHARED_ROW + OWN_ROW, STRICT, {STRICT: ["ALICE-X"]})
        self.assertEqual(n, 1)
        self.assertEqual(len(findings), 1)
        self.assertIn("is the row's target", findings[0])

    def test_an_own_row_planted_under_a_held_strict_version_is_a_finding(self):
        # the base table had no own row (the held version read it): the change breaks that version
        findings, _, n = self.check(SHARED_ROW + OWN_ROW, SHARED_ROW, OWN_AWARE,
                                    {OWN_AWARE: ["ALICE-X"], STRICT: ["ALICE-Y"]})
        self.assertEqual(n, 2)
        self.assertEqual(len(findings), 1)
        self.assertIn("reads the base table and not this one", findings[0])
        self.assertIn("ALICE-Y", findings[0])

    def test_a_held_version_already_broken_at_the_base_is_moving_not_a_finding(self):
        findings, _, n = self.check(SHARED_ROW + OWN_ROW, SHARED_ROW + OWN_ROW, OWN_AWARE,
                                    {OWN_AWARE: ["ALICE-X"], STRICT: ["ALICE-Y"]})
        self.assertEqual((findings, n), ([], 2))

    def test_a_version_broken_at_the_base_that_the_row_does_not_move_from_is_a_finding(self):
        findings, _, n = self.check(SHARED_ROW + OWN_ROW, SHARED_ROW + OWN_ROW, OWN_AWARE,
                                    {OWN_AWARE: ["ALICE-X"], STRICT: ["ALICE-Y"]}, moving=[])
        self.assertEqual(n, 2)
        self.assertEqual(len(findings), 1)
        self.assertIn("does not move from it", findings[0])

    def test_without_a_base_every_held_version_must_read_the_table(self):
        findings, _, _ = self.check(SHARED_ROW + OWN_ROW, None, OWN_AWARE, {OWN_AWARE: ["ALICE-X"], STRICT: ["ALICE-Y"]})
        self.assertEqual(len(findings), 1)

    def test_bytes_that_cannot_be_read_are_reported_not_passed(self):
        held = {sha(OWN_AWARE): ["ALICE-X"]}
        findings, unreadable, n = ctp.check(SHARED_ROW, None, sha(OWN_AWARE), held, lambda s: "fetch failed",
                                            self.work())
        self.assertEqual((findings, n), ([], 0))
        self.assertIn("fetch failed", unreadable[0])

    def test_bytes_with_another_sha256_are_reported_not_run(self):
        held = {sha(OWN_AWARE): ["ALICE-X"]}
        _, unreadable, n = ctp.check(SHARED_ROW, None, sha(OWN_AWARE), held, lambda s: STRICT, self.work())
        self.assertEqual(n, 0)
        self.assertIn("the bytes read have SHA-256", unreadable[0])

    def test_the_planned_target_reads_this_repository_s_table(self):
        # the real bytes the table plans for the consumer, on the real table
        table = (ROOT / "conformance/shared-files.toml").read_bytes()
        entry = next(e for e in sfc.load(table.decode("utf-8")) if e["path"] == ctp.CONSUMER)
        if not sfc.is_planned(entry):
            self.skipTest("the consumer's row plans no bytes at this revision")
        data = (ROOT / "conformance" / sfc.PLANNED_DIR / entry["pending"]["planned"]).read_bytes()
        self.assertEqual(sha(data), entry["pending"]["target"])
        ok, last = ctp.reads(data, table, self.work())
        self.assertTrue(ok, last)


class Versions(unittest.TestCase):
    def test_a_uniform_row_is_its_bytes(self):
        e = {"path": ctp.CONSUMER, "sha256": "s" * 64, "source": {"repo": "ALICE-X", "rev": "r" * 40}}
        self.assertEqual(ctp.versions(e), ("s" * 64, {"s" * 64: ["ALICE-X"]}))

    def test_a_pending_row_is_its_target_and_held_variants(self):
        e = {"path": ctp.CONSUMER, "pending": {"target": "t" * 64, "owner": "ALICE-X",
                                               "variants": {"v" * 64: ["ALICE-Y"], "absent": ["ALICE-Z"]}}}
        target, held = ctp.versions(e)
        self.assertEqual(target, "t" * 64)
        self.assertEqual(held, {"v" * 64: ["ALICE-Y"], "t" * 64: ["ALICE-X"]})


class MovingFrom(unittest.TestCase):
    def test_a_uniform_row_moves_from_nothing(self):
        self.assertEqual(ctp.moving_from({"sha256": "s" * 64, "source": {"repo": "ALICE-X"}}), frozenset())

    def test_a_pending_row_moves_from_its_stale_variants_and_moved_from(self):
        e = {"pending": {"target": "t" * 64, "owner": "ALICE-X", "moved_from": {"planned": "p" * 40, "target": "m" * 64},
                         "variants": {"t" * 64: ["ALICE-X"], "v" * 64: ["ALICE-Y"], "absent": ["ALICE-Z"]}}}
        self.assertEqual(ctp.moving_from(e), frozenset({"v" * 64, "m" * 64}))


class Main(unittest.TestCase):
    def test_no_version_run_is_compared_nothing(self):
        # check() of an empty set of versions runs none: that is exit 2, never a pass
        findings, unreadable, n = ctp.check(SHARED_ROW, None, "t" * 64, {}, lambda s: "unused", Path("unused"))
        self.assertEqual((findings, unreadable, n), ([], [], 0))
        self.assertEqual(ctp.verdict(findings, unreadable, n), 2)

    def test_the_exit_codes(self):
        self.assertEqual(ctp.verdict([], [], 1), 0)
        self.assertEqual(ctp.verdict(["f"], [], 1), 1)
        self.assertEqual(ctp.verdict(["f"], ["u"], 1), 2)

    def test_a_table_without_the_consumer_s_row_compares_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp) / "shared-files.toml"
            t.write_bytes(b'[[file]]\npath = "scripts/a.py"\nsha256 = "' + b"a" * 64
                          + b'"\nsource = { repo = "ALICE-X", rev = "' + b"0" * 40 + b'" }\nrepos = ["ALICE-X", "ALICE-Y"]\n')
            r = subprocess.run([sys.executable, str(ROOT / "scripts/consumer_table_parse.py"), "--table", str(t)],
                               capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("compared nothing", r.stderr)


if __name__ == "__main__":
    unittest.main()
