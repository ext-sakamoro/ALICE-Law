#!/usr/bin/env python3
"""Tests of scripts/public_text_check.py (python3 scripts/test_public_text_check.py)"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
import re
import shutil
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
        for files in ({"a.md": "two workers did it\n"}, {"a.md": "see the Backlog\n"}, {"a.md": "ys-0b found it\n"},
                      {"a.md": "user 指示で直した\n"}, {"a.md": "ssh 192.168.1.20\n"}, {"notes/ys-0b.md": "ok\n"}):
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

    def test_the_git_subcommand_as_an_argument_list_is_not_a_finding(self):
        for line in ('subprocess.run([*self.git_argv, "worktree", "remove", "--force", wt])',
                     "checkout_git('worktree', 'add', '-q', '--detach', wt, sha)",
                     'git(self.r.work, "worktree", "list", "--porcelain")'):
            self.assertEqual(ptc.kinds(line), [], line)
        for line in ('msg = "found by the worker"',               # another word in a string
                     'note = "the worktree", "was removed"',      # not followed by a subcommand
                     'args = [worktree, "add"]',                  # not quoted
                     'labels = ["worktree", "branch"]',           # followed by a word that is no subcommand
                     'title = "a worktree", "add"',               # a phrase, not the bare argument
                     'subprocess.run(["git", "worktree", "add"]); msg = "a worker did it"'):
            self.assertIn("agent process", ptc.kinds(line), line)

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
        commit(self.repo, {"x/ys-0b.txt": "ok\n"})
        code, out = run(self.repo, "--range", f"{start}..HEAD")
        self.assertEqual(code, 1)
        self.assertIn("a.md:4: agent process", out)
        self.assertIn("message:1: internal tracker", out)
        self.assertIn("path x/ys-0b.txt:1: session name", out)
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


class SubjectId(Base):
    """an id-like token in brackets at the end of a commit subject"""

    def test_a_bracketed_id_at_the_end_of_the_subject_is_a_finding(self):
        for subject in ("fix: tighten the byte-identity oracle (70)", "docs: wording (fb)", "ci: gate ( 6e )"):
            with self.subTest(subject=subject):
                commit(self.repo, {"a.md": subject + "\n"}, message=subject)
                code, out = run(self.repo, "--range", "HEAD~1..HEAD")
                self.assertEqual(code, 1, out)
                self.assertIn("message:1: session id", out)

    def test_other_bracketed_endings_pass(self):
        for subject in ("fix: the issue (#70)", "release (v0.7)", "speed up (x2)", "fix (700)", "note (70) in the middle"):
            with self.subTest(subject=subject):
                commit(self.repo, {"a.md": subject + "\n"}, message=subject)
                self.assertEqual(run(self.repo, "--range", "HEAD~1..HEAD")[0], 0, subject)

    def test_only_the_subject_line_is_read_for_this(self):
        commit(self.repo, {"a.md": "y\n"}, message="fix: a\n\nmeasured 3 runs (70)")
        self.assertEqual(run(self.repo, "--range", "HEAD~1..HEAD")[0], 0)


class LedgerLabels(Base):
    """a row label of an internal list (F11, R2, C3, A1, B4) next to a word naming the list or its row"""

    DETMATH_SUBJECT = "tracked_binaries: ALICE-SDF の検査を移植 (test は F11 canonical = symlink + submodule 両ケースに統一)"

    def test_a_label_next_to_a_list_word_is_found(self):
        for line in (self.DETMATH_SUBJECT, "R10 ledger row", "C2 行", "台帳の A3", "F7 行目を直した", "項目 B12 は未対応",
                     "see tracker R4", "canonical F1", "A9 の行を更新"):
            self.assertIn("internal tracker", ptc.kinds(line), line)

    def test_public_uses_pass(self):
        lines = [f"G{n} {w}" for n in range(1, 15) for w in ("canonical", "ledger row", "行", "台帳", "tracker", "pass")]
        lines += [f"台帳の G{n}" for n in range(1, 15)]
        lines += ["F1 score", "R2 value", "R² value", "C2 symmetry", "C2 continuity", "B3 tree", "A4 paper",
                  "R10 resistor", "F16 jet", "R36.4.3", "F32 canonical NaN", "canonical F64 bits", "B2 行列の剪断行",
                  "実行 R2", "the C2 row of the table in §1.3", "item R2 of the plan", "F1. A canonical form",
                  "AVX2 canonical", "x86 B1 stepping"]
        for line in lines:
            self.assertNotIn("internal tracker", ptc.kinds(line), line)

    def test_a_commit_message_with_the_label_fails_the_range(self):
        commit(self.repo, {"a.txt": "x\n"}, message=self.DETMATH_SUBJECT)
        code, out = run(self.repo, "--range", f"{self.base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("message:1: internal tracker", out)


class Vocabulary(unittest.TestCase):
    def test_every_hash_is_a_sha256(self):
        self.assertTrue(ptc.PRIVATE_NAME_HASHES)
        self.assertTrue(all(len(h) == 64 and int(h, 16) >= 0 for h in ptc.PRIVATE_NAME_HASHES))

    def test_the_private_name_set_is_pinned(self):
        # its size and the SHA-256 of its sorted lines: removing or changing a name changes them
        spec = importlib.util.spec_from_file_location("ptc_fresh", HERE / "public_text_check.py")
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)  # (other tests swap ptc's set)
        h = sorted(fresh.PRIVATE_NAME_HASHES)
        self.assertEqual((len(h), hashlib.sha256("\n".join(h).encode()).hexdigest()),
                         (48, "324204d07160abe230e73b824cbf06344d15f6257153ef29067f149436c643de"))

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
            self.assertIn("agent process", ptc.kinds(line), line)

    def test_review_labels_are_found(self):
        for line in ("F1/F2 review", "F3 の指摘", "F4 レビュー"):
            self.assertEqual(ptc.kinds(line), ["session id"], line)

    def with_ids(self, *ids: str):
        saved = ptc.SESSION_TOKEN_HASHES
        self.addCleanup(lambda: setattr(ptc, "SESSION_TOKEN_HASHES", saved))
        ptc.SESSION_TOKEN_HASHES = {hashlib.sha256(i.encode()).hexdigest() for i in ids}

    def test_in_a_commit_message_a_current_id_is_found_as_a_whole_token(self):
        self.with_ids("e9", "42")
        for line in ("fix: tidy the parser (e9)", "e9 検証: ok", "per e9", "reviewed by [e9/42]", "(42)", "e9-verified", "verified by 42",
                     "from 42", "cc 42", "per 42"):
            self.assertTrue(ptc.commit_session_tokens(line), line)
        for line in ("42 comparisons, 0 problems", "grew by 42%", "multiply by 42.5", "E9 done (ids are lower case)", "commit 0be9fac0", "x86_64 e9a1", "B3 phase", "v1.e9",
                     "#e9", "ae9", "1-e9", "max error e9-8", "tolerance 1e9+3", "max(fa, `e9`)"):
            self.assertFalse(ptc.commit_session_tokens(line), line)

    def test_the_commit_rule_does_not_apply_to_file_text(self):
        self.with_ids("e9")
        commit(self.repo, {"doc.md": "the joint (e9) and plan B3\n"}, message="docs: add the joint table")
        code, out = run(self.repo, "--range", f"{self.base}..HEAD")
        self.assertEqual(code, 0, out)
        commit(self.repo, {"doc2.md": "x\n"}, message="docs: more (e9)")
        code, out = run(self.repo, "--range", f"{self.base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("message:1: session id", out)
        self.assertNotIn("(e9)", out)  # the message text is not printed (a commit hash may contain the letters)

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



class AllowSkillWord(Base):
    """a product's own Agent Skills files: the format word is allowed per file, word and line count;
    any other process word there, a new file and a changed count stay findings"""

    SK = "the " + "ski" + "ll format, see the guide\n"  # the word split so this file's own text stays clean

    def setUp(self):
        super().setUp()
        self.saved_allowed = ptc.ALLOWED
        self.addCleanup(lambda: setattr(ptc, "ALLOWED", self.saved_allowed))
        commit(self.repo, {"pkg/a.md": self.SK * 2})
        word = "ski" + "ll"
        ptc.ALLOWED = {"r": {ptc.allow_hash("pkg/a.md", word): 2}}

    def test_the_word_at_its_count_passes(self):
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 0, out)

    def test_another_process_word_on_an_allowed_line_is_a_finding(self):
        commit(self.repo, {"pkg/a.md": self.SK + "a " + "wor" + "ker runs the " + "ski" + "ll\n"})
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("pkg/a.md:2: agent process", out)

    def test_a_new_file_with_the_word_is_a_finding(self):
        commit(self.repo, {"pkg/b.md": self.SK})
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("pkg/b.md:1: agent process", out)

    def test_a_changed_count_is_a_finding(self):
        commit(self.repo, {"pkg/a.md": self.SK * 3})
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("allows 3 lines", out)


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
        ptc.ALLOWED = {"r": {self.entry("docs/plugin.md"): 1}}
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("docs/other.md:1: private name", out)
        self.assertNotIn("docs/plugin.md", out)
        self.assertIn("(1 allowed mentions)", out)
        code, out = run(self.repo, "--repo-name", "another", "--tree")
        self.assertIn("docs/plugin.md:1: private name", out)

    def test_both_files_allowed_pass(self):
        ptc.ALLOWED = {"r": {self.entry("docs/plugin.md"): 1, self.entry("docs/other.md"): 1}}
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 0, out)

    def test_an_entry_that_allows_nothing_fails(self):
        ptc.ALLOWED = {"r": {self.entry("docs/plugin.md"): 1, self.entry("docs/other.md"): 1, self.entry("docs/gone.md"): 1}}
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("it allows nothing", out)
        self.assertNotIn(MADE_UP, out)

    def test_another_repositorys_entries_are_not_stale_here(self):
        ptc.ALLOWED = {"r": {self.entry("docs/plugin.md"): 1, self.entry("docs/other.md"): 1}, "elsewhere": {"0" * 64: 1}}
        self.assertEqual(run(self.repo, "--repo-name", "r", "--tree")[0], 0)

    def test_added_lines_of_a_range_are_allowed_too(self):
        ptc.ALLOWED = {"r": {self.entry("docs/new.md"): 1}}
        commit(self.repo, {"docs/new.md": f"the {MADE_UP} manifest\n"})
        code, out = run(self.repo, "--repo-name", "r", "--range", "HEAD~1..HEAD")
        self.assertEqual(code, 0, out)

    def test_commit_messages_are_never_allowed(self):
        ptc.ALLOWED = {"r": {self.entry("docs/plugin.md"): 1}}
        commit(self.repo, {"a.txt": "y\n"}, message=f"add {MADE_UP}")
        code, out = run(self.repo, "--repo-name", "r", "--range", "HEAD~1..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("message:1: private name", out)

    def test_a_prefix_entry_allows_every_file_under_it_by_whole_components(self):
        commit(self.repo, {"kits/cad/a.md": f"{MADE_UP} one\n", "kits/cad/sub/b.md": f"{MADE_UP} two\n",
                           "kits/cadx/c.md": f"{MADE_UP} three\n"})
        ptc.ALLOWED = {"r": {self.entry("kits/cad/"): 2, self.entry("docs/plugin.md"): 1, self.entry("docs/other.md"): 1}}
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("kits/cadx/c.md:1: private name", out)
        self.assertNotIn("kits/cad/a.md", out)

    def test_the_count_is_pinned(self):
        ptc.ALLOWED = {"r": {self.entry("docs/plugin.md"): 2, self.entry("docs/other.md"): 1}}
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("allows 1 lines", out)

    def test_a_count_below_one_is_refused(self):
        ptc.ALLOWED = {"r": {self.entry("docs/plugin.md"): 1, self.entry("docs/other.md"): 1, self.entry("docs/gone.md"): 0}}
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("has count 0", out)

    def test_an_agent_word_or_a_note_link_can_be_allowed_too(self):
        commit(self.repo, {"skills/x/SKILL.md": "the skills entry [[kv_namespaces]]\n"})
        # (the path names the word too: the path line is one of the entry's lines)
        ptc.ALLOWED = {"r": {self.entry("skills/x/SKILL.md", "skills"): 2, self.entry("skills/x/SKILL.md", "[[kv_namespaces]]"): 1,
                             self.entry("docs/plugin.md"): 1, self.entry("docs/other.md"): 1}}
        code, out = run(self.repo, "--repo-name", "r", "--tree")
        self.assertEqual(code, 0, out)

    def test_a_literal_pathspec_keeps_a_glob_name_to_itself(self):
        # "a*.md" next to "a.md": without a literal pathspec, a.md's added lines are read under a*.md
        ptc.ALLOWED = {"r": {self.entry("a*.md"): 1}}
        commit(self.repo, {"a*.md": f"{MADE_UP} here\n", "a.md": f"{MADE_UP} there\n"})
        code, out = run(self.repo, "--repo-name", "r", "--range", "HEAD~1..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn(" a.md:1: private name", out)
        self.assertNotIn("a*.md:", out)
        self.assertIn("(1 allowed mentions)", out)  # a.md's line was not read (and allowed) under a*.md

    def test_range_reports_the_files_it_read(self):
        commit(self.repo, {"x.md": "fine\n", "y.md": "fine\n"})
        code, out = run(self.repo, "--range", "HEAD~1..HEAD")
        self.assertIn("1 commits (2 files)", out)

    def test_the_repository_name_comes_from_the_origin_url(self):
        git(self.repo, "remote", "add", "origin", "https://example.invalid/owner/some-repo.git")
        self.assertEqual(ptc.repo_name(str(self.repo)), "some-repo")
        git(self.repo, "remote", "set-url", "origin", "git@host:owner/other.git")
        self.assertEqual(ptc.repo_name(str(self.repo)), "other")


class Paths(Base):
    """names git would quote (non-ASCII), or end in a tab (a space), are read as they are"""
    NAMES = ("docs/日本.md", "sp ace.md", "tab\tname.md", "star*.md")

    def test_tree_mode_scans_every_name(self):
        commit(self.repo, {n: "a worker line\n" for n in self.NAMES})
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 1, out)
        for n in self.NAMES:
            self.assertIn(f"{n}:1: agent process", out)

    def test_range_mode_scans_every_name(self):
        commit(self.repo, {n: "a worker line\n" for n in self.NAMES})
        code, out = run(self.repo, "--range", "HEAD~1..HEAD")
        self.assertEqual(code, 1, out)
        for n in self.NAMES:
            self.assertIn(f" {n}:1: agent process", out)
        self.assertEqual(out.count("agent process"), len(self.NAMES), out)  # star*.md is not a pattern

    def test_an_allowed_mention_in_such_a_file_is_allowed_in_both_modes(self):
        saved = ptc.ALLOWED
        self.addCleanup(lambda: setattr(ptc, "ALLOWED", saved))
        ptc.ALLOWED = {"r": {ptc.allow_hash(n, MADE_UP): 1 for n in self.NAMES}}
        commit(self.repo, {n: f"the {MADE_UP} plugin\n" for n in self.NAMES})
        self.assertEqual(run(self.repo, "--repo-name", "r", "--range", "HEAD~1..HEAD")[0], 0)
        self.assertEqual(run(self.repo, "--repo-name", "r", "--tree")[0], 0)

    def test_a_deleted_file_is_not_read(self):
        commit(self.repo, {"docs/日本.md": "fine\n"})
        commit(self.repo, {}, delete=("docs/日本.md",))
        self.assertEqual(run(self.repo, "--range", "HEAD~1..HEAD")[0], 0)

    def test_the_root_commit_is_scanned(self):
        repo = Path(self.tmp.name, "fresh")
        repo.mkdir()
        git(repo, "init", "-q")
        root = commit(repo, {"docs/日本.md": "a worker line\n"})
        code, out = run(repo, "--range", root)  # the root commit alone (no parent to diff against)
        self.assertEqual(code, 1, out)
        self.assertIn("docs/日本.md:1: agent process", out)


class Shapes(unittest.TestCase):
    """the shapes found in the trees (planted lines), and the technical text that must stay clean"""

    def test_work_session_shapes_are_found(self):
        for line in ("| 2026-08-07 | S3 (production deploy) は次 session |", "本 session で", "次々 session",
                     "単一 session", "multi-session", "Session 分割案", "## Session 分割案", "Session A (~1h)",
                     "集中 session で"):
            self.assertIn("work session", ptc.kinds(line), line)
        self.assertEqual(ptc.scan_text("f", "/// Phase A2 (next\n/// session) work\n"), ["f:1: work session"])

    def test_technical_sessions_pass(self):
        for line in ("checkout.session.completed", "Stripe Checkout Session", "a later session can resume",
                     "OpenXR Session 管理", "checkout session 作成", "HTTP session の管理", "a netcode session",
                     "内で session.locate_view() 等", "session key"):
            self.assertNotIn("work session", ptc.kinds(line), line)
        self.assertEqual(ptc.scan_text("f", "next steps\nsession key\n"), [])

    def test_note_links_and_note_names_are_found(self):
        for line in ("see [[reference-text-to-cad-absorption]]", "see [[HEGEMONY_THESIS]]", "[[feedback_x_y]]",
                     "docs [[kv_namespaces]] binding"):
            self.assertIn("note link", ptc.kinds(line, "docs/a.md"), line)
        for line in ("/// 詳細: memory `project_text_to_print_archetype.md`", "memo は `reference_kimi_k3_dspark.md`",
                     "node_type: memory", "originSessionId: x"):
            self.assertIn("internal note", ptc.kinds(line, "a.rs"), line)
        self.assertIn("internal tracker", ptc.kinds("`memory/text-to-print.md`", "a.rs"))

    def test_link_like_code_and_toml_headers_pass(self):
        for line, path in (("[[package]]", "Cargo.lock"), ("[[kv_namespaces]]", "wrangler.toml"),
                           ("  [[bin]]  # main", "Cargo.toml"), ("[[gate]] entry", "docs/a.md"),
                           ("fn f(a: &[[f64; 3]])", "a.rs"), ("if [[ $x -eq 1 ]]; then", "a.sh"),
                           ("in-memory `WeightRef`", "a.rs"), ("memory footprint", "a.md"), ("unified memory", "a.md"),
                           ("`reference_repo`", "C.md"), ("`project_pressure_red_black_gs`", "src/g.rs")):
            self.assertEqual(ptc.kinds(line, path), [], (line, path))
        self.assertIn("note link", ptc.kinds("x = 1  [[kv_namespaces]]", "wrangler.toml"))  # not line-leading

    def test_worker_is_technical_only_in_its_paths(self):
        for path, line in (("crates/worker/src/lib.rs", "the worker handler"), ("wrangler.toml", "name = \"worker\""),
                           ("migrations/0001.sql", "-- worker table"), ("Cargo.lock", 'name = "worker"'),
                           ("Cargo.lock", ' "worker-macros 0.4.2",')):
            self.assertNotIn("agent process", ptc.kinds(line, path), (path, line))
        for path, line in (("docs/ROADMAP.md", "the worker landed it"), ("crates/worker-notes/a.md", "a worker"),
                           ("xcrates/worker/a.rs", "a worker"), ("Cargo.lock", "# a worker wrote this"),
                           ("crates/worker/src/lib.rs", "the worker and a subagent")):
            self.assertIn("agent process", ptc.kinds(line, path), (path, line))

    def test_a_user_home_path_is_found_and_placeholders_pass(self):
        for line in ("at /Users/someone/ALICE-X/src", "/home/alice/work/x", "sdk.dir=/Users/someone/Library/x"):
            self.assertIn("home path", ptc.kinds(line), line)
        for line in ("cd /home/someuser/x", '"/Users/someuser/x"', "file:///Users/someuser/x", "dir=(/home/someuser/a)"):
            self.assertIn("home path", ptc.kinds(line), line)
        for line in ("https://vrchat.com/home/world/wrld_x", "see github.com/x/home/y/z", "https://h/Users/someuser/x",
                     "a/b/home/someuser/c"):
            self.assertNotIn("home path", ptc.kinds(line), line)
        for line in ("/Users/runner/work/x", "/home/runner/x", "/Users/Shared/Epic Games/x", "/Users/<you>/x",
                     "/home/$USER/x", "/home/${USER}/x", "/Users/user/x", "~/ALICE-LOL/x", "/Users"):
            self.assertNotIn("home path", ptc.kinds(line), line)

    def test_private_home_paths_are_found_and_public_ones_pass(self):
        saved = ptc.PRIVATE_REPO_HASHES
        self.addCleanup(lambda: setattr(ptc, "PRIVATE_REPO_HASHES", saved))
        ptc.PRIVATE_REPO_HASHES = {hashlib.sha256(b"zorblat-notes").hexdigest()}
        for line in ("see ~/zorblat-notes/plan.md", "/Users/someone/Zorblat-Notes/x", "/home/u/zorblat-notes/"):
            self.assertIn("private path", ptc.kinds(line), line)
        for line in ("see ~/ALICE-LOL/README.md", "/Users/runner/work/x", "zorblat-notes alone", "~/zorblat-notes2/x"):
            self.assertNotIn("private path", ptc.kinds(line), line)

    # the set as committed: its size and the SHA-256 of its sorted lines (a change of the set is a
    # change of these two numbers)
    PRIVATE_REPO_COUNT = 309
    PRIVATE_REPO_DIGEST = "7a9cd0fba91b99ad95369451cc6b64e1406c15e640c7615723ce6d0981193167"

    def standalone(self, source: str | None = None) -> Path:
        """this checker copied alone into an empty directory (as a repository's CI fetches it)"""
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        src = Path(ptc.__file__).read_text(encoding="utf-8")
        (d / "public_text_check.py").write_text(src if source is None else source(src), encoding="utf-8")
        self.assertEqual(sorted(x.name for x in d.iterdir()), ["public_text_check.py"])
        return d

    def fixture_repo(self, line: str) -> Path:
        r = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, r, True)
        (r / "NOTES.md").write_text(line + "\n", encoding="utf-8")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "fixture"]):
            subprocess.run(["git", "-C", str(r), *cmd], check=True, env=env, capture_output=True)
        return r

    def run_standalone(self, d: Path, repo: Path) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(d / "public_text_check.py"), "--repo", str(repo),
                               "--repo-name", "fixture", "--tree", "HEAD"], capture_output=True, text=True)

    def test_the_private_repository_set_is_held_in_the_checker_itself(self):
        d = self.standalone()
        got = subprocess.run([sys.executable, "-c",
                              "import hashlib, sys; sys.path.insert(0, sys.argv[1]); import public_text_check as p; "
                              "h = sorted(p.PRIVATE_REPO_HASHES); "
                              "print(len(h), hashlib.sha256('\\n'.join(h).encode()).hexdigest())", str(d)],
                             capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(got, [str(self.PRIVATE_REPO_COUNT), self.PRIVATE_REPO_DIGEST])
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", h) for h in ptc.PRIVATE_REPO_HASHES))
        self.assertFalse((Path(ptc.__file__).parent / "public_text_private_repos.txt").exists())

    def test_a_private_path_is_found_by_the_checker_copied_alone(self):
        # the canary: one more name in the committed set, and a line with its home path, read by
        # the checker copied alone (no file beside it)
        h = hashlib.sha256(b"zorblat-notes").hexdigest()
        marker = "PRIVATE_REPO_HASHES: frozenset[str] = frozenset({\n"
        d = self.standalone(lambda s: s.replace(marker, marker + f'    "{h}",\n', 1))
        r = self.run_standalone(d, self.fixture_repo("see ~/zorblat-notes/plan.md"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("NOTES.md:1: private path", r.stdout)
        r = self.run_standalone(d, self.fixture_repo("see ~/ALICE-LOL/README.md"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_an_empty_private_set_is_refused(self):
        # the mutant: the set emptied, so the private path check compares nothing
        def empty(s: str) -> str:
            start = s.index("PRIVATE_REPO_HASHES: frozenset[str] = frozenset({\n")
            end = s.index("})\n", start)
            return s[:start] + "PRIVATE_REPO_HASHES: frozenset[str] = frozenset({\n" + s[end:]
        d = self.standalone(empty)
        r = self.run_standalone(d, self.fixture_repo("see ~/zorblat-notes/plan.md"))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("private-repository set of this check is empty", r.stderr)
        saved = ptc.PRIVATE_REPO_HASHES
        self.addCleanup(lambda: setattr(ptc, "PRIVATE_REPO_HASHES", saved))
        ptc.PRIVATE_REPO_HASHES = frozenset()
        repo = self.fixture_repo("x")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ptc.main(["--repo", str(repo), "--repo-name", "fixture", "--tree"]), 2)
        self.assertIn("private-repository set of this check is empty", err.getvalue())


class Binary(Base):
    def test_a_word_in_a_binary_file_is_found_in_both_modes(self):
        data = b"\x89PNG\0\0\x01" + b"tEXt comment: a worker wrote this\0" + b"\xff" * 8
        (self.repo / "img.png").write_bytes(data)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "x")
        code, out = run(self.repo, "--tree")
        self.assertIn("img.png:", out)
        self.assertIn("agent process", out)
        code, out = run(self.repo, "--range", "HEAD~1..HEAD")
        self.assertIn("img.png", out)
        self.assertIn("agent process", out)

    def test_short_tokens_in_a_binary_run_are_dropped(self):
        saved = ptc.PRIVATE_NAME_HASHES
        self.addCleanup(lambda: setattr(ptc, "PRIVATE_NAME_HASHES", saved))
        ptc.PRIVATE_NAME_HASHES = saved | {hashlib.sha256(b"zqx").hexdigest(), hashlib.sha256(b"zqxw").hexdigest()}
        (self.repo / "noise.bin").write_bytes(b"\x89\0\x01ab.zqx!\0\xff")  # a 6-byte run, a 3-letter name
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "x")
        self.assertEqual(run(self.repo, "--tree")[0], 0)
        (self.repo / "name.bin").write_bytes(b"\x89\0\x01ab zqxw!\0\xff")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "x")
        code, out = run(self.repo, "--tree")
        self.assertIn("name.bin:1: private name", out)

    def test_utf16_text_in_a_binary_file_is_read(self):
        for enc in ("utf-16-le", "utf-16-be"):
            (self.repo / f"t-{enc}.bin").write_bytes(b"\0\xff" + "notes: a worker log".encode(enc) + b"\0\0")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "x")
        code, out = run(self.repo, "--tree")
        self.assertIn("t-utf-16-le.bin:", out)
        self.assertIn("t-utf-16-be.bin:", out)

    def test_short_runs_are_not_read(self):
        # 5 printable bytes between NULs would read as a session name if the bytes were decoded as text
        (self.repo / "b.bin").write_bytes(b"\x89\0ys-6e\0\xff\0" * 4)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "x")
        self.assertEqual(run(self.repo, "--tree")[0], 0)


