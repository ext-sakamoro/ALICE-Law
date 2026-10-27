#!/usr/bin/env python3
"""Tests of scripts/gen_private_repo_hashes.py: the block it writes into a copy of public_text_check.py

The listings here use made-up repository names; the committed block is generated from the account.

    python3 scripts/test_gen_private_repo_hashes.py
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gprh", HERE / "gen_private_repo_hashes.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def h(name: str) -> str:
    return hashlib.sha256(name.lower().encode("utf-8")).hexdigest()


class Generate(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        self.checker = d / "public_text_check.py"
        shutil.copyfile(HERE / "public_text_check.py", self.checker)
        self.listing = d / "roster.json"

    def run_gen(self, rows, *extra) -> tuple[int, str]:
        self.listing.write_text(json.dumps(rows), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = gen.main(["--roster-json", str(self.listing), "--checker", str(self.checker), *extra])
        return rc, err.getvalue()

    def sets(self) -> tuple[set[str], set[str]]:
        text = self.checker.read_text(encoding="utf-8")
        def one(name):
            i = text.index(f"{name}: frozenset[str] = frozenset({{\n")
            return set(re.findall(r'"([0-9a-f]{64})"', text[i:text.index("})\n", i)]))
        return one("PRIVATE_REPO_HASHES"), one("PRIVATE_ALICE_REPO_HASHES")

    ROWS = [{"name": "ALICE-Zorblat", "visibility": "PRIVATE"}, {"name": "zorblat-notes", "visibility": "PRIVATE"},
            {"name": "ALICE-Quux", "visibility": "PUBLIC"}, {"name": "ALICE-Frob", "visibility": "INTERNAL"}]

    def test_the_sets_are_the_private_repositories_hashed(self):
        rc, err = self.run_gen(self.ROWS)
        self.assertEqual(rc, 0, err)
        every, alice = self.sets()
        self.assertEqual(every, {h("ALICE-Zorblat"), h("zorblat-notes")})
        self.assertEqual(alice, {h("ALICE-Zorblat")})
        text = self.checker.read_text(encoding="utf-8")
        for name in ("zorblat", "quux", "frob"):
            self.assertNotIn(name, text.lower(), "no name is written, only hashes")
        self.assertEqual(self.run_gen(self.ROWS, "--check")[0], 0, "what was written is up to date")

    def test_a_repository_made_public_drops_out(self):
        self.run_gen(self.ROWS)
        rows = [dict(r, visibility="PUBLIC") if r["name"] == "zorblat-notes" else r for r in self.ROWS]
        rc, err = self.run_gen(rows, "--check")
        self.assertEqual(rc, 1, "the block is stale once a repository is public")
        self.assertIn("stale", err)
        self.assertEqual(self.run_gen(rows)[0], 0)
        self.assertEqual(self.sets()[0], {h("ALICE-Zorblat")})

    def test_nothing_to_compare_writes_nothing(self):
        before = self.checker.read_text(encoding="utf-8")
        for rows, why in (([], "empty"), ([{"name": "ALICE-Quux", "visibility": "PUBLIC"}], "no private repository"),
                          ([{"name": "zorblat-notes", "visibility": "PRIVATE"}], "no private ALICE-*")):
            with self.subTest(why=why):
                rc, err = self.run_gen(rows)
                self.assertEqual(rc, 2)
                self.assertIn(why, err)
                self.assertEqual(self.checker.read_text(encoding="utf-8"), before, "nothing written")

    def test_a_checker_without_the_markers_is_refused(self):
        self.checker.write_text("x = 1\n", encoding="utf-8")
        rc, err = self.run_gen(self.ROWS)
        self.assertEqual(rc, 2)
        self.assertIn("markers", err)


def commit_tree(files: dict[str, str]) -> Path:
    r = Path(tempfile.mkdtemp())
    for name, text in files.items():
        (r / name).parent.mkdir(parents=True, exist_ok=True)
        (r / name).write_text(text, encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "fixture"]):
        subprocess.run(["git", "-C", str(r), *cmd], check=True, env=env, capture_output=True)
    return r


class Baseline(unittest.TestCase):
    """--baseline: the private repository names the public repositories' text holds now, read by the checker"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        self.checker = d / "public_text_check.py"
        src = (HERE / "public_text_check.py").read_text(encoding="utf-8")
        marker = "PRIVATE_ALICE_REPO_HASHES: frozenset[str] = frozenset({\n"
        self.checker.write_text(src.replace(marker, marker + f'    "{h("ALICE-Zorblat")}",\n', 1), encoding="utf-8")
        self.listing = d / "public.json"
        self.repo = commit_tree({"README.md": "built on ALICE-Zorblat\nand ALICE-Zorblat\n", "docs/x.md": "clean\n"})
        self.clean = commit_tree({"README.md": "nothing private\n"})
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.addCleanup(shutil.rmtree, self.clean, True)

    def run_gen(self, rows, *extra) -> tuple[int, str]:
        self.listing.write_text(json.dumps(rows), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = gen.main(["--baseline", str(self.listing), "--checker", str(self.checker), *extra])
        return rc, err.getvalue()

    def check_tree(self, repo: Path, name: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.checker), "--repo", str(repo), "--repo-name", name,
                               "--tree", "HEAD"], capture_output=True, text=True, encoding="utf-8")

    def rows(self):
        return [{"repo": "ALICE-Public", "dir": str(self.repo), "ref": "HEAD"},
                {"repo": "ALICE-Clean", "dir": str(self.clean), "ref": "HEAD"}]

    def test_the_baseline_lets_the_tree_pass_as_it_is(self):
        self.assertEqual(self.check_tree(self.repo, "ALICE-Public").returncode, 1, "red first: no baseline")
        rc, err = self.run_gen(self.rows())
        self.assertEqual(rc, 0, err)
        text = self.checker.read_text(encoding="utf-8")
        self.assertNotIn("zorblat", text.lower().replace(h("ALICE-Zorblat"), ""), "no name is written, only hashes")
        self.assertIn(f'    "{h("ALICE-Public")}": {{\n', text)
        self.assertNotIn(h("ALICE-Clean"), text, "a repository without names has no entry")
        self.assertIn(": 2,\n", text, "the two lines of README.md")
        r = self.check_tree(self.repo, "ALICE-Public")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.run_gen(self.rows(), "--check")[0], 0, "what was written is up to date")

    def test_a_new_mention_is_a_finding_and_the_baseline_stale(self):
        self.run_gen(self.rows())
        (self.repo / "docs/x.md").write_text("now ALICE-Zorblat here too\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "more"],
                       check=True, capture_output=True)
        r = self.check_tree(self.repo, "ALICE-Public")
        self.assertEqual(r.returncode, 1)
        self.assertIn("docs/x.md: private repository name (1 lines, the baseline allows 0)", r.stdout)
        rc, err = self.run_gen(self.rows(), "--check")
        self.assertEqual(rc, 1)
        self.assertIn("stale", err)

    def test_nothing_to_compare_writes_nothing(self):
        before = self.checker.read_text(encoding="utf-8")
        rc, err = self.run_gen([])
        self.assertEqual(rc, 2)
        self.assertIn("empty", err)
        rc, err = self.run_gen([{"repo": "ALICE-Public", "dir": str(self.repo), "ref": "no-such-ref"}])
        self.assertEqual(rc, 2, "a tree the checker cannot read is not a baseline of 0")
        self.assertEqual(self.checker.read_text(encoding="utf-8"), before, "nothing written")


