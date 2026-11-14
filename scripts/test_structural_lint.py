#!/usr/bin/env python3
"""Tests for scripts/structural_lint.py and the ast-grep rules in lint/ast-grep.

* the ratchet rules on scans given in memory (a rise, a fall, a missing row, a malformed marker, a repository with no
  production Rust code);
* the marker form (`// ast-grep-ignore: <rule-id> -- <reason>`): every other form is counted and reported;
* the baseline's round trip and validation (a hashed row is refused), and --write-own;
* a scan of a corpus written here: production sites counted, sites in tests / benches / examples, in a `#[cfg(test)]`
  module, and printing in src/bin and src/main.rs not counted, and a cross-check of the print count against an
  independent lexical pass over the same corpus (comments blanked, cfg(test) modules removed by brace matching);
* exit 2 when nothing was read and when a rule test fails.

Usage: python3 scripts/test_structural_lint.py (the scanner from AST_GREP, else ast-grep on PATH)
"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import structural_lint as sl  # noqa: E402

FIELDS = sl.FIELDS


def scan(**counts) -> dict:
    s = {"files": 3, "bad_markers": [], **{f: 0 for f in FIELDS}, "libm-transcendental": 0}
    s.update(counts)
    return s


def row(**counts) -> dict:
    return {f: counts.get(f, 0) for f in FIELDS}


class Rules(unittest.TestCase):
    def test_equal_counts_pass(self):
        self.assertEqual(sl.compare({"A": scan(print=2, ignores=1)}, {"A": row(print=2, ignores=1)}), [])

    def test_a_rise_is_red(self):
        for f in FIELDS:
            out = sl.compare({"A": scan(**{f: 5})}, {"A": row(**{f: 4})})
            self.assertEqual(len(out), 1)
            self.assertIn(f"{f} 5 > baseline 4", out[0])

    def test_a_fall_is_red_until_the_baseline_is_lowered(self):
        out = sl.compare({"A": scan(hashmap=1)}, {"A": row(hashmap=2)})
        self.assertEqual(len(out), 1)
        self.assertIn("lower the baseline", out[0])

    def test_libm_is_not_gated(self):
        self.assertEqual(sl.compare({"A": scan(**{"libm-transcendental": 40})}, {"A": row()}), [])

    def test_a_repository_without_a_row_is_red(self):
        self.assertIn("no row", sl.compare({"B": scan()}, {})[0])

    def test_no_production_code_needs_no_row_and_may_not_have_one(self):
        self.assertEqual(sl.compare({"B": scan(files=0)}, {}), [])
        self.assertIn("drop the row", sl.compare({"B": scan(files=0)}, {"B": row()})[0])

    def test_a_malformed_marker_is_red(self):
        out = sl.compare({"A": scan(ignores=1, bad_markers=["src/a.rs:3"])}, {"A": row(ignores=1)})
        self.assertEqual(len(out), 1)
        self.assertIn("src/a.rs:3", out[0])


class Markers(unittest.TestCase):
    def count(self, text: str) -> tuple[int, list[str]]:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.rs"
            p.write_text(text, encoding="utf-8")
            return sl.markers([p], Path(d))

    def test_the_good_form(self):
        for t in ("// ast-grep-ignore: production-print -- the CLI's one line of output\n",
                  "    // ast-grep-ignore: production-print, hashmap-iteration-order -- sorted right after this\n"):
            self.assertEqual(self.count(t), (1, []), t)

    def test_every_other_form_is_counted_and_reported(self):
        for t in ("// ast-grep-ignore\n",
                  "// ast-grep-ignore: production-print\n",
                  "// ast-grep-ignore: production-print, a reason without the separator\n",
                  "// ast-grep-ignore: production-print -- short\n",
                  "// ast-grep-ignore: no-such-rule -- a long enough reason here\n",
                  "/* ast-grep-ignore: production-print -- a block comment form */\n"):
            n, bad = self.count(t)
            self.assertEqual((n, len(bad)), (1, 1), t)


class Baseline(unittest.TestCase):
    def test_a_hashed_row_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.toml"
            p.write_text('[repo."sha256:00ff"]\nprint = 0\nhashmap = 0\nignores = 0\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                sl.load_baseline(p)

    def test_the_committed_baseline_has_no_hashed_row(self):
        self.assertFalse([k for k in sl.load_baseline() if k.startswith("sha256:")])

    def test_round_trip(self):
        rows = {"ALICE-A": row(print=3), "ALICE-B": row(hashmap=1, ignores=2)}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.toml"
            p.write_text(sl.render(rows), encoding="utf-8")
            self.assertEqual(sl.load_baseline(p), rows)

    def test_bad_rows_are_refused(self):
        for body in ('[repo."A"]\nprint = 1\nhashmap = 0\n',
                     '[repo."A"]\nprint = -1\nhashmap = 0\nignores = 0\n',
                     '[repo."A"]\nprint = 0\nhashmap = 0\nignores = 0\nlibm = 3\n'):
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "b.toml"
                p.write_text(body, encoding="utf-8")
                with self.assertRaises(ValueError):
                    sl.load_baseline(p)

    def test_the_committed_baseline_loads(self):
        rows = sl.load_baseline()
        self.assertGreater(len(rows), 0)


CORPUS = {
    "src/lib.rs": """
