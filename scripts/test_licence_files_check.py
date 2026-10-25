#!/usr/bin/env python3
"""Tests for licence_files_check.py: a declared licence needs its full text in the repository.

The full texts come from scripts/testdata/agpl-3.0.txt (the GNU AGPL v3 text as the ALICE repositories carried it
in LICENSE-AGPL) and scripts/testdata/apache-2.0.txt (the Apache License 2.0 text as apache.org publishes it).

Usage: python3 scripts/test_licence_files_check.py
"""
from __future__ import annotations

import importlib.util
import atexit
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def scratch() -> Path:
    """a temporary directory removed when the test process ends (a fixture repository is read after the call
    that made it returns, so it cannot be a with-block)"""
    d = Path(tempfile.mkdtemp(prefix="law-test-"))
    atexit.register(shutil.rmtree, d, ignore_errors=True)
    return d

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("lfc", HERE / "licence_files_check.py")
lfc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lfc)

AGPL = (HERE / "testdata" / "agpl-3.0.txt").read_text(encoding="utf-8")
APACHE = (HERE / "testdata" / "apache-2.0.txt").read_text(encoding="utf-8")
MIT = """MIT License

Copyright (c) 2026 Someone

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
# the short notice some repositories carried as LICENSE-APACHE: it points at the licence, it is not the licence
APACHE_NOTICE = """Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0
"""
TRADEMARK = ("TRADEMARK NOTICE\n\nNothing in the licence under which this software is distributed (see\n"
             "{} and the `license` field in Cargo.toml) grants permission to use these trademarks.\n")


def without_network_clause(text: str) -> str:
    """the AGPL text with its section 13 (Remote Network Interaction) taken out: the shape of a GPL body under an
    Affero heading"""
    start = text.index("  13. Remote Network Interaction")
    end = text.index("  14. Revised Versions of this License.")
    return text[:start] + text[end:]


def repo(files: dict[str, str]) -> str:
    d = str(scratch())
    for rel, text in files.items():
        p = Path(d) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    g = ["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", d], check=True)
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "x"], check=True)
    return d


def cargo(licence: str) -> str:
    return f'[package]\nname = "x"\nversion = "0.1.0"\nlicense = "{licence}"\n'


class Declared(unittest.TestCase):
    def run_check(self, files, **kw):
        return lfc.check(repo(files), "HEAD", **kw)

    def test_mit_or_apache_with_both_full_texts(self):
        n, f, _ = self.run_check({"Cargo.toml": cargo("MIT OR Apache-2.0"), "LICENSE-MIT": MIT, "LICENSE-APACHE": APACHE})
        self.assertEqual((n, f), (2, []))

    def test_apache_rewrapped_is_still_the_text(self):
        # 9 words a line: different wrapping, the same words
        rewrapped = "\n".join(" ".join(APACHE.split()[i:i + 9]) for i in range(0, len(APACHE.split()), 9))
        n, f, _ = self.run_check({"Cargo.toml": cargo("Apache-2.0"), "LICENSE": rewrapped})
        self.assertEqual(f, [])

    def test_apache_notice_is_not_the_licence(self):
        _, f, _ = self.run_check({"Cargo.toml": cargo("MIT OR Apache-2.0"), "LICENSE-MIT": MIT, "LICENSE-APACHE": APACHE_NOTICE})
        self.assertEqual(len(f), 1)
        self.assertIn("LICENSE-APACHE is not the full text", f[0])

    def test_mit_missing(self):
        _, f, _ = self.run_check({"Cargo.toml": cargo("MIT OR Apache-2.0"), "LICENSE-APACHE": APACHE})
        self.assertEqual(len(f), 1)
        self.assertIn("MIT is declared", f[0])

    def test_mit_file_without_the_mit_text(self):
        _, f, _ = self.run_check({"Cargo.toml": cargo("MIT"), "LICENSE-MIT": "All rights reserved.\n"})
        self.assertEqual(len(f), 1)
        self.assertIn("MIT is declared", f[0])

    def test_mit_with_a_clause_added_is_not_mit(self):
        # an extra obligation appended (or put before the grant) makes it another licence
        extra = MIT + "\nAny distribution must display the notice in its user interface.\n"
        _, f, _ = self.run_check({"Cargo.toml": cargo("MIT"), "LICENSE-MIT": extra})
        self.assertEqual(len(f), 1)
        before = MIT.replace("Permission is hereby granted",
                             "Use requires prior written consent; see LICENSE-OTHER.md.\n\nPermission is hereby granted", 1)
        _, f, _ = self.run_check({"Cargo.toml": cargo("MIT"), "LICENSE-MIT": before})
        self.assertEqual(len(f), 1)

    def test_mit_rewrapped_and_other_holder(self):
        other = MIT.replace("Copyright (c) 2026 Someone", "Copyright (c) 2024-2026 Some One, Inc.")
        rewrapped = "\n".join(" ".join(other.split()[i:i + 7]) for i in range(0, len(other.split()), 7))
        _, f, _ = self.run_check({"Cargo.toml": cargo("MIT"), "LICENSE": rewrapped})
        self.assertEqual(f, [])

    def test_agpl_official(self):
        _, f, _ = self.run_check({"Cargo.toml": cargo("AGPL-3.0-or-later OR LicenseRef-Commercial"),
                                  "LICENSE-AGPL": AGPL, "LICENSE-COMMERCIAL.md": "terms\n"})
        self.assertEqual(f, [])

    def test_agpl_without_the_network_clause_fails(self):
        _, f, _ = self.run_check({"Cargo.toml": cargo("AGPL-3.0-or-later"), "LICENSE": without_network_clause(AGPL)})
        self.assertEqual(len(f), 1)
        self.assertIn("full GNU AGPL v3 text", f[0])

    def test_agpl_report_only_reports_and_passes(self):
        _, f, r = self.run_check({"Cargo.toml": cargo("AGPL-3.0-or-later"), "LICENSE": without_network_clause(AGPL)},
                                 agpl_report_only=True)
        self.assertEqual(f, [])
        self.assertEqual(len(r), 1)

    def test_licenceref_needs_its_file(self):
        _, f, _ = self.run_check({"Cargo.toml": cargo("AGPL-3.0-only OR LicenseRef-Commercial"), "LICENSE-AGPL": AGPL})
        self.assertEqual(len(f), 1)
        self.assertIn("LicenseRef-Commercial", f[0])

    def test_unknown_licence_is_a_finding(self):
        _, f, _ = self.run_check({"Cargo.toml": cargo("BSD-3-Clause"), "LICENSE": "x\n"})
        self.assertIn("no rule", f[0])

    def test_workspace_package_licence(self):
        _, f, _ = self.run_check({"Cargo.toml": '[workspace]\nmembers = []\n[workspace.package]\nlicense = "MIT"\n',
                                  "LICENSE-MIT": MIT})
        self.assertEqual(f, [])


def package_json(licence: str | None) -> str:
    return "{\n  \"name\": \"x\",\n  \"private\": true" + (f",\n  \"license\": \"{licence}\"" if licence else "") + "\n}\n"


class NodeShell(unittest.TestCase):
    """a repository with no Cargo.toml (a Next.js shell) declares its licence in package.json, at the root or one
    directory down (frontend/package.json); before, the check compared nothing there and exited 2"""

    def run_check(self, files, **kw):
        return lfc.check(repo(files), "HEAD", **kw)

    def test_frontend_package_json_with_the_agpl_text(self):
        n, f, _ = self.run_check({"frontend/package.json": package_json("AGPL-3.0-or-later"), "LICENSE": AGPL})
        self.assertEqual((n, f), (1, []))

    def test_root_package_json_without_its_text(self):
        n, f, _ = self.run_check({"package.json": package_json("MIT"), "LICENSE": AGPL})
        self.assertEqual(n, 1)
        self.assertEqual(len(f), 1)
        self.assertIn("MIT", f[0])

    def test_package_jsons_that_disagree(self):
        n, f, _ = self.run_check({"frontend/package.json": package_json("AGPL-3.0-or-later"),
                                  "admin/package.json": package_json("MIT"), "LICENSE": AGPL})
        self.assertTrue(any("disagree" in x for x in f), f)

    def test_a_deeper_package_json_is_not_the_declaration(self):
        n, _, _ = self.run_check({"frontend/vendor/x/package.json": package_json("MIT"), "LICENSE": AGPL})
        self.assertEqual(n, 0)

    def test_a_package_json_without_a_licence_declares_nothing(self):
        n, _, _ = self.run_check({"frontend/package.json": package_json(None), "LICENSE": AGPL})
        self.assertEqual(n, 0)

    def test_an_object_licence_is_not_a_declaration(self):
        # npm's old form `"license": {"type": "MIT"}`
        files = {"frontend/package.json": '{"name": "x", "license": {"type": "MIT"}}\n', "LICENSE": AGPL}
        n, f, _ = self.run_check(files)
        self.assertEqual((n, f), (0, []))

    def test_cargo_and_package_json_that_agree(self):
        n, f, _ = self.run_check({"Cargo.toml": cargo("AGPL-3.0-or-later"),
                                  "frontend/package.json": package_json("AGPL-3.0-or-later"), "LICENSE": AGPL})
        self.assertEqual((n, f), (2, []))  # the package.json and the AGPL text

    def test_a_package_json_that_differs_from_cargo_names_both(self):
        n, f, _ = self.run_check({"Cargo.toml": cargo("AGPL-3.0-only"), "frontend/package.json": package_json("MIT"),
                                  "LICENSE": AGPL})
        self.assertEqual(f, ["frontend/package.json declares MIT and Cargo.toml declares AGPL-3.0-only"])

    def test_root_package_json_is_compared_too(self):
        _, f, _ = self.run_check({"Cargo.toml": cargo("AGPL-3.0-or-later OR LicenseRef-Commercial"),
                                  "package.json": package_json("AGPL-3.0-or-later"), "LICENSE": AGPL,
                                  "LICENSE-COMMERCIAL": "terms\n"})
        self.assertEqual(len(f), 1)
        self.assertIn("package.json declares AGPL-3.0-or-later", f[0])


class PackageJsonExemption(unittest.TestCase):
    """a package that is its own distribution may declare another licence, only where listed (repository, path)

    The mechanism is tested with an entry put in for the test (the table itself lists none today)"""

    LISTED = {("ALICE-SDF", "vrchat-package/package.json"):
              "the VRChat / Unity package is distributed on its own and points at its own LICENSE.md"}

    def setUp(self):
        saved = dict(lfc.PACKAGE_JSON_EXEMPT)
        lfc.PACKAGE_JSON_EXEMPT.clear()
        lfc.PACKAGE_JSON_EXEMPT.update(self.LISTED)
        self.addCleanup(lambda: (lfc.PACKAGE_JSON_EXEMPT.clear(), lfc.PACKAGE_JSON_EXEMPT.update(saved)))

    def sdf(self, with_package=True, licence="SEE LICENSE IN LICENSE.md"):
        files = {"Cargo.toml": cargo("Apache-2.0"), "LICENSE": APACHE}
        if with_package:
            files["vrchat-package/package.json"] = package_json(licence)
        return files

    def test_each_listed_exemption_has_a_reason(self):
        self.assertGreater(len(lfc.PACKAGE_JSON_EXEMPT), 0)
        for key, why in lfc.PACKAGE_JSON_EXEMPT.items():
            self.assertGreaterEqual(len(why.split()), 6, key)

    def test_the_sdf_vrchat_package(self):
        _, f, _ = lfc.check(repo(self.sdf()), "HEAD", repo_name="ALICE-SDF")
        self.assertEqual(f, [])

    def test_a_helper_package_with_another_licence_is_a_finding(self):
        # a package that is not distributed on its own declares the repository's licence; it is not an exemption
        files = {"Cargo.toml": cargo("AGPL-3.0-only OR LicenseRef-Commercial"), "LICENSE-AGPL": AGPL,
                 "LICENSE-COMMERCIAL": "terms\n", "scripts/package.json": package_json("ISC")}
        _, f, _ = lfc.check(repo(files), "HEAD", repo_name="ALICE-Helper")
        self.assertEqual(f, ["scripts/package.json declares ISC and Cargo.toml declares AGPL-3.0-only OR LicenseRef-Commercial"])

    def test_the_same_path_in_another_repository_is_a_finding(self):
        _, f, _ = lfc.check(repo(self.sdf()), "HEAD", repo_name="ALICE-Other")
        self.assertEqual(len(f), 1)
        self.assertIn("vrchat-package/package.json declares", f[0])

    def test_an_unused_exemption_is_a_finding(self):
        for files in (self.sdf(with_package=False), self.sdf(licence="Apache-2.0")):
            _, f, _ = lfc.check(repo(files), "HEAD", repo_name="ALICE-SDF")
            self.assertEqual(len(f), 1, f)
            self.assertIn("is not used", f[0])

    def test_the_name_comes_from_origin(self):
        d = repo(self.sdf())
        subprocess.run(["git", "-C", d, "remote", "add", "origin", "git@github.com:someone/ALICE-SDF.git"], check=True)
        self.assertEqual(lfc.repo_name_of(d), "ALICE-SDF")
        _, f, _ = lfc.check(d, "HEAD")
        self.assertEqual(f, [])


class Trademark(unittest.TestCase):
    def test_generic_wording_with_licence_files(self):
        _, f, _ = lfc.check(repo({"Cargo.toml": cargo("MIT"), "LICENSE-MIT": MIT,
                                  "TRADEMARK_NOTICE": TRADEMARK.format("the LICENSE-* files")}), "HEAD")
        self.assertEqual(f, [])

    def test_points_at_a_missing_file(self):
        _, f, _ = lfc.check(repo({"Cargo.toml": cargo("MIT"), "LICENSE-MIT": MIT,
                                  "TRADEMARK_NOTICE": TRADEMARK.format("LICENSE")}), "HEAD")
        self.assertEqual(len(f), 1)
        self.assertIn("TRADEMARK_NOTICE points at LICENSE", f[0])

    def test_points_at_present_files(self):
        _, f, _ = lfc.check(repo({"Cargo.toml": cargo("AGPL-3.0-only OR LicenseRef-Commercial"), "LICENSE-AGPL": AGPL,
                                  "LICENSE-COMMERCIAL.md": "t\n",
                                  "TRADEMARK_NOTICE": TRADEMARK.format("LICENSE-AGPL, LICENSE-COMMERCIAL.md")}), "HEAD")
        self.assertEqual(f, [])


class Exit(unittest.TestCase):
    def cli(self, files, *extra):
        d = repo(files)
        return subprocess.run([sys.executable, str(HERE / "licence_files_check.py"), "--repo", d, *extra],
                              capture_output=True, text=True)

    def test_nothing_declared_exits_2(self):
        self.assertEqual(self.cli({"README.md": "x\n"}).returncode, 2)
        self.assertEqual(self.cli({"Cargo.toml": '[package]\nname = "x"\n'}).returncode, 2)

    def test_node_shell_exits_0(self):
        files = {"frontend/package.json": package_json("AGPL-3.0-or-later"), "LICENSE": AGPL}
        p = self.cli(files)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("in 1 checks", p.stdout)

    def test_a_package_json_that_is_not_an_object_exits_2(self):
        for text in ("[1, 2]\n", "{not json\n"):
            p = self.cli({"frontend/package.json": text, "LICENSE": AGPL})
            self.assertEqual(p.returncode, 2, text + p.stdout + p.stderr)

    def test_finding_exits_1_and_ok_exits_0(self):
        self.assertEqual(self.cli({"Cargo.toml": cargo("MIT")}).returncode, 1)
        self.assertEqual(self.cli({"Cargo.toml": cargo("MIT"), "LICENSE-MIT": MIT}).returncode, 0)

    def test_agpl_report_only_exit(self):
        files = {"Cargo.toml": cargo("AGPL-3.0-or-later"), "LICENSE": without_network_clause(AGPL)}
        self.assertEqual(self.cli(files).returncode, 1)
        p = self.cli(files, "--agpl-report-only")
        self.assertEqual(p.returncode, 0)
        self.assertIn("report (not failing)", p.stdout)


class TheExemptionTable(unittest.TestCase):
    """the table as shipped (no entry put in by a test)"""

    def test_every_entry_has_a_reason(self):
        for key, why in lfc.PACKAGE_JSON_EXEMPT.items():
            self.assertGreaterEqual(len(why.split()), 6, key)

    def test_the_sdf_vrchat_package_declares_the_crate_licence_without_an_exemption(self):
        self.assertNotIn(("ALICE-SDF", "vrchat-package/package.json"), lfc.PACKAGE_JSON_EXEMPT)
        files = {"Cargo.toml": cargo("LicenseRef-Proprietary"), "LICENSE-Proprietary": "terms\n",
                 "vrchat-package/package.json": package_json("LicenseRef-Proprietary")}
        _, f, _ = lfc.check(repo(files), "HEAD", repo_name="ALICE-SDF")
        self.assertEqual(f, [])
        # another licence there is a finding now that it is not listed
        files["vrchat-package/package.json"] = package_json("SEE LICENSE IN LICENSE.md")
        _, f, _ = lfc.check(repo(files), "HEAD", repo_name="ALICE-SDF")
        self.assertEqual(len(f), 1, f)
        self.assertIn("vrchat-package/package.json declares", f[0])



ME = "Moroya Sakamoto"
OTHER = "Ex" + "toria"  # the company name split, so this file's own text is not an occurrence


def cargo_authors(*authors: str, table: str = "package") -> str:
    inner = ", ".join(f'"{a}"' for a in authors)
    return f'[{table}]\nname = "x"\nversion = "0.1.0"\nlicense = "MIT"\nauthors = [{inner}]\n'


class Holder(unittest.TestCase):
    """the only rights holder is the author: rights statements and manifest authors name no one else"""

    def occ(self, files):
        return lfc.holder_occurrences(list(files), lambda p: files[p].encode("utf-8"))

    def test_a_rights_file_naming_another_holder_is_an_occurrence(self):
        for name in ("LICENSE", "LICENSE-COMMERCIAL.md", "NOTICE", "NOTICE.txt", "TRADEMARK_NOTICE", "crates/a/LICENSE-MIT"):
            self.assertEqual(self.occ({name: f"Copyright 2026 {OTHER} Inc.\n"}), {("holder", name)}, name)
        self.assertEqual(self.occ({"LICENSE": f"(c) {OTHER.upper()}\n"}), {("holder", "LICENSE")})
        self.assertEqual(self.occ({"LICENSE": "株式会社エクス" + "トーリア\n"}), {("holder", "LICENSE")})

    def test_the_enquiry_line_is_not_a_holder(self):
        # the contact line of the canonical notices names the company's site, not a rights holder
        line = "Enquiries: https://" + OTHER.lower() + ".co.jp/\n"
        self.assertEqual(self.occ({"TRADEMARK_NOTICE": "trademarks of " + ME + ".\n\n" + line}), set())
        self.assertEqual(self.occ({"TRADEMARK_NOTICE": line + f"Copyright {OTHER}\n"}), {("holder", "TRADEMARK_NOTICE")})
        self.assertEqual(self.occ({"LICENSE": "Enquiries: https://" + OTHER.lower() + ".co.jp/ and " + OTHER + "\n"}),
                         {("holder", "LICENSE")})

    def test_the_readme_and_other_files_are_not_rights_statements(self):
        files = {"README.md": f"Enquiries: https://{OTHER.lower()}.co.jp/\n", "docs/LICENSE_NOTES.md.txt": "x\n",
                 "src/licence.rs": f"// {OTHER}\n", "LICENSE": f"Copyright 2026 {ME}\n"}
        self.assertEqual(self.occ(files), set())

    def test_authors_must_be_exactly_the_author_without_an_e_mail(self):
        ok = {"Cargo.toml": cargo_authors(ME), "py/pyproject.toml": f'[project]\nname = "x"\nauthors = [{{name = "{ME}"}}]\n',
              "b/Cargo.toml": '[package]\nname = "b"\n'}  # no authors: nothing to compare
        self.assertEqual(self.occ(ok), set())
        for text in (cargo_authors(f"{ME} <m@x.y>"), cargo_authors(ME, "Someone"), cargo_authors("you"),
                     cargo_authors(f"{ME} <m@x.y>", table="workspace.package")):
            self.assertEqual(self.occ({"Cargo.toml": text}), {("authors", "Cargo.toml")}, text)
        for text in (f'[project]\nauthors = [{{name = "{ME}", email = "m@x.y"}}]\n',
                     '[tool.poetry]\nauthors = ["Someone <s@x.y>"]\n'):
            self.assertEqual(self.occ({"pyproject.toml": text}), {("authors", "pyproject.toml")}, text)

    def test_vendored_trees_are_not_read(self):
        self.assertEqual(self.occ({"vendor/z/LICENSE": f"(c) {OTHER}\n", "third_party/LICENSE": f"(c) {OTHER}\n"}), set())


class HolderBaseline(unittest.TestCase):
    """known occurrences are listed (repository by sha256 of its name); a new one fails, a fixed one is stale"""

    def base(self, repo_name, *entries):
        key = lfc.repo_key(repo_name)
        return {key: {(k, p) for k, p in entries}}

    def test_a_listed_occurrence_passes_and_a_new_one_fails(self):
        occ = {("holder", "LICENSE"), ("authors", "Cargo.toml")}
        new, stale = lfc.compare_baseline("r", occ, self.base("r", ("holder", "LICENSE")))
        self.assertEqual((new, stale), ({("authors", "Cargo.toml")}, set()))

    def test_a_fixed_occurrence_is_stale(self):
        new, stale = lfc.compare_baseline("r", set(), self.base("r", ("holder", "LICENSE")))
        self.assertEqual((new, stale), (set(), {("holder", "LICENSE")}))

    def test_another_repositorys_entries_do_not_apply(self):
        new, _ = lfc.compare_baseline("r", {("holder", "LICENSE")}, self.base("q", ("holder", "LICENSE")))
        self.assertEqual(new, {("holder", "LICENSE")})

    def test_the_baseline_file_round_trips_and_names_no_repository(self):
        b = {lfc.repo_key("ALICE-Zorblat"): {("holder", "LICENSE"), ("authors", "a/Cargo.toml")}}
        text = lfc.dump_baseline(b)
        self.assertNotIn("Zorblat", text)
        self.assertEqual(lfc.load_baseline(text), b)

    def test_a_malformed_baseline_is_refused(self):
        for text in ('[[entry]]\nrepo = "x"\nkind = "holder"\npath = "LICENSE"\n',
                     f'[[entry]]\nrepo = "{"0" * 64}"\nkind = "other"\npath = "LICENSE"\n',
                     f'[[entry]]\nrepo = "{"0" * 64}"\nkind = "holder"\n'):
            with self.assertRaises(ValueError, msg=text):
                lfc.load_baseline(text)

    def test_growth_against_the_base_is_refused(self):
        old = {lfc.repo_key("r"): {("holder", "LICENSE")}}
        grown = {lfc.repo_key("r"): {("holder", "LICENSE"), ("holder", "NOTICE")}}
        shrunk = {lfc.repo_key("r"): set()}
        self.assertEqual(lfc.baseline_growth(old, grown), [(lfc.repo_key("r"), "holder", "NOTICE")])
        self.assertEqual(lfc.baseline_growth(old, shrunk), [])
        self.assertEqual(lfc.baseline_growth(old, old), [])


class HolderRepoMode(unittest.TestCase):
    """the check of one repository against the baseline (this repository's own CI)"""

    def cli(self, files, baseline: str, *extra):
        d = repo(files)
        b = scratch() / "holder-baseline.toml"
        b.write_text(baseline, encoding="utf-8")
        return subprocess.run([sys.executable, str(HERE / "licence_files_check.py"), "--repo", d, "--repo-name", "r",
                               "--holder-baseline", str(b), *extra], capture_output=True, text=True)

    def files(self, holder_line: str):
        return {"Cargo.toml": cargo_authors(ME), "LICENSE-MIT": MIT, "NOTICE": holder_line}

    def test_a_new_occurrence_fails(self):
        p = self.cli(self.files(f"(c) {OTHER}\n"), "")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("NOTICE", p.stdout)

    def test_a_listed_occurrence_passes(self):
        b = lfc.dump_baseline({lfc.repo_key("r"): {("holder", "NOTICE")}})
        p = self.cli(self.files(f"(c) {OTHER}\n"), b)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_stale_line_is_reported_and_fails_only_when_asked(self):
        b = lfc.dump_baseline({lfc.repo_key("r"): {("holder", "NOTICE")}})
        p = self.cli(self.files(f"(c) {ME}\n"), b)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("stale", p.stdout)
        p = self.cli(self.files(f"(c) {ME}\n"), b, "--stale-fails")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)


