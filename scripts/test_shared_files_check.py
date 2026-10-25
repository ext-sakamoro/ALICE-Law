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
V3 = SHARED + b"# v3\n"


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
            # pushes go to a literal path (`./.fixture-remote`, a link to the bare repository): a hook reading this
            # test's text sees a fixture repository, not the remote named origin; fetch keeps origin/* current
            (w / FIXTURE_REMOTE).symlink_to(bare, target_is_directory=True)
            with open(w / ".git" / "info" / "exclude", "a", encoding="utf-8") as f:
                f.write(f"/{FIXTURE_REMOTE}\n")
        for rel, data in files.items():
            if data is None:
                (w / rel).unlink()
                continue
            (w / rel).parent.mkdir(parents=True, exist_ok=True)
            (w / rel).write_bytes(data)
        git("add", "-A", cwd=w)
        git("commit", "-q", "-m", "c", cwd=w)
        git("push", "-q", "./.fixture-remote", "HEAD:main", cwd=w)
        git("fetch", "-q", "origin", cwd=w)
        rev = git("rev-parse", "HEAD", cwd=w).strip()
        self.revs.setdefault(name, rev)
        return rev

    def ci(self, name: str, branch: str, files: dict[str, bytes], prefix: str = "ci/") -> str:
        """push files to <prefix><branch> (ci/ by default) of a repository, its main unchanged"""
        w = self.work / name
        branch = f"{prefix}{branch}"
        git("checkout", "-q", "-b", branch, cwd=w)
        for rel, data in files.items():
            (w / rel).parent.mkdir(parents=True, exist_ok=True)
            (w / rel).write_bytes(data)
        git("add", "-A", cwd=w)
        git("commit", "-q", "-m", "c", cwd=w)
        git("push", "-q", "./.fixture-remote", f"HEAD:refs/heads/{branch}", cwd=w)
        git("fetch", "-q", "origin", cwd=w)
        rev = git("rev-parse", "HEAD", cwd=w).strip()
        git("checkout", "-q", "main", cwd=w)
        git("reset", "-q", "--hard", "origin/main", cwd=w)
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

    def planned(self, since: str = "2026-10-10", **override) -> dict:
        """ALICE-A (the owner) and ALICE-B have landed SHARED, the registry's next=; ALICE-C still has OTHER"""
        e = self.pending(since)
        e["pending"] = {**e["pending"], "exit": "every listed repository's main has the bytes the registry row moves to",
                        "planned": sfc.git_blob(SHARED)}
        e.update(override)
        return e

    def moved(self, **override) -> dict:
        """A planned row re-rolled from SHARED (on ALICE-A, the owner, and ALICE-B) to V3, which no main holds yet"""
        e = {"path": "scripts/s.sh", "repos": ["ALICE-A", "ALICE-B"], "paths": {}, "exempt": {},
             "pending": {"since": "2026-10-10", "exit": "every listed repository's main has the bytes the registry "
                         "row moves to", "target": h(V3), "owner": "ALICE-A", "planned": sfc.git_blob(V3),
                         "moved_from": {"planned": sfc.git_blob(SHARED), "target": h(SHARED)},
                         "variants": {h(SHARED): ["ALICE-A", "ALICE-B"]}}}
        e["pending"].update(override)
        return e

    def per_repo(self, exempt: dict | None = None) -> dict:
        return {"path": "scripts/s.sh", "repos": [], "paths": {},
                "exempt": exempt if exempt is not None else {"ALICE-A": "its own data, not a shared copy",
                                                             "ALICE-B": "its own data, not a shared copy"}}

    def table(self, *entries: dict, **override) -> str:
        return gen.render(list(entries) if entries else [self.uniform(**override)])


