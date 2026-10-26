#!/usr/bin/env python3
"""Check that a repository's licence files are the licences its manifest declares.

    licence_files_check.py --repo DIR [--ref REF] [--agpl-report-only]

The declaration is the `license` of [package] (else [workspace.package]) in Cargo.toml at REF (default HEAD),
an SPDX expression; a repository with no such declaration (a Node shell) uses the `license` of package.json at the
root or one directory down (frontend/package.json), and package.json files that disagree are a finding. When
Cargo.toml declares a licence, each of those package.json files must declare the same one (a finding names both),
unless the package is a separate distribution in PACKAGE_JSON_EXEMPT (by repository and path, with the reason; an
entry the repository does not use is a finding).
For each licence in it:
  MIT               a root file LICENSE-MIT or LICENSE holding the MIT licence: the standard text word for word
                    from "Permission is hereby granted" to the end, before it only an optional "MIT License"
                    title and copyright lines (a text with clauses added is not the MIT licence)
  Apache-2.0        a root file LICENSE-APACHE or LICENSE holding the full Apache License 2.0 text
  AGPL-3.0*         a root file LICENSE-AGPL or LICENSE holding the full GNU AGPL v3 text
  LicenseRef-<x>    a root file named LICENSE-<x>, LICENSE-<x>.md or LICENSE-COMMERCIAL(.md)
A full text is recognised by the SHA-256 of its words (whitespace collapsed), so line wrapping does not matter:
the text published at apache.org / gnu.org, nothing shorter (a notice that points at the licence is not it).
TRADEMARK_NOTICE, when present, must not name a licence file the repository does not have
("(see <files> and the `license` field …)"; "the LICENSE-* files" needs at least one LICENSE-* file).

--agpl-report-only prints the AGPL findings but does not fail on them.

The only rights holder is the author. A rights statement (a tracked file named LICENSE*, NOTICE* or TRADEMARK_NOTICE*,
outside vendor/, third_party/, node_modules/ and target/) must not name the company, and the `authors` of every
Cargo.toml ([package] and [workspace.package]) and pyproject.toml ([project] and [tool.poetry]) must be exactly
"Moroya Sakamoto", without an e-mail (a manifest without `authors` has nothing to compare). The README is not a
rights statement, nor is a line that is exactly "Enquiries: https://extoria.co.jp/". Occurrences known today are listed in --holder-baseline (default conformance/holder-baseline.toml
of this repository), each repository by the SHA-256 of its name: an occurrence not listed is a finding, a listed one
that is gone is reported as stale (a finding with --stale-fails). --base-holder-baseline FILE (or --holder-base-ref
REF, the baseline at that commit) refuses entries the base does not have: the list only goes down. holder_live.py
applies the rule to every public repository of the account.
Exit 0 when every declared licence has its file, 1 on a finding, 2 when nothing was compared (no Cargo.toml or
package.json declaration) or git failed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# SHA-256 of the words of the official texts (apache.org LICENSE-2.0.txt, gnu.org agpl-3.0.txt). The comparison is of
# words only: a copy with other line breaks or spacing is the same text. The AGPL copies in the ALICE repositories
# (git blob fe6b9036) are byte-different from gnu.org's file (git blob be3f7b28) and equal to it once whitespace is
# collapsed; the Apache copy in scripts/testdata is byte-identical to apache.org's.
APACHE_2_0 = "0ffddef9e48f8a09aed5caf2d44f7ba1c1be2d9b8e0a6f693b1635b2d5566645"
AGPL_3_0 = "2cd0fb7883a3b3553dedbb0bad171646d46f51e2642951aae7c59cb5cec86c46"
# SHA-256 of the words of the MIT text from "Permission is hereby granted" to its end (spdx.org/licenses/MIT)
MIT_BODY = "fe2a9817987f862eaced948f0468c7f51d2fedfc48c5c505b246a49a3870e9a5"
MIT_START = "Permission is hereby granted"
# what may stand before the grant: an optional title and copyright lines (a holder name may hold dots and commas)
MIT_HEAD = re.compile(r"(?:MIT License )?(?:Copyright (?:\(c\)|©) [^.!?]{1,120}?(?:\. [^.!?]{0,40}?)? ?)*")


# package.json files that are separate distributions with their own licence by nature, by (repository, path): why
# each is not the repository's licence (a package that can declare the repository's licence is fixed, not listed). An entry the repository no longer needs (the file is gone or now agrees) is a finding.
# (repository, path) -> reason; none listed today (ALICE-SDF's VRChat package declares the crate's licence)
PACKAGE_JSON_EXEMPT: dict[tuple[str, str], str] = {}


# the rights holder rule
AUTHOR = "Moroya Sakamoto"
HOLDER_RE = re.compile(r"(?i)extoria|エクストーリア")
# the contact line of the canonical notices (a whole line, exactly): the site, not a rights holder
ENQUIRY_RE = re.compile(r"^Enquiries: https://extoria\.co\.jp/[ \t]*$", re.M)
RIGHTS_FILE_RE = re.compile(r"^(?:LICEN[CS]E|NOTICE|TRADEMARK_NOTICE)(?:[-._][^/]*)?$")  # by convention in capitals
NOT_READ = ("vendor/", "third_party/", "node_modules/", "target/")
KINDS = ("holder", "authors")
HOLDER_BASELINE = Path(__file__).resolve().parent.parent / "conformance" / "holder-baseline.toml"


def repo_key(name: str) -> str:
    """a repository in the baseline: sha256 of its name"""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def read_paths(path: str) -> bool:
    return not any(path.startswith(d) or f"/{d}" in path for d in NOT_READ)


def authors_ok(path: str, text: str) -> bool:
    """every listed author is exactly AUTHOR, with no e-mail; no `authors` key compares nothing"""
    doc = tomllib.loads(text)
    found: list[object] = []
    if path.rsplit("/", 1)[-1] == "Cargo.toml":
        for table in (doc.get("package") or {}, (doc.get("workspace") or {}).get("package") or {}):
            a = table.get("authors")
            if isinstance(a, list):
                found += a
    else:
        for a in ((doc.get("project") or {}).get("authors"), ((doc.get("tool") or {}).get("poetry") or {}).get("authors")):
            if isinstance(a, list):
                found += a
    return all(x == AUTHOR or (isinstance(x, dict) and x == {"name": AUTHOR}) for x in found)


def holder_occurrences(paths: list[str], read) -> set[tuple[str, str]]:
    """(kind, path) of each rights statement naming another holder and each manifest with other authors;
    read(path) returns the file's bytes"""
    occ: set[tuple[str, str]] = set()
    for path in paths:
        if not read_paths(path):
            continue
        base = path.rsplit("/", 1)[-1]
        if RIGHTS_FILE_RE.match(base):
            if HOLDER_RE.search(ENQUIRY_RE.sub("", read(path).decode("utf-8", "replace"))):
                occ.add(("holder", path))
        elif base in ("Cargo.toml", "pyproject.toml"):
            if not authors_ok(path, read(path).decode("utf-8")):
                occ.add(("authors", path))
    return occ


