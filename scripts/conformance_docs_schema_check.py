#!/usr/bin/env python3
"""Check docs/CONFORMANCE.md's gate headings against conformance/gates.toml.

Nothing ties the human-readable `### G<n> — <name>` heading for a gate to
the `[[gate]] id = "G<n>", name = "<name>"` entry a generator reads for the
same gate: a renamed gate, a gate added to one file and not the other, or a
heading that drifts from the schema's own spelling (this happened: G14's
heading read "float-environment invariants", gates.toml's name
"float-environment-invariants") would not be caught by anything else.

    conformance_docs_schema_check.py [--docs FILE] [--schema FILE]

Exit 0 when the two files name exactly the same set of (id, name) pairs,
1 when they differ (the difference named), 2 when either file has zero gate
headings/entries (compared nothing) or either file cannot be read.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

HEADING_RE = re.compile(r"^###\s+(G\d+)\s+—\s+(\S+)\s*$", re.MULTILINE)


class ParseError(ValueError):
    pass


def gates_from_docs(text: str) -> dict[str, str]:
    gates = {m.group(1): m.group(2) for m in HEADING_RE.finditer(text)}
    if not gates:
        raise ParseError("no `### G<n> — <name>` heading found (compared nothing)")
    return gates


def gates_from_schema(text: str) -> dict[str, str]:
    data = tomllib.loads(text)
    entries = data.get("gate", [])
    if not entries:
        raise ParseError("the schema lists no [[gate]] entry (compared nothing)")
    gates = {}
    for e in entries:
        if "id" not in e or "name" not in e:
            raise ParseError(f"a [[gate]] entry is missing `id` or `name`: {e!r}")
        gates[e["id"]] = e["name"]
    return gates


def check(docs_text: str, schema_text: str) -> list[str]:
    docs = gates_from_docs(docs_text)
    schema = gates_from_schema(schema_text)
    problems = []
    for gate_id in sorted(set(docs) | set(schema)):
        in_docs = gate_id in docs
        in_schema = gate_id in schema
        if in_docs and not in_schema:
            problems.append(f"{gate_id}: in docs/CONFORMANCE.md (as {docs[gate_id]!r}), not in the schema")
        elif in_schema and not in_docs:
            problems.append(f"{gate_id}: in the schema (as {schema[gate_id]!r}), not in docs/CONFORMANCE.md")
        elif docs[gate_id] != schema[gate_id]:
            problems.append(f"{gate_id}: docs says {docs[gate_id]!r}, schema says {schema[gate_id]!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--docs", default="docs/CONFORMANCE.md")
    p.add_argument("--schema", default="conformance/gates.toml")
    a = p.parse_args(argv)

    try:
        docs_text = Path(a.docs).read_text(encoding="utf-8")
        schema_text = Path(a.schema).read_text(encoding="utf-8")
    except OSError as e:
        print(f"conformance-docs-schema: cannot read {e.filename}: {e.strerror}")
        return 2

    try:
        problems = check(docs_text, schema_text)
    except ParseError as e:
        print(f"conformance-docs-schema: {e}")
        return 2

    if problems:
        print(f"conformance-docs-schema: {len(problems)} mismatch(es) between {a.docs} and {a.schema}:")
        for p_ in problems:
            print(f"  {p_}")
        return 1
    gates = gates_from_docs(docs_text)
    print(f"conformance-docs-schema: ok, {len(gates)} gates match in id and name")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
