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
  generated views read it), not as met; an `n/a` also needs a scanner in its evidence (a checker of
  scripts/ at main with its own test, run by a workflow, not a generic driver) and a mutant that says
  it was measured.

A crate with no manifest fails with its name, unless `conformance/missing-manifests.toml` lists
it (with the repository whose change removes the entry and the reason): a listed crate still
without a manifest is reported as expected; a listed crate whose manifest has landed fails as a
stale entry, so the list shrinks in the change that lands the manifest. It is never a skip.

The status counts of each repository's manifest, with the crates it is read for, are written to
`conformance/manifest-state.toml` with `--write`; without
it, the committed file must equal what main shows (a manifest changed upstream is a finding until
the file is regenerated), which keeps the README tables generated from it deterministic. A difference
is explained by `conformance/pending-state.toml` (written by scripts/land_law_state.py --write before the
crate lands). The rule is on the counts, not on the history (a landing may rebase): for a repository an
entry names, main's counts equal to the recorded ones mean the entry has landed (a note; a finding with
`--strict`, the receipt before a push to main, and after PENDING_MAX_DAYS days); otherwise the state must
record the counts computed from the manifest at the entry's commit, and an entry older than
PENDING_MAX_DAYS days is a finding.

    conformance_roster_check.py [--roster FILE] [--gates FILE] [--missing FILE] [--state FILE]
                                [--owner OWNER] [--pending FILE] [--strict] [--today YYYY-MM-DD]
                                [--write | --internal]
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
import datetime
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
    `::<name>` / `#<token>` suffix are removed (`path#token` is the anchor form of a place in a file);
    None when the first word is not path-like"""
    text = LABEL_RE.sub("", str(entry).replace("`", " ").strip())
    words = text.split()
    if not words:
        return None
    word = re.split(r"::|:(?=\d)|:$|#", words[0])[0].rstrip(".,;)")
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


def repo_crates(roster: dict) -> dict[str, list[str]]:
    """{repository: its crates, sorted} of the crates that are not exempt (a repository's state section lists them)"""
    out: dict[str, list[str]] = {}
    for crate, repo in sorted(roster_crates(roster).items()):
        out.setdefault(repo, []).append(crate)
    return out


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


# A new gate is phased in: its row carries `adoption = { since = <date>, until = <date> }`, and until the
# end of that window a manifest without an entry for it reads as `unverified` (a note, counted in the
# state), never as `pass`; after `until` the absent entry is a finding again, as for every other gate.
# The window is at most ADOPTION_MAX_DAYS days, and CHANGELOG.md has a line naming the gate with both
# dates, so moving either date (an extension) needs a new line naming the new dates.
ADOPTION_MAX_DAYS = 45
TODAY: datetime.date | None = None  # set by main from --today; None: today, UTC


def today() -> datetime.date:
    return TODAY or datetime.datetime.now(datetime.timezone.utc).date()


def adoption_window(g: dict) -> tuple[datetime.date, datetime.date] | None:
    a = g.get("adoption")
    if a is None:
        return None
    try:
        return datetime.date.fromisoformat(str(a["since"])), datetime.date.fromisoformat(str(a["until"]))
    except (KeyError, TypeError, ValueError):
        return None


def adoption_findings(gates: dict, changelog: str | None) -> list[str]:
    """what is wrong with the adoption windows of conformance/gates.toml"""
    out = []
    for g in gates.get("gate", []):
        if "adoption" not in g:
            continue
        gid = g.get("id")
        w = adoption_window(g)
        if not isinstance(g["adoption"], dict) or w is None:
            out.append(f"conformance/gates.toml: {gid} adoption needs `since` and `until` (ISO dates)")
            continue
        since, until = w
        if until < since or (until - since).days > ADOPTION_MAX_DAYS:
            out.append(f"conformance/gates.toml: {gid} adoption window {since}..{until} is "
                       f"{(until - since).days} days (at most {ADOPTION_MAX_DAYS})")
        if changelog is None:
            out.append(f"{gid} adoption: CHANGELOG.md cannot be read to find the line naming its dates")
        elif not any(gid in line and str(since) in line and str(until) in line for line in changelog.splitlines()):
            out.append(f"CHANGELOG.md: no line names {gid} with its adoption dates {since} and {until} "
                       f"(a new or moved window is recorded there)")
    return out


def gate_phase_in(gates: dict) -> dict[str, str]:
    """the gates whose adoption window holds today: gate id -> the last day"""
    out = {}
    for g in gates.get("gate", []):
        w = adoption_window(g)
        if w and w[0] <= today() <= w[1] and (w[1] - w[0]).days <= ADOPTION_MAX_DAYS:
            out[g["id"]] = str(w[1])
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


MEMBER_TEST_RE = re.compile(r"^(?P<dir>.+)/tests/[^/]+\.rs$")


def integration_test_file(path: str, paths: set[str], read) -> bool:
    """whether `path` is an integration-test target: `tests/<file>.rs` at the root, or `<dir>/tests/<file>.rs` of a
    package directory (`<dir>/Cargo.toml` has a [package]: a workspace member, or a package the root workspace does
    not list, which cargo builds the same way when it is built); a tests/ directory under any other directory (a
    module under src/, a virtual manifest, no manifest) and a subdirectory of tests/ are not one"""
    if path.startswith("tests/"):
        return True
    m = MEMBER_TEST_RE.match(path)
    if not m:
        return False
    manifest = f"{m['dir']}/Cargo.toml"
    if manifest not in paths or read is None:
        return False
    try:
        return isinstance(tomllib.loads(read(manifest) or "").get("package"), dict)
    except tomllib.TOMLDecodeError:
        return False


def test_fn_gap(ref: str, paths: set[str], read) -> str | None:
    """None when `<path>.rs::<fn>` is a test that runs: a `fn` carrying `#[test]`, with no `#[ignore]`
    and no always-false `#[cfg(any())]` among its attributes, in an integration-test file (tests/ of the root or
    of a workspace member, integration_test_file) or after a `#[cfg(test)]`; otherwise why not"""
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
    if not integration_test_file(path, paths, read) and not re.search(r"#\[\s*cfg\s*\(\s*test\s*\)\s*\]", head):
        return f"{path}: {name} is outside tests/ (of the root or of a workspace member) and no #[cfg(test)] precedes it"
    return None


NA_SCANNER_DIR = "scripts/"
MEASURED = re.compile(r"\bmeasured\b", re.IGNORECASE)
# drivers that run many checks: naming one does not say which check measured the absence
NA_GENERIC = frozenset({"preflight.sh", "run_tests.sh", "law_tests.sh", "shared_files.sh", "public_text.sh"})


def na_scanner(t: str, paths: set[str], read) -> bool:
    """whether the evidence path `t` is a scanner that measures: a checker under scripts/ at the ref, not a generic
    driver, with its own test (scripts/test_<name>.py), and run by a workflow at the ref (named on a line of a
    workflow file that is not a comment)"""
    if not t.startswith(NA_SCANNER_DIR) or not exists(t, paths):
        return False
    name = t.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    if name in NA_GENERIC or f"{NA_SCANNER_DIR}test_{stem}.py" not in paths or read is None:
        return False
    for w in sorted(x for x in paths if x.startswith(".github/workflows/") and x.endswith((".yml", ".yaml"))):
        text = read(w) or ""
        if any(t in line and not line.lstrip().startswith("#") for line in text.splitlines()):
            return True
    return False


def na_evidence_gap(named: list[str], paths: set[str], mutant: str, read=None) -> str | None:
    """what an `n/a` lacks (docs/CONFORMANCE.md: its evidence is the scanner that catches a violation elsewhere
    finding zero sites here, its mutant is adding one site, measured): a scanner (see na_scanner) among the
    evidence's paths, and a mutant that says it was measured"""
    if not any(na_scanner(t, paths, read) for t in named):
        return (f"a scanner of {NA_SCANNER_DIR} at the ref in its evidence, with its own test and run by a workflow "
                f"(the absence must be measured by a check)")
    if not MEASURED.search(mutant):
        return "a mutant marked measured (the added site must have been tried, not described)"
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


# an evidence anchor: `path#token`, a place in a file named by a string that occurs there exactly once
# (or `path#A>B`: A once, then the first B after it), the form that replaces line numbers
ANCHOR_RE = re.compile(r"(?P<path>(?:[\w.\-]+/)*[\w.\-]+\.[\w]+|(?:[\w.\-]+/)+[\w.\-]+)#(?P<token>.+)", re.S)


def anchors_in(text: str):
    """the anchors of a string: its code spans (the text between a pair of backticks) that are a path,
    `#` and a token; a span such as `#[cfg(test)]` is code, not an anchor"""
    for span in text.split("`")[1::2]:
        m = ANCHOR_RE.fullmatch(span)
        if m:
            yield m


def anchor_gap(path: str, token: str, paths: set[str], read) -> str | None:
    """None when `path#token` names one place: the path is a file at the ref and the token occurs in it
    exactly once, or it splits at a `>` into a scope A that occurs once and a B that occurs after it (the
    whole token is tried first, so a token like `-> f32` is not read as a scope); otherwise why not"""
    if path not in paths:
        return f"{path} is not a file at the ref (anchors take the path from the repository root)"
    text = read(path) if read is not None else None
    if text is None:
        return f"{path} could not be read"
    n = text.count(token)
    if n == 1:
        return None
    for i, ch in enumerate(token):
        if ch == ">" and 0 < i < len(token) - 1:
            a, b = token[:i], token[i + 1:]
            if text.count(a) == 1 and b in text[text.index(a) + len(a):]:
                return None
    return f"{token!r} occurs {n} times in {path} (an anchor must name one place: lengthen it or scope it as A>B)"


# A clause (a string cut at `;` and at the end of a sentence) that names a pin, by its file or by the
# script that reads it, makes every ALICE-Law revision it writes a claim about that pin: `ALICE-Law <hex>`
# or `revision (<hex>)`. A clause that names no pin (the revision a table was first generated at, say)
# is history and is not compared.
CLAUSE_SPLIT_RE = re.compile(r";|(?<=[.!?])\s")
PIN_NAME_RES = (
    (re.compile(r"\.github/([a-z0-9][a-z0-9-]*-pin)\b"), None),
    (re.compile(r"\bshared_files\.sh\b"), "shared-files-pin"),
    (re.compile(r"\bg11_table\.sh\b"), "g11-table-pin"),
    (re.compile(r"\bpublic_text\.sh\b"), "public-text-pin"),
)
PIN_CLAIM_RE = re.compile(r"\bALICE-Law ([0-9a-f]{7,40})\b|\brevision \(([0-9a-f]{7,40})\)")
PIN_REV_RE = re.compile(r"^rev ([0-9a-f]{40})\s*$", re.M)


def pin_mentions(text: str) -> list[tuple[tuple[str, ...], str]]:
    """(pin files the clause names, revision) for every ALICE-Law revision written in a clause that
    names a pin"""
    out = []
    for clause in CLAUSE_SPLIT_RE.split(text):
        pins = []
        for r, pin in PIN_NAME_RES:
            for m in r.finditer(clause):
                p = pin or m.group(1)
                if p not in pins:
                    pins.append(p)
        if not pins:
            continue
        for m in PIN_CLAIM_RE.finditer(clause):
            out.append((tuple(pins), m.group(1) or m.group(2)))
    return out


def pin_mention_gap(pins: tuple[str, ...], rev: str, read) -> str | None:
    """None when one of the clause's pin files at the ref pins a revision that starts with `rev`;
    otherwise why not"""
    seen = []
    for pin in pins:
        text = read(f".github/{pin}") if read is not None else None
        if text is None:
            seen.append(f".github/{pin} could not be read at the ref")
            continue
        m = PIN_REV_RE.search(text)
        if m is None:
            seen.append(f".github/{pin} has no `rev <40 hex>` line")
            continue
        if m.group(1).startswith(rev):
            return None
        seen.append(f".github/{pin} pins {m.group(1)[:12]}")
    return f"names ALICE-Law {rev} next to its pin, but " + ", ".join(seen)


def strings_in(value, field: str):
    """(field, string) for every string inside a gate's value: a list's items and a table's values,
    recursively, each under the top-level field it sits in"""
    if isinstance(value, str):
        yield field, value
    elif isinstance(value, list):
        for v in value:
            yield from strings_in(v, field)
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings_in(v, field)


def check_manifest(crate: str, text: str, vocab: dict[str, tuple[str, ...]], paths: set[str],
                   warnings: list[str], at: str = "main", read=None,
                   phase_in: dict[str, str] | None = None) -> tuple[list[str], dict[str, int], int]:
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
    counts = dict.fromkeys(STATUSES, 0)
    for gid in sorted(set(vocab) - ids, key=lambda g: int(g[1:])):
        if gid in (phase_in or {}):
            warnings.append(f"{where}: gate {gid} has no entry, read as unverified while it is phased in "
                            f"(until {phase_in[gid]})")
            counts["unverified"] += 1
        else:
            findings.append(f"{where}: gate {gid} has no entry")
    for gid in sorted(ids - set(vocab), key=lambda g: int(g[1:])):
        findings.append(f"{where}: {gid} is not a gate of conformance/gates.toml")
    compared = 0
    na_checked = na_downgraded = 0
    anchors: dict[str, int] = {}
    pin_mentions_checked = 0
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
        # every string of the gate's table (evidence, mutant, note, tie_tests, any later field)
        for field in sorted(e):
            for _, x in strings_in(e[field], field):
                for m in anchors_in(x):
                    anchors[field] = anchors.get(field, 0) + 1
                    why = anchor_gap(m["path"], m["token"], paths, read)
                    if why:
                        findings.append(f"{where}: {gid} {field} anchor `{m['path']}#{m['token']}`: {why}")
                for pins, rev in pin_mentions(x):
                    pin_mentions_checked += 1
                    why = pin_mention_gap(pins, rev, read)
                    if why:
                        findings.append(f"{where}: {gid} {field} {why}")
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
        if status == "n/a":
            na_checked += 1
            gap = na_evidence_gap(named, paths, str(mutant), read)
            if gap:
                na_downgraded += 1
                warnings.append(f"{where}: {gid} n/a without {gap}, read as unverified")
                status = "unverified"
        counts[status] += 1
    if anchors:
        per = ", ".join(f"{k} {v}" for k, v in sorted(anchors.items()))
        warnings.append(f"{where}: {sum(anchors.values())} anchor(s) checked ({per})")
    if pin_mentions_checked:
        warnings.append(f"{where}: {pin_mentions_checked} ALICE-Law revision(s) next to a pin file checked")
    if na_checked:
        warnings.append(f"{where}: {na_checked} n/a cell(s) checked for a measured scanner, "
                        f"{na_downgraded} read as unverified")
    return findings, counts, compared


def gh_api(path: str) -> dict | None:
    p = subprocess.run(["gh", "api", path], capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        if "HTTP 404" in p.stderr or "Not Found" in p.stderr:
            return None
        raise InputError(f"gh api {path}: {p.stderr.strip()[:200]}")
    return json.loads(p.stdout)


def gh_fetcher(owner: str, ref: str = "main"):
    def api(path: str) -> dict | None:
        p = subprocess.run(["gh", "api", path], capture_output=True, text=True, encoding="utf-8")
        if p.returncode != 0:
            if "HTTP 404" in p.stderr or "Not Found" in p.stderr:
                return None
            raise InputError(f"gh api {path}: {p.stderr.strip()[:200]}")
        return json.loads(p.stdout)

    def fetch(repo: str) -> tuple[str | None, set[str]]:
        tree = api(f"repos/{owner}/{repo}/git/trees/{ref}?recursive=1")
        if tree is None:
            raise InputError(f"{owner}/{repo}: {ref} has no tree")
        if tree.get("truncated"):
            raise InputError(f"{owner}/{repo}: the file tree is truncated")
        paths = {t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"}
        if MANIFEST not in paths:
            return None, paths, None
        blob = api(f"repos/{owner}/{repo}/contents/{MANIFEST}?ref={ref}")
        if blob is None:
            return None, paths, None
        cache: dict[str, str | None] = {}

        def read(path: str) -> str | None:
            if path not in cache:
                b = api(f"repos/{owner}/{repo}/contents/{path}?ref={ref}") if path in paths else None
                cache[path] = base64.b64decode(b["content"]).decode("utf-8", "replace") if b and b.get("content") else None
            return cache[path]
        return base64.b64decode(blob["content"]).decode("utf-8"), paths, read
    return fetch


def git_fetcher(repo_dir: Path, ref: str):
    """the (manifest text, blob paths) of one local clone at `ref`, the shape gh_fetcher returns for main"""
    def git(*args: str) -> str:
        p = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True, text=True, encoding="utf-8")
        if p.returncode != 0:
            raise InputError(f"git {' '.join(args)} in {repo_dir}: {p.stderr.strip()[:200]}")
        return p.stdout

    def fetch() -> tuple[str | None, set[str]]:
        p = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                           capture_output=True, text=True, encoding="utf-8")
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
    p = subprocess.run(["git", "-C", str(repo_dir), "remote", "get-url", "origin"], capture_output=True, text=True, encoding="utf-8")
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
    findings, counts, compared = check_manifest(repo_name(repo_dir), text, vocab, paths, notes, at=ref, read=read,
                                                phase_in=gate_phase_in(gates))
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


# a line-number reference in a manifest (docs/CONFORMANCE.md: evidence names a place by an anchor `path#token`,
# which the roster checks, never by a line number, which goes stale silently)
# a line-number reference: "(line 3)", "lines 3 to 9", a GitHub "#L42" / "#L3-L9" anchor, a path followed by "L42",
# or any file extension followed by ":42" (the extension starts with a letter, so "1.5:1" and versions are not one)
LINE_REF = re.compile(r"(\(lines? \d[^)]*\)|\blines? \d+(?:\s*(?:to|and|-|–|,)\s*\d+)*|#L\d+(?:-L?\d+)?"
                      r"|\.[A-Za-z][A-Za-z0-9]{0,7}(?::\d+|,? L\d+\b))")


def line_ref_matches(value: str) -> list[re.Match]:
    """LINE_REF's matches in one string, without a host:port in a URL"""
    out = []
    for m in LINE_REF.finditer(value):
        start = m.start()
        while start > 0 and not value[start - 1].isspace() and value[start - 1] not in "(\"'`":
            start -= 1
        if "://" in value[start:m.start()]:
            continue
        out.append(m)
    return out


def line_refs(text: str) -> int:
    """the line-number references in the string fields of a manifest's gate tables"""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return 0  # an unreadable manifest is the manifest check's finding
    n = 0

    def walk(v):
        nonlocal n
        if isinstance(v, str):
            n += len(line_ref_matches(v))
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
    for k, v in data.items():
        if re.fullmatch(r"G\d+", k):
            walk(v)
    return n


def load_line_baseline(path: Path) -> dict[str, int] | None:
    """repository -> the line-number references its manifest may still hold (None: no baseline file, so none may)"""
    if not path.exists():
        return None
    data = load_toml(path)
    unknown = set(data) - {"baseline"}
    if unknown:
        raise InputError(f"{path.name}: unknown top-level keys {sorted(unknown)}")
    out = {}
    for repo, n in data.get("baseline", {}).items():
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise InputError(f"{path.name}: {repo}: the baseline must be a count (an integer >= 0)")
        out[repo] = n
    return out


def line_ref_findings(texts: dict[str, str], baseline: dict[str, int] | None, strict: bool = True,
                      below_since=None, today: datetime.date | None = None) -> tuple[list[str], list[str]]:
    """(findings, notes): each manifest holds exactly its baseline's line-number references; with no baseline
    file, none. Above the baseline is a finding (a new reference). Below it, the slack would let the crate add
    references back up to the baseline, so it is a finding until the baseline is lowered: with `strict` (the
    receipt before a push to main, the lander), or when `below_since(repo, baseline)` (the date of the crate's
    main commit that took the count below the baseline, None if unknown) is unknown or more than
    PENDING_MAX_DAYS days before `today`; a fresh one is a note naming the slack."""
    findings, notes = [], []
    for repo in sorted(texts):
        n = line_refs(texts[repo])
        allowed = 0 if baseline is None else baseline.get(repo, 0)
        notes.append(f"{repo}: {n} line-number reference(s) in {MANIFEST}, baseline {allowed}")
        if n > allowed:
            findings.append(f"{repo}: {MANIFEST} has {n} line-number reference(s), the baseline allows {allowed}: name "
                            f"the place by an anchor `path#token` (scripts/line_refs_propose.py proposes one)")
        elif n < allowed:
            msg = (f"{repo}: {MANIFEST} has {n} line-number reference(s), below its baseline {allowed}: lower "
                   f"the baseline in conformance/line-refs-baseline.toml to {n}")
            since = None if strict or below_since is None or today is None else below_since(repo, allowed)
            if since is None:
                findings.append(msg + ("" if strict else " (the date it went below is not known)"))
            elif (today - since).days > PENDING_MAX_DAYS:
                findings.append(msg + f" (below since {since.isoformat()}, {(today - since).days} days)")
            else:
                notes.append(f"slack: {repo}: baseline {allowed}, count {n}, below since {since.isoformat()} "
                             f"(a finding after {PENDING_MAX_DAYS} days and under --strict)")
    if baseline is not None:
        for repo in sorted(set(baseline) - set(texts)):
            findings.append(f"{repo}: in conformance/line-refs-baseline.toml but no manifest was read for it")
    return findings, notes


LINE_HISTORY_DEPTH = 30


def gh_manifest_history(owner: str):
    """repo -> [(sha, commit date)] of the commits on main that changed its manifest, newest first"""
    def history(repo: str) -> list[tuple[str, datetime.date]]:
        p = subprocess.run(["gh", "api", f"repos/{owner}/{repo}/commits?sha=main&path={MANIFEST}"
                            f"&per_page={LINE_HISTORY_DEPTH}"], capture_output=True, text=True, encoding="utf-8")
        if p.returncode != 0:
            raise InputError(f"gh api commits of {owner}/{repo}: {p.stderr.strip()[:200]}")
        return [(c["sha"], datetime.date.fromisoformat(c["commit"]["committer"]["date"][:10]))
                for c in json.loads(p.stdout)]
    return history


def below_since_from(history, at):
    """below_since for line_ref_findings: walk the manifest's commits on main from the newest; the date of the
    oldest commit of the newest run below `allowed` (None when the run reaches past the history read)"""
    def below_since(repo: str, allowed: int) -> datetime.date | None:
        since = None
        for sha, date in history(repo):
            text = at(repo, sha)[0]
            if text is not None and line_refs(text) >= allowed:
                return since
            since = date
        return None
    return below_since


PENDING_MAX_DAYS = 3
PENDING_HEADER = ("# Generated by scripts/land_law_state.py --write (and kept by conformance_roster_check.py --write):\n"
                  "# a crate commit that has not landed yet, whose status counts manifest-state.toml already records\n"
                  "# (the state is recorded before the crate lands; do not edit by hand)\n")


def load_pending(path: Path) -> dict[str, dict]:
    """repository -> {"sha", "since"} of conformance/pending-state.toml (none when the file is absent)"""
    if not path.exists():
        return {}
    data = load_toml(path)
    unknown = set(data) - {"pending"}
    if unknown:
        raise InputError(f"{path.name}: unknown top-level keys {sorted(unknown)}")
    out = {}
    for repo, e in data.get("pending", {}).items():
        if not isinstance(e, dict) or set(e) != {"sha", "since"}:
            raise InputError(f"{path.name}: [pending.\"{repo}\"] must be {{ sha, since }}")
        if not isinstance(e["sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", e["sha"]):
            raise InputError(f"{path.name}: {repo}: sha must be a full 40-hex commit id")
        try:
            since = datetime.date.fromisoformat(str(e["since"]))
        except ValueError:
            raise InputError(f"{path.name}: {repo}: since must be YYYY-MM-DD") from None
        out[repo] = {"sha": e["sha"], "since": since.isoformat()}
    return out


def render_pending(pending: dict[str, dict]) -> str:
    out = [PENDING_HEADER]
    for repo in sorted(pending):
        out.append(f'[pending."{repo}"]\nsha = "{pending[repo]["sha"]}"\nsince = "{pending[repo]["since"]}"\n')
    return "\n".join(out)


def state_sections(state_text: str) -> dict[str, dict]:
    """repository -> its whole [manifest."<repo>"] table (crates and counts) of a manifest-state.toml text"""
    if not state_text.strip():
        return {}
    try:
        data = tomllib.loads(state_text)
    except tomllib.TOMLDecodeError as e:
        raise InputError(f"manifest-state.toml: {e}") from e
    m = data.get("manifest", {})
    if not isinstance(m, dict):
        raise InputError("manifest-state.toml: [manifest] is not a table")
    return m


def table_counts(state_text: str) -> dict[str, dict]:
    """repository -> status counts recorded in a manifest-state.toml text (empty for an empty text)"""
    if not state_text.strip():
        return {}
    try:
        data = tomllib.loads(state_text)
    except tomllib.TOMLDecodeError as e:
        raise InputError(f"manifest-state.toml: {e}") from e
    return {r: {s: v.get(s, 0) for s in STATUSES} for r, v in data.get("manifest", {}).items()}


def apply_pending(states: dict[str, dict], table: dict[str, dict], pending: dict[str, dict], gates: dict, at,
                  today: datetime.date, strict: bool,
                  repo_crates: dict[str, list[str]] | None = None) -> tuple[dict, dict, list[str], list[str]]:
    """(the state the table must record, the entries still pending, findings, notes). The rule is on the counts,
    not on the history (a landing may rebase, and other commits may land first): for a repository a pending entry
    names, when main's counts equal the table's the entry has landed (a note; a finding with `strict` or after
    PENDING_MAX_DAYS days); otherwise the table must record the counts computed from the manifest at the entry's
    commit (read there, not trusted), within PENDING_MAX_DAYS days."""
    expected = {r: {"crates": list(v["crates"]), "counts": dict(v["counts"])} for r, v in states.items()}
    kept: dict[str, dict] = {}
    findings: list[str] = []
    notes: list[str] = []
    vocab = gate_vocabulary(gates)
    for repo, e in sorted(pending.items()):
        sha, since = e["sha"], datetime.date.fromisoformat(e["since"])
        age = (today - since).days
        if age < 0:
            findings.append(f"{repo}: pending entry {sha[:12]} is dated {e['since']}, after today")
        first = repo not in states  # no manifest at main: the entry records the repository's first manifest
        if first and repo not in (repo_crates or {}):
            findings.append(f"{repo}: pending entry {sha[:12]} names a repository the state does not record")
            continue
        if not first and states[repo]["counts"] == table.get(repo):
            msg = (f"{repo}: pending entry {sha[:12]}: main's counts are the recorded ones, so it has landed "
                   f"(drop the entry: land_law_state.py --write or conformance_roster_check.py --write)")
            if strict or age > PENDING_MAX_DAYS:
                findings.append(msg + (f" ({age} days old)" if age > PENDING_MAX_DAYS else ""))
            else:
                notes.append(msg)
            continue
        kept[repo] = e
        text, paths, *rest = at(repo, sha)
        if text is None:
            findings.append(f"{repo}: pending entry {sha[:12]} has no {MANIFEST}")
            continue
        f, counts, n = check_manifest(repo, text, vocab, paths, notes, read=rest[0] if rest else None,
                                      phase_in=gate_phase_in(gates))
        if f or n == 0:
            findings += f or [f"{repo}: pending entry {sha[:12]}: no gate compared"]
            continue
        if counts != table.get(repo):
            findings.append(f"{repo}: the state records counts that are neither main's nor those of pending entry "
                            f"{sha[:12]} (recorded {table.get(repo)}, at {sha[:12]} {counts})")
            continue
        if first:
            expected[repo] = {"crates": list(repo_crates[repo]), "counts": counts}
        else:
            expected[repo]["counts"] = counts
        notes.append(f"{repo}: pending: the state records {sha[:12]}" + (" (its first manifest)" if first else "")
                     + f", not landed yet (since {e['since']})")
        if age > PENDING_MAX_DAYS:
            findings.append(f"{repo}: pending entry {sha[:12]} since {e['since']}, more than {PENDING_MAX_DAYS} "
                            f"days without landing")
    return expected, kept, findings, notes


def check(roster: dict, gates: dict, missing: dict, fetch, texts: dict | None = None,
          pending: dict | None = None) -> tuple[list[str], list[str], dict, int, int, int]:
    """(findings, notes, states, manifests read, gates compared, distinct manifest files); `texts`, when given,
    receives each repository's manifest text. A crate with no manifest at main is expected when missing-manifests.toml
    lists it, or when `pending` (pending-state.toml) names its repository: the state records the first manifest
    before the crate lands it (apply_pending reads and checks it at the pending commit)"""
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
            if texts is not None and cache[repo][0] is not None:
                texts[repo] = cache[repo][0]
        text, paths, *rest = cache[repo]
        reader = rest[0] if rest else None
        if text is None:
            if crate in listed:
                notes.append(f"{crate}: no {MANIFEST} at main, as missing-manifests.toml expects "
                             f"(fixed in {listed[crate]['fixed-in']}: {listed[crate]['why']})")
            elif repo in (pending or {}):
                notes.append(f"{crate}: no {MANIFEST} at main yet: pending-state.toml records its first manifest "
                             f"at {pending[repo]['sha'][:12]}")
            else:
                findings.append(f"{crate}: no {MANIFEST} at the main of {repo}")
            continue
        if crate in listed:
            findings.append(f"{crate}: {MANIFEST} is at main, so its missing-manifests.toml entry is stale "
                            f"(remove it)")
        read += 1
        if repo not in checked:  # one manifest per repository, checked once for every crate it is read for
            f, counts, n = check_manifest(repo, text, vocab, paths, notes, read=reader, phase_in=gate_phase_in(gates))
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


def main(argv: list[str] | None = None, fetch=None, at=None, history=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roster", default=str(ROOT / "release-train.toml"))
    ap.add_argument("--gates", default=str(ROOT / "conformance" / "gates.toml"))
    ap.add_argument("--missing", default=str(ROOT / "conformance" / "missing-manifests.toml"))
    ap.add_argument("--state", default=str(ROOT / "conformance" / "manifest-state.toml"))
    ap.add_argument("--pending", default=str(ROOT / "conformance" / "pending-state.toml"),
                    help="the crate commits the state records before they land (land_law_state.py --write)")
    ap.add_argument("--strict", action="store_true",
                    help="a pending entry whose commit has landed, or a manifest below its line-reference "
                         "baseline, is a finding (the pre-push receipt), not a note")
    ap.add_argument("--state-drift-note", metavar="BASE_STATE",
                    help="the landing gate of a change in this repository, with BASE_STATE the state file at the base "
                         "(origin/main): a section of the committed state the change left as the base has it and that "
                         "differs from the crates' mains is a note (a crate landed meanwhile); a section the change "
                         "added, removed or edited must equal the regenerated one (a finding otherwise); the daily run, "
                         "without this flag, fails on any difference and the state is regenerated")
    ap.add_argument("--today", help="the date pending entries are aged against (default: today, UTC)")
    ap.add_argument("--line-baseline", default=str(ROOT / "conformance" / "line-refs-baseline.toml"),
                    help="the line-number references each manifest may still hold (absent: none may)")
    ap.add_argument("--owner", default=None, help="the GitHub owner (default: [train] github of the roster)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write the status counts to --state")
    mode.add_argument("--internal", action="store_true",
                      help="check the committed state file alone, reading no other repository")
    mode.add_argument("--check-manifest", metavar="DIR",
                      help="check the manifest of the local clone DIR at --ref, before it lands")
    ap.add_argument("--ref", default="HEAD", help="the git ref --check-manifest reads (default HEAD)")
    a = ap.parse_args(argv)
    global TODAY
    TODAY = datetime.date.fromisoformat(a.today) if a.today else None
    try:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        changelog = None
    try:
        adoption = adoption_findings(load_toml(Path(a.gates)), changelog)
    except (InputError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for f in adoption:
        print(f"found: {f}")
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
        return 1 if findings or adoption else 0
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
        return 1 if findings or adoption else 0
    try:
        roster = load_toml(Path(a.roster))
        gates = load_toml(Path(a.gates))
        missing = load_toml(Path(a.missing)) if Path(a.missing).exists() else {}
        owner = a.owner or roster.get("train", {}).get("github")
        if not owner:
            raise InputError("no GitHub owner ([train] github in the roster, or --owner)")
        texts: dict[str, str] = {}
        pending_path = Path(a.pending)
        pending = load_pending(pending_path)
        findings, notes, states, read, compared, files = check(roster, gates, missing, fetch or gh_fetcher(owner),
                                                               texts, pending)
        today = (datetime.date.fromisoformat(a.today) if a.today
                 else datetime.datetime.now(datetime.timezone.utc).date())
        at = at or (lambda repo, sha: gh_fetcher(owner, sha)(repo))
        lf, ln = line_ref_findings(texts, load_line_baseline(Path(a.line_baseline)), a.strict,
                                   below_since_from(history or gh_manifest_history(owner), at), today)
        findings += lf
        notes += ln
        state_text = Path(a.state).read_text(encoding="utf-8") if Path(a.state).exists() else ""
        expected, kept, pf, pn = apply_pending(
            states, table_counts(state_text), pending, gates,
            at, today, a.strict, repo_crates(roster))
        findings += pf
        notes += pn
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
    rendered = render_state(expected)
    if a.state_drift_note is not None:
        try:  # the base is read whether or not the state differs: an unreadable input is never a pass
            state_sections(Path(a.state_drift_note).read_text(encoding="utf-8"))
        except (OSError, InputError) as e:
            print(f"error: the base state {a.state_drift_note}: {e}", file=sys.stderr)
            return 2
    if not findings:
        if a.write:
            state_path.write_text(rendered, encoding="utf-8")
            print(f"conformance roster: wrote {state_path.name}")
            if pending_path.exists() or kept:
                pending_path.write_text(render_pending(kept), encoding="utf-8")
                print(f"conformance roster: wrote {pending_path.name} ({len(kept)} pending)")
        elif not state_path.exists() or state_path.read_text(encoding="utf-8") != rendered:
            regen = ("(regenerate manifest-state: run python3 scripts/conformance_roster_check.py --write && "
                     "python3 scripts/gen_law_status.py, and commit both)")
            if a.state_drift_note is None:
                findings.append(f"{state_path.name} differs from the manifests at main {regen}")
                print(f"found: {findings[-1]}")
            else:
                try:
                    committed = state_sections(state_path.read_text(encoding="utf-8") if state_path.exists() else "")
                    base = state_sections(Path(a.state_drift_note).read_text(encoding="utf-8"))
                except (OSError, InputError) as e:
                    print(f"error: the base state {a.state_drift_note}: {e}", file=sys.stderr)
                    return 2
                want = state_sections(rendered)
                for repo in sorted(set(committed) | set(base) | set(want)):
                    c, b, w = committed.get(repo), base.get(repo), want.get(repo)
                    if c == w:
                        continue
                    if c == b:
                        print(f'note: {state_path.name}: [manifest."{repo}"] differs from the manifests at main '
                              f"(a crate landed after the base; the daily run fails on it) {regen}")
                    else:
                        findings.append(f'{state_path.name}: [manifest."{repo}"] was changed by this change and '
                                        f"differs from the manifests at main {regen}")
                        print(f"found: {findings[-1]}")
    print(f"conformance roster: {files} manifests read for {read} crates (crates that share a repository share "
          f"its manifest), {compared} gates compared, {len(findings)} findings")
    return 1 if findings or adoption else 0


if __name__ == "__main__":
    sys.exit(main())