FIXTURE_REMOTE = ".fixture-remote"  # in each work tree: a link to its bare repository


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
    def lock(self, text: str) -> Path:
        p = Path(self.tmp.name) / "live.yml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_a_count_lock_equal_to_the_table_s_uniform_copies_passes(self):
        self.t.write_text(self.w.table(), encoding="utf-8")
        code, out = self.check("--lock-file", str(self.lock("run: x --live --expect-copies 2\n")))
        self.assertEqual(code, 0, out)
        self.assertIn("count lock in", out)

    def test_a_count_lock_that_did_not_move_with_the_table_is_red(self):
        # a row gained a holder (3 listed copies) and the workflow still says 2: red offline, not first on main
        self.t.write_text(self.w.table(repos=["ALICE-A", "ALICE-B", "ALICE-C"]), encoding="utf-8")
        code, out = self.check("--lock-file", str(self.lock("--expect-copies 2\n")))
        self.assertEqual(code, 1, out)
        self.assertIn("the count lock says 2, the table lists 3", out)

    def test_the_count_lock_file_counts_only_uniform_rows(self):
        c_row = {"path": "scripts/c.sh", "sha256": h(b"c\n"), "source": {"repo": "ALICE-C", "rev": "0" * 40},
                 "repos": ["ALICE-A", "ALICE-C"], "paths": {}, "exempt": {}}
        self.t.write_text(self.w.table(c_row, self.w.pending()), encoding="utf-8")
        self.assertEqual(self.check("--lock-file", str(self.lock("--expect-copies 2")))[0], 0)
        self.assertEqual(self.check("--lock-file", str(self.lock("--expect-copies 5")))[0], 1)

    def test_a_lock_file_without_exactly_one_lock_is_not_read(self):
        self.t.write_text(self.w.table(), encoding="utf-8")
        for text in ("no lock here\n", "--expect-copies 2\n--expect-copies 2\n"):
            code, out = self.check("--lock-file", str(self.lock(text)))
            self.assertEqual(code, 2, out)
            self.assertIn("needs exactly one", out)

    def test_an_exemption_is_a_reason_or_a_reason_with_an_until_date(self):
        ok = {"reason": "its own script until it converges", "until": "2026-11-11"}
        self.assertEqual(sfc.load(self.w.table(exempt={"ALICE-C": ok}))[0]["exempt"], {"ALICE-C": ok})
        for bad in ({"reason": "its own script until it converges"},  # a table without until
                    {"reason": "short", "until": "2026-11-11"},
                    {"reason": "its own script until it converges", "until": "2026-13-40"},
                    # DATE, not what the interpreter's fromisoformat also reads (3.11+ takes these as dates)
                    {"reason": "its own script until it converges", "until": "20261111xx"},
                    {"reason": "its own script until it converges", "until": "2026-W45-3"},
                    {"reason": "its own script until it converges", "until": "20261111"},
                    {"reason": "its own script until it converges", "until": "2026-11-11", "x": "y"},
                    {"until": "2026-11-11"}):
            with self.assertRaises(sfc.Unreadable, msg=str(bad)):
                sfc.load(self.w.table(exempt={"ALICE-C": bad}))

    def test_gen_writes_an_expiring_exemption_back_as_it_reads(self):
        ex = {"ALICE-C": {"reason": "its own script until it converges", "until": "2026-11-11"},
              "ALICE-D": "a different script of its own"}
        text = gen.render([{"path": "scripts/s.sh", "sha256": h(b"s\n"), "source": {"repo": "ALICE-A", "rev": "0" * 40},
                            "repos": ["ALICE-A", "ALICE-B"], "paths": {}, "exempt": ex}])
        self.assertEqual(sfc.load(text)[0]["exempt"], ex)

    def test_a_product_repository_in_the_hashed_set_is_a_repository_name(self):
        # a product of the account outside the ALICE-* names (text-to-print) holds copies of the Law tooling too;
        # it is named only through sha256 of its lower-case name, and every rule that reads a name takes it
        self.assertTrue(sfc.is_repo_name("text-to-print"))
        self.assertTrue(sfc.is_repo_name("ALICE-A"))
        self.t.write_text(self.w.table(repos=["ALICE-A", "ALICE-B", "text-to-print"]), encoding="utf-8")
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("repos 3", out)

    def test_a_name_outside_the_set_is_refused_wherever_a_name_is_read(self):
        for name in ("other-product", "Text-To-Print", "text-to-print ", "", "ALICE-", "alice-zip"):
            self.assertFalse(sfc.is_repo_name(name), name)
        with self.assertRaises(sfc.Unreadable):
            sfc.load(self.w.table(repos=["ALICE-A", "ALICE-B", "other-product"]))
        c_row = {"path": "scripts/c.sh", "sha256": h(b"c\n"), "source": {"repo": "other-product", "rev": "0" * 40},
                 "repos": ["ALICE-A", "ALICE-B"], "paths": {}, "exempt": {}}
        with self.assertRaises(sfc.Unreadable):
            sfc.load(self.w.table(c_row))
        c_row = {"path": "scripts/c.sh", "sha256": h(b"c\n"), "source": {"repo": "ALICE-A", "rev": "0" * 40},
                 "repos": ["ALICE-A", "ALICE-B"], "paths": {}, "exempt": {"other-product": "a reason long enough"}}
        with self.assertRaises(sfc.Unreadable):
            sfc.load(self.w.table(c_row))

    def test_a_product_repository_can_be_a_row_s_source_and_a_pending_owner(self):
        c_row = {"path": "scripts/c.sh", "sha256": h(b"c\n"), "source": {"repo": "text-to-print", "rev": "0" * 40},
                 "repos": ["ALICE-A", "text-to-print"], "paths": {}, "exempt": {}}
        self.assertEqual(sfc.load(self.w.table(c_row))[0]["source"]["repo"], "text-to-print")
        c_row = {"path": "scripts/c.sh", "sha256": h(b"c\n"), "source": {"repo": "ALICE-A", "rev": "0" * 40},
                 "repos": ["ALICE-A", "ALICE-B"], "paths": {}, "exempt": {"text-to-print": "a reason long enough"}}
        self.assertEqual(sfc.load(self.w.table(c_row))[0]["exempt"], {"text-to-print": "a reason long enough"})

    def test_the_set_holds_hashes_only_and_a_fictional_entry_admits_exactly_that_name(self):
        entry = hashlib.sha256("fictional-product".encode("utf-8")).hexdigest()
        self.assertNotIn(entry, sfc.PRODUCT_REPO_HASHES)
        self.assertFalse(sfc.is_repo_name("fictional-product"))
        self.addCleanup(sfc.PRODUCT_REPO_HASHES.discard, entry)
        sfc.PRODUCT_REPO_HASHES.add(entry)
        self.assertTrue(sfc.is_repo_name("fictional-product"))
        self.assertFalse(sfc.is_repo_name("fictional-product2"))
        self.assertTrue(sfc.PRODUCT_REPO_HASHES)
        for x in sfc.PRODUCT_REPO_HASHES:
            self.assertRegex(x, r"^[0-9a-f]{64}$")

    def test_gen_reads_a_product_owner_through_the_same_rule(self):
        code, out = run(gen.main, ["--table", str(self.t), "--check",
                                   "--target", "scripts/c.sh=" + "0" * 64 + "@other-product"])
        self.assertEqual(code, 2, out)
        self.assertIn("needs PATH=SHA256@", out)

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

    def test_a_per_repository_entry_takes_no_dated_exemption(self):
        # a per-repository row has no bytes for an expired exemption's copy to converge on
        dated = {"ALICE-C": {"reason": "its own data, never shared", "until": "2026-11-11"}}
        with self.assertRaises(sfc.Unreadable) as cm:
            sfc.load(self.w.table(self.w.per_repo(exempt=dated)))
        self.assertIn("until", str(cm.exception))

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
        self.assertIn("1 unplanned pending rows, the base table has 0", out)
        base.write_text(self.w.table(self.w.pending()), encoding="utf-8")
        self.assertEqual(self.check("--base-table", str(base))[0], 0)

    def test_the_ratchet_admits_a_planned_row_and_counts_both_kinds(self):
        base = Path(self.tmp.name) / "base.toml"
        base.write_text(self.w.table(), encoding="utf-8")
        self.t.write_text(self.w.table(self.w.planned()), encoding="utf-8")
        code, out = self.check("--base-table", str(base))
        self.assertEqual(code, 0, out)
        self.assertIn("pending 1 (planned 1, unplanned 0), the base table 0 (planned 0, unplanned 0)", out)

    def test_a_planned_row_turning_unplanned_is_refused_by_the_ratchet(self):
        # the base had a planned row; dropping its planned id makes it an unplanned divergence that grew
        base = Path(self.tmp.name) / "base.toml"
        base.write_text(self.w.table(self.w.planned()), encoding="utf-8")
        self.t.write_text(self.w.table(self.w.pending()), encoding="utf-8")
        code, out = self.check("--base-table", str(base))
        self.assertEqual(code, 1, out)
        self.assertIn("1 unplanned pending rows, the base table has 0", out)

    def test_the_ratchet_refuses_a_pending_date_moved_later(self):
        base = Path(self.tmp.name) / "base.toml"
        for row in (self.w.pending, self.w.planned):
            with self.subTest(row=row.__name__):
                base.write_text(self.w.table(row(since="2026-10-10")), encoding="utf-8")
                self.t.write_text(self.w.table(row(since="2026-10-17")), encoding="utf-8")
                code, out = self.check("--base-table", str(base))
                self.assertEqual(code, 1, out)
                self.assertIn("the base table says 2026-10-10", out)
                # the same date, or an earlier one, passes
                self.t.write_text(self.w.table(row(since="2026-10-09")), encoding="utf-8")
                self.assertEqual(self.check("--base-table", str(base))[0], 0)

    def test_planned_defects_are_refused(self):
        good = self.w.planned()["pending"]
        for bad in ({"planned": "x" * 40}, {"planned": "0" * 39},
                    # the owner must hold the target in the recorded variants
                    {"owner": "ALICE-C"}):
            with self.subTest(bad=bad):
                self.t.write_text(self.w.table(self.w.planned(pending={**good, **bad})), encoding="utf-8")
                self.assertEqual(self.check()[0], 2)


    def test_a_planned_new_file_no_main_holds_yet_loads(self):
        # a file moving into every listed repository: every copy is absent, so the owner holds "absent" (nothing to
        # move from); absent next to a present copy is still refused for the owner
        new = {"path": "scripts/n.sh", "repos": ["ALICE-A", "ALICE-B"], "paths": {}, "exempt": {},
               "pending": {"since": "2026-10-10", "exit": "every listed repository's main has the bytes the registry "
                           "row moves to", "target": h(V3), "owner": "ALICE-A", "planned": sfc.git_blob(V3),
                           "variants": {"absent": ["ALICE-A", "ALICE-B"]}}}
        self.assertEqual(len(sfc.load(self.w.table(new))), 1)
        half = {**new, "pending": {**new["pending"], "variants": {"absent": ["ALICE-A"], h(SHARED): ["ALICE-B"]}}}
        with self.assertRaisesRegex(sfc.Unreadable, "owner must hold the target"):
            sfc.load(self.w.table(half))

    def test_moved_defects_are_refused(self):
        self.assertEqual(len(sfc.load(self.w.table(self.w.moved()))), 1)
        old = {"planned": sfc.git_blob(SHARED), "target": h(SHARED)}
        # a moved_from without its target (render cannot write one, so the text is edited)
        text = self.w.table(self.w.moved()).replace(f', target = "{h(SHARED)}" }}', " }")
        self.assertIn(f'moved_from = {{ planned = "{sfc.git_blob(SHARED)}" }}', text)
        with self.assertRaises(sfc.Unreadable):
            sfc.load(text)
        for bad in ({"moved_from": {"planned": "x" * 40, "target": h(SHARED)}},
                    # the bytes it moves from are not the target
                    {"moved_from": {"planned": sfc.git_blob(V3), "target": h(SHARED)}},
                    {"moved_from": {"planned": sfc.git_blob(SHARED), "target": h(V3)}},
                    # the owner holds neither the target nor the bytes it moves from
                    {"moved_from": {**old, "target": h(OTHER)}}):
            with self.subTest(bad=bad):
                with self.assertRaises(sfc.Unreadable):
                    sfc.load(self.w.table(self.w.moved(**bad)))
        unplanned = self.w.pending()
        unplanned["pending"]["moved_from"] = old
        with self.assertRaisesRegex(sfc.Unreadable, "belongs to a planned row"):
            sfc.load(self.w.table(unplanned))

    def test_a_variant_recorded_only_in_the_move_commit_is_refused(self):
        base = Path(self.tmp.name) / "base.toml"
        planned_shared = self.w.moved()
        planned_shared["pending"] = {k: v for k, v in planned_shared["pending"].items() if k != "moved_from"}
        planned_shared["pending"].update(target=h(SHARED), planned=sfc.git_blob(SHARED))
        base.write_text(self.w.table(planned_shared), encoding="utf-8")
        # the move commit also records OTHER as a variant the base never had: it cannot be accepted later
        moved = self.w.moved(variants={h(SHARED): ["ALICE-A"], h(OTHER): ["ALICE-B"]})
        self.t.write_text(self.w.table(moved), encoding="utf-8")
        code, out = self.check("--base-table", str(base))
        self.assertEqual(code, 1, out)
        self.assertIn("the base table does not have (a variant counts only when recorded before the move)", out)

    def test_a_planned_id_moved_without_naming_the_base_is_refused(self):
        base = Path(self.tmp.name) / "base.toml"
        planned_shared = self.w.moved()
        planned_shared["pending"] = {k: v for k, v in planned_shared["pending"].items() if k != "moved_from"}
        planned_shared["pending"].update(target=h(SHARED), planned=sfc.git_blob(SHARED))
        base.write_text(self.w.table(planned_shared), encoding="utf-8")
        # the move names the base: passes, the ratchet counts it as planned
        self.t.write_text(self.w.table(self.w.moved()), encoding="utf-8")
        code, out = self.check("--base-table", str(base))
        self.assertEqual(code, 0, out)
        self.assertIn("pending 1 (planned 1, unplanned 0), the base table 1 (planned 1, unplanned 0)", out)
        # the same move again on the next commit: the base already recorded it
        base.write_text(self.w.table(self.w.moved()), encoding="utf-8")
        self.assertEqual(self.check("--base-table", str(base))[0], 0)
        base.write_text(self.w.table(planned_shared), encoding="utf-8")
        # the planned id moved with no moved_from: the owner holds SHARED, so it reads only as SHARED's target
        silent = self.w.moved()
        silent["pending"] = {**{k: v for k, v in silent["pending"].items() if k != "moved_from"},
                             "planned": sfc.git_blob(V3), "target": h(SHARED)}
        self.t.write_text(self.w.table(silent), encoding="utf-8")
        code, out = self.check("--base-table", str(base))
        self.assertEqual(code, 1, out)
        self.assertIn("without moved_from naming the base's planned id and target", out)
        # moved_from naming other bytes than the base's
        wrong = self.w.moved(moved_from={"planned": sfc.git_blob(OTHER), "target": h(SHARED)})
        self.t.write_text(self.w.table(wrong), encoding="utf-8")
        self.assertEqual(self.check("--base-table", str(base))[0], 1)
        # moved_from added to a row whose planned id did not change: the base planned the row without it
        same_id = self.w.moved(target=h(SHARED), planned=sfc.git_blob(SHARED),
                               moved_from={"planned": sfc.git_blob(OTHER), "target": h(OTHER)})
        same_id["pending"]["variants"] = {h(SHARED): ["ALICE-A", "ALICE-B"]}
        self.t.write_text(self.w.table(same_id), encoding="utf-8")
        code, out = self.check("--base-table", str(base))
        self.assertEqual(code, 1, out)
        self.assertIn("not the base table's planned id", out)
        # a move the base does not explain at all (no planned row there)
        base.write_text(self.w.table(self.w.uniform()), encoding="utf-8")
        self.t.write_text(self.w.table(self.w.moved()), encoding="utf-8")
        code, out = self.check("--base-table", str(base))
        self.assertEqual(code, 1, out)
        self.assertIn("not the base table's planned id", out)


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

    def test_an_expired_exemption_whose_copy_differs_is_red_in_the_crate(self):
        t = self.w.table(exempt={"ALICE-C": {"reason": "its own script until it converges", "until": "2026-10-20"}})
        self.t.write_text(t, encoding="utf-8")
        code, out = self.check("--repo", str(self.w.work / "ALICE-C"), "--name", "ALICE-C", "--today", "2026-10-21")
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/s.sh: the exemption of ALICE-C expired on 2026-10-20", out)

    def test_an_unlisted_copy_at_a_listed_path_is_red(self):
        code, out = self.crate("ALICE-C")
        self.assertEqual(code, 1, out)
        self.assertIn("neither lists nor exempts", out)

    def adopt(self, data: bytes):
        """ALICE-C (not listed for scripts/s.sh) holds it with `data`"""
        (self.w.work / "ALICE-C" / "scripts/s.sh").write_bytes(data)

    def test_an_unlisted_copy_with_the_entry_bytes_is_a_counted_adopting_note(self):
        # a crate adopting a shared file cannot be listed before its main has it
        self.adopt(SHARED)
        code, out = self.crate("ALICE-C")
        self.assertEqual(code, 0, out)
        self.assertIn("scripts/s.sh: adopting: not yet listed for ALICE-C, the same bytes as the entry", out)
        self.assertIn("adopting: 1 copy(ies) with an entry's bytes, not yet listed for ALICE-C", out)
        self.assertIn("copies 1", out)

    def test_an_unlisted_copy_with_a_pending_target_is_adopting_too(self):
        p = self.w.pending()
        p["repos"] = ["ALICE-A", "ALICE-B"]
        p["pending"] = {**p["pending"], "variants": {h(SHARED): ["ALICE-A"], h(OTHER): ["ALICE-B"]}}
        self.adopt(SHARED)
        code, out = self.crate("ALICE-C", self.w.table(p))
        self.assertEqual(code, 0, out)
        self.assertIn("adopting: not yet listed for ALICE-C", out)

    def test_an_unlisted_copy_with_one_byte_changed_is_red(self):
        data = bytearray(SHARED)
        data[0] ^= 1
        self.adopt(bytes(data))
        code, out = self.crate("ALICE-C")
        self.assertEqual(code, 1, out)
        self.assertIn("neither lists nor exempts for ALICE-C", out)

    def test_an_unlisted_copy_of_a_per_repository_path_is_red(self):
        self.adopt(SHARED)
        code, out = self.crate("ALICE-C", self.w.table(self.w.per_repo()))
        self.assertEqual(code, 1, out)
        self.assertIn("none: per-repository data", out)

    def test_a_path_no_entry_names_is_not_adopted(self):
        # only a path an entry names can be adopted: a copy elsewhere is not compared at all
        (self.w.work / "ALICE-C" / "scripts/other.sh").write_bytes(SHARED)
        (self.w.work / "ALICE-C" / "scripts/s.sh").unlink()
        code, out = self.crate("ALICE-C")
        self.assertEqual(code, 2, out)
        self.assertNotIn("adopting", out)

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
        self.assertIn("own copies not at a pending row's target: 1 (scripts/s.sh)", out)

    def test_own_copies_off_target_are_counted_and_a_copy_at_target_is_not(self):
        new = b"new bytes\n"
        row = self.w.pending()
        row["pending"] = {**row["pending"], "target": h(new)}
        code, out = self.crate("ALICE-C", self.w.table(row))
        self.assertIn("own copies not at a pending row's target: 1 (scripts/s.sh)", out)
        (self.w.work / "ALICE-C" / "scripts/s.sh").write_bytes(new)
        code, out = self.crate("ALICE-C", self.w.table(row))
        self.assertEqual(code, 0, out)
        self.assertIn("own copies not at a pending row's target: 0", out)
        # an absent copy on a row that lists the crate is not at target either
        (self.w.work / "ALICE-C" / "scripts/s.sh").unlink()
        self.assertEqual(sfc.off_target(sfc.load(self.w.table(row)), self.w.work / "ALICE-C", "ALICE-C"),
                         ["scripts/s.sh"])

    def test_a_uniform_row_is_never_off_target(self):
        code, out = self.crate("ALICE-B", self.w.table())
        self.assertEqual(code, 0, out)
        self.assertIn("own copies not at a pending row's target: 0", out)

    def test_a_per_repository_path_held_without_an_exemption_is_red(self):
        code, out = self.crate("ALICE-C", self.w.table(self.w.per_repo()))
        self.assertEqual(code, 1, out)
        self.assertIn("neither lists nor exempts", out)

    def test_a_pending_copy_with_the_target_bytes_passes_before_a_regeneration(self):
        # the row moves A, B, C to NEW; C already took it before the table recorded it as a variant
        new = b"new bytes\n"
        (self.w.work / "ALICE-C" / "scripts/s.sh").write_bytes(new)
        row = self.w.pending()
        row["pending"] = {**row["pending"], "target": h(new)}
        code, out = self.crate("ALICE-C", self.w.table(row))
        self.assertEqual(code, 0, out)
        self.assertIn("the target", out)

    def test_a_pending_copy_that_is_no_variant_is_red(self):
        (self.w.work / "ALICE-C" / "scripts/s.sh").write_bytes(b"third\n")
        code, out = self.crate("ALICE-C", self.w.table(self.w.pending()))
        self.assertEqual(code, 1, out)
        self.assertIn("none of the pending row's variants nor its target", out)


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
        git("push", "-q", "./.fixture-remote", "side", cwd=w)
        git("fetch", "-q", "origin", cwd=w)
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

    def test_a_holder_this_change_dropped_is_a_finding_not_an_unlisted_note(self):
        # the base table lists ALICE-C with the shared bytes; the change removes it from the row (ALICE-C stays read
        # through another row); the landing path (no --strict) must not read it back as a rollout landed elsewhere
        t2 = b"t\n"
        for r in ("ALICE-A", "ALICE-C"):
            self.w.commit(r, {"scripts/t.sh": t2})
        self.w.commit("ALICE-C", {"scripts/s.sh": SHARED})
        other = {"path": "scripts/t.sh", "sha256": h(t2), "source": {"repo": "ALICE-A", "rev": self.w.revs["ALICE-A"]},
                 "repos": ["ALICE-A", "ALICE-C"], "paths": {}}
        other["source"]["rev"] = git("rev-parse", "HEAD", cwd=self.w.work / "ALICE-A").strip()
        base = Path(self.tmp.name) / "base.toml"
        base.write_text(self.w.table(self.w.uniform(repos=["ALICE-A", "ALICE-B", "ALICE-C"]), other), encoding="utf-8")
        self.t.write_text(self.w.table(self.w.uniform(), other), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 0, out)  # without a base: a rollout note, as before
        self.assertIn("unlisted holder, same bytes", out)
        code, out = self.live("--base-table", str(base))
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/s.sh: ALICE-C is listed in the base table and this change removed it", out)

    def expiring(self, until: str) -> None:
        self.t.write_text(self.w.table(exempt={"ALICE-C": {"reason": "its own script until it converges",
                                                           "until": until}}), encoding="utf-8")

    def test_an_exemption_with_until_holds_until_its_date(self):
        self.expiring("2026-10-20")
        code, out = self.live(today="2026-10-20")
        self.assertEqual(code, 0, out)

    def test_an_expired_exemption_whose_copy_still_differs_is_red(self):
        # the exemption cannot outlive its reason: after `until`, ALICE-C's copy must be the row's bytes
        self.expiring("2026-10-20")
        code, out = self.live(today="2026-10-21")
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/s.sh: the exemption of ALICE-C expired on 2026-10-20", out)

    def test_an_expired_exemption_whose_copy_converged_passes(self):
        self.expiring("2026-10-20")
        self.w.commit("ALICE-C", {"scripts/s.sh": SHARED})
        code, out = self.live(today="2026-10-21")
        self.assertEqual(code, 0, out)

    def test_the_count_lock_is_red_on_a_different_count(self):
        self.t.write_text(self.w.table(exempt={"ALICE-C": "a different script of its own"}), encoding="utf-8")
        self.assertEqual(self.live("--expect-copies", "3")[0], 1)

    def test_the_count_lock_counts_only_the_copies_of_uniform_rows(self):
        # a pending row's copies appear as its rollout lands; the lock does not move with them
        rev = self.w.commit("ALICE-C", {"scripts/c.sh": b"c\n"})
        self.w.commit("ALICE-A", {"scripts/c.sh": b"c\n"})
        c_row = {"path": "scripts/c.sh", "sha256": h(b"c\n"), "source": {"repo": "ALICE-C", "rev": rev},
                 "repos": ["ALICE-A", "ALICE-C"], "paths": {}, "exempt": {}}
        self.t.write_text(self.w.table(c_row, self.w.pending()), encoding="utf-8")
        code, out = self.live("--expect-copies", "2")
        self.assertEqual(code, 0, out)
        self.assertIn("count lock: 2 copies of uniform rows", out)
        self.assertEqual(self.live("--expect-copies", "5")[0], 1)

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

    def holder_c(self, data: bytes):
        """ALICE-C is read through a row of its own (scripts/c.sh), and holds scripts/s.sh with `data`"""
        rev = self.w.commit("ALICE-C", {"scripts/c.sh": b"c\n", "scripts/s.sh": data})
        self.w.commit("ALICE-A", {"scripts/c.sh": b"c\n"})
        c_row = {"path": "scripts/c.sh", "sha256": h(b"c\n"), "source": {"repo": "ALICE-C", "rev": rev},
                 "repos": ["ALICE-A", "ALICE-C"], "paths": {}, "exempt": {}}
        self.t.write_text(self.w.table(c_row, self.w.uniform()), encoding="utf-8")

    def test_an_unlisted_holder_with_the_same_bytes_is_a_counted_note(self):
        # a rollout landed before the table was regenerated: not drift
        self.holder_c(SHARED)
        code, out = self.live()
        self.assertEqual(code, 0, out)
        self.assertIn("unlisted holder, same bytes: 1", out)
        self.assertIn("unlisted holder: scripts/s.sh on ALICE-C main", out)
        self.assertNotIn("pending: unlisted", out)

    def test_an_unlisted_holder_with_one_byte_changed_is_red(self):
        data = bytearray(SHARED)
        data[0] ^= 1
        self.holder_c(bytes(data))
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn("present on ALICE-C main (sha256", out)

    def test_strict_makes_an_unlisted_holder_with_the_same_bytes_a_finding(self):
        self.holder_c(SHARED)
        code, out = self.live("--strict")
        self.assertEqual(code, 1, out)
        self.assertIn("present on ALICE-C main (the same bytes as its entry)", out)

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