class BaselineLive(unittest.TestCase):
    """--baseline live: every repository the API lists as public (not only ALICE-*), each default branch at
    depth 1, as Conformance (live) reads them"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        self.checker = d / "public_text_check.py"
        src = (HERE / "public_text_check.py").read_text(encoding="utf-8")
        marker = "PRIVATE_ALICE_REPO_HASHES: frozenset[str] = frozenset({\n"
        self.checker.write_text(src.replace(marker, marker + f'    "{h("ALICE-Zorblat")}",\n', 1), encoding="utf-8")
        self.remote = d / "remote"
        self.listed = []
        for name, files in (("ALICE-Public", {"README.md": "built on ALICE-Zorblat\n"}),
                            ("site-thing", {"index.md": "uses ALICE-Zorblat\nand ALICE-Zorblat\n"}),
                            ("ALICE-Clean", {"README.md": "nothing private\n"})):
            r = commit_tree(files)
            self.addCleanup(shutil.rmtree, r, True)
            (self.remote / "o").mkdir(parents=True, exist_ok=True)
            shutil.move(str(r), str(self.remote / "o" / name))
            branch = subprocess.run(["git", "-C", str(self.remote / "o" / name), "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True, check=True).stdout.strip()
            self.listed.append({"name": name, "full_name": f"o/{name}", "default_branch": branch, "size": 1})
        self.listed.append({"name": "ALICE-Empty", "full_name": "o/ALICE-Empty", "default_branch": "main", "size": 0})
        saved = (gen.GITHUB, gen.hl.public_repos)
        self.addCleanup(lambda: (setattr(gen, "GITHUB", saved[0]), setattr(gen.hl, "public_repos", saved[1])))
        gen.GITHUB = str(self.remote) + "/"
        gen.hl.public_repos = lambda owner: list(self.listed)

    def run_gen(self, *extra) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = gen.main(["--baseline", "live", "--checker", str(self.checker), "--jobs", "2", *extra])
        return rc, err.getvalue()

    def entries(self) -> str:
        text = self.checker.read_text(encoding="utf-8")
        return text[text.index(gen.BASELINE_BEGIN):text.index(gen.BASELINE_END)]

    def test_every_listed_public_repository_is_read(self):
        rc, err = self.run_gen()
        self.assertEqual(rc, 0, err)
        block = self.entries()
        self.assertIn(h("ALICE-Public"), block)
        self.assertIn(h("site-thing"), block, "a public repository that is not ALICE-* is read too")
        self.assertIn(": 2,\n", block, "the two lines of the other repository")
        self.assertNotIn(h("ALICE-Clean"), block)
        self.assertEqual(self.run_gen("--check")[0], 0)

    def test_local_reads_the_checkout_at_head(self):
        local = commit_tree({"README.md": "ALICE-Zorblat\nALICE-Zorblat\nALICE-Zorblat\n"})
        self.addCleanup(shutil.rmtree, local, True)
        rc, err = self.run_gen("--local", f"ALICE-Public={local}")
        self.assertEqual(rc, 0, err)
        block = self.entries()
        i = block.index(h("ALICE-Public"))
        self.assertIn(": 3,\n", block[i:block.index("    },", i)],
                      "the checkout (3 lines), not the default branch (1 line), is read")
        rc, err = self.run_gen("--local", f"ALICE-Unlisted={local}")
        self.assertEqual(rc, 2)
        self.assertIn("not a listed public repository", err)

    def test_a_repository_that_cannot_be_read_writes_nothing(self):
        before = self.checker.read_text(encoding="utf-8")
        self.listed.append({"name": "ALICE-Gone", "full_name": "o/ALICE-Gone", "default_branch": "main", "size": 1})
        rc, err = self.run_gen()
        self.assertEqual(rc, 2)
        self.assertIn("could not be read", err)
        self.listed[:] = []
        rc, err = self.run_gen()
        self.assertEqual(rc, 2)
        self.assertIn("no public repository", err)
        self.assertEqual(self.checker.read_text(encoding="utf-8"), before, "nothing written")


class Committed(unittest.TestCase):
    def test_the_committed_block_is_one_the_generator_wrote(self):
        text = (HERE / "public_text_check.py").read_text(encoding="utf-8")
        start, end = text.index(gen.BEGIN), text.index(gen.END) + len(gen.END) + 1
        every = sorted(set(re.findall(r'"([0-9a-f]{64})"', text[start:text.index("PRIVATE_ALICE_REPO_HASHES", start)])))
        alice = sorted(set(re.findall(r'"([0-9a-f]{64})"', text[text.index("PRIVATE_ALICE_REPO_HASHES", start):end])))
        self.assertGreater(len(alice), 0)
        self.assertEqual(text[start:end], gen.block(every, alice), "the block is in the generator's form")


if __name__ == "__main__":
    unittest.main()
