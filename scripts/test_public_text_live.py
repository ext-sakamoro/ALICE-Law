#!/usr/bin/env python3
"""Tests of public_text_live.py: real git repositories served over file://, the listing replaced."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_text_live as pl  # noqa: E402

# the words are built at run time: this file is published and scanned like any other
LINK = "[[" + "sample_note" + "_link]]"
TRACKER = "Back" + "log"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class World:
    def __init__(self, root: Path):
        self.root = root
        self.repos: list[dict] = []

    def repo(self, name: str, files: dict[str, str]) -> Path:
        g = self.root / "ext" / name
        if not g.exists():
            subprocess.run(["git", "init", "-q", "-b", "main", str(g)], check=True, env=ENV)
            self.repos.append({"name": name, "full_name": f"ext/{name}", "default_branch": "main", "size": 1})
        for p, text in files.items():
            f = g / p
            if text is None:
                f.unlink()
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        subprocess.run(["git", "-C", str(g), "add", "-A"], check=True, env=ENV)
        subprocess.run(["git", "-C", str(g), "commit", "-q", "--allow-empty", "-m", "c"], check=True, env=ENV)
        return g


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = World(Path(self.tmp.name))
        self.baseline = Path(self.tmp.name) / "baseline.toml"
        self._listing, self._github = pl.hl.public_repos, pl.GITHUB
        pl.hl.public_repos = lambda owner: list(self.w.repos)
        pl.GITHUB = "file://" + str(self.w.root) + "/"
        self.w.repo("ALICE-A", {"README.md": "a clean text\n"})
        self.w.repo("ALICE-B", {"README.md": f"see {LINK}\n", "docs/x.md": f"the {TRACKER} row\n"})

    def tearDown(self):
        pl.hl.public_repos, pl.GITHUB = self._listing, self._github
        self.tmp.cleanup()

    def run_live(self, *argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pl.main(["--baseline", str(self.baseline), "--jobs", "2", *argv])
        return code, out.getvalue() + err.getvalue()


class Live(Base):
    def test_a_written_baseline_then_passes(self):
        code, out = self.run_live("--write")
        self.assertEqual(code, 0, out)
        self.assertIn("2 read", out)
        code, out = self.run_live()
        self.assertEqual(code, 0, out)
        self.assertIn("0 findings", out)

    def test_the_baseline_names_no_repository_and_no_path(self):
        self.run_live("--write")
        text = self.baseline.read_text(encoding="utf-8")
        self.assertNotIn("ALICE-B", text)
        self.assertNotIn("README", text)
        self.assertEqual(len(pl.load_baseline(text)), 2)

    def test_a_new_finding_in_a_repository_without_its_own_check_is_red(self):
        self.run_live("--write")
        self.w.repo("ALICE-A", {"src/lib.rs": f"// {LINK}\n"})
        code, out = self.run_live()
        self.assertEqual(code, 1, out)
        self.assertIn("found: ALICE-A: src/lib.rs:", out)
        self.assertNotIn(LINK, out)  # the place and the kind, never the word

    def test_one_more_at_a_listed_place_is_red(self):
        self.run_live("--write")
        self.w.repo("ALICE-B", {"README.md": f"see {LINK}\nand {LINK}\n"})
        code, out = self.run_live()
        self.assertEqual(code, 1, out)
        self.assertIn("(2, the baseline has 1)", out)

    def test_a_listed_finding_moving_to_another_line_is_the_same_entry(self):
        self.run_live("--write")
        self.w.repo("ALICE-B", {"README.md": f"intro\n\nsee {LINK}\n"})
        code, out = self.run_live()
        self.assertEqual(code, 0, out)

    def test_a_fixed_finding_is_stale_and_shrink_lowers_it(self):
        self.run_live("--write")
        self.w.repo("ALICE-B", {"docs/x.md": "the row\n"})
        code, out = self.run_live()
        self.assertEqual(code, 1, out)
        self.assertIn("stale baseline entry", out)
        code, out = self.run_live("--shrink")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(pl.load_baseline(self.baseline.read_text(encoding="utf-8"))), 1)
        self.assertEqual(self.run_live()[0], 0)

    def test_shrink_lowers_a_count_and_the_removed_one_cannot_come_back(self):
        self.w.repo("ALICE-B", {"README.md": f"see {LINK}\nand {LINK}\n"})
        self.run_live("--write")
        self.w.repo("ALICE-B", {"README.md": f"see {LINK}\n"})
        self.assertEqual(self.run_live("--shrink")[0], 0)
        self.assertIn(" = 1\n", self.baseline.read_text(encoding="utf-8"))
        self.assertEqual(self.run_live()[0], 0)
        self.w.repo("ALICE-B", {"README.md": f"see {LINK}\nand {LINK}\n"})
        code, out = self.run_live()
        self.assertEqual(code, 1, out)

    def test_shrink_never_adds_a_finding(self):
        self.run_live("--write")
        self.w.repo("ALICE-A", {"src/lib.rs": f"// {LINK}\n"})
        before = self.baseline.read_text(encoding="utf-8")
        self.assertEqual(self.run_live("--shrink")[0], 0)
        self.assertEqual(self.baseline.read_text(encoding="utf-8"), before)
        self.assertEqual(self.run_live()[0], 1)

    def test_a_repository_that_turns_private_leaves_its_entries_stale(self):
        self.run_live("--write")
        self.w.repos = [r for r in self.w.repos if r["name"] != "ALICE-B"]
        code, out = self.run_live()
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count("stale baseline entry"), 2)

    def test_an_unreadable_repository_is_no_result(self):
        self.run_live("--write")
        self.w.repos.append({"name": "ALICE-C", "full_name": "ext/ALICE-C", "default_branch": "main", "size": 1})
        code, out = self.run_live()
        self.assertEqual(code, 2, out)
        self.assertIn("couldn't read: ALICE-C", out)

    def test_no_listed_repository_is_no_result(self):
        self.w.repos = []
        code, out = self.run_live("--write")
        self.assertEqual(code, 2, out)
        self.assertIn("no public repository was listed", out)

    def test_only_empty_repositories_compare_nothing(self):
        for r in self.w.repos:
            r["size"] = 0
        code, out = self.run_live("--write")
        self.assertEqual(code, 2, out)

    def test_local_reads_the_checkout_under_test(self):
        self.run_live("--write")
        g = self.w.root / "ext" / "ALICE-B"
        subprocess.run(["git", "-C", str(g), "rm", "-q", "docs/x.md"], check=True, env=ENV)
        subprocess.run(["git", "-C", str(g), "commit", "-q", "-m", "fix"], check=True, env=ENV)
        subprocess.run(["git", "-C", str(g), "reset", "-q", "--soft", "HEAD~1"], check=True, env=ENV)
        # HEAD still has the file (the removal is staged, not committed): the committed tree is read
        code, out = self.run_live("--local", f"ALICE-B={g}")
        self.assertEqual(code, 0, out)
        self.assertIn("ALICE-B read from its checkout", out)

    def test_local_must_name_a_listed_public_repository(self):
        code, out = self.run_live("--local", f"ALICE-Z={self.w.root}")
        self.assertEqual(code, 2, out)
        self.assertIn("not a listed public repository", out)


class Baseline(unittest.TestCase):
    def test_a_malformed_line_or_a_repeated_key_is_unreadable(self):
        k = "a" * 64
        for text in (f'"{k}" = 0\n', f'"{k}" = x\n', f'"{k[:63]}" = 1\n', f'"{k}" = 1\n"{k}" = 2\n', "junk\n"):
            with self.assertRaises(pl.Unreadable, msg=text):
                pl.load_baseline(text)
        self.assertEqual(pl.load_baseline(pl.dump_baseline({k: 3})), {k: 3})

    def test_the_line_number_is_not_part_of_the_key(self):
        self.assertEqual(pl.key("R", "a.md:3: note link"), pl.key("R", "a.md:30: note link"))
        self.assertNotEqual(pl.key("R", "a.md:3: note link"), pl.key("S", "a.md:3: note link"))


if __name__ == "__main__":
    unittest.main()