class Planned(Base):
    def live(self, *argv: str, today: str = "2026-10-17"):
        return self.check("--live", "--today", today, *argv)

    def new_file(self, since: str = "2026-10-17") -> dict:
        """scripts/n.sh, planned into ALICE-A (the owner) and ALICE-B, no main holds it yet"""
        return {"path": "scripts/n.sh", "repos": ["ALICE-A", "ALICE-B"], "paths": {}, "exempt": {},
                "pending": {"since": since, "exit": "every listed repository's main has the bytes the registry row "
                            "moves to", "target": h(V3), "owner": "ALICE-A", "planned": sfc.git_blob(V3),
                            "variants": {"absent": ["ALICE-A", "ALICE-B"]}}}

    def test_a_new_file_s_owner_lands_within_three_days(self):
        self.w.ci("ALICE-A", "n-v3", {"scripts/n.sh": V3})
        self.t.write_text(self.w.table(self.w.uniform(), self.new_file(since="2026-10-14")), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 0, out)
        self.t.write_text(self.w.table(self.w.uniform(), self.new_file(since="2026-10-13")), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/n.sh: new file planned since 2026-10-13", out)

    def test_a_new_file_s_copy_with_other_bytes_is_red(self):
        # while the row is all absent the table records no copy: a non-owner main with any bytes but the target is a
        # finding, whatever else the live check says about it
        self.w.ci("ALICE-A", "n-v3", {"scripts/n.sh": V3})
        self.w.commit("ALICE-B", {"scripts/n.sh": OTHER})
        self.t.write_text(self.w.table(self.w.uniform(), self.new_file()), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn(f"scripts/n.sh: new file: ALICE-B main has sha256 {h(OTHER)[:12]}, not the target", out)

    def test_a_new_file_s_owner_landed_is_past_the_three_days(self):
        # once the owner's main holds the planned bytes the row is an ordinary planned one: the 3 days of a new file
        # no longer apply (the table, not regenerated yet, still records every copy absent)
        self.w.commit("ALICE-A", {"scripts/n.sh": V3})
        self.t.write_text(self.w.table(self.w.uniform(), self.new_file(since="2026-10-13")), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 0, out)

    def test_a_moved_row_recorded_all_absent_is_not_read_as_a_new_file(self):
        # a row that moves from earlier bytes is a rollout, not a new file, even when the table records every copy
        # absent: its owner's main must hold the target (or the earlier bytes), a ci/ tip is not enough
        self.w.ci("ALICE-A", "n-v3", {"scripts/n.sh": V3})
        e = self.new_file()
        e["pending"]["moved_from"] = {"planned": sfc.git_blob(SHARED), "target": h(SHARED)}
        self.t.write_text(self.w.table(self.w.uniform(), e), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/n.sh: planned row, but the owner ALICE-A main does not hold the target", out)

    def test_a_planned_row_whose_owner_holds_the_target_passes(self):
        self.t.write_text(self.w.table(self.w.planned()), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 0, out)
        self.assertIn("pending: scripts/s.sh: pending 7 day(s), 2 variant(s)", out)

    def test_a_planned_id_that_is_not_the_target_bytes_is_red(self):
        p = self.w.planned()
        p["pending"]["planned"] = sfc.git_blob(OTHER)
        self.t.write_text(self.w.table(p), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn("the owner ALICE-A main does not hold the target", out)

    def test_an_owner_whose_main_lacks_the_target_is_red(self):
        self.t.write_text(self.w.table(self.w.planned()), encoding="utf-8")
        self.w.commit("ALICE-A", {"scripts/s.sh": OTHER})
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn("the owner ALICE-A main does not hold the target", out)

    def test_a_planned_row_that_has_converged_is_red(self):
        self.t.write_text(self.w.table(self.w.planned()), encoding="utf-8")
        self.w.commit("ALICE-C", {"scripts/s.sh": SHARED})
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn("it has converged", out)

    def test_a_planned_row_past_its_own_time_is_red(self):
        # 20 days: inside the unplanned 21, outside the planned 14
        self.t.write_text(self.w.table(self.w.planned()), encoding="utf-8")
        code, out = self.live(today="2026-10-30")
        self.assertEqual(code, 1, out)
        self.assertIn("more than 14 days", out)
        self.assertEqual(self.live(today="2026-10-24")[0], 0)


    def test_a_moved_row_passes_while_the_owner_holds_either_pair(self):
        self.t.write_text(self.w.table(self.w.moved()), encoding="utf-8")
        code, out = self.live()
        self.assertEqual(code, 0, out)
        self.w.commit("ALICE-A", {"scripts/s.sh": V3})
        code, out = self.live()
        self.assertEqual(code, 0, out)
        self.assertIn("the variants on the mains have moved since the table", out)

    def test_a_moved_row_with_a_third_bytes_value_is_red(self):
        self.t.write_text(self.w.table(self.w.moved()), encoding="utf-8")
        self.w.commit("ALICE-B", {"scripts/s.sh": OTHER})
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn(f"ALICE-B main has sha256 {h(OTHER)[:12]}, none of the planned target", out)
        self.assertNotIn("stale (recorded earlier bytes): ALICE-A, ALICE-B", out)

    def test_a_mid_rollout_re_roll_accepts_an_older_recorded_variant_and_counts_it_stale(self):
        # the row recorded OTHER (an older rollout's bytes) on ALICE-B; the re-roll moves from SHARED to V3
        row = self.w.moved(variants={h(SHARED): ["ALICE-A"], h(OTHER): ["ALICE-B"]})
        self.t.write_text(self.w.table(row), encoding="utf-8")
        self.w.commit("ALICE-B", {"scripts/s.sh": OTHER})
        code, out = self.live()
        self.assertEqual(code, 0, out)
        self.assertIn("moving to " + h(V3)[:12] + ": 0 of 2 copies at the target; stale (recorded earlier bytes): "
                      "ALICE-A, ALICE-B", out)

    def test_a_moving_row_with_an_unrecorded_fourth_value_is_red(self):
        row = self.w.moved(variants={h(SHARED): ["ALICE-A"], h(OTHER): ["ALICE-B"]})
        self.t.write_text(self.w.table(row), encoding="utf-8")
        self.w.commit("ALICE-B", {"scripts/s.sh": b"never recorded\n"})
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn("or a variant the table recorded", out)

    def test_a_moved_row_whose_owner_holds_neither_pair_is_red(self):
        self.t.write_text(self.w.table(self.w.moved()), encoding="utf-8")
        self.w.commit("ALICE-A", {"scripts/s.sh": OTHER})
        code, out = self.live()
        self.assertEqual(code, 1, out)
        self.assertIn("the owner ALICE-A main does not hold the target", out)

    def test_a_moved_row_whose_earlier_bytes_are_gone_is_noted(self):
        self.t.write_text(self.w.table(self.w.moved()), encoding="utf-8")
        self.w.commit("ALICE-A", {"scripts/s.sh": V3})
        self.w.commit("ALICE-B", {"scripts/s.sh": None})
        code, out = self.live()
        self.assertEqual(code, 0, out)
        self.assertIn("no main holds the bytes the planned row moved from any more", out)


class FirstMove(Base):
    """The first move of a uniform row (SHARED on ALICE-A and ALICE-B) to V3, which no main holds yet"""

    TODAY = "2026-10-20"

    def first(self, **override) -> dict:
        return self.w.moved(**{"since": self.TODAY, "first_move": True, **override})

    def live(self, *entries: dict, today: str = TODAY):
        self.t.write_text(self.w.table(*entries), encoding="utf-8")
        return self.check("--live", "--today", today)

    def base(self, *entries: dict) -> Path:
        b = Path(self.tmp.name) / "base.toml"
        b.write_text(self.w.table(*entries), encoding="utf-8")
        return b

    def test_the_flag_is_only_for_a_planned_row_with_moved_from(self):
        e = self.w.planned()
        e["pending"]["first_move"] = True
        self.t.write_text(self.w.table(e), encoding="utf-8")
        code, out = self.check()
        self.assertEqual(code, 2, out)
        self.assertIn("pending.first_move must be true, on a planned row with moved_from", out)

    def test_the_planned_bytes_must_exist_with_the_target_s_sha256(self):
        code, out = self.live(self.first())
        self.assertEqual(code, 1, out)
        self.assertIn("on the tip of neither its main nor a ci/ branch", out)
        # a ci/ branch of another repository does not count, nor a branch of the owner outside main and ci/
        self.w.ci("ALICE-B", "s-v3", {"scripts/s.sh": V3})
        self.assertEqual(self.live(self.first())[0], 1)
        self.w.ci("ALICE-A", "s-v3", {"scripts/s.sh": V3}, prefix="wip/")
        self.assertEqual(self.live(self.first())[0], 1)
        self.w.ci("ALICE-A", "s-v3", {"scripts/s.sh": V3})
        code, out = self.live(self.first())
        self.assertEqual(code, 0, out)
        # a typo'd target (the blob is there, its SHA-256 is not the table's)
        code, out = self.live(self.first(target="e" * 64))
        self.assertEqual(code, 1, out)
        self.assertIn("not the target eeeeeeeeeeee", out)

    def proof(self, data: bytes, name: str | None = None) -> Path:
        d = Path(self.tmp.name) / sfc.PLANNED_DIR
        d.mkdir(exist_ok=True)
        f = d / (name or sfc.git_blob(data))
        f.write_bytes(data)
        return f

    def test_the_planned_bytes_may_be_carried_next_to_the_table(self):
        # the owner cannot push them first once it runs the crate check: the table's repository carries them
        self.proof(V3)
        code, out = self.live(self.first())
        self.assertEqual(code, 0, out)
        self.assertIn("planned bytes: 1 file(s) in planned/ compared with their rows", out)
        # offline too: the file is checked against its row in every mode
        self.t.write_text(self.w.table(self.first()), encoding="utf-8")
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("planned bytes: 1 file(s)", out)

    def test_a_carried_file_with_one_byte_changed_is_red(self):
        f = self.proof(V3)
        f.write_bytes(V3[:-1] + b"X")  # its name still says V3's blob
        code, out = self.live(self.first())
        self.assertEqual(code, 1, out)
        self.assertIn("its git blob id is", out)
        self.assertIn("not the target", out)

    def test_a_carried_file_with_the_right_sha256_under_another_blob_name_is_red(self):
        # the bytes hash to the target, but the row plans another blob: the name binds the file to the row
        row = self.first(planned="1" * 40)
        self.proof(V3, name="1" * 40)
        code, out = self.live(row)
        self.assertEqual(code, 1, out)
        self.assertIn("its git blob id is", out)
        self.assertIn("not its name", out)

    def test_a_carried_file_with_the_blob_name_but_another_target_is_red(self):
        self.proof(V3)
        code, out = self.live(self.first(target="e" * 64))
        self.assertEqual(code, 1, out)
        self.assertIn("not the target eeeeeeeeeeee", out)

    def test_a_planted_file_no_row_names_is_red(self):
        self.proof(V3)
        self.proof(b"planted\n")
        code, out = self.live(self.first())
        self.assertEqual(code, 1, out)
        self.assertIn("no planned row names it", out)

    def test_a_carried_file_left_after_the_row_converged_is_red(self):
        # the move landed everywhere and the table was regenerated to uniform: the file is stale
        self.proof(V3)
        self.t.write_text(self.w.table(self.w.uniform(sha256=h(V3))), encoding="utf-8")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("no planned row names it", out)

    def test_the_planned_directory_holds_files_only(self):
        (Path(self.tmp.name) / sfc.PLANNED_DIR / "sub").mkdir(parents=True)
        self.t.write_text(self.w.table(self.first()), encoding="utf-8")
        self.assertEqual(self.check()[0], 2)

    def test_the_owner_lands_within_three_days(self):
        self.w.ci("ALICE-A", "s-v3", {"scripts/s.sh": V3})
        self.assertEqual(self.live(self.first(since="2026-10-17"))[0], 0)
        code, out = self.live(self.first(since="2026-10-16"))
        self.assertEqual(code, 1, out)
        self.assertIn("has not landed", out)
        # once the owner has landed, the planned bytes are on its main and the 3 days no longer apply
        self.w.commit("ALICE-A", {"scripts/s.sh": V3})
        landed = self.first(since="2026-10-16", variants={h(SHARED): ["ALICE-B"], h(V3): ["ALICE-A"]})
        code, out = self.live(landed)
        self.assertEqual(code, 0, out)

    def test_no_third_value_and_no_missing_copy_while_it_moves(self):
        self.w.ci("ALICE-A", "s-v3", {"scripts/s.sh": V3})
        self.w.commit("ALICE-B", {"scripts/s.sh": OTHER})
        code, out = self.live(self.first())
        self.assertEqual(code, 1, out)
        self.assertIn("first move: ALICE-B main has sha256", out)
        self.w.commit("ALICE-B", {"scripts/s.sh": None})
        code, out = self.live(self.first())
        self.assertEqual(code, 1, out)
        self.assertIn("first move: ALICE-B has no copy", out)

    def test_the_base_must_be_uniform_with_exactly_the_bytes_it_moves_from(self):
        self.t.write_text(self.w.table(self.first()), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.w.uniform())))
        self.assertEqual(code, 0, out)
        self.assertIn("pending 1 (planned 1, unplanned 0), the base table 0 (planned 0, unplanned 0)", out)
        # the base row is not uniform (pending), or uniform with other bytes
        self.assertEqual(self.check("--base-table", str(self.base(self.w.planned())))[0], 1)
        self.assertEqual(self.check("--base-table", str(self.base(self.w.uniform(sha256=h(OTHER)))))[0], 1)
        # the same shape without the flag is refused as before
        self.t.write_text(self.w.table(self.w.moved()), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.w.uniform())))
        self.assertEqual(code, 1, out)
        self.assertIn("not the base table's planned id", out)
        # a copy recorded at other bytes or absent: not the first move of a uniform row
        for variants in ({h(SHARED): ["ALICE-A"], h(OTHER): ["ALICE-B"]}, {h(SHARED): ["ALICE-A"], "absent": ["ALICE-B"]}):
            self.t.write_text(self.w.table(self.first(variants=variants)), encoding="utf-8")
            self.assertEqual(self.check("--base-table", str(self.base(self.w.uniform())))[0], 1, variants)

    def test_the_flag_stays_until_the_owner_lands(self):
        unflagged = self.first()
        del unflagged["pending"]["first_move"]
        self.t.write_text(self.w.table(unflagged), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.first())))
        self.assertEqual(code, 1, out)
        self.assertIn("first_move dropped while the owner ALICE-A does not hold the target", out)
        # the owner has landed (the row records it holding the target): the flag goes
        landed = self.first(variants={h(SHARED): ["ALICE-B"], h(V3): ["ALICE-A"]})
        del landed["pending"]["first_move"]
        self.t.write_text(self.w.table(landed), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.first())))
        self.assertEqual(code, 0, out)

    def test_a_recorded_landing_is_read_against_the_owner_s_main(self):
        # the table claims the owner landed (and drops the flag) while its main still has SHARED and V3 is nowhere
        forged = self.first(variants={h(SHARED): ["ALICE-B"], h(V3): ["ALICE-A"]})
        del forged["pending"]["first_move"]
        self.t.write_text(self.w.table(forged), encoding="utf-8")
        self.assertEqual(self.check("--base-table", str(self.base(self.first())))[0], 0)  # the table alone agrees
        code, out = self.live(forged)
        self.assertEqual(code, 1, out)
        self.assertIn("the table records the owner ALICE-A at the target", out)
        # the owner has landed for real: live passes
        self.w.commit("ALICE-A", {"scripts/s.sh": V3})
        code, out = self.live(forged)
        self.assertEqual(code, 0, out)

    def test_a_pending_date_after_today_is_a_finding(self):
        self.w.ci("ALICE-A", "s-v3", {"scripts/s.sh": V3})
        code, out = self.live(self.first(since="2027-10-20"))
        self.assertEqual(code, 1, out)
        self.assertIn("after today (2026-10-20): a future date switches the age limits off", out)
        # the same for a pending row that is not a first move
        code, out = self.live(self.w.pending(since="2026-10-21"))
        self.assertEqual(code, 1, out)
        self.assertIn("pending since 2026-10-21, after today", out)

    def test_a_first_move_is_re_rolled_from_the_same_bytes_on_the_same_date(self):
        # a wrong plan rolls forward in one commit: from the bytes the base moves from, the flag and the date kept
        v4 = SHARED + b"# v4\n"
        second = self.first(target=h(v4), planned=sfc.git_blob(v4))
        self.t.write_text(self.w.table(second), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.first())))
        self.assertEqual(code, 0, out)
        # a later date restarts the clock
        self.t.write_text(self.w.table(self.first(target=h(v4), planned=sfc.git_blob(v4), since="2026-10-29")),
                          encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.first())))
        self.assertEqual(code, 1, out)
        self.assertIn("only stays or goes back", out)
        # from other bytes than the base's move: a second move before the first has landed
        other = self.first(target=h(v4), planned=sfc.git_blob(v4),
                           moved_from={"planned": sfc.git_blob(OTHER), "target": h(OTHER)},
                           variants={h(OTHER): ["ALICE-A", "ALICE-B"]})
        self.t.write_text(self.w.table(other), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.first())))
        self.assertEqual(code, 1, out)
        self.assertIn("one planned move at a time", out)
        # without the flag while the owner does not hold the new target
        unflagged = self.first(target=h(v4), planned=sfc.git_blob(v4))
        del unflagged["pending"]["first_move"]
        self.t.write_text(self.w.table(unflagged), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.first())))
        self.assertEqual(code, 1, out)
        self.assertIn("first_move dropped", out)
        # the same first move again (the next commit) is fine
        self.t.write_text(self.w.table(self.first()), encoding="utf-8")
        self.assertEqual(self.check("--base-table", str(self.base(self.first())))[0], 0)

    def test_a_planned_row_leaves_the_table_only_at_its_target(self):
        # closing a plan by hand (back to the bytes it moves from) and planning it again later would restart its
        # clock: a planned row of the base is uniform only at its own target, and stays in the table
        self.t.write_text(self.w.table(self.w.uniform()), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.first())))
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/s.sh: planned to", out)
        self.assertIn("leaves the table only at its target", out)
        self.t.write_text(self.w.table(self.w.uniform(path="scripts/t.sh")), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.first())))
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/s.sh: planned to", out)
        # converged at the target: fine
        self.t.write_text(self.w.table(self.w.uniform(sha256=h(V3))), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.first())))
        self.assertEqual(code, 0, out)
        # the same for a planned row that is not a first move
        self.t.write_text(self.w.table(self.w.uniform(sha256=h(OTHER))), encoding="utf-8")
        self.assertEqual(self.check("--base-table", str(self.base(self.w.planned())))[0], 1)
        self.t.write_text(self.w.table(self.w.uniform()), encoding="utf-8")
        self.assertEqual(self.check("--base-table", str(self.base(self.w.planned())))[0], 0)

    def new_file(self, data: bytes, **override) -> dict:
        e = {"path": "scripts/n.sh", "repos": ["ALICE-A", "ALICE-B"], "paths": {}, "exempt": {},
             "pending": {"since": self.TODAY, "exit": "every listed repository's main has the bytes the registry "
                         "row moves to", "target": h(data), "owner": "ALICE-A", "planned": sfc.git_blob(data),
                         "variants": {"absent": ["ALICE-A", "ALICE-B"]}}}
        e["pending"].update(override)
        return e

    def test_a_new_file_is_re_rolled_before_it_lands_on_the_same_date(self):
        v, v2 = b"#!/bin/sh\n# new\n", b"#!/bin/sh\n# new v2\n"
        self.t.write_text(self.w.table(self.w.uniform(), self.new_file(v2)), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.w.uniform(), self.new_file(v))))
        self.assertEqual(code, 0, out)
        self.t.write_text(self.w.table(self.w.uniform(), self.new_file(v2, since="2026-10-29")), encoding="utf-8")
        self.assertEqual(self.check("--base-table", str(self.base(self.w.uniform(), self.new_file(v))))[0], 1)
        # once a copy is on a main it is not a new file any more: the owner holding bytes the base did not plan
        landed = self.new_file(v2, variants={h(v2): ["ALICE-A"], "absent": ["ALICE-B"]})
        self.t.write_text(self.w.table(self.w.uniform(), landed), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.w.uniform(), self.new_file(v))))
        self.assertEqual(code, 1, out)
        # a base whose owner already held the planned bytes is not a new file any more: re-rolled to a table that
        # records every copy absent, it would move from nothing although a main had a copy
        held = self.new_file(v, variants={h(v): ["ALICE-A"], "absent": ["ALICE-B"]})
        self.t.write_text(self.w.table(self.w.uniform(), self.new_file(v2)), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.w.uniform(), held)))
        self.assertEqual(code, 1, out)
        self.assertIn("without moved_from naming the base's planned id and target", out)
        # and removing the new file's row is refused
        self.t.write_text(self.w.table(self.w.uniform()), encoding="utf-8")
        code, out = self.check("--base-table", str(self.base(self.w.uniform(), self.new_file(v))))
        self.assertEqual(code, 1, out)
        self.assertIn("scripts/n.sh: planned to", out)


class TheTable(unittest.TestCase):
    def test_the_repository_table_reads(self):
        code, out = run(sfc.main, ["--table", str(HERE.parent / "conformance" / "shared-files.toml")])
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
