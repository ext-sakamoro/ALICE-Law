#!/usr/bin/env python3
"""Tests of scripts/shared_files_check.py (python3 scripts/test_shared_files_check.py)

The fetch and live modes run against local bare repositories standing in for GitHub."""
from __future__ import annotations

import contextlib
import hashlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gen_shared_files as gen  # noqa: E402
import shared_files_check as sfc  # noqa: E402

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent"}
SHARED = b"#!/bin/sh\necho shared\n"


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout


def h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue() + err.getvalue()


class World:
    """Bare repositories ALICE-A, ALICE-B (sharing scripts/s.sh) and ALICE-C (a different s.sh)."""

    def __init__(self, base: Path):
        self.base = base
        self.work = base / "work"
        self.remote = base / "remote"
        self.revs: dict[str, str] = {}
        for name, data in (("ALICE-A", SHARED), ("ALICE-B", SHARED), ("ALICE-C", b"other\n")):
            self.commit(name, {"scripts/s.sh": data, "README": name.encode()})

    def commit(self, name: str, files: dict[str, bytes], first: bool = True) -> str:
        w = self.work / name
        bare = self.remote / name
        if not w.exists():
            git("init", "-q", "-b", "main", str(w))
            git("init", "-q", "--bare", "-b", "main", str(bare))
            git("config", "uploadpack.allowFilter", "true", cwd=bare)
            git("remote", "add", "origin", str(bare), cwd=w)
        for rel, data in files.items():
            (w / rel).parent.mkdir(parents=True, exist_ok=True)
            (w / rel).write_bytes(data)
        git("add", "-A", cwd=w)
        git("commit", "-q", "-m", "c", cwd=w)
        git("push", "-q", "origin", "HEAD:main", cwd=w)
        rev = git("rev-parse", "HEAD", cwd=w).strip()
        self.revs.setdefault(name, rev)
        return rev

    def table(self, **override) -> str:
        e = {"path": "scripts/s.sh", "sha256": h(SHARED), "source": {"repo": "ALICE-A", "rev": self.revs["ALICE-A"]},
             "repos": ["ALICE-A", "ALICE-B"], "paths": {}, "exempt": {}}
        e.update(override)
        return gen.render([e])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = World(Path(self.tmp.name))
        self.saved = sfc.GITHUB
        sfc.GITHUB = "file://" + str(self.w.remote) + "/"
        self.t = Path(self.tmp.name) / "shared-files.toml"

    def tearDown(self):
        sfc.GITHUB = self.saved
        self.tmp.cleanup()

    def check(self, *argv: str):
        return run(sfc.main, ["--table", str(self.t), *argv])


class Schema(Base):
    def test_a_good_table_reads(self):
        self.t.write_text(self.w.table(), encoding="utf-8")
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("compared: files 1, repos 2, copies 2", out)

    def test_an_empty_table_fails_as_compared_nothing(self):
        self.t.write_text(gen.HEADER, encoding="utf-8")
        self.assertEqual(self.check()[0], 2)

    def test_load_refuses_an_empty_table_itself(self):
        # not only main's final guard: the loader refuses a table with no entry
        with self.assertRaises(sfc.Unreadable):
            sfc.load(gen.HEADER)

    def test_defects_are_refused(self):
        for bad in ({"sha256": "0" * 63}, {"repos": ["ALICE-B", "ALICE-A"]}, {"repos": ["ALICE-B"]},
                    {"path": "../x"}, {"paths": {"ALICE-Z": "x"}},
                    {"exempt": {"ALICE-C": "short"}}, {"exempt": {"ALICE-B": "listed repos cannot be exempt"}}):
            with self.subTest(bad=bad):
                self.t.write_text(self.w.table(**bad), encoding="utf-8")
                self.assertEqual(self.check()[0], 2)


