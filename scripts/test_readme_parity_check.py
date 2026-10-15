#!/usr/bin/env python3
"""Tests for readme_parity_check.py (two small README pairs in a temporary directory)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rpc", HERE / "readme_parity_check.py")
rpc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rpc)

EN = """# Tool

[日本語](README_JP.md)

Checks alice-zip 0.10.0 against ALICE-DB with 14 gates (14 in all); the digest is `abc` (`9f7cf676708a`).
See [Usage](#usage) and [semver](https://crates.io/crates/semver). Two bodies fall.

## Usage

```sh
run --check   # a comment
```

### Details

Values over 2000 lines.

- first item
- second item

## Usage

See [the second one](#usage-1).
"""
JA = """# Tool

[English](README.md)

alice-zip 0.10.0 を ALICE-DB と 14 の gate (全部で 14) で照らす digest は `abc` (`9f7cf676708a`)
[使い方](#使い方) と [semver](https://crates.io/crates/semver) を見る 2 つの物体が落ちる

## 使い方

```sh
run --check   # a comment
```

### 詳細

2000 行を超える値

- 1 つ目
- 2 つ目

## 使い方

[2 つ目](#使い方-1) を見る
"""


HEADINGS = "".join(f'[[heading]]\nlevel = {lv}\nen = "{e}"\nja = "{j}"\n\n' for lv, e, j in
                   ((1, "Tool", "Tool"), (2, "Usage", "使い方"), (3, "Details", "詳細"), (2, "Usage", "使い方")))


class Parity(unittest.TestCase):
    def run_pair(self, en=EN, ja=JA, headings=HEADINGS):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "en.md").write_text(en, encoding="utf-8")
            (Path(d) / "ja.md").write_text(ja, encoding="utf-8")
            (Path(d) / "h.toml").write_text(headings, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = rpc.main(["--en", str(Path(d) / "en.md"), "--ja", str(Path(d) / "ja.md"),
                                 "--headings", str(Path(d) / "h.toml")])
        return code, out.getvalue()

    def test_a_faithful_translation_agrees(self):
        code, out = self.run_pair()
        self.assertEqual(code, 0, out)
        self.assertIn("0 differences", out)

    def test_each_kind_of_drift_is_found(self):
        cases = {
            "headings": JA.replace("### 詳細\n", "### 細目\n"),
            "code blocks": JA.replace("# a comment", "# コメント"),
            "inline code": JA.replace("`abc`", "`abd`"),
            "numbers": JA.replace("0.10.0", "0.11.0"),
            "hashes": JA.replace("9f7cf676708a", "9f7cf676708b"),
            "names": JA.replace("ALICE-DB", "ALICE-Zip"),
            "links": JA.replace("https://crates.io/crates/semver", "https://docs.rs/semver"),
        }
        for kind, ja in cases.items():
            code, out = self.run_pair(ja=ja)
            self.assertEqual(code, 1, (kind, out))
            self.assertIn(f"found: {kind}", out, kind)

    def test_a_dropped_code_block_is_found(self):
        code, out = self.run_pair(ja=JA.replace("```sh\nrun --check   # a comment\n```\n", ""))
        self.assertEqual(code, 1, out)
        self.assertIn("found: code blocks: en has 1, ja 0", out)

    def test_a_dropped_number_of_two_digits_is_found_and_a_single_digit_counter_is_not(self):
        code, out = self.run_pair(ja=JA.replace("2000 行", "多くの行"))
        self.assertEqual(code, 1, out)
        self.assertIn("only in README.md ['2000']", out)
        # a repeated value counts each time; two digits are enough
        code, out = self.run_pair(ja=JA.replace(" (全部で 14)", ""))
        self.assertEqual(code, 1, out)
        self.assertIn("only in README.md ['14']", out)
        code, out = self.run_pair(ja=JA.replace("2 つの物体", "二つの物体"))
        self.assertEqual(code, 0, out)

    def test_an_anchor_link_must_name_a_heading_of_its_own_file(self):
        code, out = self.run_pair(ja=JA.replace("(#使い方)", "(#usage)"))
        self.assertEqual(code, 1, out)
        self.assertIn("README_JP.md: the link #usage names no heading", out)
        code, out = self.run_pair(en=EN.replace("(#usage)", "(#use)"))
        self.assertEqual(code, 1, out)
        self.assertIn("README.md: the link #use names no heading", out)

    def test_each_file_links_to_the_other_at_the_top(self):
        code, out = self.run_pair(ja=JA.replace("[English](README.md)\n", ""))
        self.assertEqual(code, 1, out)
        self.assertIn("README_JP.md: no link to README.md before the first section", out)
        moved = EN.replace("[日本語](README_JP.md)\n", "") + "\n[日本語](README_JP.md)\n"
        code, out = self.run_pair(en=moved)
        self.assertEqual(code, 1, out)
        self.assertIn("README.md: no link to README_JP.md before the first section", out)

    def test_sections_moved_renamed_or_shortened_in_one_file_are_found(self):
        swapped = JA.replace("### 詳細\n\n2000 行を超える値\n", "").replace(
            "## 使い方\n\n```sh", "### 詳細\n\n2000 行を超える値\n\n## 使い方\n\n```sh", 1)
        code, out = self.run_pair(ja=swapped)
        self.assertEqual(code, 1, out)
        self.assertIn("found: headings: heading 2", out)
        # a heading renamed together with its anchor link in one file
        renamed = JA.replace("## 使い方", "## 用法").replace("#使い方-1", "#用法-1")
        code, out = self.run_pair(ja=renamed)
        self.assertEqual(code, 1, out)
        self.assertIn("readme_headings.toml pairs", out)
        # a paragraph with no value, code or link dropped; a list item dropped
        for ja in (JA.replace("[2 つ目](#使い方-1) を見る\n", ""), JA.replace("- 2 つ目\n", "")):
            code, out = self.run_pair(ja=ja)
            self.assertEqual(code, 1, out)
            self.assertIn("found: sections:", out)

    def test_a_heading_added_at_the_end_of_one_file_is_found(self):
        code, out = self.run_pair(ja=JA + "\n## 付録\n\n追加の節\n")
        self.assertEqual(code, 1, out)
        self.assertIn("headings: README.md has 4, README_JP.md 5, readme_headings.toml lists 4", out)

    def test_a_heading_map_that_lists_nothing_exits_2(self):
        self.assertEqual(self.run_pair(headings="")[0], 2)
        self.assertEqual(self.run_pair(headings='[[heading]]\nen = "x"\n')[0], 2)

    def test_heading_ids_follow_github(self):
        self.assertEqual(rpc.slug("Runbook: one crate"), "runbook-one-crate")
        self.assertEqual(rpc.slug("Runbook: crate ごと"), "runbook-crate-ごと")
        self.assertEqual(rpc.slug("公開する text"), "公開する-text")
        self.assertEqual(rpc.slug("`code` and [a link](x)"), "code-and-a-link")
        # a repeated heading takes -1: the second "Usage" is #usage-1, and #usage-2 names nothing
        code, out = self.run_pair(en=EN.replace("(#usage-1)", "(#usage-2)"))
        self.assertEqual(code, 1, out)
        self.assertIn("README.md: the link #usage-2 names no heading", out)

    def test_nothing_compared_or_an_unreadable_file_exits_2(self):
        self.assertEqual(self.run_pair(en="plain\n", ja="plain\n")[0], 2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(rpc.main(["--en", "/nonexistent/a.md", "--ja", "/nonexistent/b.md"]), 2)


if __name__ == "__main__":
    unittest.main()
