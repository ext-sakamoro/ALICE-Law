#!/usr/bin/env python3
"""Tests of scripts/surface_claims_propose.py (python3 scripts/test_surface_claims_propose.py)

The proposal written to a repository that has not adopted G15 must round-trip through surface_claims_check.py to
no undeclared, readme, declared-not-found or found-not-declared finding for every surface it declares; a surface
found only by a name (unreal) is never declared; a crate that already declares is left as it is; a second run
changes nothing; the mutants at the bottom must each turn a case red.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(root: Path):
    sys.path.insert(0, str(root))
    for mod in ("surface_claims_check", "surface_claims_propose"):
        sys.modules.pop(mod, None)
    spec = importlib.util.spec_from_file_location("surface_claims_propose", root / "surface_claims_propose.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    sys.path.remove(str(root))
    return m


CI = ("on:\n  push:\n    branches: [main]\njobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n"
      "      - run: cargo build --target wasm32-unknown-unknown && wasm-bindgen x.wasm\n"
      "      - run: node --test tests/js\n"
      "      - run: cargo build --target wasm32-wasip1\n"
      "      - run: cargo rustc --features ffi --crate-type cdylib\n"
      "      - run: maturin build && pip install target/wheels/*.whl && pytest\n")
LIB = ("#[wasm_bindgen]\npub fn w() {}\n"
       "#[no_mangle]\npub extern \"C\" fn alice_x_new() {}\n"
       "#[pyfunction]\nfn p() {}\n")
CS = "class X { [DllImport(\"alice_x\")] static extern void alice_x_new(); }\n"
UE_H = "extern \"C\" { void alice_x_new(); }\n"
README = "# alice-x\n\nText.\n\n## License\n\nMIT\n"
README_JP = "# alice-x\n\n説明\n\n## ライセンス\n\nMIT\n"


def repo(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp(prefix="scp-"))
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    return d


def fixture(**over) -> Path:
    files = {
        "Cargo.toml": '[package]\nname = "alice-x"\nversion = "0.1.0"\n\n[dependencies]\nserde = "1"\n',
        "src/lib.rs": LIB,
        ".github/workflows/ci.yml": CI,
        "bindings/unity/X.cs": CS,
        "bindings/ue5/x.h": UE_H,
        "fuzz/Cargo.toml": '[package]\nname = "alice-x-fuzz"\nversion = "0.0.0"\n',
        "fuzz/fuzz_targets/a.rs": "fn main() {}\n",
        "README.md": README,
        "README_JP.md": README_JP,
    }
    files.update(over)
    return repo({k: v for k, v in files.items() if v is not None})


class Cases(unittest.TestCase):
    root = HERE

    def setUp(self) -> None:
        self.m = load(self.root)
        self.scc = self.m.scc

    def write(self, d: Path) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = self.m.main(["--repo", str(d), "--write"])
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        return code, out.getvalue()

    def kinds(self, d: Path) -> list[str]:
        findings, compared = self.scc.check_repo(d)
        self.assertGreater(compared, 0)
        return sorted(k for k, _ in findings)

    def test_the_proposal_round_trips_to_no_declaration_or_readme_finding(self):
        d = fixture()
        self.assertEqual(self.write(d)[0], 0)
        found = self.kinds(d)
        for k in ("undeclared", "readme", "declared-not-found", "vocabulary"):
            self.assertNotIn(k, found, found)
        # the only declaration gap left is the surface found by a name (unreal), which is not declared
        self.assertEqual([t for k, t in self.scc.check_repo(d)[0] if k == "found-not-declared"],
                         ["Cargo.toml: unreal found in bindings/ue5/x.h, not declared"])

    def test_what_is_declared(self):
        d = fixture()
        self.write(d)
        import tomllib
        root = tomllib.loads((d / "Cargo.toml").read_text())["package"]["metadata"]["alice"]["surfaces"]
        self.assertEqual(root, ["rust", "browser", "wasi", "c", "python", "unity"])
        fuzz = tomllib.loads((d / "fuzz/Cargo.toml").read_text())["package"]["metadata"]["alice"]["surfaces"]
        self.assertEqual(fuzz, [])
        self.assertEqual(self.scc.readme_table((d / "README.md").read_text()), set(root))
        self.assertEqual(self.scc.readme_table((d / "README_JP.md").read_text()), set(root))

    def test_a_name_only_surface_is_listed_for_review_and_never_declared(self):
        d = fixture()
        code, out = self.write(d)
        self.assertIn("review (found by a name, not declared): unreal in bindings/ue5/x.h", out)
        self.assertNotIn("unreal", (d / "Cargo.toml").read_text())
        self.assertNotIn("| unreal |", (d / "README.md").read_text())

    def test_the_section_goes_before_the_license_heading(self):
        d = fixture()
        self.write(d)
        text = (d / "README.md").read_text()
        self.assertLess(text.index("<!-- surfaces -->"), text.index("## License"))
        jp = (d / "README_JP.md").read_text()
        self.assertLess(jp.index("<!-- surfaces -->"), jp.index("## ライセンス"))
        d = fixture(**{"README.md": "# alice-x\n\nno license heading\n"})
        self.write(d)
        self.assertTrue((d / "README.md").read_text().rstrip().endswith("<!-- /surfaces -->"))

    def test_an_existing_table_is_replaced_not_duplicated(self):
        d = fixture(**{"README.md": "# x\n\n<!-- surfaces -->\n| Surface | x |\n|---|---|\n| rust | x |\n<!-- /surfaces -->\n"})
        self.write(d)
        text = (d / "README.md").read_text()
        self.assertEqual(text.count("<!-- surfaces -->"), 1)
        self.assertIn("| python |", text)

    def test_a_crate_that_declares_is_left_as_it_is(self):
        cargo = '[package]\nname = "alice-x"\nversion = "0.1.0"\n\n[package.metadata.alice]\nsurfaces = ["rust"]\n'
        d = fixture(**{"Cargo.toml": cargo})
        self.write(d)
        self.assertEqual((d / "Cargo.toml").read_text(), cargo)
        # the README lists what the crates declare, not what the code shows
        self.assertEqual(self.scc.readme_table((d / "README.md").read_text()), {"rust"})

    def test_an_existing_metadata_table_gets_the_key(self):
        cargo = '[package]\nname = "alice-x"\nversion = "0.1.0"\n\n[package.metadata.alice]\nowner = "x"\n'
        d = fixture(**{"Cargo.toml": cargo})
        self.assertEqual(self.write(d)[0], 0)
        text = (d / "Cargo.toml").read_text()
        self.assertEqual(text.count("[package.metadata.alice]"), 1)
        self.assertIn('surfaces = ["rust", "browser", "wasi", "c", "python", "unity"]', text)
        self.assertIn('owner = "x"', text)

    def test_a_second_run_changes_nothing(self):
        d = fixture()
        self.write(d)
        p = self.m.propose(d)
        self.assertEqual(self.m.changes(d, p), {})

    def test_dry_run_writes_nothing(self):
        d = fixture()
        before = (d / "Cargo.toml").read_text()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.m.main(["--repo", str(d)]), 0)
        self.assertEqual((d / "Cargo.toml").read_text(), before)
        self.assertIn("+surfaces = [", out.getvalue())

    def test_no_crate_exits_2(self):
        d = repo({"README.md": "x\n"})
        err = io.StringIO()
        with contextlib.redirect_stdout(err), contextlib.redirect_stderr(err):
            self.assertEqual(self.m.main(["--repo", str(d)]), 2)


def mutants() -> list[tuple[str, str, str]]:
    return [
        ("noop-probe", "STRUCTURAL = (", "STRUCTURAL = ("),
        ("unreal-declared", 'REVIEW_ONLY = ("unreal",)', "REVIEW_ONLY = ()"),
        ("python-not-declared", 'STRUCTURAL = ("rust", "browser", "wasi", "c", "python", "unity")',
         'STRUCTURAL = ("rust", "browser", "wasi", "c", "unity")'),
        ("declared-crate-overwritten", "        if c[\"declared\"] is not None:\n            declare = None",
         "        if False:\n            declare = None"),
        ("readme-union-misses-declared", "            union |= set(c[\"declared\"]) & set(scc.VOCABULARY)\n",
         "            pass\n"),
        ("jp-readme-skipped", '    "README_JP.md": ("## 出口",', '    "README_JP-skip.md": ("## 出口",'),
        ("license-ignored", "    if m:\n        return text[:m.start()] + section", "    if False:\n        return text[:m.start()] + section"),
        ("table-duplicated", "    if TABLE_RX.search(text):", "    if False:"),
        ("metadata-table-duplicated", "    if m:\n        new = text[:m.end()]", "    if False:\n        new = text[:m.end()]"),
        ("dry-run-writes", "    if a.write:", "    if True:"),
    ]


class Mutants(unittest.TestCase):
    def test_every_mutant_is_killed(self):
        survived = []
        for name, old, new in mutants():
            with tempfile.TemporaryDirectory() as t:
                root = Path(t)
                shutil.copy(HERE / "surface_claims_check.py", root)
                src = (HERE / "surface_claims_propose.py").read_text(encoding="utf-8")
                self.assertIn(old, src, f"mutant {name}: pattern not found (stale mutant)")
                (root / "surface_claims_propose.py").write_text(src.replace(old, new), encoding="utf-8")

                class T(Cases):
                    pass

                T.root = root
                res = unittest.TestResult()
                unittest.defaultTestLoader.loadTestsFromTestCase(T).run(res)
                if not (res.failures or res.errors):
                    survived.append(name)
        self.assertEqual(survived, ["noop-probe"], f"mutants survived (only the no-op probe may): {survived}")


if __name__ == "__main__":
    unittest.main()
