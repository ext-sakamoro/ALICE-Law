#!/usr/bin/env python3
"""Check that README.md names every repository the release train lists.

`release-train.toml` is the single source for which crates this repository's
checks span (`scripts/release_train.py load_roster`): a crate list typed out
again in README.md goes stale the moment a crate joins the train and nobody
remembers to edit the second copy (this happened: `alice-edge` joined the
train and three README sentences kept saying "eight core crates"). This
script is that assertion, not a generator — README is still written by hand,
this only fails the build when it falls out of sync with the roster.

    readme_roster_check.py [--roster FILE] [--readme FILE]

Exit 0 when every roster repository name appears in the README text, 1 when
one is missing (named in the output), 2 when the roster lists no repository
(compared nothing) or either file cannot be read.
"""
from __future__ import annotations

import argparse
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


def check(roster_text: str, readme_text: str) -> list[str]:
    repos = repos_from_roster(roster_text)
    if not repos:
        raise rt.RosterError("the roster lists no repository (compared nothing)")
    return [repo for repo in repos if repo not in readme_text]


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
    except rt.RosterError as e:
        print(f"readme-roster: {e}")
        return 2

    repos = repos_from_roster(roster_text)
    if missing:
        print(f"readme-roster: {len(missing)}/{len(repos)} roster repositories not named in {a.readme}:")
        for repo in missing:
            print(f"  {repo}")
        return 1
    print(f"readme-roster: ok, {len(repos)} roster repositories all named in {a.readme}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