# home directories spelled in pieces so this file's own text holds no home path
HOME = b"/ho" + b"me/"
USERS = b"/Use" + b"rs/"


def elf(*strings: bytes) -> bytes:
    """a small ELF-like blob: a header, NUL-separated strings, padding"""
    return b"\x7fELF\x02\x01\x01\0" + b"\0" * 8 + b"\0".join(strings) + b"\0" + b"\xff" * 16


class BinaryBuildPaths(Base):
    """a home directory compiled into a tracked binary file (a build path in debug info or a
    panic message) is a finding, given as the path and a count; a container is read, and what
    cannot be read is reported"""

    def add(self, files: dict[str, bytes]):
        for name, data in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_bytes(data)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "x")

    def test_a_registry_path_in_a_binary_is_found_in_both_modes_without_its_text(self):
        reg = HOME + b"someuser/.cargo/registry/src/x/lib.rs"
        self.add({"libs/libfoo.so": elf(b"panicked at", reg, b"index out of bounds")})
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("libs/libfoo.so: 1 build paths: binary home path", out)
        self.assertNotIn("someuser", out)
        code, out = run(self.repo, "--range", "HEAD~1..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("libs/libfoo.so: 1 build paths: binary home path", out)

    def test_path_forms(self):
        bs = lambda b: b.replace(b"/", b"\\")
        cases = {
            "glued": elf(b"assertion failed" + USERS + b"ys/proj/src/lib.rs"),
            "glued-2": elf(b"src/main.rs" + USERS + b"ys/a.rs" + b"unwrap()x" + HOME + b"ys/work/b.rs"),
            "windows": elf(b"C:" + bs(USERS + b"ys/.cargo/registry/src/x/lib.rs")),
            "windows-fwd": elf(b"C:" + USERS + b"ys/proj/a.rs"),
            "utf8-name": elf(USERS + "山田".encode() + b"/proj/a.rs"),
            "space-name": elf(USERS + b"John Doe/proj/a.rs"),
            "plus-name": elf(HOME + b"a+b/proj/a.rs"),
            "root": elf(b"/root/.cargo/registry/src/x/lib.rs"),
            "placeholder-registry": elf(b"build" + USERS + b"user/.cargo/git/checkouts/q/src/a.rs"),
            "file-url": elf(b"see file://" + USERS + b"bob/x.rs"),
            "long-run": elf(b"A" * 100000 + USERS + b"ys/proj/a.rs"),
            "utf16-le": b"MZ\0\0" + "build C:\\Users\\wide\\proj\\".encode("utf-16-le") + b"\0\0",
            # UTF-16 at an odd byte offset after a byte that is not NUL: only the odd alignment reads it
            "utf16-odd": b"MZ\x01" + "/Users/wide/proj/".encode("utf-16-le") + b"\0\0",
            "utf16-be": b"MZ\0\0\0" + "build /Users/wide/proj/".encode("utf-16-be") + b"\0\0",
        }
        self.add({f"{k}.bin": v for k, v in cases.items()})
        out = run(self.repo, "--tree")[1]
        for k in cases:
            want = 2 if k == "glued-2" else 1
            self.assertIn(f"{k}.bin: {want} build paths: binary home path", out, k)

    def test_urls_placeholders_and_the_ci_runner_do_not_count(self):
        self.add({"ok.dylib": elf(b"https://example.org" + HOME + b"world/abc", b"x https://e.org/a" + USERS + b"b/c",
                                  HOME + b"runner/.cargo/registry/src/y", USERS + b"runner/work/a/b.rs",
                                  HOME + b"user/project/a.rs", b"symbol_table_entry")})
        self.assertEqual(run(self.repo, "--tree")[0], 0)

    def test_a_named_allowance_takes_the_binary_at_its_count_and_only_for_a_file(self):
        self.add({"fixtures/sample.so": elf(HOME + b"someuser/.cargo/registry/src/x", HOME + b"someuser/b/c.rs")})
        allow = '[public-text]\n[[public-text.allow]]\npath = "{p}"\nclass = "binary home path"\ncount = {n}\nwhy = "a fixture"\n'
        commit(self.repo, {".public-text.toml": allow.format(p="fixtures/sample.so", n=2)})
        self.assertEqual(run(self.repo, "--tree")[0], 0)
        commit(self.repo, {".public-text.toml": allow.format(p="fixtures/sample.so", n=1)})
        self.assertIn("allow fixtures/sample.so / binary home path allows 2 lines", run(self.repo, "--tree")[1])
        commit(self.repo, {".public-text.toml": allow.format(p="fixtures/", n=2)})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run(self.repo, "--tree")[0], 2)

    def test_containers_are_read_and_what_cannot_be_read_is_reported(self):
        import bz2, gzip, lzma, tarfile, zipfile, zlib
        so = elf(USERS + b"ys/.cargo/registry/src/x/lib.rs")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("lib/arm64-v8a/libx.so", so)
        apk = buf.getvalue()
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("inner.apk", apk)
        tbuf = io.BytesIO()
        with tarfile.open(fileobj=tbuf, mode="w:gz") as t:
            info = tarfile.TarInfo("pkg/libx.so")
            info.size = len(so)
            t.addfile(info, io.BytesIO(so))
        self.add({"app.apk": apk, "nested.zip": outer.getvalue(), "x.crate": tbuf.getvalue(), "s.gz": gzip.compress(so),
                  "s.bz2": bz2.compress(so), "s.xz": lzma.compress(so), "s.zlib": zlib.compress(so),
                  "bad.zip": b"PK\x03\x04garbage\0\0", "a.7z": b"7z\xbc\xaf\x27\x1c\0\0rest",
                  "clean.zip": self.zipped(b"symbol_table_entry\0")})
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 1, out)
        for name in ("app.apk", "nested.zip", "x.crate", "s.gz", "s.bz2", "s.xz", "s.zlib"):
            self.assertIn(f"{name}: 1 build paths: binary home path", out, name)
        self.assertIn("bad.zip: zip: unscanned container", out)
        self.assertIn("a.7z: 7z: unscanned container", out)
        self.assertNotIn("clean.zip", out)

    def test_a_zstd_stream_is_read_or_reported_by_the_python_at_hand(self):
        so = elf(USERS + b"ys/.cargo/registry/src/x/lib.rs")
        if ptc.zstd_available():
            from compression import zstd
            self.add({"s.zst": zstd.compress(so)})
            self.assertIn("s.zst: 1 build paths: binary home path", run(self.repo, "--tree")[1])
        else:
            self.add({"s.zst": b"\x28\xb5\x2f\xfd" + b"\0" * 16})
        # a Python without the reader: the stream is reported, the run goes on and says so
        saved = ptc.zstd_available
        self.addCleanup(lambda: setattr(ptc, "zstd_available", saved))
        ptc.zstd_available = lambda: False
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("s.zst: zstd, zstd unavailable before Python 3.14: unscanned container", out)
        self.assertIn("has no zstd reader", out)

    def test_compressed_streams_inside_a_binary_are_read(self):
        import struct, zlib
        dwarf = b"\0DW_AT_comp_dir\0" + USERS + b"ys/.cargo/registry/src/x/lib.rs\0" + HOME + b"ys/proj/a.rs\0"
        cases = {
            "nul-zlib.bin": b"\0" + zlib.compress(dwarf),
            # a .zdebug_* section: "ZLIB", the size, then the zlib stream
            "zdebug.so": elf(b".zdebug_info", b"x") + b"ZLIB" + struct.pack(">Q", len(dwarf)) + zlib.compress(dwarf, 9) + b"\0" * 8,
            # an SHF_COMPRESSED section: the compression header (type 1 = zlib), then the stream
            "chdr.so": elf(b".debug_str") + struct.pack("<IIQQ", 1, 0, len(dwarf), 1) + zlib.compress(dwarf) + b"\xff" * 4,
            # two streams in one file: each is read (the same two paths in both are counted once)
            "two.bin": b"\x01\0" + zlib.compress(dwarf) + b"\0\x02" + zlib.compress(dwarf.replace(b"a.rs", b"b.rs")),
        }
        if ptc.zstd_available():
            from compression import zstd
            cases["zstd-inside.bin"] = elf(b"head") + zstd.compress(dwarf) + b"\0tail"
        # bytes that look like a zlib header but do not inflate, and a header asking for a preset
        # dictionary: read as they are, nothing found, no failure
        cases["noise.bin"] = b"\0" + b"\x78\x9c\xff\xfe\x00" * 50 + b"\x78\xbb" + b"\0" * 20
        self.add(cases)
        out = run(self.repo, "--tree")[1]
        self.assertIn("nul-zlib.bin: 2 build paths: binary home path", out)
        self.assertIn("zdebug.so: 2 build paths: binary home path", out)
        self.assertIn("chdr.so: 2 build paths: binary home path", out)
        self.assertIn("two.bin: 3 build paths: binary home path", out)
        if ptc.zstd_available():
            self.assertIn("zstd-inside.bin: 2 build paths: binary home path", out)
        self.assertNotIn("noise.bin", out)

    def test_a_path_is_counted_once_however_it_is_stored(self):
        import gzip, zlib
        p = USERS + b"ys/.cargo/registry/src/x/lib.rs"
        # level 0 stores the bytes as they are: the path is seen raw and inflated, and is one path
        self.add({"stored.bin": b"\0" + zlib.compress(b"\0" + p + b"\0", 0) + b"\0" + p + b"\0",
                  "repeated.bin": elf(p, p, p, b"C:" + p.replace(b"/", b"\\"))})
        out = run(self.repo, "--tree")[1]
        self.assertIn("stored.bin: 1 build paths: binary home path", out)
        self.assertIn("repeated.bin: 1 build paths: binary home path", out)

    def test_a_broken_stream_and_a_gzip_member_inside_a_binary_are_read(self):
        import gzip, zlib
        p = b"\0" + USERS + b"ys/proj/src/lib.rs\0" + b"x" * 200000
        whole = zlib.compress(p)
        self.add({"cut.bin": b"\0\x01" + whole[:-40] + b"\0\0",          # breaks off before its end
                  "badsum.bin": b"\0\x01" + whole[:-4] + b"\xde\xad\xbe\xef",  # wrong checksum
                  "gz-inside.bin": elf(b"head") + gzip.compress(p) + b"\0tail"})
        out = run(self.repo, "--tree")[1]
        for name in ("cut.bin", "badsum.bin", "gz-inside.bin"):
            self.assertIn(f"{name}: 1 build paths: binary home path", out, name)

    def test_a_stream_inside_a_binary_over_the_budget_is_unscanned(self):
        import zlib
        saved = ptc.CONTAINER_BUDGET
        self.addCleanup(lambda: setattr(ptc, "CONTAINER_BUDGET", saved))
        ptc.CONTAINER_BUDGET = 1 << 20
        self.add({"big.bin": b"\0\x01" + zlib.compress(b"x" * (2 << 20))})
        self.assertIn("big.bin: zlib stream, over 1 MiB: unscanned container", run(self.repo, "--tree")[1])

    def zipped(self, data: bytes) -> bytes:
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("a.bin", data)
        return buf.getvalue()

    def test_an_lfs_pointer_is_reported_in_both_modes(self):
        self.add({"big.so": b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"0" * 64 + b"\nsize 123\n"})
        self.assertIn("big.so: Git LFS pointer: unscanned LFS object", run(self.repo, "--tree")[1])
        self.assertIn("big.so: Git LFS pointer: unscanned LFS object", run(self.repo, "--range", "HEAD~1..HEAD")[1])

    def test_a_symlink_and_a_submodule_are_not_binary_findings(self):
        self.add({"a.bin": elf(b"symbol_table_entry")})
        os.symlink("a.bin", self.repo / "link.bin")
        sha = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "add", "-A")
        git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},sub")
        git(self.repo, "commit", "-q", "-m", "x")
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 0, out)
        self.assertIn("not read: 1 entries (first: sub (submodule))", out)
        self.assertEqual(run(self.repo, "--range", "HEAD~1..HEAD")[0], 0)

    def test_a_clean_binary_and_a_repository_without_binaries_pass(self):
        self.assertEqual(run(self.repo, "--tree")[0], 0)  # no binary: nothing to compare here, not a failure
        self.add({"lib.a": elf(b"symbol_table_entry", b"rustc version 1.90")})
        self.assertEqual(run(self.repo, "--tree")[0], 0)

    def test_skipping_binaries_is_caught(self):
        # mutant: no file is taken as binary; the files git takes as binary are then reported unread
        self.add({"libs/libfoo.so": elf(b"symbol_table_entry"), "b.wasm": b"\0asm\x01\0\0\0" + b"\0" * 9000 + b"tail"})
        saved = ptc.is_binary
        self.addCleanup(lambda: setattr(ptc, "is_binary", saved))
        ptc.is_binary = lambda data: False
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("2 binary files were not read as binary (first: b.wasm)", out)

