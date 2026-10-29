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
        d = self.tree({"conformance/a.toml": "", "scripts/reader.py": "conformance/a.toml\n",
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
        d = self.tree({"conformance/a.toml": "", "scripts/test_reader.py": "open('conformance/a.toml')\n",
                       ".github/workflows/ci.yml": CI.format(s="test_reader.py")})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 1, out)
        self.assertIn("no script reads it", out)

    def test_the_planned_bytes_are_one_file(self):
        d = self.tree({"conformance/planned/abc": "", "conformance/planned/def": "",
                       "scripts/reader.py": 'PLANNED_DIR = "planned"\n',
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

    # ba's three probes (2026-10-29): a name that is a substring of another, a basename in another directory, and a
    # script named only in a workflow's paths: list were all read as covered

    def test_a_file_whose_name_ends_another_files_name_is_not_read_by_it(self):
        d = self.tree({"conformance/files.toml": "", "conformance/shared-files.toml": "",
                       "scripts/reader.py": "open('conformance/shared-files.toml')\n",
                       ".github/workflows/ci.yml": CI.format(s="reader.py")})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 1, out)
        self.assertIn("unmapped: conformance/files.toml: no script reads it", out)
        self.assertIn("ok: conformance/shared-files.toml", out)

    def test_a_file_in_another_directory_is_matched_by_its_whole_path(self):
        d = self.tree({"conformance/sub/gates.toml": "", "conformance/gates.toml": "",
                       "scripts/reader.py": "open('conformance/gates.toml')\n",
                       ".github/workflows/ci.yml": CI.format(s="reader.py")})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 1, out)
        self.assertIn("unmapped: conformance/sub/gates.toml: no script reads it", out)

    def test_a_path_under_another_directory_does_not_name_the_file(self):
        # left boundary: fixtures/conformance/a.toml is another file than conformance/a.toml
        d = self.tree({"conformance/a.toml": "", "scripts/reader.py": "open('fixtures/conformance/a.toml')\n",
                       ".github/workflows/ci.yml": CI.format(s="reader.py")})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 1, out)
        self.assertIn("unmapped: conformance/a.toml: no script reads it", out)

    def test_a_joined_path_names_the_file(self):
        d = self.tree({"conformance/sub/gates.toml": "",
                       "scripts/reader.py": 'p = ROOT / "conformance" / "sub" / "gates.toml"\n',
                       ".github/workflows/ci.yml": CI.format(s="reader.py")})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 0, out)

    def test_a_script_named_only_in_paths_is_not_run(self):
        wf = ('on:\n  push:\n    branches: ["ci/**"]\n    paths:\n      - "scripts/reader.py"\n\n'
              'jobs:\n  a:\n    steps:\n      - run: python3 scripts/other.py\n')
        d = self.tree({"conformance/a.toml": "", "scripts/reader.py": "open('conformance/a.toml')\n",
                       ".github/workflows/ci.yml": wf})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 1, out)
        self.assertIn("unmapped: conformance/a.toml: its readers (reader.py) run on main only", out)

    def test_a_script_in_a_block_run_step_is_run(self):
        wf = ('on:\n  push:\n    branches: ["ci/**"]\n\njobs:\n  a:\n    steps:\n      - name: x\n'
              '        run: |\n          echo start\n          python3 scripts/reader.py\n      - name: y\n'
              '        uses: actions/checkout@v5\n')
        d = self.tree({"conformance/a.toml": "", "scripts/reader.py": "open('conformance/a.toml')\n",
                       ".github/workflows/ci.yml": wf})
        code, lines = m.check(d)
        out = "\n".join(lines)
        self.assertEqual(code, 0, out)

    def test_the_repository_maps_every_conformance_file(self):
        code, out = m.check(HERE.parent)
        self.assertEqual(code, 0, "\n".join(out))


if __name__ == "__main__":
    unittest.main()
