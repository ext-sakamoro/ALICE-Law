#!/usr/bin/env python3
"""Check that every release-train crate declares its ALICE-Law conformance, at its main.

Each crate in `release-train.toml` that is not exempt must hold `alice-law-conformance.toml`
at the root of its repository's main branch, with one entry per gate of
`conformance/gates.toml` and the fields docs/CONFORMANCE.md's "Manifest" section asks for:

- every gate id of gates.toml, and no other `G<n>` id;
- `status` one of the gate's status vocabulary (the legacy `missing` is read as
  `unverified`, with a warning);
- an evidence entry whose first word is a path names a file that exists at main;
- `fail-known` and `n/a` carry a note; `fail-known` is located: its note or evidence names a file
  that exists at main (the site, with or without its line, or the file where the required thing
  is absent);
- `pass` / `n/a` without evidence or without a mutant is read as `unverified` (a warning, as the
  generated views read it), not as met.

A crate with no manifest fails with its name, unless `conformance/missing-manifests.toml` lists
it (with the repository whose change removes the entry and the reason): a listed crate still
without a manifest is reported as expected; a listed crate whose manifest has landed fails as a
stale entry, so the list shrinks in the change that lands the manifest. It is never a skip.

The status counts of each repository's manifest, with the crates it is read for, are written to
`conformance/manifest-state.toml` with `--write`; without
it, the committed file must equal what main shows (a manifest changed upstream is a finding until
the file is regenerated), which keeps the README tables generated from it deterministic.

    conformance_roster_check.py [--roster FILE] [--gates FILE] [--missing FILE] [--state FILE]
                                [--owner OWNER] [--write]

Exit 0 when every crate passes, 1 on a finding, 2 when nothing was compared (no crate, no gate,
no manifest read) or an input cannot be read. Reads each repository through `gh api`
(GH_TOKEN in CI): its manifest and its file tree at main, two calls per repository.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MANIFEST = "alice-law-conformance.toml"
LEGACY = {"missing": "unverified"}
STATUSES = ("pass", "fail-known", "unverified", "n/a")
GATE_ID_RE = re.compile(r"^G\d+$")
# a key spelled like a gate id in another case or with leading zeros (`g15`, `G01`)
GATE_LIKE_RE = re.compile(r"^[gG]0*\d+$")
EXTENSIONS = "rs|toml|py|yml|yaml|md|sh|txt|json|lock|cfg|ini|js|ts|wgsl|glsl|c|h|cpp|hpp|swift|kt"
# a path-like word in a note or an evidence entry, after backticks and URLs are removed: a run of
# `<dir>/` segments with an optional file, or a file name with an extension; a following `:<line>`
# or `::<name>` is not part of it
WORD_PATH_RE = re.compile(r"(?<![\w./:-])((?:\.\.?/)*(?:[\w.\-]+/)+[\w.\-]*|[\w\-][\w.\-]*\.(?:" + EXTENSIONS
                          + r"))(?![\w])")
LINE_AFTER_RE = re.compile(r"^(?::\d+|\s+lines?\s+\d+)")
URL_RE = re.compile(r"\w+://\S+")
# files at the repository root that describe the crate, not where a requirement is met or unmet
ROOT_META_RE = re.compile(r"^(?:Cargo\.toml|Cargo\.lock|README[\w.\-]*|CHANGELOG[\w.\-]*|LICENSE[\w.\-]*|NOTICE[\w.\-]*|"
                          r"CONTRIBUTING[\w.\-]*|alice-law-conformance\.toml)$")


# a path of another repository named in prose ("ALICE-Law's scripts/x.py") is not this one's
OTHER_REPO_BEFORE_RE = re.compile(r"ALICE-[\w\-]+(?:'s)?\s*$")
LABEL_RE = re.compile(r"^[A-Za-z][\w \-]{0,24}:\s+(?=\S)")


def first_path(entry: str) -> str | None:
    """the path an evidence entry starts with (docs/CONFORMANCE.md: an entry whose first word is a
    path must exist), after backticks, a `label:` prefix, a leading `./` and a `:<line>` /
    `::<name>` suffix are removed; None when the first word is not path-like"""
    text = LABEL_RE.sub("", str(entry).replace("`", " ").strip())
    words = text.split()
    if not words:
        return None
    word = re.split(r"::|:(?=\d)|:$", words[0])[0].rstrip(".,;)")
    while word.startswith("./"):
        word = word[2:]
    if "/" in word or re.search(r"\.(?:" + EXTENSIONS + r")$", word):
        return word
    return None


def path_words(text: str) -> list[tuple[str, bool]]:
    """(path token, followed by a line number) for each path-like word of the text that is not
    named as another repository's"""
    text = URL_RE.sub(" ", str(text).replace("`", " "))
    out = []
    for m in WORD_PATH_RE.finditer(text):
        if OTHER_REPO_BEFORE_RE.search(text[:m.start(1)]):
            continue
        token = m.group(1).rstrip(".,;)")
        while token.startswith("./"):
            token = token[2:]
        if token:
            out.append((token, bool(LINE_AFTER_RE.match(text[m.end(1):]))))
    return out


