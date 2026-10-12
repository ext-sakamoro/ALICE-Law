#!/usr/bin/env python3
"""Check that README.md's intro paragraph names every release-train repository.

`release-train.toml` is the single source for which crates this repository's
checks span (`scripts/release_train.py load_roster`): a crate list typed out
again in README.md goes stale the moment a crate joins the train and nobody
remembers to edit the second copy (this happened: `alice-edge` joined the
train and three README sentences kept saying "eight core crates"). This
script is that assertion, not a generator — README is still written by hand,
this only fails the build when it falls out of sync with the roster.

Checked against the intro paragraph specifically (the first paragraph after
the title), not the whole file: a name appearing anywhere else in README
(Status, a table, a link) would otherwise make this pass while the sentence
that actually claims to list every crate silently drops one. Matched whole:
a repository name must not be matched by it appearing only as part of a
longer one (`ALICE-LOL` inside `ALICE-LOL-Macro`) or a name being a
substring of something unrelated.

    readme_roster_check.py [--roster FILE] [--readme FILE]

Exit 0 when every roster repository name appears, whole, in that paragraph,
1 when one is missing (named in the output), 2 when the roster lists no
repository (compared nothing), the README has no paragraph after its title,
or either file cannot be read.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_train as rt  # noqa: E402

# This repository checks the other crates; it is not one of the repositories
# README is expected to name as a subject of those checks (same convention as
# dep_consistency.py's CORE and release_train.py's own roster handling).
SELF_REPO = "ALICE-Law"


def repos_from_roster(roster_text: str) -> list[str]:
    roster = rt.load_roster(roster_text)
    names = {c["repo"] for c in roster.values() if "repo" in c}
    names.discard(SELF_REPO)
    return sorted(names)


class ReadmeFormatError(ValueError):
    pass


def intro_paragraph(readme_text: str) -> str:
    """The first paragraph after the title: the title line, a blank line,
    then every line up to the next blank line (or the end of the file)."""
    paragraphs = re.split(r"\n\s*\n", readme_text.strip(), maxsplit=2)
    if len(paragraphs) < 2:
        raise ReadmeFormatError("no paragraph found after the title (expected a title, a blank line, then text)")
    return paragraphs[1]


def names_whole(text: str, name: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9-]){re.escape(name)}(?![A-Za-z0-9-])", text) is not None


def check(roster_text: str, readme_text: str) -> list[str]:
    repos = repos_from_roster(roster_text)
    if not repos:
        raise rt.RosterError("the roster lists no repository (compared nothing)")
    paragraph = intro_paragraph(readme_text)
    return [repo for repo in repos if not names_whole(paragraph, repo)]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--roster", default="release-train.toml")
    p.add_argument("--readme", default="README.md")
    a = p.parse_args(argv)

    try:
        roster_text = Path(a.roster).read_text(encoding="utf-8")
        readme_text = Path(a.readme).read_text(encoding="utf-8")
    except OSError as e:
        print(f"readme-roster: cannot read {e.filename}: {e.strerror}")
        return 2

    try:
        missing = check(roster_text, readme_text)
    except (rt.RosterError, ReadmeFormatError) as e:
        print(f"readme-roster: {e}")
        return 2

    repos = repos_from_roster(roster_text)
    if missing:
        print(f"readme-roster: {len(missing)}/{len(repos)} roster repositories not named, whole, "
              f"in {a.readme}'s intro paragraph:")
        for repo in missing:
            print(f"  {repo}")
        return 1
    print(f"readme-roster: ok, {len(repos)} roster repositories all named, whole, in {a.readme}'s intro paragraph")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
