#!/usr/bin/env python3
"""Tests for scripts/no_panic_ratchet.py and tools/no-panic-scan.

* the ratchet rules on scans given in memory (a rise, a fall, a missing row, an unparsed file, a repository with no
  production Rust code, 0 files);
* the baseline's round trip and validation (a hashed key is refused: a private repository keeps its own row in its
  own repository), the committed baseline holding public names only, and the own-file path (--write-own, --only
  with --baseline);
* a cross-check of the syntax-tree count against an independent lexical pass written here (comments and literals
  blanked, `cfg(test)` / `cfg(all(test, ..))` / `#[test]` items removed by brace matching) over a corpus of files on
  disk: the two must agree site for site on every file, and the corpus must hold at least one site of every lint.

Usage: python3 scripts/test_no_panic_ratchet.py (builds tools/no-panic-scan with cargo, or NO_PANIC_SCAN=<binary>)
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import no_panic_ratchet as npr  # noqa: E402

LINTS = npr.LINTS


def scan(**counts) -> dict:
    s = {"files": 3, "unparsed": 0, "unparsed_files": [], **{lint: 0 for lint in LINTS}}
    s.update(counts)
    return s


def row(**counts) -> dict:
    return {lint: counts.get(lint, 0) for lint in LINTS}


class Rules(unittest.TestCase):
    def test_equal_counts_pass(self):
        self.assertEqual(npr.compare({"A": scan(unwrap=2)}, {"A": row(unwrap=2)}), [])

    def test_a_rise_is_red(self):
        f = npr.compare({"A": scan(expect=5)}, {"A": row(expect=4)})
        self.assertEqual(len(f), 1)
        self.assertIn("expect 5 > baseline 4", f[0])

    def test_a_fall_is_red_until_the_baseline_is_lowered(self):
        f = npr.compare({"A": scan(panic=1)}, {"A": row(panic=2)})
        self.assertEqual(len(f), 1)
        self.assertIn("lower the baseline", f[0])

    def test_a_repository_without_a_row_is_red(self):
        f = npr.compare({"B": scan()}, {})
        self.assertIn("no row", f[0])

    def test_unparsed_code_is_red(self):
        f = npr.compare({"A": scan(unparsed=1, unparsed_files=["src/x.rs"])}, {"A": row()})
        self.assertIn("do not parse", f[0])

    def test_no_production_code_is_not_compared_but_a_row_for_it_is_red(self):
        self.assertEqual(npr.compare({"C": scan(files=0)}, {}), [])
        self.assertIn("drop the row", npr.compare({"C": scan(files=0)}, {"C": row()})[0])

    def test_every_lint_is_compared(self):
        for lint in LINTS:
            with self.subTest(lint=lint):
                self.assertEqual(len(npr.compare({"A": scan(**{lint: 1})}, {"A": row()})), 1)


class Baseline(unittest.TestCase):
    def test_round_trip(self):
        rows = {"ALICE-A": row(unwrap=3), "ALICE-B": row(todo=1)}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.toml"
            p.write_text(npr.render(rows), encoding="utf-8")
            self.assertEqual(npr.load_baseline(p), rows)

    def test_bad_rows_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.toml"
            for body in ('[repo."A"]\nunwrap = -1\n', '[repo."A"]\nunwrap = 1\n',
                         '[repo."A"]\n' + "".join(f"{lint} = 0\n" for lint in LINTS) + "extra = 1\n",
                         f'[repo."sha256:{"0" * 64}"]\n' + "".join(f"{lint} = 0\n" for lint in LINTS)):
                p.write_text(body, encoding="utf-8")
                with self.subTest(body=body), self.assertRaises(ValueError):
                    npr.load_baseline(p)

    def test_the_committed_baseline_loads_holds_public_names_only_and_is_not_empty(self):
        rows = npr.load_baseline()
        self.assertGreater(len(rows), 0)
        text = npr.BASELINE.read_text(encoding="utf-8")
        self.assertNotIn("sha256", text, "no hashed name in the shared baseline")
        for k in rows:
            self.assertTrue(k.startswith("ALICE-"), k)


# ── an independent lexical pass, for the cross-check ────────────────────────────────────────────

TOKENS = {
    "unwrap": re.compile(r"\.\s*unwrap\s*\(\s*\)"),
    "expect": re.compile(r"\.\s*expect\s*\(\s*[^)\s]"),
    "panic": re.compile(r"(?<![A-Za-z0-9_])panic\s*!"),
    "todo": re.compile(r"(?<![A-Za-z0-9_])todo\s*!"),
    "unimplemented": re.compile(r"(?<![A-Za-z0-9_])unimplemented\s*!"),
    "unreachable": re.compile(r"(?<![A-Za-z0-9_])unreachable\s*!"),
    # assert!, assert_eq!, assert_ne!; the debug_ forms are not sites (the "_" before them fails the look-behind)
    "assert": re.compile(r"(?<![A-Za-z0-9_])assert(?:_eq|_ne)?\s*!"),
}
CLIPPY = {"unwrap_used": "unwrap", "expect_used": "expect", "panic": "panic", "todo": "todo",
          "unimplemented": "unimplemented", "unreachable": "unreachable"}


def blank(text: str) -> str:
    """comments to spaces, every string / char literal to the token `0` (newlines kept), so an argument that is a
    literal still reads as an argument"""
    lit = lambda seg: "0" + "\n" * seg.count("\n")  # noqa: E731
    out, i, n = [], 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i)); i = j; continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j])); i = j; continue
        m = re.match(r'r(#*)"', text[i:])
        if m and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            end = '"' + m.group(1)
            j = text.find(end, i + len(m.group(0)))
            j = n if j < 0 else j + len(end)
            out.append(lit(text[i:j])); i = j; continue
        if text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(lit(text[i:j + 1])); i = j + 1; continue
        m2 = re.match(r"'(\\.|[^\\'])'", text[i:])
        if m2:
            out.append("0"); i += len(m2.group(0)); continue
        out.append(text[i]); i += 1
    return "".join(out)


TEST_ATTR = re.compile(r"#\[\s*(cfg\s*\(\s*(test|all\s*\([^\]]*\btest\b[^\]]*\))\s*\)|test)\s*\]")


def strip_tests(text: str) -> str:
    spans = []
    for m in TEST_ATTR.finditer(text):
        if "any(" in m.group(0) or "not(" in m.group(0):
            continue
        j = m.end()
        while j < len(text) and text[j] not in "{;":
            j += 1
        if j >= len(text):
            continue
        if text[j] == ";":
            spans.append((m.start(), j + 1)); continue
        depth, k = 0, j
        while k < len(text):
            depth += {"{": 1, "}": -1}.get(text[k], 0)
            if depth == 0:
                break
            k += 1
        spans.append((m.start(), k + 1))
    out, last = [], 0
    for s, e in sorted(spans):
        if s < last:
            continue
        out.append(text[last:s]); last = e
    return "".join(out) + text[last:]


def lexical(text: str) -> dict[str, int]:
    raw = text
    code = strip_tests(blank(text))
    c = {lint: len(rx.findall(code)) for lint, rx in TOKENS.items()}
    # attributes naming the lints (inner and outer, outside the removed test items)
    for m in re.finditer(r"#!?\[\s*(allow|expect)\s*\(([^\]]*)\)\s*\]", strip_tests(raw)):
        for clippy, lint in CLIPPY.items():
            if re.search(r"clippy::" + clippy + r"(?![A-Za-z0-9_])", m.group(2)):
                c[lint] += 1
    return c


CORPUS = {
    "src/lib.rs": "#![allow(clippy::expect_used)]\nmod a;\n#[cfg(test)]\nmod tests;\n"
                  "pub fn f(x: Option<u8>) -> u8 { x.unwrap() }\n",
    "src/a.rs": "pub fn g(v: Result<u8, ()>) -> u8 {\n    // v.unwrap() in a comment\n    let s = \"panic!\";\n"
                "    match v { Ok(x) => x, Err(_) => unreachable!(\"never\") }\n}\n"
                "#[cfg(test)]\nmod t { fn h() { None::<u8>.unwrap(); todo!(); } }\n"
                "pub fn k() { assert!(Some(1).expect(\"one\") > 0); }\n"
                "#[allow(clippy::panic, clippy::module_name_repetitions)]\npub fn p() { panic!(\"p\") }\n"
                "pub fn q() -> u8 { todo!() }\npub fn r() { unimplemented!() }\n"
                "pub fn s(a: u8) { assert_eq!(a, 1); assert_ne!(a, 2); debug_assert!(a > 0); debug_assert_eq!(a, 1); }\n",
    "src/tests.rs": "fn t() { Some(1).unwrap(); }\n",
    "tests/it.rs": "fn t() { Some(1).unwrap(); }\n",
    "crates/m/src/main.rs": "fn main() { std::env::args().nth(1).expect(\"arg\"); }\n",
}


class OwnFile(unittest.TestCase):
    """a private repository's own row: written by --write-own, checked by --only with --baseline"""

    @classmethod
    def setUpClass(cls):
        npr.scanner()

    def test_write_own_then_only_against_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            (root / "src").mkdir(parents=True)
            (root / "src" / "lib.rs").write_text("pub fn f(x: Option<u8>) -> u8 { x.unwrap() }\n", encoding="utf-8")
            own = Path(d) / "own.toml"
            self.assertEqual(npr.main(["--write-own", f"ALICE-Own={root}", "--baseline", str(own)]), 0)
            self.assertEqual(npr.load_baseline(own), {"ALICE-Own": row(unwrap=1)})
            self.assertEqual(npr.main(["--only", f"ALICE-Own={root}", "--baseline", str(own)]), 0)
            # a new site is red against the own row
            (root / "src" / "lib.rs").write_text(
                "pub fn f(x: Option<u8>) -> u8 { x.unwrap() + x.unwrap() }\n", encoding="utf-8")
            self.assertEqual(npr.main(["--only", f"ALICE-Own={root}", "--baseline", str(own)]), 1)
            # a repository with no production code compares nothing: exit 2
            empty = Path(d) / "empty"
            empty.mkdir()
            self.assertEqual(npr.main(["--only", f"ALICE-Own={empty}", "--baseline", str(own)]), 2)

    def test_write_own_refuses_the_shared_baseline(self):
        self.assertEqual(npr.main(["--write-own", "ALICE-Own=."]), 2)


class CrossCheck(unittest.TestCase):
    """the syntax-tree scan against the lexical pass, file by file over the production files of the corpus"""

    @classmethod
    def setUpClass(cls):
        cls.bin = npr.scanner()

    def test_the_two_counts_agree_and_see_every_lint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel, text in CORPUS.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8")
            whole = npr.scan_dir(self.bin, root)
            # production files only, the test module file (src/tests.rs, loaded by #[cfg(test)] mod tests;)
            # and tests/ excluded
            expect = {lint: 0 for lint in LINTS}
            for rel in ("src/lib.rs", "src/a.rs", "crates/m/src/main.rs"):
                for lint, n in lexical(CORPUS[rel]).items():
                    expect[lint] += n
            self.assertEqual(whole["files"], 3)
            self.assertEqual(whole["unparsed"], 0)
            self.assertEqual({lint: whole[lint] for lint in LINTS}, expect)
            for lint in LINTS:
                self.assertGreater(expect[lint], 0, f"the corpus has no {lint} site: the check compares nothing")


if __name__ == "__main__":
    unittest.main()
