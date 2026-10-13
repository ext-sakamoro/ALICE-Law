#!/usr/bin/env python3
"""Tests of scripts/public_text_check.py (python3 scripts/test_public_text_check.py)"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ptc", HERE / "public_text_check.py")
ptc = importlib.util.module_from_spec(spec)
sys.modules["ptc"] = ptc
spec.loader.exec_module(ptc)

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GIT_OPTIONAL_LOCKS": "0"}
MADE_UP = "zorblat quux"  # stands in for a private name; only its hash is in the set during a test


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**ENV, "HOME": str(repo)}).stdout


def commit(repo: Path, files: dict[str, str], message: str = "x", delete: tuple[str, ...] = ()) -> str:
    for rel, text in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    for rel in delete:
        (repo / rel).unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def run(repo: Path, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = ptc.main(["--repo", str(repo), *argv])
    return code, out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name, "r")
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        self.base = commit(self.repo, {"README.md": "# integration checks\n\nmemory footprint, a netcode session\n"})
        self.saved = ptc.PRIVATE_NAME_HASHES
        ptc.PRIVATE_NAME_HASHES = self.saved | {hashlib.sha256(MADE_UP.encode()).hexdigest()}

    def tearDown(self):
        ptc.PRIVATE_NAME_HASHES = self.saved
        self.tmp.cleanup()


class Tree(Base):
    def test_a_clean_tree_passes_and_counts(self):
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 0, out)
        self.assertIn("0 findings in 1 files", out)

    def test_internal_words_in_text_or_path_are_found(self):
        for files in ({"a.md": "two workers did it\n"}, {"a.md": "see the Backlog\n"}, {"a.md": "ys-3a found it\n"},
                      {"a.md": "user 指示で直した\n"}, {"a.md": "ssh 192.168.1.20\n"}, {"notes/ys-3a.md": "ok\n"}):
            commit(self.repo, files)
            code, out = run(self.repo, "--tree")
            self.assertEqual(code, 1, files)
            git(self.repo, "reset", "-q", "--hard", self.base)

    def test_a_private_name_is_found_and_not_printed(self):
        commit(self.repo, {"a.md": "Zorblat Quux の設定\n"})
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 1)
        self.assertIn("a.md:1: private name", out)
        self.assertNotIn("orblat", out.lower())

    def test_a_private_name_glued_to_another_word_is_found(self):
        ptc.PRIVATE_NAME_HASHES = self.saved | {hashlib.sha256(b"zorblat").hexdigest()}
        for text in ("see zorblat-notes\n", "see zorblat_notes\n", "see notes.zorblat\n", "see Zorblat-Quux-x\n",
                     "path/to/.zorblat/x\n"):
            git(self.repo, "reset", "-q", "--hard", self.base)
            commit(self.repo, {"a.md": text})
            code, out = run(self.repo, "--tree")
            self.assertEqual(code, 1, text)
            self.assertNotIn("orblat", out.lower())
        git(self.repo, "reset", "-q", "--hard", self.base)
        commit(self.repo, {"a.md": "zorblatx and xzorblat are other words\n"})
        self.assertEqual(run(self.repo, "--tree")[0], 0)

    def test_a_glued_private_name_in_a_path_is_found(self):
        ptc.PRIVATE_NAME_HASHES = self.saved | {hashlib.sha256(b"zorblat").hexdigest()}
        commit(self.repo, {"docs/zorblat-notes.md": "ok\n"})
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 1)
        self.assertIn("path docs/", out)

    def test_ordinary_words_and_commands_pass(self):
        commit(self.repo, {"ci.yml": "run: git worktree add /tmp/base main\n", "b.md": "the memory footprint\n"})
        self.assertEqual(run(self.repo, "--tree")[0], 0)

    def test_the_checker_and_its_tests_are_exempt(self):
        commit(self.repo, {"scripts/public_text_check.py": "worker Backlog\n",
                           "scripts/test_public_text_check.py": "worker\n"})
        self.assertEqual(run(self.repo, "--tree")[0], 0)


class Range(Base):
    def test_messages_added_lines_and_paths_are_found_with_their_line(self):
        commit(self.repo, {"a.md": "one\ntwo\n"})
        start = git(self.repo, "rev-parse", "HEAD").strip()
        commit(self.repo, {"a.md": "one\ntwo\nthree\nthe worker said\n"}, message="fix")
        commit(self.repo, {}, message="fix (found in the Backlog)")
        commit(self.repo, {"x/ys-3a.txt": "ok\n"})
        code, out = run(self.repo, "--range", f"{start}..HEAD")
        self.assertEqual(code, 1)
        self.assertIn("a.md:4: agent process", out)
        self.assertIn("message:1: internal tracker", out)
        self.assertIn("path x/ys-3a.txt:1: session name", out)
        self.assertIn("3 commits", out)

    def test_removed_lines_are_not_findings(self):
        commit(self.repo, {"a.md": "the worker said\n"})
        start = git(self.repo, "rev-parse", "HEAD").strip()
        commit(self.repo, {"a.md": "said\n"})
        self.assertEqual(run(self.repo, "--range", f"{start}..HEAD")[0], 0)

    def test_an_empty_range_compares_nothing_and_exits_2(self):
        self.assertEqual(run(self.repo, "--range", "HEAD..HEAD")[0], 2)

    def test_an_unknown_ref_exits_2(self):
        self.assertEqual(run(self.repo, "--range", "nope..HEAD")[0], 2)


class Vocabulary(unittest.TestCase):
    def test_every_hash_is_a_sha256(self):
        self.assertTrue(ptc.PRIVATE_NAME_HASHES)
        self.assertTrue(all(len(h) == 64 and int(h, 16) >= 0 for h in ptc.PRIVATE_NAME_HASHES))

    def test_the_repository_is_clean(self):
        code, out = run(HERE.parent, "--tree")
        self.assertEqual(code, 0, out)



class SessionIds(Base):
    """id-like tokens next to a word naming a review or a session; no id is listed"""

    def test_an_id_like_token_next_to_a_review_or_session_word_is_found(self):
        for line in ("e9 レビュー対応", "e9 の指摘で直した", "c1 review", "per e9 session", "c1レビュー",
                     "e9 セッション", "c1 検証", "e9指摘", "session 4d", "セッション 7a", "Session e9",
                     "session: e9", "session=c1", "sessions e9", "(e9/c1) review", "c1、指摘", "[e9] review"):
            self.assertEqual(ptc.kinds(line), ["session id"], line)

    def test_numbers_dates_versions_and_other_contexts_pass(self):
        for line in ("e9 tests", "0xe9 review", "#c1 review", "2026-09-15 review", "v1.53 review", "x86_64's",
                     "oracle 10's", "53 の候補", "ae9 review", "e91 review", "a netcode session", "e9", "c1-c2",
                     "session key", "F1 score and F2 keys"):
            self.assertEqual(ptc.kinds(line), [], line)

    def test_this_session_in_either_script_is_found(self):
        for line in ("この session で", "このセッションで"):
            self.assertEqual(ptc.kinds(line), ["agent process"], line)

    def test_review_labels_are_found(self):
        for line in ("F1/F2 review", "F3 の指摘", "F4 レビュー"):
            self.assertEqual(ptc.kinds(line), ["session id"], line)

    def test_a_commit_message_with_an_id_is_found_and_the_id_not_printed(self):
        commit(self.repo, {"a.txt": "x\n"}, message="fix: e9 の指摘")
        code, out = run(self.repo, "--range", f"{self.base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("message:1: session id", out)
        # the commit hashes in the output are hex and may hold the two letters; the id as a word may not
        self.assertNotRegex(out, r"(?<![0-9a-f])e9(?![0-9a-f])")
        self.assertNotIn("の指摘", out)

    def test_the_checker_holds_no_list_of_ids(self):
        self.assertFalse(hasattr(ptc, "SESSION_ID_HASHES"))



class AllowList(Base):
    """a product's mention of a name, allowed per repository, file and name"""

    def setUp(self):
        super().setUp()
        self.saved_allowed = ptc.ALLOWED
        self.addCleanup(lambda: setattr(ptc, "ALLOWED", self.saved_allowed))
        commit(self.repo, {"docs/plugin.md": f"install the {MADE_UP} plugin\n",
                           "docs/other.md": f"see {MADE_UP}\n"})

    def entry(self, path: str, name: str = MADE_UP) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ptc.main(["--allow-hash", path, name]), 0)
        return out.getvalue().strip()

    def test_the_helper_builds_the_hash_the_matcher_uses(self):
        self.assertEqual(self.entry("docs/plugin.md"), ptc.allow_hash("docs/plugin.md", MADE_UP))
        self.assertEqual(self.entry("docs/plugin.md", "  ZORBLAT   Quux "), self.entry("docs/plugin.md"))
        self.assertRegex(self.entry("docs/plugin.md"), r"^[0-9a-f]{64}$")

    def test_the_helper_refuses_a_word_the_check_does_not_find(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ptc.main(["--allow-hash", "docs/plugin.md", "ordinary words"]), 2)

    def test_an_allowed_mention_passes_only_in_its_file_and_repository(self):
        ptc.ALLOWED = {"r": {self.entry("docs/plugin.md")}}
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("docs/other.md:1: private name", out)
        self.assertNotIn("docs/plugin.md", out)
        self.assertIn("(1 allowed mentions)", out)
        code, out = run(self.repo, "--repo-name", "another", "--tree")
        self.assertIn("docs/plugin.md:1: private name", out)

    def test_both_files_allowed_pass(self):
        ptc.ALLOWED = {"r": {self.entry("docs/plugin.md"), self.entry("docs/other.md")}}
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 0, out)

    def test_an_entry_that_allows_nothing_fails(self):
        ptc.ALLOWED = {"r": {self.entry("docs/plugin.md"), self.entry("docs/other.md"), self.entry("docs/gone.md")}}
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("allows nothing", out)
        self.assertNotIn(MADE_UP, out)

    def test_another_repositorys_entries_are_not_stale_here(self):
        ptc.ALLOWED = {"r": {self.entry("docs/plugin.md"), self.entry("docs/other.md")}, "elsewhere": {"0" * 64}}
        self.assertEqual(run(self.repo, "--repo-name", "r", "--tree")[0], 0)

    def test_added_lines_of_a_range_are_allowed_too(self):
        ptc.ALLOWED = {"r": {self.entry("docs/new.md")}}
        commit(self.repo, {"docs/new.md": f"the {MADE_UP} manifest\n"})
        code, out = run(self.repo, "--repo-name", "r", "--range", "HEAD~1..HEAD")
        self.assertEqual(code, 0, out)

    def test_commit_messages_are_never_allowed(self):
        ptc.ALLOWED = {"r": {self.entry("docs/plugin.md")}}
        commit(self.repo, {"a.txt": "y\n"}, message=f"add {MADE_UP}")
        code, out = run(self.repo, "--repo-name", "r", "--range", "HEAD~1..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("message:1: private name", out)

    def test_the_repository_name_comes_from_the_origin_url(self):
        git(self.repo, "remote", "add", "origin", "https://example.invalid/owner/some-repo.git")
        self.assertEqual(ptc.repo_name(str(self.repo)), "some-repo")
        git(self.repo, "remote", "set-url", "origin", "git@host:owner/other.git")
        self.assertEqual(ptc.repo_name(str(self.repo)), "other")

if __name__ == "__main__":
    unittest.main()
