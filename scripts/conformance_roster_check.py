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
                                [--owner OWNER] [--write | --internal]
    conformance_roster_check.py --check-manifest DIR [--ref REF] [--gates FILE]

`--internal` reads nothing from the other repositories: it checks only that the committed state
file is consistent with this repository (it parses, it covers exactly the release-train crates
that are not exempt and not listed in missing-manifests.toml, each crate under its own repository,
once, and each repository's counts sum to the number of gates). That is what the pre-push gate
and the branch / pull request CI run, so a manifest landing in another repository does not make
this repository's own change fail. The comparison with the members' mains (the default mode) is
unchanged and runs on every push to main and daily (.github/workflows/conformance-live.yml); when
it finds the state stale it says how to regenerate it.

`--check-manifest DIR` checks one member's manifest at a git ref of a local clone (`--ref`,
default HEAD) before it lands: the same `check_manifest` the comparison with main runs (the gate
ids, the status vocabulary, the evidence paths and the location of a `fail-known`, the
`pass` / `n/a` read as `unverified` without evidence or a mutant), against the file tree of that
ref instead of main's. A branch's own CI does not run this check, so a manifest can pass there and
still be refused here once it lands; verifiers run it on the branch first. It prints the status
counts the state file would record; it reads no other repository and writes nothing. Exit 2 when
the ref has no manifest, no file or no gate was compared, or the clone or ref cannot be read.

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


# comments, raw strings (`r"…"`, `r#"…"#`, `br#"…"#`, inner quotes allowed), strings and char / byte
# literals (`'"'`, `b'\''`): a quote inside a char literal or a raw string must not start a string, or
# the code after it is blanked as if it were one (a lifetime `'a` has no closing quote and stays)
RUST_NON_CODE_RE = re.compile(r'//[^\n]*|/\*.*?\*/|(?<![\w])b?r(#*)".*?"\1|"(?:\\.|[^"\\])*"'
                              r"|(?<![\w])b?'(?:\\.|[^'\\\n])'", re.S)


def rust_code(text: str) -> str:
    """Rust source with comments and string literals blanked (a name in a comment is not a definition)"""
    return RUST_NON_CODE_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


SYMBOL_REF_RE = re.compile(r"^(?P<path>[\w./\-]+\.rs)::(?P<name>[A-Za-z_][A-Za-z0-9_]*)$")


def symbol_defined(ref: str, paths: set[str], read) -> str | None:
    """None when `<path>::<name>` names a file at the ref that defines `name` (a fn, type, const, static or
    enum variant written as `name` after one of those keywords, or `name {` / `name(` / `name,` inside it);
    otherwise why not"""
    m = SYMBOL_REF_RE.match(ref.strip())
    if not m:
        return f"{ref!r} is not <path>.rs::<name>"
    path, name = m["path"], m["name"]
    if path not in paths:
        return f"{path} is not in the tree"
    if read is None:
        return f"{path} could not be read"
    text = read(path)
    if text is None:
        return f"{path} could not be read"
    text = rust_code(text)
    if not re.search(r"\b(?:fn|struct|enum|trait|type|const|static|mod)\s+" + name + r"\b", text) and \
            not re.search(r"^\s*" + name + r"\b\s*[({,]", text, re.M):
        return f"{path} does not define {name}"
    return None


