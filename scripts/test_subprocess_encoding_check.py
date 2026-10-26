#!/usr/bin/env python3
"""Tests for scripts/subprocess_encoding_check.py."""
from __future__ import annotations

import datetime
import importlib.util
import io
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("subprocess_encoding_check", HERE / "subprocess_encoding_check.py")
s = importlib.util.module_from_spec(spec)
sys.modules["subprocess_encoding_check"] = s
spec.loader.exec_module(s)

DAY = datetime.date(2026, 10, 26)
EXEMPT = '[[exempt]]\npath = "{path}"\nowner = "ALICE-X"\nreason = "{reason}"\nuntil = "{until}"\n'


class Calls(unittest.TestCase):
    def test_text_mode_without_encoding_is_found(self):
        src = ("import subprocess\n"
               "subprocess.run(['git'], capture_output=True, text=True)\n"
               "subprocess.Popen(['git'],\n    universal_newlines=True,\n)\n")
        self.assertEqual(s.text_mode_calls(src), (2, [2, 3]))

    def test_an_encoding_or_bytes_mode_is_not_found(self):
        src = ("import subprocess\n"
               "subprocess.run(['git'], text=True, encoding='utf-8')\n"
               "subprocess.check_output(['git'])\n"
               "subprocess.run(['git'], text=False)\n")
        self.assertEqual(s.text_mode_calls(src), (3, []))


class Check(unittest.TestCase):
    def tree(self, files: dict[str, str], exempt: str = "") -> tuple[Path, Path]:
        d = tempfile.TemporaryDirectory(prefix="subprocess-encoding-")
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8")
        (root / "exempt.toml").write_text(exempt, encoding="utf-8")
        return root, root / "exempt.toml"

    BAD = "import subprocess\nsubprocess.run(['git'], text=True)\n"
    GOOD = "import subprocess\nsubprocess.run(['git'], text=True, encoding='utf-8')\n"

    def test_a_missing_encoding_fails_naming_the_line(self):
        root, ex = self.tree({"a.py": self.BAD, "b.py": self.GOOD})
        inspected, problems = s.check(root, ex, DAY)
        self.assertEqual(inspected, 2)
        self.assertEqual(len(problems), 1)
        self.assertIn("a.py: text-mode subprocess call without encoding= at line(s) 2", problems[0])

    def test_an_exemption_before_its_date_passes(self):
        root, ex = self.tree({"a.py": self.BAD}, EXEMPT.format(path="a.py", reason="r", until="2026-11-09"))
        self.assertEqual(s.check(root, ex, DAY), (1, []))

    def test_an_exemption_past_its_date_fails(self):
        root, ex = self.tree({"a.py": self.BAD}, EXEMPT.format(path="a.py", reason="r", until="2026-10-25"))
        _, problems = s.check(root, ex, DAY)
        self.assertEqual(len(problems), 1)
        self.assertIn("exemption expired on 2026-10-25 (owner ALICE-X)", problems[0])

    def test_an_exemption_no_longer_needed_fails(self):
        root, ex = self.tree({"a.py": self.GOOD}, EXEMPT.format(path="a.py", reason="r", until="2026-11-09"))
        _, problems = s.check(root, ex, DAY)
        self.assertEqual(problems, ["exempt.toml: a.py has no text-mode call without encoding=: remove its entry"])

    def test_an_exemption_without_a_reason_or_date_fails(self):
        root, ex = self.tree({"a.py": self.BAD}, EXEMPT.format(path="a.py", reason="", until="soon"))
        _, problems = s.check(root, ex, DAY)
        self.assertIn("exempt.toml: an entry for a.py has no reason", problems)
        self.assertIn("exempt.toml: a.py: until is not a YYYY-MM-DD date", problems)

    def test_nothing_inspected_is_a_failure(self):
        root, _ = self.tree({"a.py": "print(1)\n"})
        with redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
            code = s.main([str(root), str(root / "exempt.toml"), "--today", "2026-10-26"])
        self.assertEqual(code, 2)
        self.assertIn("nothing was checked", err.getvalue())


class ThisRepository(unittest.TestCase):
    def test_scripts_name_their_encoding_or_are_exempt_until_a_date(self):
        inspected, problems = s.check(HERE, HERE / "subprocess_encoding_exempt.toml", DAY)
        self.assertGreater(inspected, 0)
        self.assertEqual(problems, [])

    def test_the_paths_the_shared_file_checker_decodes_are_ascii(self):
        # shared_files_check.py is exempt because its text-mode calls only decode object ids, refs,
        # dates and table paths; a non-ASCII path in the table would not decode on a Windows lane
        table = tomllib.loads((HERE.parent / "conformance" / "shared-files.toml").read_text(encoding="utf-8"))
        paths = [r["path"] for r in table["file"]]
        self.assertGreater(len(paths), 0)
        self.assertEqual([p for p in paths if not p.isascii()], [])


if __name__ == "__main__":
    unittest.main()