class Fork(Base):
    def config(self, text: str, **kw):
        return commit(self.repo, {".public-text.toml": text}, **kw)

    def fork_config(self, base: str) -> str:
        return f'[public-text]\nupstream-fork = true\nupstream-fork-why = "upstream project"\nupstream-base = "{base}"\n'

    def commit_as(self, email: str, files: dict, message: str) -> str:
        for rel, text in files.items():
            (self.repo / rel).write_text(text, encoding="utf-8")
        git(self.repo, "add", "-A")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", message], check=True, capture_output=True,
                       env={**ENV, "HOME": str(self.repo), "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email})
        return git(self.repo, "rev-parse", "HEAD").strip()

    def test_the_tree_of_a_fork_reads_only_what_changed_after_the_fork_point(self):
        commit(self.repo, {"up.md": "a worker\n", "up2.md": "fine\n"})
        base = git(self.repo, "rev-parse", "HEAD").strip()
        self.config(self.fork_config(base))
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 0, out)  # the upstream text as forked is not read
        self.assertIn("in 1 files", out)  # the settings file
        commit(self.repo, {"up2.md": "now a worker edit\n", "ours.md": "a subagent\n"})
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("up2.md:1: agent process", out)
        self.assertIn("ours.md:1: agent process", out)
        self.assertNotIn("up.md:", out)

    def test_every_commit_after_the_fork_point_is_read_whoever_wrote_it(self):
        self.commit_as("up@example.org", {"up.md": "a worker upstream\n"}, "upstream work")
        base = git(self.repo, "rev-parse", "HEAD").strip()
        self.config(self.fork_config(base))
        self.commit_as("alias@example.net", {"leak.md": "notes: a worker log\n"}, "worker change")
        code, out = run(self.repo, "--range", f"{self.base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("leak.md:1: agent process", out)
        self.assertIn("message:1: agent process", out)
        self.assertNotIn("up.md", out)  # the upstream commit (up to the fork point) is not read
        self.assertIn("upstream fork at", out)

    def test_a_change_of_the_settings_is_a_finding_with_its_keys(self):
        self.config('[public-text]\ntechnical-paths = ["svc/"]\n')
        code, out = run(self.repo, "--range", "HEAD~1..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn(".public-text.toml: policy config changed (technical-paths)", out)
        base = git(self.repo, "rev-parse", "HEAD~1").strip()
        self.config(self.fork_config(base))
        later = git(self.repo, "rev-parse", "HEAD").strip()
        self.config(self.fork_config(later))  # the fork point moved forward
        code, out = run(self.repo, "--range", "HEAD~1..HEAD")
        self.assertIn("policy config changed (upstream-base)", out)

    def test_a_fork_needs_a_reason_and_a_base_that_is_an_ancestor(self):
        base = git(self.repo, "rev-parse", "HEAD").strip()
        for text in ("[public-text]\nupstream-fork = true\n",
                     '[public-text]\nupstream-fork = true\nupstream-fork-why = "x"\n',
                     '[public-text]\nupstream-fork = true\nupstream-fork-why = "x"\nupstream-base = "abc"\n',
                     self.fork_config("0" * 40),
                     f'[public-text]\nupstream-base = "{base}"\n',
                     '[public-text]\nupstream-fork = false\nworkers = ["x/"]\n'):
            self.config(text)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run(self.repo, "--tree")[0], 2, text)

    def test_a_base_that_is_not_an_ancestor_is_refused(self):
        side = git(self.repo, "commit-tree", "HEAD^{tree}", "-m", "side").strip()  # a real commit, not an ancestor
        self.config(self.fork_config(side))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(run(self.repo, "--tree")[0], 2)

    def test_technical_paths_from_the_repository_settings(self):
        commit(self.repo, {"svc/a.rs": "spawn a worker\n"})
        self.assertEqual(run(self.repo, "--tree")[0], 1)
        self.config('[public-text]\ntechnical-paths = ["svc/"]\n')
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 0, out)
        self.assertIn("technical-paths ['svc/']", out)