def load_baseline(text: str) -> dict[str, set[tuple[str, str]]]:
    out: dict[str, set[tuple[str, str]]] = {}
    for e in tomllib.loads(text).get("entry", []):
        repo, kind, path = e.get("repo"), e.get("kind"), e.get("path")
        if not (isinstance(repo, str) and re.fullmatch(r"[0-9a-f]{64}", repo)):
            raise ValueError(f"baseline entry {e}: repo is not a sha256")
        if kind not in KINDS or not (isinstance(path, str) and path):
            raise ValueError(f"baseline entry {e}: kind must be one of {KINDS} and path a non-empty string")
        if set(e) != {"repo", "kind", "path"}:
            raise ValueError(f"baseline entry {e}: only repo, kind and path")
        out.setdefault(repo, set()).add((kind, path))
    return out


def dump_baseline(b: dict[str, set[tuple[str, str]]]) -> str:
    lines = ["# Known rights-holder occurrences (see scripts/licence_files_check.py): each repository by the SHA-256",
             "# of its name. The list only goes down; a fixed occurrence is removed in the same change that fixes it.", ""]
    for repo in sorted(b):
        for kind, path in sorted(b[repo]):
            lines += ["[[entry]]", f'repo = "{repo}"', f'kind = "{kind}"', f"path = {json.dumps(path, ensure_ascii=False)}", ""]
    return "\n".join(lines)