class HolderBaseRef(unittest.TestCase):
    """--holder-base-ref reads the base baseline from a commit of the repository"""

    def test_growth_against_a_commit_fails_and_an_introduction_is_noted(self):
        files = {"Cargo.toml": cargo_authors(ME), "LICENSE-MIT": MIT}
        d = repo(files)
        b = Path(d) / "conformance" / "holder-baseline.toml"
        run = lambda *x: subprocess.run([sys.executable, str(HERE / "licence_files_check.py"), "--repo", d,  # noqa: E731
                                         "--repo-name", "r", "--holder-baseline", str(b), *x], capture_output=True, text=True)
        b.parent.mkdir()
        b.write_text(lfc.dump_baseline({}), encoding="utf-8")
        p = run("--holder-base-ref", "HEAD")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("being introduced", p.stdout)
        g = ["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-qm", "base"], check=True)
        b.write_text(lfc.dump_baseline({lfc.repo_key("q"): {("holder", "NOTICE")}}), encoding="utf-8")
        p = run("--holder-base-ref", "HEAD")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("baseline grew", p.stdout)


class HolderBaseRefMustResolve(unittest.TestCase):
    """an empty or unresolvable base ref compares nothing: refused, not read from the index"""

    def test_empty_and_bogus_refs_exit_2(self):
        d = repo({"Cargo.toml": cargo_authors(ME), "LICENSE-MIT": MIT})
        b = Path(d) / "conformance" / "holder-baseline.toml"
        b.parent.mkdir()
        b.write_text(lfc.dump_baseline({}), encoding="utf-8")
        g = ["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-qm", "base"], check=True)
        for ref in ("", " ", "no-such-ref", "0" * 40):
            p = subprocess.run([sys.executable, str(HERE / "licence_files_check.py"), "--repo", d, "--repo-name", "r",
                                "--holder-baseline", str(b), "--holder-base-ref", ref], capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, (ref, p.stdout + p.stderr))
            self.assertIn("base ref", p.stderr)


class TheHolderBaseline(unittest.TestCase):
    """the baseline as shipped"""

    PATH = HERE.parent / "conformance" / "holder-baseline.toml"

    def test_it_loads_and_lists_something(self):
        b = lfc.load_baseline(self.PATH.read_text(encoding="utf-8"))
        self.assertGreater(sum(len(v) for v in b.values()), 0)

    def test_it_is_written_in_its_own_canonical_form(self):
        text = self.PATH.read_text(encoding="utf-8")
        self.assertEqual(lfc.dump_baseline(lfc.load_baseline(text)), text)


if __name__ == "__main__":
    sys.exit(unittest.main())