def test_fn_gap(ref: str, paths: set[str], read) -> str | None:
    """None when `<path>.rs::<fn>` is a test that runs: a `fn` carrying `#[test]`, with no `#[ignore]`
    and no always-false `#[cfg(any())]` among its attributes, in a file under tests/ or after a
    `#[cfg(test)]` in a file under src/; otherwise why not"""
    why = symbol_defined(ref, paths, read)
    if why:
        return why
    m = SYMBOL_REF_RE.match(ref.strip())
    path, name = m["path"], m["name"]
    code = rust_code(read(path))
    at = re.search(r"\bfn\s+" + name + r"\b", code)
    if not at:
        return f"{path}: {name} is not a fn"
    # the attributes directly above the fn (a #[...] block may span lines)
    head = code[:at.start()]
    lines = head.split("\n")
    attrs: list[str] = []
    depth = 0
    for line in reversed(lines[:-1] if lines and not lines[-1].strip() else lines):
        t = line.strip()
        if depth == 0 and not t:
            break
        if depth == 0 and not (t.startswith("#[") or t.startswith("pub") or t.endswith("]") or t.endswith(",")
                               or t.startswith("async")):
            break
        attrs.append(t)
        depth += t.count("]") - t.count("[")
    block = " ".join(attrs)
    if not re.search(r"#\[\s*test\s*\]", block):
        return f"{path}: {name} has no #[test]"
    if re.search(r"#\[\s*ignore\b", block):
        return f"{path}: {name} is #[ignore]d"
    if re.search(r"#\[\s*cfg\s*\(\s*any\s*\(\s*\)\s*\)\s*\]", block):
        return f"{path}: {name} is under #[cfg(any())], never built"
    if not path.startswith("tests/") and not re.search(r"#\[\s*cfg\s*\(\s*test\s*\)\s*\]", head):
        return f"{path}: {name} is outside tests/ and no #[cfg(test)] precedes it"
    return None


def law_evidence_gap(gid: str, e: dict, paths: set[str], read) -> str | None:
    """for a G2 or G8 `pass`: what the manifest lacks of the evidence the gate text asks for, or None"""
    if gid == "G2":
        ties = e.get("tie_tests")
        if not isinstance(ties, list) or not all(isinstance(x, str) for x in ties):
            return "tie_tests (one `<class>: <path>.rs::<test>` per class of scalar site, [] when it has none)"
        for t in ties:
            # the class label may contain ": " itself; the reference never does
            label, sep, ref = t.rpartition(": ")
            if not sep or not label.strip():
                return f"tie_tests entry {t!r} is not `<class>: <path>.rs::<test>`"
            why = test_fn_gap(ref, paths, read)
            if why:
                return f"tie_tests entry {t!r}: {why}"
    if gid == "G8":
        writers = e.get("writers")
        if not isinstance(writers, list) or not writers or not all(isinstance(x, str) for x in writers):
            return "writers (every function writing a durable form, `<path>.rs::<fn>`; an empty list compared nothing)"
        for w in writers:
            why = symbol_defined(w, paths, read)
            if why:
                return f"writers entry {w!r}: {why}"
        if e.get("legacy") != "none":
            for key in ("legacy_status", "legacy_policy"):
                ref = e.get(key)
                if not isinstance(ref, str):
                    return (f"{key} (the typed status of data read without an identifier and the policy refusing "
                            f"it, `<path>.rs::<name>`; or legacy = \"none\" when no such data is readable)")
                why = symbol_defined(ref, paths, read)
                if why:
                    return f"{key} {ref!r}: {why}"
    return None


def check_manifest(crate: str, text: str, vocab: dict[str, tuple[str, ...]], paths: set[str],
                   warnings: list[str], at: str = "main", read=None) -> tuple[list[str], dict[str, int], int]:
    """(findings, status counts, gates compared) for one crate's manifest text; `paths` are the files at `at`,
    `read(path)` their text (None: a G2 / G8 pass cannot be shown and reads as unverified)"""
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
            findings.append(f"{where}: {gid} evidence names paths that are not at {at}: {gone}")
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
                                f"names no file at {at} outside the root files describing the crate, by its "
                                f"path, or by its name with a line number)")
        if status in ("pass", "n/a"):
            shown = [t for t in named if exists(t, paths)]
            lack = f"evidence naming a path at {at}" if not shown else ("a mutant" if not str(mutant).strip() else "")
            if lack:
                warnings.append(f"{where}: {gid} {status} without {lack}, read as unverified")
                status = "unverified"
        if status == "pass":
            gap = law_evidence_gap(gid, e, paths, read)
            if gap:
                warnings.append(f"{where}: {gid} pass without {gap}, read as unverified")
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
            return None, paths, None
        blob = api(f"repos/{owner}/{repo}/contents/{MANIFEST}?ref=main")
        if blob is None:
            return None, paths, None
        cache: dict[str, str | None] = {}

        def read(path: str) -> str | None:
            if path not in cache:
                b = api(f"repos/{owner}/{repo}/contents/{path}?ref=main") if path in paths else None
                cache[path] = base64.b64decode(b["content"]).decode("utf-8", "replace") if b and b.get("content") else None
            return cache[path]
        return base64.b64decode(blob["content"]).decode("utf-8"), paths, read
    return fetch


