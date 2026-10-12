#!/usr/bin/env python3
"""Tests of scripts/readme_roster_check.py (python3 scripts/test_readme_roster_check.py)"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rrc", HERE / "readme_roster_check.py")
rrc = importlib.util.module_from_spec(spec)
sys.modules["rrc"] = rrc
spec.loader.exec_module(rrc)


def roster(*entries: tuple[str, str]) -> str:
    return "".join(
        f'[crate.{name}]\nversion = "1.0.0"\nstate = "published"\norder = {i}\nrepo = "{repo}"\n\n'
        for i, (name, repo) in enumerate(entries, start=1))


class ReposFromRoster(unittest.TestCase):
    def test_distinct_repos_sorted_self_excluded(self):
        text = roster(("alice-lol", "ALICE-LOL"), ("alice-lol-macro", "ALICE-LOL"),
                       ("alice-zip", "ALICE-Zip"), ("alice-law-integration", "ALICE-Law"))
        self.assertEqual(rrc.repos_from_roster(text), ["ALICE-LOL", "ALICE-Zip"])

    def test_entry_with_no_repo_is_skipped(self):
        text = '[crate.x]\nversion = "1.0.0"\nstate = "planned"\norder = 1\n'
        self.assertEqual(rrc.repos_from_roster(text), [])

    def test_empty_roster_raises(self):
        with self.assertRaises(rrc.rt.RosterError):
            rrc.repos_from_roster("")


class Check(unittest.TestCase):
    def test_every_repo_named_passes(self):
        text = roster(("alice-lol", "ALICE-LOL"), ("alice-zip", "ALICE-Zip"))
        self.assertEqual(rrc.check(text, "about ALICE-LOL and ALICE-Zip"), [])

    def test_a_missing_repo_is_named(self):
        text = roster(("alice-lol", "ALICE-LOL"), ("alice-zip", "ALICE-Zip"))
        self.assertEqual(rrc.check(text, "about ALICE-LOL only"), ["ALICE-Zip"])

    def test_every_repo_missing_is_every_repo_named(self):
        text = roster(("alice-lol", "ALICE-LOL"), ("alice-zip", "ALICE-Zip"))
        self.assertEqual(sorted(rrc.check(text, "unrelated text")), ["ALICE-LOL", "ALICE-Zip"])

    def test_a_substring_match_is_not_enough_to_hide_a_miss(self):
        # "ALICE-LOL" is not a substring of "ALICE-LOL-extra" in the direction
        # that matters here: the README text must CONTAIN the repo name, so a
        # repo name that is itself a substring of unrelated README text would
        # be a false pass -- this fixture is a reminder, not a real risk,
        # since repo names are checked as members-of-readme-text, not the
        # reverse; kept as a named case for documentation value.
        text = roster(("alice-lol", "ALICE-LOL"))
        self.assertEqual(rrc.check(text, "mentions ALICE-LOL-extra too"), [])

    def test_no_repositories_in_roster_raises(self):
        with self.assertRaises(rrc.rt.RosterError):
            rrc.check('[crate.x]\nversion = "1.0.0"\nstate = "planned"\norder = 1\n', "anything")


class Main(unittest.TestCase):
    def write(self, tmp: Path, roster_text: str, readme_text: str) -> tuple[Path, Path]:
        r = tmp / "release-train.toml"
        d = tmp / "README.md"
        r.write_text(roster_text, encoding="utf-8")
        d.write_text(readme_text, encoding="utf-8")
        return r, d

    def test_exit_0_when_all_named(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            r, d = self.write(Path(t), roster(("alice-lol", "ALICE-LOL")), "ALICE-LOL here")
            self.assertEqual(rrc.main(["--roster", str(r), "--readme", str(d)]), 0)

    def test_exit_1_when_one_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            r, d = self.write(Path(t), roster(("alice-lol", "ALICE-LOL"), ("alice-zip", "ALICE-Zip")),
                               "ALICE-LOL only")
            self.assertEqual(rrc.main(["--roster", str(r), "--readme", str(d)]), 1)

    def test_exit_2_when_roster_file_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "README.md"
            d.write_text("x", encoding="utf-8")
            self.assertEqual(rrc.main(["--roster", str(Path(t) / "nope.toml"), "--readme", str(d)]), 2)

    def test_exit_2_when_readme_file_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            r = Path(t) / "release-train.toml"
            r.write_text(roster(("alice-lol", "ALICE-LOL")), encoding="utf-8")
            self.assertEqual(rrc.main(["--roster", str(r), "--readme", str(Path(t) / "nope.md")]), 2)

    def test_exit_2_when_roster_lists_no_repo(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            r, d = self.write(Path(t), '[crate.x]\nversion = "1.0.0"\nstate = "planned"\norder = 1\n', "x")
            self.assertEqual(rrc.main(["--roster", str(r), "--readme", str(d)]), 2)


class RealFiles(unittest.TestCase):
    def test_this_repositorys_readme_names_every_roster_repository(self):
        root = HERE.parent
        self.assertEqual(rrc.main(["--roster", str(root / "release-train.toml"),
                                    "--readme", str(root / "README.md")]), 0)


if __name__ == "__main__":
    unittest.main()