def exists(token: str, paths: set[str]) -> bool:
    """the token is a file at main, a directory (a file is under it), or the bare name of exactly
    one file (`ci.yml` for `.github/workflows/ci.yml`)"""
    t = token.rstrip("/")
    if (not token.endswith("/") and t in paths) or any(p.startswith(t + "/") for p in paths):
        return True
    return "/" not in t and len([p for p in paths if p.endswith("/" + t)]) == 1


def locates(token: str, with_line: bool, paths: set[str]) -> bool:
    """the token names one file at main where a requirement is met or unmet: its exact path (not a
    directory and not a root file describing the crate), or a bare trailing part of exactly one
    path when a line number follows (`ci.yml:40`), or of exactly one workflow"""
    if token in paths:
        return not ROOT_META_RE.match(token)
    if "/" not in token.strip("/"):
        hits = [p for p in paths if p.endswith("/" + token)]
        # a bare name of exactly one file: with a line number anywhere, or without one for a
        # workflow (the workflow where a step or a leg is absent)
        return len(hits) == 1 and not ROOT_META_RE.match(hits[0]) and (
            with_line or hits[0].startswith(".github/workflows/"))
    return False


class InputError(Exception):
    pass


def load_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise InputError(f"{path}: {e}") from e


def roster_crates(roster: dict) -> dict[str, str]:
    """{crate: repository} of the crates that are not exempt"""
    out = {}
    for name, c in roster.get("crate", {}).items():
        if c.get("state") == "exempt":
            continue
        if not isinstance(c.get("repo"), str) or not c["repo"]:
            raise InputError(f"release-train.toml: crate {name} has no repo")
        out[name] = c["repo"]
    return out


def gate_vocabulary(gates: dict) -> dict[str, tuple[str, ...]]:
    default = tuple(gates.get("status_vocabulary", STATUSES))
    out = {}
    for g in gates.get("gate", []):
        gid = g.get("id")
        if not isinstance(gid, str) or not GATE_ID_RE.match(gid):
            raise InputError(f"conformance/gates.toml: gate id {gid!r}")
        out[gid] = tuple(g.get("status_vocabulary", default))
    return out