def git_fetcher(repo_dir: Path, ref: str):
    """the (manifest text, blob paths) of one local clone at `ref`, the shape gh_fetcher returns for main"""
    def git(*args: str) -> str:
        p = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True, text=True)
        if p.returncode != 0:
            raise InputError(f"git {' '.join(args)} in {repo_dir}: {p.stderr.strip()[:200]}")
        return p.stdout

    def fetch() -> tuple[str | None, set[str]]:
        p = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise InputError(f"{repo_dir}: ref {ref!r} not found")
        commit = p.stdout.strip()
        paths = set(git("ls-tree", "-r", "--name-only", "-z", commit).split("\0")) - {""}
        if MANIFEST not in paths:
            return None, paths, None

        def read(path: str) -> str | None:
            return git("show", f"{commit}:{path}") if path in paths else None
        return git("show", f"{commit}:{MANIFEST}"), paths, read
    return fetch


def repo_name(repo_dir: Path) -> str:
    """the repository's name from its origin remote (ALICE-Physics), else the clone's directory name"""
    p = subprocess.run(["git", "-C", str(repo_dir), "remote", "get-url", "origin"], capture_output=True, text=True)
    url = p.stdout.strip() if p.returncode == 0 else ""
    name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git") if url else ""
    return name or repo_dir.resolve().name


def check_one(repo_dir: Path, ref: str, gates: dict, fetch=None) -> tuple[list[str], list[str], dict, int, int]:
    """(findings, notes, status counts, gates compared, files at the ref) for one manifest at a ref"""
    vocab = gate_vocabulary(gates)
    if not vocab:
        raise InputError("conformance/gates.toml lists no gate")
    text, paths, *rest = (fetch or git_fetcher(repo_dir, ref))()
    read = rest[0] if rest else None
    if not paths:
        raise InputError(f"{repo_dir} at {ref}: no file (compared nothing)")
    if text is None:
        raise InputError(f"{repo_dir} at {ref}: no {MANIFEST}")
    notes: list[str] = []
    findings, counts, compared = check_manifest(repo_name(repo_dir), text, vocab, paths, notes, at=ref, read=read)
    return findings, notes, counts, compared, len(paths)


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
        text, paths, *rest = cache[repo]
        reader = rest[0] if rest else None
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
            f, counts, n = check_manifest(repo, text, vocab, paths, notes, read=reader)
            findings += f
            compared += n
            checked[repo] = not f
            if not f:
                states[repo] = {"crates": [], "counts": counts}
        if checked[repo]:
            states[repo]["crates"].append(crate)
    files = len({repo for repo, got in cache.items() if got[0] is not None})
    return findings, notes, states, read, compared, files


