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
OTHER = b"other\n"


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
        for name, data in (("ALICE-A", SHARED), ("ALICE-B", SHARED), ("ALICE-C", OTHER)):
            self.commit(name, {"scripts/s.sh": data, "README": name.encode()})

    def commit(self, name: str, files: dict[str, bytes | None]) -> str:
        w = self.work / name
        bare = self.remote / name
        if not w.exists():
            git("init", "-q", "-b", "main", str(w))
            git("init", "-q", "--bare", "-b", "main", str(bare))
            git("config", "uploadpack.allowFilter", "true", cwd=bare)
            git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=bare)
            git("remote", "add", "origin", str(bare), cwd=w)
        for rel, data in files.items():
            if data is None:
                (w / rel).unlink()
                continue
            (w / rel).parent.mkdir(parents=True, exist_ok=True)
            (w / rel).write_bytes(data)
        git("add", "-A", cwd=w)
        git("commit", "-q", "-m", "c", cwd=w)
        git("push", "-q", "origin", "HEAD:main", cwd=w)
        rev = git("rev-parse", "HEAD", cwd=w).strip()
        self.revs.setdefault(name, rev)
        return rev

    def uniform(self, **override) -> dict:
        e = {"path": "scripts/s.sh", "sha256": h(SHARED), "source": {"repo": "ALICE-A", "rev": self.revs["ALICE-A"]},
             "repos": ["ALICE-A", "ALICE-B"], "paths": {}, "exempt": {}}
        e.update(override)
        return e

    def pending(self, since: str = "2026-10-10", **override) -> dict:
        e = {"path": "scripts/s.sh", "repos": ["ALICE-A", "ALICE-B", "ALICE-C"], "paths": {}, "exempt": {},
             "pending": {"since": since, "exit": "the copies converge on the shared bytes", "target": h(SHARED),
                         "owner": "ALICE-A", "variants": {h(SHARED): ["ALICE-A", "ALICE-B"], h(OTHER): ["ALICE-C"]}}}
        e.update(override)
        return e

    def per_repo(self, exempt: dict | None = None) -> dict:
        return {"path": "scripts/s.sh", "repos": [], "paths": {},
                "exempt": exempt if exempt is not None else {"ALICE-A": "its own data, not a shared copy",
                                                             "ALICE-B": "its own data, not a shared copy"}}

    def table(self, *entries: dict, **override) -> str:
        return gen.render(list(entries) if entries else [self.uniform(**override)])


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
        self.assertIn("compared: files 1, repos 2, copies 2, pending 0", out)

    def test_a_pending_row_reads_and_is_counted(self):
        self.t.write_text(self.w.table(self.w.pending()), encoding="utf-8")
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("pending 1", out)

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

    def test_pending_defects_are_refused(self):
        good = self.w.pending()["pending"]
        for bad in ({"target": "x"}, {"owner": "ALICE-Q"}, {"exit": "soon"}, {"since": "10/10/2026"},
                    {"variants": {h(SHARED): ["ALICE-A", "ALICE-B"]}}):
            with self.subTest(bad=bad):
                self.t.write_text(self.w.table(self.w.pending(pending={**good, **bad})), encoding="utf-8")
                self.assertEqual(self.check()[0], 2)

    def test_a_pending_row_with_no_target_is_refused(self):
        p = {k: v for k, v in self.w.pending()["pending"].items() if k != "target"}
        text = self.w.table(self.w.pending()).replace(f'target = "{h(SHARED)}", ', "")
        self.assertNotIn("target", text.split("pending =")[1].split("variants")[0])
        self.t.write_text(text, encoding="utf-8")
        self.assertEqual(self.check()[0], 2)
        del p

    def test_a_per_repository_entry_must_exempt_its_holders(self):
        self.assertEqual(sfc.load(self.w.table(self.w.per_repo()))[0]["repos"], [])
        with self.assertRaisesRegex(sfc.Unreadable, "must exempt each repository"):
            sfc.load(self.w.table(self.w.per_repo(exempt={})))

    def test_the_ratchet_refuses_more_pending_rows_than_the_base(self):
        base = Path(self.tmp.name) / "base.toml"
        base.write_text(self.w.table(), encoding="utf-8")
        self.t.write_text(self.w.table(self.w.pending()), encoding="utf-8")
        code, out = self.check("--base-table", str(base))
        self.assertEqual(code, 1, out)
        self.assertIn("pending only goes down", out)
        base.write_text(self.w.table(self.w.pending()), encoding="utf-8")
        self.assertEqual(self.check("--base-table", str(base))[0], 0)


