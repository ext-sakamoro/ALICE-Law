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


class Landing(unittest.TestCase):
    """--landing: a fall prints the row to lower and passes; a rise against the row or against main, unparsed code,
    no row and an unreadable main stay red"""

    def land(self, have: dict, row_: dict, main: dict | None, lowered: list[str] | None = None) -> list[str]:
        return npr.compare({"A": have}, {"A": row_}, landing=True, lowered=lowered,
                           base=None if main is None else {"A": main})

    def test_a_fall_at_a_landing_names_the_row_and_is_no_finding(self):
        lowered: list[str] = []
        self.assertEqual(self.land(scan(panic=1, unwrap=3), row(panic=2, unwrap=3), scan(panic=2, unwrap=3),
                                   lowered), [])
        self.assertEqual(lowered, ["A: panic 1 < baseline 2: lower the row [repo.A] panic to 1"])

    def test_a_site_added_back_under_a_stale_row_is_red_against_main(self):
        # day 0: 10 -> 9 lands against a row of 10 (passes, the row waits to be lowered)
        self.assertEqual(self.land(scan(unwrap=9), row(unwrap=10), scan(unwrap=10)), [])
        # a second landing before the row is lowered: 9 -> 10 is <= the row, but above main
        f = self.land(scan(unwrap=10), row(unwrap=10), scan(unwrap=9))
        self.assertEqual(len(f), 1)
        self.assertIn("unwrap 10 > main 9 (a new site against main)", f[0])

    def test_growth_against_the_row_is_red_even_when_main_is_higher(self):
        f = self.land(scan(expect=5), row(expect=4), scan(expect=6))
        self.assertEqual(len(f), 1)
        self.assertIn("expect 5 > baseline 4", f[0])

    def test_a_missing_or_unreadable_main_is_red(self):
        self.assertIn("no read of the main", self.land(scan(), row(), None)[0])
        self.assertIn("main: 1 file(s) do not parse",
                      self.land(scan(), row(), scan(unparsed=1, unparsed_files=["x.rs"]))[0])
        self.assertIn("main: no production Rust code read", self.land(scan(), row(), scan(files=0))[0])

    def test_a_rise_at_a_landing_stays_red_beside_a_fall(self):
        lowered: list[str] = []
        f = self.land(scan(panic=1, expect=5), row(panic=2, expect=4), scan(panic=2, expect=5), lowered)
        self.assertEqual(len(f), 1)
        self.assertIn("expect 5 > baseline 4", f[0])
        self.assertEqual(len(lowered), 1)

    def test_unparsed_no_row_and_dropped_code_stay_red_at_a_landing(self):
        self.assertIn("do not parse", self.land(scan(unparsed=1, unparsed_files=["x.rs"]), row(), scan())[0])
        self.assertIn("no row", npr.compare({"B": scan()}, {}, landing=True, base={"B": scan()})[0])
        self.assertIn("drop the row", npr.compare({"C": scan(files=0)}, {"C": row()}, landing=True,
                                                  base={"C": scan()})[0])

    def test_without_landing_a_fall_stays_red(self):
        self.assertIn("lower the baseline", npr.compare({"A": scan(panic=1)}, {"A": row(panic=2)}, landing=False)[0])

    def test_landing_needs_only_and_base_needs_landing(self):
        self.assertEqual(npr.main(["--landing"]), 2)
        self.assertEqual(npr.main(["--only", "A=.", "--landing"]), 2)
        self.assertEqual(npr.main(["--only", "A=.", "--base", "."]), 2)


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

    def test_a_landing_through_main(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            def tree(name: str, n: int) -> Path:
                root = Path(d) / name
                (root / "src").mkdir(parents=True, exist_ok=True)
                body = " + ".join(["x.unwrap()"] * n) if n else "0"
                (root / "src" / "lib.rs").write_text(f"pub fn f(x: Option<u8>) -> u8 {{ {body} }}\n",
                                                     encoding="utf-8")
                return root
            own = Path(d) / "own.toml"
            main2, land1 = tree("main2", 2), tree("land1", 1)
            self.assertEqual(npr.main(["--write-own", f"ALICE-Own={main2}", "--baseline", str(own)]), 0)
            args = ["--baseline", str(own), "--landing"]
            self.assertEqual(npr.main(["--only", f"ALICE-Own={land1}", "--baseline", str(own)]), 1)  # live: red
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(npr.main(["--only", f"ALICE-Own={land1}", "--base", str(main2), *args]), 0)
            self.assertIn("landing: ALICE-Own: unwrap 1 < baseline 2: lower the row [repo.ALICE-Own] unwrap to 1 "
                          "in own.toml", out.getvalue())
            # main is now at 1 and the row still says 2: a landing back to 2 is red against main
            main1, land2 = tree("main1", 1), tree("land2", 2)
            self.assertEqual(npr.main(["--only", f"ALICE-Own={land2}", "--base", str(main1), *args]), 1)
            # growth against the row, unparsed code, a missing main
            self.assertEqual(npr.main(["--only", f"ALICE-Own={tree('land3', 3)}", "--base", str(tree('main3', 3)),
                                       *args]), 1)
            bad = tree("bad", 1)
            (bad / "src" / "lib.rs").write_text("pub fn f( {\n", encoding="utf-8")
            self.assertEqual(npr.main(["--only", f"ALICE-Own={bad}", "--base", str(main2), *args]), 1)
            self.assertEqual(npr.main(["--only", f"ALICE-Own={land1}", "--base", str(bad), *args]), 1)
            self.assertEqual(npr.main(["--only", f"ALICE-Own={land1}", *args]), 2)   # --landing needs --base
            self.assertEqual(npr.main(["--only", f"ALICE-Own={land1}", "--base", str(Path(d) / "absent"), *args]), 2)
            empty = Path(d) / "empty"
            empty.mkdir()
            self.assertEqual(npr.main(["--only", f"ALICE-Own={empty}", "--base", str(main2), *args]), 2)

    def test_write_own_refuses_the_shared_baseline(self):
        self.assertEqual(npr.main(["--write-own", "ALICE-Own=."]), 2)


class ZipLanding(unittest.TestCase):
    """the landing that went unseen: ALICE-Zip 71e89ed moved a license key from one date library to another and added
    four `.expect()` calls in libalice-enterprise/src/license/key.rs; main held 14 and the row said 14, the landing held
    18. The push-path check (--only --landing --base main) must refuse it before it lands"""

    @classmethod
    def setUpClass(cls):
        npr.scanner()

    @staticmethod
    def key_rs(n: int) -> str:
        calls = "\n".join(f'    let v{i} = parse(s, {i}).expect("field {i} of a license key");' for i in range(n))
        return ("fn parse(s: &str, i: usize) -> Option<u32> { s.split('-').nth(i)?.parse().ok() }\n"
                f"pub fn decode(s: &str) -> u32 {{\n{calls}\n    0\n}}\n")

    def tree(self, d: Path, name: str, n: int) -> Path:
        root = d / name / "libalice-enterprise" / "src" / "license"
        root.mkdir(parents=True)
        (root / "key.rs").write_text(self.key_rs(n), encoding="utf-8")
        return d / name

    def test_four_added_expects_are_red_at_the_landing(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            main14, land18 = self.tree(d, "main", 14), self.tree(d, "landing", 18)
            base = d / "baseline.toml"
            base.write_text(npr.render({"ALICE-Zip": row(expect=14)}), encoding="utf-8")
            args = ["--baseline", str(base), "--landing", "--base", str(main14)]
            self.assertEqual(npr.main(["--only", f"ALICE-Zip={main14}", *args]), 0)
            import contextlib
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(npr.main(["--only", f"ALICE-Zip={land18}", *args]), 1)
            self.assertIn("ALICE-Zip: expect 18", out.getvalue())
            # one site more is enough
            self.assertEqual(npr.main(["--only", f"ALICE-Zip={self.tree(d, 'plus1', 15)}", *args]), 1)

    def test_a_main_with_no_production_file_compares_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            land = self.tree(d, "landing", 14)
            empty = d / "empty-main"
            empty.mkdir()
            base = d / "baseline.toml"
            base.write_text(npr.render({"ALICE-Zip": row(expect=14)}), encoding="utf-8")
            self.assertNotEqual(npr.main(["--only", f"ALICE-Zip={land}", "--baseline", str(base), "--landing",
                                          "--base", str(empty)]), 0)


class ScannerCache(unittest.TestCase):
    """--scanner-cache: built once per source revision, verified before each use, rebuilt on any mismatch"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "src"
        (self.src / "src").mkdir(parents=True)
        (self.src / "Cargo.toml").write_text("[package]\nname = \"s\"\n", encoding="utf-8")
        (self.src / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        self.cache = Path(self.tmp.name) / "cache"
        self.builds = 0

    def build(self, target: Path) -> Path:
        self.builds += 1
        out = target / "bin"
        out.write_bytes(b"binary built from " + npr.scanner_source_key(self.src).encode())
        return out

    def get(self) -> Path:
        return npr.cached_scanner(self.cache, self.src, self.build)

    def test_a_valid_entry_is_reused(self):
        a = self.get()
        b = self.get()
        self.assertEqual(a, b)
        self.assertEqual(self.builds, 1)
        self.assertEqual(a.parent.name, npr.scanner_source_key(self.src))

    def test_a_changed_source_is_a_new_key_and_a_new_build(self):
        a = self.get()
        (self.src / "src" / "main.rs").write_text("fn main() { let _ = 1; }\n", encoding="utf-8")
        b = self.get()
        self.assertNotEqual(a.parent, b.parent)
        self.assertEqual(self.builds, 2)

    def test_a_tampered_or_missing_binary_or_key_record_rebuilds(self):
        a = self.get()
        a.write_bytes(b"something else")
        self.get()
        self.assertEqual(self.builds, 2)
        a.unlink()
        self.get()
        self.assertEqual(self.builds, 3)
        (a.parent / "source.sha256").write_text("0" * 64 + "\n", encoding="utf-8")
        self.get()
        self.assertEqual(self.builds, 4)
        (a.parent / "binary.sha256").unlink()
        self.get()
        self.assertEqual(self.builds, 5)

    def test_prepare_needs_a_cache(self):
        self.assertEqual(npr.main(["--prepare-scanner"]), 2)

    def test_the_key_ignores_a_target_directory_and_needs_a_manifest(self):
        k = npr.scanner_source_key(self.src)
        (self.src / "target" / "release").mkdir(parents=True)
        (self.src / "target" / "release" / "x").write_bytes(b"x")
        self.assertEqual(npr.scanner_source_key(self.src), k)
        (self.src / "Cargo.toml").unlink()
        with self.assertRaises(npr.hl.Unreadable):
            npr.scanner_source_key(self.src)


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
