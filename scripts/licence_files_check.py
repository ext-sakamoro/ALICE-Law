#!/usr/bin/env python3
"""Check that a repository's licence files are the licences its Cargo.toml declares.

    licence_files_check.py --repo DIR [--ref REF] [--agpl-report-only]

The declaration is the `license` of [package] (else [workspace.package]) in Cargo.toml at REF (default HEAD),
an SPDX expression. For each licence in it:
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
Exit 0 when every declared licence has its file, 1 on a finding, 2 when nothing was compared (no Cargo.toml, no
declaration) or git failed.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tomllib

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
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def spdx_ids(expr: str) -> list[str]:
    return [t for t in re.split(r"[\s()]+", expr) if t and t not in ("OR", "AND", "WITH")]


def check(repo: str, ref: str, agpl_report_only: bool = False) -> tuple[int, list[str], list[str]]:
    """(compared, findings, reports): one finding per declared licence without its file"""
    root = [p for p in git(repo, "ls-tree", "--name-only", ref).split("\n") if p]
    show = lambda p: git(repo, "show", f"{ref}:{p}")  # noqa: E731
    if "Cargo.toml" not in root:
        return 0, [], []
    manifest = tomllib.loads(show("Cargo.toml"))
    expr = (manifest.get("package") or {}).get("license") or ((manifest.get("workspace") or {}).get("package") or {}).get("license")
    if not isinstance(expr, str) or not expr.strip():
        return 0, [], []
    files = {p: show(p) for p in root if re.match(r"(?i)^licen[cs]e", p)}
    findings: list[str] = []
    reports: list[str] = []
    compared = 0
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
    a = ap.parse_args(argv)
    try:
        compared, findings, reports = check(a.repo, a.ref, a.agpl_report_only)
    except (subprocess.CalledProcessError, tomllib.TOMLDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for r in reports:
        print(f"report (not failing): {r}")
    for f in findings:
        print(f"found: {f}")
    print(f"licence files: {len(findings)} findings, {len(reports)} reports in {compared} checks at {a.ref}")
    if compared == 0:
        print("error: nothing was compared (no Cargo.toml or no licence declared)", file=sys.stderr)
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