class Repo(Base):
    def crate(self, name: str, table: str | None = None):
        self.t.write_text(table or self.w.table(), encoding="utf-8")
        return self.check("--repo", str(self.w.work / name), "--name", name)

    def test_equal_copy_passes(self):
        code, out = self.crate("ALICE-B")
        self.assertEqual(code, 0, out)
        self.assertIn("copies 1", out)

    def test_a_flipped_byte_in_one_copy_is_red(self):
        f = self.w.work / "ALICE-B" / "scripts/s.sh"
        data = bytearray(f.read_bytes())
        data[0] ^= 1
        f.write_bytes(bytes(data))
        self.assertEqual(self.crate("ALICE-B")[0], 1)

    def test_a_missing_copy_is_red(self):
        (self.w.work / "ALICE-B" / "scripts/s.sh").unlink()
        code, out = self.crate("ALICE-B")
        self.assertEqual(code, 1, out)
        self.assertIn("missing", out)

    def test_an_unlisted_copy_at_a_listed_path_is_red(self):
        code, out = self.crate("ALICE-C")
        self.assertEqual(code, 1, out)
        self.assertIn("neither lists nor exempts", out)

    def test_an_exempt_repository_is_not_red_but_compares_nothing(self):
        self.assertEqual(self.crate("ALICE-C", self.w.table(exempt={"ALICE-C": "a different script of its own"}))[0], 2)

    def test_check_repo_refuses_to_compare_nothing_itself(self):
        # not only main's final guard: the crate check raises when it compared nothing
        with self.assertRaises(sfc.Unreadable):
            sfc.check_repo(sfc.load(self.w.table()), Path(self.tmp.name), "ALICE-Q")

    def test_a_crate_the_table_never_names_compared_nothing(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.t.write_text(self.w.table(), encoding="utf-8")
        self.assertEqual(self.check("--repo", str(empty), "--name", "ALICE-Q")[0], 2)

    def test_an_alternative_path_is_used(self):
        self.w.commit("ALICE-B", {"tools/s.sh": SHARED, "scripts/s.sh": None})
        self.assertEqual(self.crate("ALICE-B", self.w.table(paths={"ALICE-B": "tools/s.sh"}))[0], 0)

    def test_a_pending_copy_that_is_a_variant_is_listed_not_a_pass_claim(self):
        code, out = self.crate("ALICE-C", self.w.table(self.w.pending()))
        self.assertEqual(code, 0, out)
        self.assertIn("pending: scripts/s.sh: pending since 2026-10-10, not yet the target", out)
        self.assertIn("pending 1", out)

    def test_a_per_repository_path_held_without_an_exemption_is_red(self):
        code, out = self.crate("ALICE-C", self.w.table(self.w.per_repo()))
        self.assertEqual(code, 1, out)
        self.assertIn("neither lists nor exempts", out)

    def test_a_pending_copy_that_is_no_variant_is_red(self):
        (self.w.work / "ALICE-C" / "scripts/s.sh").write_bytes(b"third\n")
        code, out = self.crate("ALICE-C", self.w.table(self.w.pending()))
        self.assertEqual(code, 1, out)
        self.assertIn("none of the pending row's variants", out)


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
    def live(self, *argv: str, today: str = "2026-10-17"):
        return self.check("--live", "--today", today, *argv)

    def test_every_main_equal_passes_with_the_count_lock(self):
        self.t.write_text(self.w.table(exempt={"ALICE-C": "a different script of its own"}), encoding="utf-8")
        code, out = self.live("--expect-copies", "2")
        self.assertEqual(code, 0, out)

    def test_a_main_that_moved_is_red(self):
        self.t.write_text(self.w.table(exempt={"ALICE-C": "a different script of its own"}), encoding="utf-8")
        self.w.commit("ALICE-B", {"scripts/s.sh": SHARED + b"#\n"})
        self.assertEqual(self.live()[0], 1)

    def test_the_count_lock_is_red_on_a_different_count(self):
        self.t.write_text(self.w.table(exempt={"ALICE-C": "a different script of its own"}), encoding="utf-8")
        self.assertEqual(self.live("--expect-copies", "3")[0], 1)

    def test_a_copy_on_a_main_the_entry_neither_lists_nor_exempts_is_red(self):
        # ALICE-C is a table repository through another row, so its main is read
        rev = self.w.commit("ALICE-C", {"scripts/c.sh": b"c\n"})
        self.w.commit("ALICE-A", {"scripts/c.sh": b"c\n"})
        c_row = {"path": "scripts/c.sh", "sha256": h(b"c\n"), "source": {"repo": "ALICE-C", "rev": rev},
                 "repos": ["ALICE-A", "ALICE-C"], "paths": {}, "exempt": {}}
        self.t.write_text(self.w.table(c_row, self.w.uniform()), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn("present on ALICE-C main", out)

    def test_discovery_reds_a_byte_identical_file_with_no_entry(self):
        self.w.commit("ALICE-A", {"scripts/new.py": b"print(1)\n"})
        self.w.commit("ALICE-B", {"scripts/new.py": b"print(1)\n"})
        self.t.write_text(self.w.table(exempt={"ALICE-C": "a different script of its own"}), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/new.py: byte-identical in ALICE-A, ALICE-B but has no entry", out)

    def test_a_new_holder_of_a_per_repository_path_is_red(self):
        # ALICE-C is read through the uniform row of scripts/c.sh, and has scripts/s.sh unexempted
        rev = self.w.commit("ALICE-C", {"scripts/c.sh": b"c\n"})
        self.w.commit("ALICE-A", {"scripts/c.sh": b"c\n"})
        c_row = {"path": "scripts/c.sh", "sha256": h(b"c\n"), "source": {"repo": "ALICE-C", "rev": rev},
                 "repos": ["ALICE-A", "ALICE-C"], "paths": {}, "exempt": {}}
        self.t.write_text(self.w.table(c_row, self.w.per_repo()), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/s.sh: present on ALICE-C main", out)

    def test_a_pending_row_is_listed_not_red_within_its_time(self):
        self.t.write_text(self.w.table(self.w.pending()), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 0, out)
        self.assertIn("pending: scripts/s.sh: pending 7 day(s), 2 variant(s)", out)

    def test_a_pending_row_past_its_time_is_red(self):
        self.t.write_text(self.w.table(self.w.pending()), encoding="utf-8")
        code, out = self.live(today="2026-11-30")
        self.assertEqual(code, 1, out)
        self.assertIn("more than 21 days", out)


class TheTable(unittest.TestCase):
    def test_the_repository_table_reads(self):
        code, out = run(sfc.main, ["--table", str(HERE.parent / "conformance" / "shared-files.toml")])
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
