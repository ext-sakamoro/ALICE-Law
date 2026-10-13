#!/usr/bin/env python3
"""Tests of scripts/public_text_check.py (python3 scripts/test_public_text_check.py)"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import re
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
            self.assertIn("agent process", ptc.kinds(line), line)

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

    def test_the_private_repository_list_is_loaded_and_holds_only_hashes(self):
        self.assertGreater(len(ptc.PRIVATE_REPO_HASHES), 0)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", h) for h in ptc.PRIVATE_REPO_HASHES))


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