def check_manifest(crate: str, text: str, vocab: dict[str, tuple[str, ...]], paths: set[str],
                   warnings: list[str]) -> tuple[list[str], dict[str, int], int]:
    """(findings, status counts, gates compared) for one crate's manifest text"""
    where = f"{crate}: {MANIFEST}"
    try:
        manifest = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [f"{where}: not valid TOML ({e})"], {}, 0
    findings: list[str] = []
    ids = {k for k in manifest if GATE_ID_RE.match(k)}
    for k in sorted(k for k in manifest if GATE_LIKE_RE.match(k) and not GATE_ID_RE.match(k)):
        findings.append(f"{where}: {k!r} is not spelled as a gate id (G<n>, upper case, no leading zero)")
    for gid in sorted(set(vocab) - ids, key=lambda g: int(g[1:])):
        findings.append(f"{where}: gate {gid} has no entry")
    for gid in sorted(ids - set(vocab), key=lambda g: int(g[1:])):
        findings.append(f"{where}: {gid} is not a gate of conformance/gates.toml")
    counts = dict.fromkeys(STATUSES, 0)
    compared = 0
    for gid in sorted(ids & set(vocab), key=lambda g: int(g[1:])):
        e = manifest[gid]
        compared += 1
        if not isinstance(e, dict) or not isinstance(e.get("status"), str):
            findings.append(f"{where}: {gid} has no status")
            continue
        status = e["status"]
        if status in LEGACY:
            warnings.append(f"{where}: {gid} status {status!r} is the earlier vocabulary, read as {LEGACY[status]!r}")
            status = LEGACY[status]
        if status not in vocab[gid]:
            findings.append(f"{where}: {gid} status {status!r} is not one of {list(vocab[gid])}")
            continue
        evidence = e.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(x, str) for x in evidence):
            findings.append(f"{where}: {gid} evidence is not a list of strings")
            continue
        named = [t for t in (first_path(x) for x in evidence) if t]
        gone = sorted({t for t in named if not exists(t, paths)})
        if gone:
            findings.append(f"{where}: {gid} evidence names paths that are not at main: {gone}")
        mutant = e.get("mutant", "")
        if "mutant" in e and not isinstance(mutant, str):
            findings.append(f"{where}: {gid} mutant is not a string")
            mutant = ""
        note = str(e.get("note", "")).strip()
        if status in ("fail-known", "n/a") and not note:
            findings.append(f"{where}: {gid} is {status} without a note")
        if status == "fail-known" and note:
            words = [w for s in [note, *evidence] for w in path_words(s)]
            if not any(locates(t, line, paths) for t, line in words):
                findings.append(f"{where}: {gid} is fail-known without a located site (its note or evidence "
                                f"names no file at main outside the root files describing the crate, by its "
                                f"path, or by its name with a line number)")
        if status in ("pass", "n/a"):
            shown = [t for t in named if exists(t, paths)]
            lack = "evidence naming a path at main" if not shown else ("a mutant" if not str(mutant).strip() else "")
            if lack:
                warnings.append(f"{where}: {gid} {status} without {lack}, read as unverified")
                status = "unverified"
        counts[status] += 1
    return findings, counts, compared


