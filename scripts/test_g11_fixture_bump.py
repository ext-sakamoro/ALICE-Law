#!/usr/bin/env python3
"""Tests of scripts/g11_fixture_bump.py: a holder's clone moved to an ALICE-Law checkout's revision writes what the
holder's own bump commit writes (pins, manifest names, copies at their targets), and a fixture that cannot be built
exits 2."""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import importlib.util

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("g11fb", HERE / "g11_fixture_bump.py")
fb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fb)

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
OLD = "fb59f51a148b507e1af6c4126afc403201e26347"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          encoding="utf-8", env=ENV).stdout.strip()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def blob(b: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


class RenameRev(unittest.TestCase):
    def test_every_prefix_length_is_renamed_with_as_many_digits(self):
        new = "4501108d4e67484ba18183b410c09a543c323904"
        text = f"a ALICE-Law {OLD[:7]} b ALICE-Law {OLD[:8]} c ALICE-Law {OLD} d"
        out, n = fb.rename_rev(text, OLD, new)
        self.assertEqual(out, f"a ALICE-Law {new[:7]} b ALICE-Law {new[:8]} c ALICE-Law {new} d")
        self.assertEqual(n, 3)

    def test_an_eight_digit_name_leaves_no_stray_digit(self):
        # the form a 7-digit text replacement got wrong (fb59f51a -> 4501108a)
        out, _ = fb.rename_rev(f"ALICE-Law {OLD[:8]}'s table", OLD, "4501108d" + "0" * 32)
        self.assertEqual(out, "ALICE-Law 4501108d's table")

    def test_a_line_naming_another_pin_keeps_its_revision(self):
        # a note about .github/public-text-pin may name the same revision as the old shared-files pin: that pin does
        # not move, so its note does not either (2026-11-28 Physics: a blanket replace renamed it)
        text = (f"a: .github/public-text-pin pins ALICE-Law {OLD[:8]}\n"
                f"b: .github/shared-files-pin (ALICE-Law {OLD[:7]})\nc: ALICE-Law {OLD[:7]}\n")
        out, n = fb.rename_rev(text, OLD, "1" * 40)
        self.assertEqual(out, f"a: .github/public-text-pin pins ALICE-Law {OLD[:8]}\n"
                              f"b: .github/shared-files-pin (ALICE-Law 1111111)\nc: ALICE-Law 1111111\n")
        self.assertEqual(n, 2)

    def test_another_revision_and_a_short_prefix_are_left_alone(self):
        text = f"ALICE-Law 0e8c14e and ALICE-Law {OLD[:6]}"
        self.assertEqual(fb.rename_rev(text, OLD, "1" * 40), (text, 0))


class Bump(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.law, self.holder, self.src = root / "law", root / "holder", root / "src" / "ALICE-Src"
        for d in (self.law / "scripts", self.law / "conformance" / "planned", self.holder / ".github",
                  self.holder / "scripts", self.src / "scripts"):
            d.mkdir(parents=True)
        self.v1, self.v2, self.uni = b"print('v1')\n", b"print('v2')\n", b"print('uniform')\n"
        # the source repository of the uniform row
        (self.src / "scripts" / "uni.py").write_bytes(self.uni)
        git(self.src.parent, "init", "-q", str(self.src))
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "s")
        self.src_rev = git(self.src, "rev-parse", "HEAD")
        (self.law / "scripts" / "shared_files_check.py").write_text("print('checker')\n", encoding="utf-8")
        (self.law / "scripts" / "g11_table_check.py").write_text("print('g11')\n", encoding="utf-8")
        (self.law / "conformance" / "planned" / blob(self.v2)).write_bytes(self.v2)
        self.table(holder_in_uniform=True)
        git(self.law.parent, "init", "-q", str(self.law))
        self.commit_law()
        (self.holder / ".github" / "shared-files-pin").write_text(
            f"name ALICE-H\nrev {OLD}\ntable-sha256 {'0' * 64}\nchecker-sha256 {'0' * 64}\n", encoding="utf-8")
        (self.holder / ".github" / "g11-table-pin").write_text(
            f"name ALICE-H\nrev {OLD}\nchecker-sha256 {'0' * 64}\n", encoding="utf-8")
        (self.holder / "alice-law-conformance.toml").write_text(
            f'note = "pinned at ALICE-Law {OLD[:8]}, older ALICE-Law 0e8c14e"\n', encoding="utf-8")
        (self.holder / "scripts" / "moving.py").write_bytes(self.v1)
        (self.holder / "scripts" / "own.sh").write_bytes(b"own\n")
        git(self.holder.parent, "init", "-q", str(self.holder))
        git(self.holder, "add", "-A")
        git(self.holder, "commit", "-q", "-m", "h")

    def tearDown(self):
        self.tmp.cleanup()

    def table(self, holder_in_uniform: bool, uni_sha: str | None = None) -> None:
        repos = '["ALICE-H", "ALICE-Src"]' if holder_in_uniform else '["ALICE-Src"]'
        (self.law / "conformance" / "shared-files.toml").write_text(
            '[[file]]\npath = "scripts/moving.py"\n'
            f'pending = {{ since = "2026-11-28", exit = "x", target = "{sha(self.v2)}", owner = "ALICE-H", '
            f'planned = "{blob(self.v2)}", variants = {{ "{sha(self.v1)}" = ["ALICE-H"] }} }}\n'
            'repos = ["ALICE-H"]\n'
            '[[file]]\npath = "scripts/uni.py"\n'
            f'sha256 = "{uni_sha or sha(self.uni)}"\nsource = {{ repo = "ALICE-Src", rev = "{self.src_rev}" }}\n'
            f'repos = {repos}\n'
            '[[file]]\npath = "scripts/own.sh"\nown = "each repository\'s own"\n', encoding="utf-8")

    def commit_law(self) -> None:
        git(self.law, "add", "-A")
        git(self.law, "commit", "-q", "-m", "l")
        self.rev = git(self.law, "rev-parse", "HEAD")

    def run_bump(self):
        out = io.StringIO()
        url = str(self.src.parent / "{repo}")
        with contextlib.redirect_stdout(out):
            code = fb.main(["--holder", str(self.holder), "--law", str(self.law), "--source-url", url])
        return code, out.getvalue()

    def test_the_bump_writes_what_a_holder_s_bump_commit_writes(self):
        code, out = self.run_bump()
        self.assertEqual(code, 0, out)
        pin = (self.holder / ".github" / "shared-files-pin").read_text(encoding="utf-8")
        self.assertEqual(pin, f"name ALICE-H\nrev {self.rev}\n"
                              f"table-sha256 {sha((self.law / 'conformance' / 'shared-files.toml').read_bytes())}\n"
                              f"checker-sha256 {sha((self.law / 'scripts' / 'shared_files_check.py').read_bytes())}\n")
        g11 = (self.holder / ".github" / "g11-table-pin").read_text(encoding="utf-8")
        self.assertIn(f"rev {self.rev}\n", g11)
        self.assertIn(f"checker-sha256 {sha((self.law / 'scripts' / 'g11_table_check.py').read_bytes())}", g11)
        self.assertEqual((self.holder / "alice-law-conformance.toml").read_text(encoding="utf-8"),
                         f'note = "pinned at ALICE-Law {self.rev[:8]}, older ALICE-Law 0e8c14e"\n')
        self.assertEqual((self.holder / "scripts" / "moving.py").read_bytes(), self.v2)
        self.assertEqual((self.holder / "scripts" / "uni.py").read_bytes(), self.uni)
        self.assertEqual((self.holder / "scripts" / "own.sh").read_bytes(), b"own\n")
        self.assertIn("2 copies set to their targets, 1 manifest name(s) renamed, g11-table-pin moved", out)
        self.assertEqual(git(self.holder, "status", "--porcelain"), "", "the bump is committed")

    def test_the_holder_given_as_a_relative_path_and_a_row_read_from_its_source(self):
        # `--holder .` from inside the clone, with a copy that must be fetched from the source repository
        # (2026-11-28 ALICE-LOL: the cache path became "-sources/..." and git read it as an option)
        (self.holder / "scripts" / "uni.py").unlink(missing_ok=True)
        git(self.holder, "commit", "-q", "--allow-empty", "-m", "no uni")
        cwd = os.getcwd()
        os.chdir(self.holder)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = fb.main(["--holder", ".", "--law", str(self.law), "--source-url", str(self.src.parent / "{repo}")])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual((self.holder / "scripts" / "uni.py").read_bytes(), self.uni)
        self.assertIn("g11 fixture bump: holder: ", out.getvalue())
        self.assertFalse((self.holder / "-sources").exists())

    def test_a_row_not_listing_the_public_holder_is_not_copied(self):
        self.table(holder_in_uniform=False)
        self.commit_law()
        code, out = self.run_bump()
        self.assertEqual(code, 0, out)
        self.assertFalse((self.holder / "scripts" / "uni.py").exists())

    def test_a_row_exempting_the_holder_is_not_copied(self):
        t = self.law / "conformance" / "shared-files.toml"
        t.write_text(t.read_text(encoding="utf-8").replace(
            'repos = ["ALICE-H", "ALICE-Src"]\n',
            'repos = ["ALICE-H", "ALICE-Src"]\nexempt = { "ALICE-H" = "runs its own copy in place" }\n'), encoding="utf-8")
        self.commit_law()
        code, out = self.run_bump()
        self.assertEqual(code, 0, out)
        self.assertFalse((self.holder / "scripts" / "uni.py").exists())

    def test_a_private_holder_takes_the_rows_whose_path_it_holds(self):
        pin = self.holder / ".github" / "shared-files-pin"
        pin.write_text(pin.read_text(encoding="utf-8") + "visibility private\n", encoding="utf-8")
        self.table(holder_in_uniform=False)
        self.commit_law()
        (self.holder / "scripts" / "uni.py").write_bytes(b"stale\n")
        git(self.holder, "add", "-A")
        git(self.holder, "commit", "-q", "-m", "p")
        code, out = self.run_bump()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.holder / "scripts" / "uni.py").read_bytes(), self.uni)
        self.assertTrue(pin.read_text(encoding="utf-8").endswith("visibility private\n"))

    def test_bytes_that_do_not_have_the_row_s_sha256_are_not_built(self):
        self.table(holder_in_uniform=True, uni_sha="1" * 64)
        self.commit_law()
        code, out = self.run_bump()
        self.assertEqual(code, 2, out)
        self.assertIn("not built", out)

    def test_a_missing_planned_blob_is_not_built(self):
        (self.law / "conformance" / "planned" / blob(self.v2)).unlink()
        self.commit_law()
        code, out = self.run_bump()
        self.assertEqual(code, 2, out)
        self.assertIn("conformance/planned/", out)

    def test_a_clone_without_a_pin_is_not_built(self):
        (self.holder / ".github" / "shared-files-pin").unlink()
        code, out = self.run_bump()
        self.assertEqual(code, 2, out)
        self.assertIn("not a holder", out)


if __name__ == "__main__":
    unittest.main()