def check_state(roster: dict, gates: dict, missing: dict, text: str) -> tuple[list[str], int]:
    """(findings, repositories read) for the committed state file alone (--internal)"""
    crates = roster_crates(roster)
    vocab = gate_vocabulary(gates)
    if not crates:
        raise InputError("release-train.toml lists no crate to check")
    if not vocab:
        raise InputError("conformance/gates.toml lists no gate")
    try:
        state = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [f"the state file does not parse: {e}"], 0
    entries = state.get("manifest", {})
    if set(state) - {"manifest"}:
        return [f"the state file has unknown tables: {sorted(set(state) - {'manifest'})}"], 0
    listed = set(missing.get("missing", {}))
    findings: list[str] = []
    seen: dict[str, str] = {}
    for repo, e in sorted(entries.items()):
        if not isinstance(e, dict):
            findings.append(f"{repo}: not a table")
            continue
        unknown = set(e) - {"crates", *STATUSES}
        if unknown:
            findings.append(f"{repo}: unknown keys {sorted(unknown)}")
        counts = [e.get(st) for st in STATUSES]
        if not all(isinstance(c, int) and not isinstance(c, bool) and c >= 0 for c in counts):
            findings.append(f"{repo}: each of {', '.join(STATUSES)} must be a count (an integer >= 0)")
        elif sum(counts) != len(vocab):
            findings.append(f"{repo}: the counts sum to {sum(counts)}, not to the {len(vocab)} gates")
        names = e.get("crates")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            findings.append(f"{repo}: crates must be a non-empty list of names")
            continue
        for n in names:
            if n not in crates:
                findings.append(f"{repo}: {n} is not a release-train crate that is checked")
            elif crates[n] != repo:
                findings.append(f"{repo}: {n} belongs to {crates[n]} in release-train.toml")
            if n in seen:
                findings.append(f"{n} is listed under both {seen[n]} and {repo}")
            seen.setdefault(n, repo)
    for n in sorted(set(crates) - set(seen) - listed):
        findings.append(f"{n} ({crates[n]}) has no entry in the state file")
    for n in sorted(set(seen) & listed):
        findings.append(f"{n} has an entry in the state file and is listed in missing-manifests.toml")
    return findings, len(entries)


def main(argv: list[str] | None = None, fetch=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roster", default=str(ROOT / "release-train.toml"))
    ap.add_argument("--gates", default=str(ROOT / "conformance" / "gates.toml"))
    ap.add_argument("--missing", default=str(ROOT / "conformance" / "missing-manifests.toml"))
    ap.add_argument("--state", default=str(ROOT / "conformance" / "manifest-state.toml"))
    ap.add_argument("--owner", default=None, help="the GitHub owner (default: [train] github of the roster)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write the status counts to --state")
    mode.add_argument("--internal", action="store_true",
                      help="check the committed state file alone, reading no other repository")
    mode.add_argument("--check-manifest", metavar="DIR",
                      help="check the manifest of the local clone DIR at --ref, before it lands")
    ap.add_argument("--ref", default="HEAD", help="the git ref --check-manifest reads (default HEAD)")
    a = ap.parse_args(argv)
    if a.check_manifest:
        try:
            gates = load_toml(Path(a.gates))
            findings, notes, counts, compared, nfiles = check_one(Path(a.check_manifest), a.ref, gates, fetch)
        except (InputError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        for n in notes:
            print(f"note: {n}")
        for f in findings:
            print(f"found: {f}")
        if compared == 0:
            print("error: the manifest has no gate entry (compared nothing)", file=sys.stderr)
            return 2
        shown = ", ".join(f"{s} {counts[s]}" for s in STATUSES) if not findings else "not counted"
        print(f"manifest check: {repo_name(Path(a.check_manifest))}@{a.ref}: {compared} gates compared against "
              f"{nfiles} files, {len(findings)} findings ({shown})")
        return 1 if findings else 0
    if a.internal:
        try:
            roster = load_toml(Path(a.roster))
            gates = load_toml(Path(a.gates))
            missing = load_toml(Path(a.missing)) if Path(a.missing).exists() else {}
            text = Path(a.state).read_text(encoding="utf-8")
            findings, n = check_state(roster, gates, missing, text)
        except (InputError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        for f in findings:
            print(f"found: {f}")
        if n == 0 and not findings:
            print("error: the state file has no manifest entry (compared nothing)", file=sys.stderr)
            return 2
        print(f"conformance state (internal): {n} repositories, {len(findings)} findings")
        return 1 if findings else 0
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
            findings.append(f"{state_path.name} differs from the manifests at main (regenerate manifest-state: "
                            f"run python3 scripts/conformance_roster_check.py --write && "
                            f"python3 scripts/gen_law_status.py, and commit both)")
            print(f"found: {findings[-1]}")
    print(f"conformance roster: {files} manifests read for {read} crates (crates that share a repository share "
          f"its manifest), {compared} gates compared, {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