use std::collections::HashMap;
pub fn a(x: f64) -> f64 {
    println!("a {x}");          // counted
    eprintln!("b");             // counted
    let _ = dbg!(x);            // counted
    x.sin() + f64::exp(x)       // libm, counted but not gated
}
pub fn b() -> Vec<u32> {
    let mut m = HashMap::new();
    m.insert(1u32, 2u32);
    m.keys().copied().collect() // counted
}
pub fn c() -> u32 {
    let mut m = HashMap::new();
    m.insert(1u32, 2u32);
    let mut n = 0;
    for (k, _) in &m { n += k; } // counted
    n
}
pub fn d() {
    // a comment with println!("not code")
    let s = "println!(\\"in a string\\")";
    let _ = s;
}
pub fn e() {
    // ast-grep-ignore: production-print -- the one status line of the library
    println!("kept");
}
#[cfg(test)]
mod tests {
    #[test]
    fn t() { println!("test"); }
}
""",
    "src/deep/mod.rs": """
pub fn f() { print!("x"); eprint!("y"); }
#[cfg(test)]
#[allow(dead_code)]
mod t { fn g() { println!("z"); } }
""",
    "src/bin/tool.rs": 'fn main() { println!("cli"); }\n',
    "src/main.rs": 'fn main() { println!("cli"); }\n',
    "tests/it.rs": 'fn x() { println!("t"); }\n',
    "tests/src/helper.rs": 'pub fn x() { println!("t"); }\n',
    "benches/src/b.rs": 'pub fn x() { println!("t"); }\n',
    "benches/b.rs": 'fn x() { println!("t"); }\n',
    "examples/e.rs": 'fn x() { println!("t"); }\n',
}


def lexical_prints(text: str) -> int:
    """an independent count: comments and string literals blanked, cfg(test) modules removed by brace matching"""
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    text = re.sub(r"//[^\n]*", "", text)
    while True:
        m = re.search(r"#\[cfg\(test\)\](?:\s*#\[[^\]]*\])*\s*mod\s+\w+\s*\{", text)
        if not m:
            break
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        text = text[:m.start()] + text[i:]
    return len(re.findall(r"\b(?:println|eprintln|print|eprint|dbg)!\s*\(", text))


class Corpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = sl.scanner()
        sl.rule_tests(cls.exe)
        cls.dir = Path(tempfile.mkdtemp())
        for rel, text in CORPUS.items():
            p = cls.dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        cls.s = sl.scan_dir(cls.exe, cls.dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def test_counts(self):
        # src/lib.rs, src/deep/mod.rs, src/bin/tool.rs, src/main.rs; not tests / benches / examples
        self.assertEqual(self.s["files"], 4)
        # lib.rs a: 3, deep: 2 (the suppressed one in e and the cfg(test) ones not counted)
        self.assertEqual(self.s["print"], 5)
        self.assertEqual(self.s["hashmap"], 2)
        self.assertEqual(self.s["libm-transcendental"], 2)
        self.assertEqual((self.s["ignores"], self.s["bad_markers"]), (1, []))

    def test_print_agrees_with_an_independent_lexical_pass(self):
        lexical = 0
        for rel, text in CORPUS.items():
            parts = Path(rel).parts
            if parts[0] != "src" or "bin" in parts or rel == "src/main.rs":
                continue
            lexical += lexical_prints(text)
        # the lexical pass sees the suppressed site in e; the ratchet does not and counts the marker instead
        self.assertEqual(lexical, self.s["print"] + self.s["ignores"])
        self.assertGreater(lexical, 0)


class WriteOwn(unittest.TestCase):
    def test_write_own_then_only_against_it(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "repo" / "src"
            src.mkdir(parents=True)
            (src / "lib.rs").write_text('pub fn f() { println!("x"); }\n', encoding="utf-8")
            own = Path(d) / "repo" / "conformance-own" / "structural-lint.toml"
            self.assertEqual(sl.main(["--write-own", f"ALICE-P={src.parent}", "--baseline", str(own)]), 0)
            self.assertEqual(sl.load_baseline(own), {"ALICE-P": row(print=1)})
            self.assertEqual(sl.main(["--only", f"ALICE-P={src.parent}", "--baseline", str(own)]), 0)
            (src / "lib.rs").write_text('pub fn f() { println!("x"); print!("y"); }\n', encoding="utf-8")
            self.assertEqual(sl.main(["--only", f"ALICE-P={src.parent}", "--baseline", str(own)]), 1)

    def test_write_own_refuses_no_code_and_a_malformed_marker(self):
        with tempfile.TemporaryDirectory() as d:
            own = Path(d) / "own.toml"
            self.assertEqual(sl.main(["--write-own", f"ALICE-P={d}", "--baseline", str(own)]), 2)
            (Path(d) / "src").mkdir()
            (Path(d) / "src" / "lib.rs").write_text("// ast-grep-ignore\npub fn f() {}\n", encoding="utf-8")
            self.assertEqual(sl.main(["--write-own", f"ALICE-P={d}", "--baseline", str(own)]), 2)
            self.assertFalse(own.exists())

    def test_write_own_needs_its_own_file(self):
        # the default baseline is swapped for a temporary file, so a broken guard cannot overwrite the real one
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "baseline.toml"
            fake.write_text("", encoding="utf-8")
            with mock.patch.object(sl, "BASELINE", fake):
                self.assertEqual(sl.main(["--write-own", f"ALICE-Law={HERE.parent}"]), 2)
            self.assertEqual(fake.read_text(encoding="utf-8"), "")


class ScannerVersion(unittest.TestCase):
    """the rules and their cases are written against one ast-grep version, the one CI installs: another version can
    match differently (0.51.2 reports nothing on a case 0.51.0 matches) and must stop the run with the version to
    install, not with a rule test that seems to fail"""

    def fake(self, d: Path, version: str) -> Path:
        exe = Path(d) / "ast-grep"
        exe.write_text(f'#!/bin/sh\necho "$@" >> "{d}/calls"\n[ "$1" = --version ] && echo "ast-grep {version}"\nexit 0\n',
                       encoding="utf-8")
        exe.chmod(0o755)
        return exe

    def test_another_version_is_exit_2_with_the_version_to_install_and_no_rule_test(self):
        with tempfile.TemporaryDirectory() as d:
            exe = self.fake(d, "0.51.2")
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"AST_GREP": str(exe)}), redirect_stderr(err):
                self.assertEqual(sl.main(["--only", f"ALICE-Law={HERE.parent}"]), 2)
            self.assertIn("0.51.2", err.getvalue())
            self.assertIn(f"install ast-grep {sl.AST_GREP_VERSION}", err.getvalue())
            self.assertNotIn("test", (Path(d) / "calls").read_text(encoding="utf-8"))

    def test_an_unreadable_version_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            exe = self.fake(d, "")
            with mock.patch.dict(os.environ, {"AST_GREP": str(exe)}):
                with self.assertRaises(sl.hl.Unreadable):
                    sl.scanner()

    def test_the_pinned_version_passes(self):
        with tempfile.TemporaryDirectory() as d:
            exe = self.fake(d, sl.AST_GREP_VERSION)
            with mock.patch.dict(os.environ, {"AST_GREP": str(exe)}):
                self.assertEqual(sl.scanner(), str(exe))

    def test_the_pin_is_the_version_every_workflow_installs(self):
        pins = [(p.name, m) for p in (HERE.parent / ".github" / "workflows").glob("*.yml")
                for m in re.findall(r"ast-grep-cli==([0-9.]+)", p.read_text(encoding="utf-8"))]
        self.assertGreater(len(pins), 0, "no workflow installs ast-grep: nothing compared")
        for name, v in pins:
            self.assertEqual(v, sl.AST_GREP_VERSION, name)


class Exits(unittest.TestCase):
    def test_nothing_read_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            b = Path(d) / "b.toml"
            b.write_text("", encoding="utf-8")
            self.assertEqual(sl.main(["--only", f"ALICE-X={d}", "--baseline", str(b)]), 2)

    def test_a_failing_rule_test_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "ast-grep"
            shutil.copytree(sl.CONFIG.parent, cfg)
            t = cfg / "rule-tests" / "production-print-test.yml"
            # a case the rule matches, put under valid: the test must fail
            t.write_text(t.read_text(encoding="utf-8").replace("\nvalid:", '\nvalid:\n  - "fn f() { println!(\\"x\\"); }"', 1),
                         encoding="utf-8")
            with mock.patch.object(sl, "CONFIG", cfg / "sgconfig.yml"):
                self.assertEqual(sl.main(["--only", f"ALICE-Law={HERE.parent}"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
