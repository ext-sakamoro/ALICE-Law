#!/usr/bin/env python3
"""Tests of scripts/conformance_docs_schema_check.py
(python3 scripts/test_conformance_docs_schema_check.py)"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cdsc", HERE / "conformance_docs_schema_check.py")
cdsc = importlib.util.module_from_spec(spec)
sys.modules["cdsc"] = cdsc
spec.loader.exec_module(cdsc)


def docs(*entries: tuple[str, str]) -> str:
    return "# Conformance\n\n## Gates\n\n" + "".join(f"### {gid} — {name}\n\nbody\n\n" for gid, name in entries)


def schema(*entries: tuple[str, str]) -> str:
    return "".join(f'[[gate]]\nid = "{gid}"\nname = "{name}"\ndefinition = "x"\n\n' for gid, name in entries)


class GatesFromDocs(unittest.TestCase):
    def test_parses_every_heading(self):
        self.assertEqual(cdsc.gates_from_docs(docs(("G1", "a"), ("G2", "b"))), {"G1": "a", "G2": "b"})

    def test_no_heading_raises(self):
        with self.assertRaises(cdsc.ParseError):
            cdsc.gates_from_docs("# Conformance\n\nno gate headings here\n")


class GatesFromSchema(unittest.TestCase):
    def test_parses_every_entry(self):
        self.assertEqual(cdsc.gates_from_schema(schema(("G1", "a"), ("G2", "b"))), {"G1": "a", "G2": "b"})

    def test_no_entry_raises(self):
        with self.assertRaises(cdsc.ParseError):
            cdsc.gates_from_schema("schema_version = 0\n")

    def test_entry_missing_name_raises(self):
        with self.assertRaises(cdsc.ParseError):
            cdsc.gates_from_schema('[[gate]]\nid = "G1"\ndefinition = "x"\n')


class Check(unittest.TestCase):
    def test_identical_sets_pass(self):
        d = docs(("G1", "a"), ("G2", "b"))
        s = schema(("G1", "a"), ("G2", "b"))
        self.assertEqual(cdsc.check(d, s), [])

    def test_a_gate_only_in_docs_is_named(self):
        d = docs(("G1", "a"), ("G2", "b"))
        s = schema(("G1", "a"))
        problems = cdsc.check(d, s)
        self.assertEqual(len(problems), 1)
        self.assertIn("G2", problems[0])
        self.assertIn("not in the schema", problems[0])

    def test_a_gate_only_in_schema_is_named(self):
        d = docs(("G1", "a"))
        s = schema(("G1", "a"), ("G2", "b"))
        problems = cdsc.check(d, s)
        self.assertEqual(len(problems), 1)
        self.assertIn("G2", problems[0])
        self.assertIn("not in docs", problems[0])

    def test_a_renamed_gate_is_named(self):
        d = docs(("G1", "old-name"))
        s = schema(("G1", "new-name"))
        problems = cdsc.check(d, s)
        self.assertEqual(len(problems), 1)
        self.assertIn("G1", problems[0])
        self.assertIn("old-name", problems[0])
        self.assertIn("new-name", problems[0])


class Main(unittest.TestCase):
    def write(self, tmp: Path, docs_text: str, schema_text: str) -> tuple[Path, Path]:
        d = tmp / "CONFORMANCE.md"
        s = tmp / "gates.toml"
        d.write_text(docs_text, encoding="utf-8")
        s.write_text(schema_text, encoding="utf-8")
        return d, s

    def test_exit_0_when_matching(self):
        with tempfile.TemporaryDirectory() as t:
            d, s = self.write(Path(t), docs(("G1", "a")), schema(("G1", "a")))
            self.assertEqual(cdsc.main(["--docs", str(d), "--schema", str(s)]), 0)

    def test_exit_1_when_mismatched(self):
        with tempfile.TemporaryDirectory() as t:
            d, s = self.write(Path(t), docs(("G1", "a")), schema(("G1", "b")))
            self.assertEqual(cdsc.main(["--docs", str(d), "--schema", str(s)]), 1)

    def test_exit_2_when_docs_file_missing(self):
        with tempfile.TemporaryDirectory() as t:
            s = Path(t) / "gates.toml"
            s.write_text(schema(("G1", "a")), encoding="utf-8")
            self.assertEqual(cdsc.main(["--docs", str(Path(t) / "nope.md"), "--schema", str(s)]), 2)

    def test_exit_2_when_schema_file_missing(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "CONFORMANCE.md"
            d.write_text(docs(("G1", "a")), encoding="utf-8")
            self.assertEqual(cdsc.main(["--docs", str(d), "--schema", str(Path(t) / "nope.toml")]), 2)

    def test_exit_2_when_docs_has_no_headings(self):
        with tempfile.TemporaryDirectory() as t:
            d, s = self.write(Path(t), "# Conformance\n\nno headings\n", schema(("G1", "a")))
            self.assertEqual(cdsc.main(["--docs", str(d), "--schema", str(s)]), 2)


class RealFiles(unittest.TestCase):
    def test_this_repositorys_docs_and_schema_match_at_15_each(self):
        root = HERE.parent
        self.assertEqual(cdsc.gates_from_docs((root / "docs" / "CONFORMANCE.md").read_text(encoding="utf-8")),
                         cdsc.gates_from_schema((root / "conformance" / "gates.toml").read_text(encoding="utf-8")))
        docs_gates = cdsc.gates_from_docs((root / "docs" / "CONFORMANCE.md").read_text(encoding="utf-8"))
        self.assertEqual(len(docs_gates), 15)
        self.assertEqual(cdsc.main(["--docs", str(root / "docs" / "CONFORMANCE.md"),
                                     "--schema", str(root / "conformance" / "gates.toml")]), 0)



class G1MethodList(unittest.TestCase):
    """the one list of f32 / f64 methods G1 rules on (conformance/gates.toml, gate G1 `methods`)"""

    def test_the_list_is_well_formed(self):
        import tomllib
        gates = tomllib.loads((Path(__file__).resolve().parent.parent / "conformance" / "gates.toml")
                              .read_text(encoding="utf-8"))["gate"]
        g1 = next(g for g in gates if g["id"] == "G1")
        methods = g1["methods"]
        self.assertGreater(len(methods), 0)
        names = [m["name"] for m in methods]
        self.assertEqual(len(names), len(set(names)), "a method is listed twice")
        for m in methods:
            self.assertIn(m["verdict"], ("banned", "allowed", "pending"), m)
            self.assertTrue(m["reason"].strip(), m)
            # a pending method says what a crate does until it is decided; a decided one has no default
            if m["verdict"] == "pending":
                self.assertIn(m.get("default"), ("banned", "allowed"), m)
            else:
                self.assertNotIn("default", m, m)
        verdict = {m["name"]: m["verdict"] for m in methods}
        self.assertEqual(verdict["sqrt"], "allowed")
        self.assertEqual(verdict["mul_add"], "allowed")
        self.assertEqual(verdict["powi"], "pending")  # until the cross-target measurement decides it
        self.assertEqual(next(m for m in methods if m["name"] == "powi")["default"], "banned")
        for banned in ("sin", "cos", "exp", "ln", "powf", "hypot", "cbrt", "atan2"):
            self.assertEqual(verdict[banned], "banned", banned)

if __name__ == "__main__":
    unittest.main()