class Repo(Base):
    def test_equal_copy_passes(self):
        self.t.write_text(self.w.table(), encoding="utf-8")
        code, out = self.check("--repo", str(self.w.work / "ALICE-B"), "--name", "ALICE-B")
        self.assertEqual(code, 0, out)
        self.assertIn("copies 1", out)

    def test_a_flipped_byte_in_one_copy_is_red(self):
        self.t.write_text(self.w.table(), encoding="utf-8")
        f = self.w.work / "ALICE-B" / "scripts/s.sh"
        data = bytearray(f.read_bytes())
        data[0] ^= 1
        f.write_bytes(bytes(data))
        self.assertEqual(self.check("--repo", str(self.w.work / "ALICE-B"), "--name", "ALICE-B")[0], 1)

    def test_a_missing_copy_is_red(self):
        self.t.write_text(self.w.table(), encoding="utf-8")
        (self.w.work / "ALICE-B" / "scripts/s.sh").unlink()
        code, out = self.check("--repo", str(self.w.work / "ALICE-B"), "--name", "ALICE-B")
        self.assertEqual(code, 1, out)
        self.assertIn("missing", out)

    def test_an_unlisted_copy_at_a_listed_path_is_red(self):
        self.t.write_text(self.w.table(), encoding="utf-8")
        code, out = self.check("--repo", str(self.w.work / "ALICE-C"), "--name", "ALICE-C")
        self.assertEqual(code, 1, out)
        self.assertIn("neither lists nor exempts", out)

    def test_an_exempt_repository_is_not_red_but_compares_nothing(self):
        self.t.write_text(self.w.table(exempt={"ALICE-C": "a different script of its own"}), encoding="utf-8")
        self.assertEqual(self.check("--repo", str(self.w.work / "ALICE-C"), "--name", "ALICE-C")[0], 2)

    def test_check_repo_refuses_to_compare_nothing_itself(self):
        # not only main's final guard: the crate check raises when it compared nothing
        entries = sfc.load(self.w.table())
        with self.assertRaises(sfc.Unreadable):
            sfc.check_repo(entries, Path(self.tmp.name), "ALICE-Q")

    def test_a_crate_the_table_never_names_compared_nothing(self):
        self.t.write_text(self.w.table(), encoding="utf-8")
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertEqual(self.check("--repo", str(empty), "--name", "ALICE-Q")[0], 2)

    def test_an_alternative_path_is_used(self):
        self.w.commit("ALICE-B", {"tools/s.sh": SHARED})
        (self.w.work / "ALICE-B" / "scripts/s.sh").unlink()
        self.t.write_text(self.w.table(paths={"ALICE-B": "tools/s.sh"}), encoding="utf-8")
        self.assertEqual(self.check("--repo", str(self.w.work / "ALICE-B"), "--name", "ALICE-B")[0], 0)


class Fetch(Base):
    def test_a_source_on_main_with_the_bytes_passes(self):
        self.t.write_text(self.w.table(), encoding="utf-8")
        code, out = self.check("--fetch")
        self.assertEqual(code, 0, out)

    def test_a_source_rev_not_on_main_is_red(self):
        w = self.w.work / "ALICE-A"
        git("checkout", "-q", "-b", "side", cwd=w)
        (w / "x").write_text("x", encoding="utf-8")
        git("add", "x", cwd=w)
        git("commit", "-q", "-m", "side", cwd=w)
        git("push", "-q", "origin", "side", cwd=w)
        side = git("rev-parse", "HEAD", cwd=w).strip()
        self.t.write_text(self.w.table(source={"repo": "ALICE-A", "rev": side}), encoding="utf-8")
        code, out = self.check("--fetch")
        self.assertEqual(code, 1, out)
        self.assertIn("not on ALICE-A's main", out)

    def test_wrong_bytes_at_the_source_are_red(self):
        self.t.write_text(self.w.table(sha256=h(b"else")), encoding="utf-8")
        self.assertEqual(self.check("--fetch")[0], 1)


class Live(Base):
    def test_every_main_equal_passes_with_the_count_lock(self):
        self.t.write_text(self.w.table(), encoding="utf-8")
        code, out = self.check("--live", "--expect-copies", "2")
        self.assertEqual(code, 0, out)

    def test_a_main_that_moved_is_red(self):
        self.t.write_text(self.w.table(), encoding="utf-8")
        self.w.commit("ALICE-B", {"scripts/s.sh": SHARED + b"#\n"})
        self.assertEqual(self.check("--live")[0], 1)

    def test_the_count_lock_is_red_on_a_different_count(self):
        self.t.write_text(self.w.table(), encoding="utf-8")
        self.assertEqual(self.check("--live", "--expect-copies", "3")[0], 1)


class TheTable(unittest.TestCase):
    def test_the_repository_table_reads(self):
        code, out = run(sfc.main, ["--table", str(HERE.parent / "conformance" / "shared-files.toml")])
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