class ConfigApproval(Base):
    """a change of the settings file is reviewed when a later, separate commit records its hash in
    the pin file; the change alone, or with its approval in the same commit, is a finding"""

    CONFIG = '[public-text]\ntechnical-paths = ["tools/"]\n'

    def pin(self, text: str) -> str:
        return ("rev " + "0" * 40 + "\nsha256 " + "1" * 64 + "\nconfig-sha256 "
                + hashlib.sha256(text.encode()).hexdigest() + "\n")

    def head(self) -> str:
        return git(self.repo, "rev-parse", "HEAD").strip()

    def test_a_config_change_alone_is_a_finding(self):
        commit(self.repo, {".public-text.toml": self.CONFIG})
        code, out = run(self.repo, "--range", f"{self.base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("policy config changed (technical-paths)", out)

    def test_a_later_approval_makes_the_change_reviewed_in_every_range(self):
        commit(self.repo, {".public-text.toml": self.CONFIG})
        config_commit = self.head()
        commit(self.repo, {".github/public-text-pin": self.pin(self.CONFIG)})
        code, out = run(self.repo, "--range", f"{self.base}..HEAD")                  # both commits
        self.assertEqual(code, 0, out)
        approval = self.head()
        self.assertIn(f"config change {config_commit[:12]} (technical-paths) approved by {approval[:12]}", out)
        self.assertEqual(run(self.repo, "--range", f"{config_commit}..HEAD")[0], 0)  # the approval alone
        self.assertEqual(run(self.repo, "--range", f"{self.base}...HEAD")[0], 0)     # a three-dot range
        self.assertEqual(run(self.repo, "--tree")[0], 0)

    def test_a_change_and_its_approval_in_one_commit_is_a_finding(self):
        commit(self.repo, {".public-text.toml": self.CONFIG, ".github/public-text-pin": self.pin(self.CONFIG)})
        code, out = run(self.repo, "--range", f"{self.base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("approved in the same commit", out)

    def test_a_change_after_the_approval_needs_a_new_one(self):
        commit(self.repo, {".public-text.toml": self.CONFIG})
        commit(self.repo, {".github/public-text-pin": self.pin(self.CONFIG)})
        commit(self.repo, {".public-text.toml": '[public-text]\ntechnical-paths = ["tools/", "x/"]\n'})
        code, out = run(self.repo, "--range", f"{self.base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("policy config changed", out)
        self.assertIn("config-sha256 does not match", run(self.repo, "--tree")[1])

    def test_an_approval_of_other_settings_does_not_count(self):
        commit(self.repo, {".public-text.toml": self.CONFIG})
        commit(self.repo, {".github/public-text-pin": self.pin("[public-text]\n")})
        code, out = run(self.repo, "--range", f"{self.base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("policy config changed", out)
        self.assertIn("config-sha256 does not match", run(self.repo, "--tree")[1])


class RangeAllow(Base):
    """--range reads the allow entries of the range's base: an added line of an allowed class passes,
    entries added inside the range do not apply to it, and an entry caps the distinct lines added"""

    def settings(self, entries) -> str:
        text = "[public-text]\n" + "".join(
            f'[[public-text.allow]]\npath = "{p}"\nclass = "{c}"\ncount = {n}\nwhy = "shader attributes"\n'
            for p, c, n in entries)
        return commit(self.repo, {".public-text.toml": text})

    def test_an_added_line_of_a_class_the_base_allows_passes(self):
        base = self.settings([("shaders/a.metal", "note link", 2)])
        commit(self.repo, {"shaders/a.metal": "vertex float4 f(uint v [[vertex_id]]) { return 0; }\n"})
        code, out = run(self.repo, "--range", f"{base}..HEAD")
        self.assertEqual(code, 0, out)

    def test_an_entry_added_in_the_same_range_does_not_apply(self):
        base = git(self.repo, "rev-parse", "HEAD").strip()
        self.settings([("shaders/a.metal", "note link", 2)])
        commit(self.repo, {"shaders/a.metal": "vertex float4 f(uint v [[vertex_id]]) { return 0; }\n"})
        code, out = run(self.repo, "--range", f"{base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("policy config changed", out)
        self.assertIn("shaders/a.metal:1: note link", out)

    def test_another_path_or_more_lines_than_the_count_is_a_finding(self):
        base = self.settings([("shaders/a.metal", "note link", 2)])
        commit(self.repo, {"shaders/b.metal": "uint v [[vertex_id]]\n"})
        code, out = run(self.repo, "--range", f"{base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("shaders/b.metal:1: note link", out)
        base = git(self.repo, "rev-parse", "HEAD").strip()
        commit(self.repo, {"shaders/a.metal": "uint a [[vertex_id]]\nuint b [[instance_id]]\nuint c [[base_vertex]]\n"})
        code, out = run(self.repo, "--range", f"{base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("allow shaders/a.metal / note link allows 2 lines", out)
        self.assertIn("adds 3", out)

    def test_the_same_line_added_twice_counts_once(self):
        base = self.settings([("shaders/a.metal", "note link", 1)])
        commit(self.repo, {"shaders/a.metal": "uint v [[vertex_id]]\n"})
        commit(self.repo, {"shaders/a.metal": "// x\n"})
        commit(self.repo, {"shaders/a.metal": "uint v [[vertex_id]]\n"})  # removed and added again
        self.assertEqual(run(self.repo, "--range", f"{base}..HEAD")[0], 0)

    def test_lines_already_there_count_toward_the_entry(self):
        # 2 allowed lines before the range, 1 added: 3 in all against a count of 2
        commit(self.repo, {"shaders/a.metal": "uint a [[vertex_id]]\nuint b [[instance_id]]\n"})
        base = self.settings([("shaders/a.metal", "note link", 2)])
        commit(self.repo, {"shaders/a.metal": "uint a [[vertex_id]]\nuint b [[instance_id]]\nuint c [[base_vertex]]\n"})
        code, out = run(self.repo, "--range", f"{base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("allows 2 lines, 3 at", out)
        commit(self.repo, {"shaders/a.metal": "uint a [[vertex_id]]\nuint c [[base_vertex]]\n"})  # back to 2 in all
        self.assertEqual(run(self.repo, "--range", f"{base}..HEAD")[0], 0)

    def test_identical_lines_count_once_among_the_added_lines(self):
        # the added-lines cap counts distinct (path, text) pairs; the total at the tip counts every line,
        # as the tree check does
        base = self.settings([("shaders/a.metal", "note link", 1)])
        commit(self.repo, {"shaders/a.metal": "uint v [[vertex_id]]\n// x\nuint v [[vertex_id]]\n"})
        code, out = run(self.repo, "--range", f"{base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertNotIn("adds", out)
        self.assertIn("allows 1 lines, 2 at", out)

    def test_a_three_dot_range_reads_the_merge_base(self):
        merge_base = self.settings([("shaders/a.metal", "note link", 1)])
        git(self.repo, "checkout", "-q", "-b", "side")
        side = commit(self.repo, {".public-text.toml": "[public-text]\n"})  # the other side drops the entry
        git(self.repo, "checkout", "-q", merge_base)
        commit(self.repo, {"shaders/a.metal": "uint v [[vertex_id]]\n"})
        self.assertEqual(run(self.repo, "--range", f"{side}...HEAD")[0], 1)  # the side's own config change
        out = run(self.repo, "--range", f"{side}...HEAD")[1]
        self.assertNotIn("shaders/a.metal:1: note link", out)  # allowed by the merge base's entry


class Terms(unittest.TestCase):
    """programming terms and English verbs that only look like process words or ids"""

    def test_a_worker_identifier_or_thread_is_not_the_process(self):
        # a closed list of pool-size arguments, and a thread
        for line in ("ThreadPoolExecutor(max_workers=16)", "DataLoader(num_workers=4)", "n_workers = 8",
                     "first touched from its worker thread", "else the worker threads run",
                     "- **Cloudflare Workers**: a separate binary"):
            self.assertNotIn("agent process", ptc.kinds(line), line)
        for line in ("a worker did X", "worker 3", "found by the worker", "the workers ran the tests",
                     "a worker, threading the needle", "workers: 4",
                     "found by worker_3", "worker_id = 3", "found_by_workers",   # any other identifier
                     "Worker 3 measured it", "WORKER found it", "Workers ran it"):  # any case
            self.assertIn("agent process", ptc.kinds(line), line)

    def test_the_other_process_words_in_any_case_but_not_in_identifiers(self):
        for line in ("Subagent ran", "Skills used", "SKILLS:", "a Multi-Agent setup"):
            self.assertIn("agent process", ptc.kinds(line), line)
        for line in ("see SKILL.md", "SKILL_PACKAGE_DIR = PACKAGE_ROOT", "def test_a_skills_path_segment(self):"):
            self.assertNotIn("agent process", ptc.kinds(line), line)

    def in_repo(self, repo):
        saved = ptc.ALLOW_STATE.get("repo")
        self.addCleanup(ptc.ALLOW_STATE.__setitem__, "repo", saved)
        ptc.ALLOW_STATE["repo"] = repo

    def test_only_the_repositorys_own_skill_directory_is_a_path_not_the_process_word(self):
        internal = ("Checks follow skills/karikari-review/SKILL.md before every release.",
                    "The skills/public-comm-style/ rules apply to commit text.",
                    "See skills/context-handoff.md for the handoff.",
                    "everything under `skills/` is the package", "the skills used here", "Skills/lol-sdf/x",
                    "plugin/skills/lol-sdf/x.md", "plugin/skills/implicit-cad/x.md", "x./skills/lol-sdf/y",
                    "skills used, see `skills/lol-sdf/SKILL.md`")
        own = {"ALICE-LOL": ("- `skills/lol-sdf/x.md` is copied", "// grammar: skills/lol-sdf/references/lol.gbnf",
                             "see `skills/lol-sdf/SKILL.md` and (skills/lol-sdf/scripts/print.sh)"),
               "ALICE-SDF": ("- `skills/implicit-cad/SKILL.md` is updated",
                             "See [the guide](../skills/implicit-cad/SKILL.md) or ./skills/implicit-cad/x.sh")}
        for repo, lines in own.items():
            self.in_repo(repo)
            for line in lines:  # must not catch: the repository's own skill directory as a path
                self.assertNotIn("agent process", ptc.kinds(line), (repo, line))
                self.assertFalse(ptc.folded_agent_word(line), (repo, line))
            for line in internal:  # must catch: any other skills/ path and the word in prose
                self.assertIn("agent process", ptc.kinds(line), (repo, line))
            self.assertTrue(ptc.folded_agent_word(lines[0] + " by a w0rker"), repo)
        # the same lines in another repository (or none): findings
        for repo in ("ALICE-SDF", "ALICE-Physics", None):
            self.in_repo(repo)
            for line in own["ALICE-LOL"] + internal:
                self.assertIn("agent process", ptc.kinds(line), (repo, line))
        self.in_repo("ALICE-LOL")
        self.assertIn("agent process", ptc.kinds(own["ALICE-SDF"][0]))
        self.assertTrue(ptc.folded_agent_word("see SKILL.md, by a w0rker"))
        self.assertFalse(ptc.folded_agent_word("see SKILL.md"))

    def test_this_session_and_in_chat_are_process_words(self):
        for line in ("This session's simulator tooling", "as decided in this session", "asked in chat",
                     "THIS SESSION", "In Chat"):
            self.assertIn("agent process", ptc.kinds(line), line)
        for line in ("a netcode session", "the session ends", "chat in channels", "within chatter", "Thisession",
                     "fn render(t: &str) -> String { /* fall back to the default in chat_template */ }",
                     "key in chat_history", "the field this session_id holds", "in chat2"):
            self.assertNotIn("agent process", ptc.kinds(line), line)

    def test_the_verb_be_before_review_is_not_an_id(self):
        # decided: a bare "be" before review is never read as an id ("be" is not a current id, and the
        # hashed-id check of commit messages still covers every current one)
        for line in ("and cannot be reviewed. Such files", "it must be reviewed first", "be review: ok",
                     "be reviewed this"):
            self.assertNotIn("session id", ptc.kinds(line), line)
        for line in ("fb reviewed it", "6e review", "(be) review", "be/fb review", "session be"):
            self.assertIn("session id", ptc.kinds(line), line)


class DisguisedWords(Base):
    """the closed agent words in a commit message, read through NFKC, look-alikes and separators"""

    def test_disguised_forms_are_findings(self):
        for line in ("found by the w0rker", "wor-ker 3", "w o r k e r ran it", "\u0455kill used", "sub_agent",
                     "\uff57\uff4f\uff52\uff4b\uff45\uff52", "w.o.r.k.e.r", "wo\u200brker", "mult1agent"):
            self.assertTrue(ptc.folded_agent_word(line), line)

    def test_ordinary_text_and_plain_words_are_left_alone(self):
        for line in ("networker", "skilled work", "a work er", "box2 worked", "max_workers=4",
                     "worker thread",            # a plain word: kinds() decides (exempt there)
                     "s0 k1 l1"):
            self.assertFalse(ptc.folded_agent_word(line), line)

    def test_a_disguised_word_in_a_commit_message_fails_the_range(self):
        commit(self.repo, {"a.md": "x\n"}, message="fix: found by the w0rker")
        code, out = run(self.repo, "--range", f"{self.base}..HEAD")
        self.assertEqual(code, 1, out)
        self.assertIn("message:1: agent process (disguised)", out)


class ClassAllow(Base):
    """a kind of finding allowed in a scope by the repository's settings, at a pinned count"""

    def setUp(self):
        super().setUp()
        commit(self.repo, {"scripts/lint.py": 'DENY = [r"Jetson", r"workers?"]\nSAMPLE = "ys-0b"\nIP = "192.168.1.20"\n',
                           "docs/a.md": "a Jetson board\n"})

    def settings(self, entries):
        text = "[public-text]\n" + "".join(
            f'[[public-text.allow]]\npath = "{p}"\nclass = "{c}"\ncount = {n}\nwhy = "the linter\'s own word list"\n'
            for p, c, n in entries)
        commit(self.repo, {".public-text.toml": text})

    def test_allowed_classes_pass_only_in_their_scope_and_at_their_count(self):
        self.settings([("scripts/lint.py", "device", 1), ("scripts/lint.py", "agent process", 1),
                       ("scripts/lint.py", "session name", 1), ("scripts/lint.py", "private address", 1)])
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("docs/a.md:1: device", out)  # another file is not in the scope
        self.assertNotIn("scripts/lint.py:", out)
        self.settings([("scripts/lint.py", "device", 2), ("scripts/lint.py", "agent process", 1),
                       ("scripts/lint.py", "session name", 1), ("scripts/lint.py", "private address", 1),
                       ("docs/a.md", "device", 1)])
        code, out = run(self.repo, "--tree")
        self.assertEqual(code, 1, out)
        self.assertIn("allow scripts/lint.py / device allows 1 lines", out)

    def test_a_private_name_cannot_be_allowed_by_class_and_entries_are_checked(self):
        for entries in ([("scripts/lint.py", "private name", 1)], [("scripts/lint.py", "device", 0)],
                        [("scripts/", "device", 1)], [("docs/", "session id", 1)],
                        [("/abs.py", "device", 1)], [("scripts/lint.py", "device", 1), ("scripts/lint.py", "device", 1)]):
            self.settings(entries)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run(self.repo, "--tree")[0], 2, entries)
        commit(self.repo, {".public-text.toml": '[public-text]\n[[public-text.allow]]\npath = "x"\nclass = "device"\ncount = 1\n'})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run(self.repo, "--tree")[0], 2)  # no reason

    def test_suggest_allow_prints_the_entries_with_their_counts(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ptc.main(["--repo", str(self.repo), "--suggest-allow", "HEAD", "scripts/lint.py"]), 0)
        text = out.getvalue()
        for cls in ("device", "agent process", "session name", "private address"):
            self.assertIn(f'class = "{cls}"\ncount = 1', text)
        self.assertIn("# 1 files under scripts/lint.py", text)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ptc.main(["--repo", str(self.repo), "--suggest-allow", "HEAD", "nowhere/"]), 2)


class Devices(Base):
    def test_device_names_in_any_case_and_device_details(self):
        for line in ("ssh to the jetson board", "a MACBOOK here", "raspberry pi 5", "an Orin Nano 8GB", "orin-nx"):
            self.assertIn("device", ptc.kinds(line), line)
        for line in ("JetPack 6.0", "jetpack R36", "JetPack SDK", "the L4T kernel"):
            self.assertIn("device detail", ptc.kinds(line), line)
        for line in ("Android Jetpack Compose", "a jetpacks game", "ALT4T"):
            self.assertNotIn("device detail", ptc.kinds(line), line)

    def test_the_short_board_names_as_whole_tokens(self):
        for line in ("CacheBudget::l2_rpi5()", "label L2-RasPi5", "raspi_case", "an RPi 4", "Raspberry Pi 5",
                     "l2_rpi5_dual", "L2x2-RasPi5", "rpi", "Ras Pi 4", "x.raspi.",
                     "pub fn budgetRpi5()", "struct BudgetRasPi4", "RpiCache", "CACHE_RPI5", "RasPi5Dual",
                     "raspberry-pi", "raspberry_pi", "RaspberryPi 5", "Rpi4B"):
            self.assertIn("device", ptc.kinds(line), line)
        for line in ("grasping", "Graspi", "rpix", "therapist", "raspberries", "rasping", "crpi", "wraspi2",
                     "RPIX", "GRASPI", "budgetrpix", "Grasping", "TheRapist"):
            self.assertNotIn("device", ptc.kinds(line), line)

    def test_a_release_string_next_to_a_device_line(self):
        self.assertIn("f:2: device detail", ptc.scan_text("f", "a jetson board\nrunning R36.4.3\n"))
        self.assertIn("f:1: device detail", ptc.scan_text("f", "R36.4.3\non the jetson\n"))
        self.assertEqual([x for x in ptc.scan_text("f", "release R36.4.3 of the tool\n") if "detail" in x], [])

    def test_the_host_name_list_holds_only_hashes(self):
        self.assertTrue(ptc.HOST_NAME_HASHES)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", h) for h in ptc.HOST_NAME_HASHES))

    def test_host_names_by_hash_in_any_case(self):
        saved = ptc.HOST_NAME_HASHES
        self.addCleanup(lambda: setattr(ptc, "HOST_NAME_HASHES", saved))
        ptc.HOST_NAME_HASHES = {hashlib.sha256(b"zorb-box").hexdigest()}
        for line in ("ssh zorb-box", "on ZORB-BOX.local", "(zorb-box)"):
            self.assertIn("host name", ptc.kinds(line), line)
        for line in ("zorb box", "zorb-boxes", "a zorb-box2 run"):
            self.assertNotIn("host name", ptc.kinds(line), line)

if __name__ == "__main__":
    unittest.main()