def compare_baseline(name: str, occ: set[tuple[str, str]],
                     b: dict[str, set[tuple[str, str]]]) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(new, stale) for the repository called name"""
    listed = b.get(repo_key(name), set())
    return occ - listed, listed - occ


def baseline_growth(old: dict[str, set[tuple[str, str]]],
                    new: dict[str, set[tuple[str, str]]]) -> list[tuple[str, str, str]]:
    return sorted((r, k, p) for r, entries in new.items() for k, p in entries - old.get(r, set()))


def repo_name_of(repo: str) -> str:
    """the repository's name: the last part of the origin URL, else the checkout's directory name"""
    r = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"], capture_output=True, text=True, encoding="utf-8")
    url = r.stdout.strip() if r.returncode == 0 else git(repo, "rev-parse", "--show-toplevel").strip()
    return re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1])


def words(text: str) -> str:
    return " ".join(text.split())


def digest(text: str) -> str:
    return hashlib.sha256(words(text).encode("utf-8")).hexdigest()


def is_mit(text: str) -> bool:
    """the MIT licence: the standard body word for word, before it only a title and copyright lines"""
    w = words(text)
    i = w.find(MIT_START)
    if i < 0:
        return False
    body_ok = hashlib.sha256(w[i:].encode("utf-8")).hexdigest() == MIT_BODY
    return body_ok and MIT_HEAD.fullmatch(w[:i]) is not None


def git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True, encoding="utf-8").stdout


def spdx_ids(expr: str) -> list[str]:
    return [t for t in re.split(r"[\s()]+", expr) if t and t not in ("OR", "AND", "WITH")]


def check(repo: str, ref: str, agpl_report_only: bool = False,
          repo_name: str | None = None) -> tuple[int, list[str], list[str]]:
    """(compared, findings, reports): one finding per declared licence without its file"""
    root = [p for p in git(repo, "ls-tree", "--name-only", ref).split("\n") if p]
    show = lambda p: git(repo, "show", f"{ref}:{p}")  # noqa: E731
    findings: list[str] = []
    reports: list[str] = []
    compared = 0
    expr = None
    if "Cargo.toml" in root:
        manifest = tomllib.loads(show("Cargo.toml"))
        expr = (manifest.get("package") or {}).get("license") or ((manifest.get("workspace") or {}).get("package") or {}).get("license")
    # the `license` of package.json at the root or one directory down (a Node shell's frontend/package.json)
    tracked = [p for p in git(repo, "ls-tree", "-r", "--name-only", ref).split("\n")
               if p == "package.json" or re.fullmatch(r"[^/]+/package\.json", p)]
    declared: dict[str, str] = {}
    for p in tracked:
        doc = json.loads(show(p))
        if not isinstance(doc, dict):
            raise ValueError(f"{p} is not a JSON object")
        lic = doc.get("license")  # npm's old `{"type": ...}` form is not a declaration
        if isinstance(lic, str) and lic.strip():
            declared[p] = words(lic)
    name = repo_name or repo_name_of(repo)
    if isinstance(expr, str) and expr.strip():
        # the Cargo declaration is the repository's: a package.json that declares another licence is a finding,
        # unless that package is a separate distribution listed in PACKAGE_JSON_EXEMPT
        for p, lic in declared.items():
            compared += 1
            if lic != words(expr) and (name, p) not in PACKAGE_JSON_EXEMPT:
                findings.append(f"{p} declares {lic} and Cargo.toml declares {words(expr)}")
        for (r, p), why in PACKAGE_JSON_EXEMPT.items():
            if r == name and (p not in declared or declared[p] == words(expr)):
                findings.append(f"the exemption for {p} ({why}) is not used: remove it")
    else:
        if not declared:
            return 0, [], []
        expr = next(iter(declared.values()))
        if len(set(declared.values())) > 1:
            compared += 1
            findings.append("the package.json files disagree on the licence: "
                            + ", ".join(f"{p} {v}" for p, v in declared.items()))
    files = {p: show(p) for p in root if re.match(r"(?i)^licen[cs]e", p)}
    for lid in dict.fromkeys(spdx_ids(expr)):
        compared += 1
        if lid == "MIT":
            ok = [p for p in ("LICENSE-MIT", "LICENSE") if p in files and is_mit(files[p])]
            if not ok:
                findings.append("MIT is declared but no LICENSE-MIT (or LICENSE) holds the MIT licence")
        elif lid == "Apache-2.0":
            if not any(p in files and digest(files[p]) == APACHE_2_0 for p in ("LICENSE-APACHE", "LICENSE")):
                short = [p for p in ("LICENSE-APACHE", "LICENSE") if p in files and "Apache License" in files[p]]
                findings.append("Apache-2.0 is declared but no LICENSE-APACHE (or LICENSE) holds the full Apache "
                                "License 2.0 text" + (f" ({', '.join(short)} is not the full text)" if short else ""))
        elif lid.startswith("AGPL-3.0"):
            if not any(p in files and digest(files[p]) == AGPL_3_0 for p in ("LICENSE-AGPL", "LICENSE")):
                near = [p for p in ("LICENSE-AGPL", "LICENSE") if p in files and "AFFERO" in files[p].upper()]
                msg = ("AGPL-3.0 is declared but no LICENSE-AGPL (or LICENSE) holds the full GNU AGPL v3 text"
                       + (f" ({', '.join(near)} is not the full text)" if near else ""))
                (reports if agpl_report_only else findings).append(msg)
        elif lid.startswith("LicenseRef-"):
            name = lid[len("LicenseRef-"):]
            want = {f"LICENSE-{name}", f"LICENSE-{name}.md", "LICENSE-COMMERCIAL", "LICENSE-COMMERCIAL.md"}
            if not want & set(files):
                findings.append(f"{lid} is declared but no LICENSE-{name}(.md) or LICENSE-COMMERCIAL(.md) is present")
        else:
            findings.append(f"{lid} is declared and this check has no rule for it")
    if "TRADEMARK_NOTICE" in root:
        compared += 1
        m = re.search(r"\(see\s+(.*?)\s+and the `license` field", show("TRADEMARK_NOTICE"), re.S)
        if m:
            named = words(m.group(1))
            if named == "the LICENSE-* files":
                if not any(p.upper().startswith("LICENSE-") for p in files):
                    findings.append("TRADEMARK_NOTICE points at the LICENSE-* files and there is none")
            else:
                missing = [n for n in re.split(r",\s*|\s+and\s+", named) if n and n not in root]
                if missing:
                    findings.append(f"TRADEMARK_NOTICE points at {', '.join(missing)}, which this repository does not have")
    return compared, findings, reports


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--agpl-report-only", action="store_true")
    ap.add_argument("--repo-name", help="the repository's name for PACKAGE_JSON_EXEMPT and the holder baseline "
                                        "(default: from origin)")
    ap.add_argument("--holder-baseline", default=str(HOLDER_BASELINE))
    ap.add_argument("--stale-fails", action="store_true", help="a listed occurrence that is gone is a finding")
    ap.add_argument("--base-holder-baseline", metavar="FILE",
                    help="the baseline at the merge base: an entry it does not have is a finding")
    ap.add_argument("--holder-base-ref", metavar="REF",
                    help="the same, read from conformance/holder-baseline.toml at REF in --repo (when REF has no "
                         "baseline, the list is being introduced and nothing is compared)")
    a = ap.parse_args(argv)
    try:
        compared, findings, reports = check(a.repo, a.ref, a.agpl_report_only, a.repo_name)
    except (subprocess.CalledProcessError, tomllib.TOMLDecodeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        baseline = load_baseline(Path(a.holder_baseline).read_text(encoding="utf-8"))
        tracked = [x for x in git(a.repo, "ls-tree", "-r", "-z", "--name-only", a.ref).split("\0") if x]
        occ = holder_occurrences(tracked, lambda x: subprocess.run(["git", "-C", a.repo, "show", f"{a.ref}:{x}"],
                                                                   capture_output=True, check=True).stdout)
        name = a.repo_name or repo_name_of(a.repo)
        base_text = Path(a.base_holder_baseline).read_text(encoding="utf-8") if a.base_holder_baseline else None
        if a.holder_base_ref is not None:
            ref = a.holder_base_ref.strip()
            ok = ref and subprocess.run(["git", "-C", a.repo, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}"],
                                        capture_output=True).returncode == 0
            if not ok:
                print(f"error: the holder base ref {a.holder_base_ref!r} is not a commit of {a.repo} "
                      "(nothing would be compared)", file=sys.stderr)
                return 2
            r = subprocess.run(["git", "-C", a.repo, "show", f"{ref}:conformance/holder-baseline.toml"],
                               capture_output=True, text=True, encoding="utf-8")
            if r.returncode == 0:
                base_text = r.stdout
            else:
                print(f"note: no holder baseline at {a.holder_base_ref} (it is being introduced)")
        grown = baseline_growth(load_baseline(base_text), baseline) if base_text is not None else []
    except (OSError, subprocess.CalledProcessError, tomllib.TOMLDecodeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    new, stale = compare_baseline(name, occ, baseline)
    if not tracked:
        print("error: no tracked file was read for the holder rule", file=sys.stderr)
        return 2
    for kind, path in sorted(new):
        findings.append(f"{path}: " + ("names another rights holder" if kind == "holder" else
                                       f'authors other than exactly "{AUTHOR}" (no e-mail)'))
    for kind, path in sorted(stale):
        (findings if a.stale_fails else reports).append(f"stale baseline entry: {kind} {path} (fixed: remove the entry)")
    for r, kind, path in grown:
        findings.append(f"the holder baseline grew: {kind} {path} for repository {r[:12]} (the list only goes down)")
    for r in reports:
        print(f"report (not failing): {r}")
    for f in findings:
        print(f"found: {f}")
    print(f"licence files: {len(findings)} findings, {len(reports)} reports in {compared} checks at {a.ref}")
    if compared == 0:
        print("error: nothing was compared (no licence declared in Cargo.toml or package.json)", file=sys.stderr)
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