def gh_fetcher(owner: str):
    def api(path: str) -> dict | None:
        p = subprocess.run(["gh", "api", path], capture_output=True, text=True)
        if p.returncode != 0:
            if "HTTP 404" in p.stderr or "Not Found" in p.stderr:
                return None
            raise InputError(f"gh api {path}: {p.stderr.strip()[:200]}")
        return json.loads(p.stdout)

    def fetch(repo: str) -> tuple[str | None, set[str]]:
        tree = api(f"repos/{owner}/{repo}/git/trees/main?recursive=1")
        if tree is None:
            raise InputError(f"{owner}/{repo}: main has no tree")
        if tree.get("truncated"):
            raise InputError(f"{owner}/{repo}: the file tree is truncated")
        paths = {t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"}
        if MANIFEST not in paths:
            return None, paths
        blob = api(f"repos/{owner}/{repo}/contents/{MANIFEST}?ref=main")
        if blob is None:
            return None, paths
        return base64.b64decode(blob["content"]).decode("utf-8"), paths
    return fetch


def render_state(states: dict[str, dict]) -> str:
    out = ["# Generated by scripts/conformance_roster_check.py --write: the status counts of each",
           "# alice-law-conformance.toml at the main of a release-train repository, and the crates it is",
           "# read for (crates that share a repository share its one manifest; do not edit by hand)", ""]
    for repo in sorted(states):
        c = states[repo]
        out.append(f'[manifest."{repo}"]')
        out.append("crates = [" + ", ".join(f'"{x}"' for x in sorted(c["crates"])) + "]")
        for s in STATUSES:
            out.append(f'"{s}" = {c["counts"][s]}')
        out.append("")
    return "\n".join(out)


def check(roster: dict, gates: dict, missing: dict, fetch) -> tuple[list[str], list[str], dict, int, int, int]:
    """(findings, notes, states, manifests read, gates compared, distinct manifest files)"""
    crates = roster_crates(roster)
    vocab = gate_vocabulary(gates)
    if not crates:
        raise InputError("release-train.toml lists no crate to check")
    if not vocab:
        raise InputError("conformance/gates.toml lists no gate")
    listed = missing.get("missing", {})
    for name, e in listed.items():
        if name not in crates:
            raise InputError(f"missing-manifests.toml: {name} is not a release-train crate")
        if not isinstance(e, dict) or not str(e.get("fixed-in", "")).strip() or not str(e.get("why", "")).strip():
            raise InputError(f"missing-manifests.toml: {name} needs `fixed-in` (the repository whose change "
                             f"adds the manifest) and `why`")
    findings: list[str] = []
    notes: list[str] = []
    states: dict[str, dict] = {}
    read = compared = 0
    cache: dict[str, tuple[str | None, set[str]]] = {}
    checked: dict[str, bool] = {}  # repository -> its manifest checked clean
    for crate, repo in sorted(crates.items()):
        if repo not in cache:
            cache[repo] = fetch(repo)
        text, paths = cache[repo]
        if text is None:
            if crate in listed:
                notes.append(f"{crate}: no {MANIFEST} at main, as missing-manifests.toml expects "
                             f"(fixed in {listed[crate]['fixed-in']}: {listed[crate]['why']})")
            else:
                findings.append(f"{crate}: no {MANIFEST} at the main of {repo}")
            continue
        if crate in listed:
            findings.append(f"{crate}: {MANIFEST} is at main, so its missing-manifests.toml entry is stale "
                            f"(remove it)")
        read += 1
        if repo not in checked:  # one manifest per repository, checked once for every crate it is read for
            f, counts, n = check_manifest(repo, text, vocab, paths, notes)
            findings += f
            compared += n
            checked[repo] = not f
            if not f:
                states[repo] = {"crates": [], "counts": counts}
        if checked[repo]:
            states[repo]["crates"].append(crate)
    files = len({repo for repo, (text, _) in cache.items() if text is not None})
    return findings, notes, states, read, compared, files


def main(argv: list[str] | None = None, fetch=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roster", default=str(ROOT / "release-train.toml"))
    ap.add_argument("--gates", default=str(ROOT / "conformance" / "gates.toml"))
    ap.add_argument("--missing", default=str(ROOT / "conformance" / "missing-manifests.toml"))
    ap.add_argument("--state", default=str(ROOT / "conformance" / "manifest-state.toml"))
    ap.add_argument("--owner", default=None, help="the GitHub owner (default: [train] github of the roster)")
    ap.add_argument("--write", action="store_true", help="write the status counts to --state")
    a = ap.parse_args(argv)
    try:
        roster = load_toml(Path(a.roster))
        gates = load_toml(Path(a.gates))
        missing = load_toml(Path(a.missing)) if Path(a.missing).exists() else {}
        owner = a.owner or roster.get("train", {}).get("github")
        if not owner:
            raise InputError("no GitHub owner ([train] github in the roster, or --owner)")
        findings, notes, states, read, compared, files = check(roster, gates, missing, fetch or gh_fetcher(owner))
    except InputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for n in notes:
        print(f"note: {n}")
    for f in findings:
        print(f"found: {f}")
    if read == 0 or compared == 0:
        print(f"error: nothing was compared ({read} manifests, {compared} gates)", file=sys.stderr)
        return 2
    state_path = Path(a.state)
    rendered = render_state(states)
    if not findings:
        if a.write:
            state_path.write_text(rendered, encoding="utf-8")
            print(f"conformance roster: wrote {state_path.name}")
        elif not state_path.exists() or state_path.read_text(encoding="utf-8") != rendered:
            findings.append(f"{state_path.name} differs from the manifests at main "
                            f"(regenerate with --write)")
            print(f"found: {findings[-1]}")
    print(f"conformance roster: {files} manifests read for {read} crates (crates that share a repository share "
          f"its manifest), {compared} gates compared, {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
