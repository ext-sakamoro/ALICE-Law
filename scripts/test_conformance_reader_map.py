#!/usr/bin/env python3
"""Tests of scripts/conformance_reader_map.py (python3 scripts/test_conformance_reader_map.py)"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import conformance_reader_map as m  # noqa: E402

CI = 'on:\n  push:\n    branches: ["ci/**"]\n    paths:\n      - "conformance/**"\n\njobs:\n  a:\n    steps:\n      - run: python3 scripts/{s}\n'
MAIN = 'on:\n  push:\n    branches: [main]\n\njobs:\n  a:\n    steps:\n      - run: python3 scripts/{s}\n'


class ReaderMap(unittest.TestCase):
    def tree(self, files: dict[str, str]) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(d)])
        for rel, text in files.items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        return d

    def test_a_file_read_by_a_script_run_on_ci_passes(self):
        d = self.tree({"conformance/a.toml": "", "scripts/reader.py": "open('conformance/a.toml')\n",
                       ".github/workflows/ci.yml": CI.format(s="reader.py")})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 0, out)
        self.assertIn("ok: conformance/a.toml: read on ci/** by reader.py", out)
        self.assertIn("conformance_reader_map: 1 file(s), 1 ci/** workflow(s), 0 unmapped", out)

    def test_a_reader_run_on_main_only_is_unmapped(self):
        d = self.tree({"conformance/a.toml": "", "scripts/reader.py": "a.toml\n",
                       ".github/workflows/ci.yml": CI.format(s="other.py"),
                       ".github/workflows/main.yml": MAIN.format(s="reader.py")})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 1, out)
        self.assertIn("unmapped: conformance/a.toml: its readers (reader.py) run on main only", out)

    def test_a_file_no_script_names_is_unmapped(self):
        d = self.tree({"conformance/a.toml": "", "scripts/reader.py": "x\n",
                       ".github/workflows/ci.yml": CI.format(s="reader.py")})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 1, out)
        self.assertIn("unmapped: conformance/a.toml: no script reads it", out)

    def test_a_test_naming_the_file_is_not_a_reader(self):
        d = self.tree({"conformance/a.toml": "", "scripts/test_reader.py": "a.toml\n",
                       ".github/workflows/ci.yml": CI.format(s="test_reader.py")})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 1, out)
        self.assertIn("no script reads it", out)

    def test_the_planned_bytes_are_one_file(self):
        d = self.tree({"conformance/planned/abc": "", "conformance/planned/def": "",
                       "scripts/reader.py": "PLANNED = 'planned/'\n",
                       ".github/workflows/ci.yml": CI.format(s="reader.py")})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 0, out)
        self.assertIn("conformance_reader_map: 1 file(s)", out)

    def test_no_conformance_file_compares_nothing(self):
        d = self.tree({"scripts/reader.py": "x\n", ".github/workflows/ci.yml": CI.format(s="reader.py")})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 2, out)
        self.assertIn("compared nothing", out)

    def test_no_ci_workflow_compares_nothing(self):
        d = self.tree({"conformance/a.toml": "", "scripts/reader.py": "a.toml\n",
                       ".github/workflows/main.yml": MAIN.format(s="reader.py")})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 2, out)
        self.assertIn("no workflow runs on ci/**", out)

    def test_the_repository_maps_every_conformance_file(self):
        code, out = m.check(HERE.parent)
        self.assertEqual(code, 0, "\n".join(out))


if __name__ == "__main__":
    unittest.main()
