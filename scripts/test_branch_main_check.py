#!/usr/bin/env python3
"""Tests of scripts/branch_main_check.py on manifests and lock files of known shape."""
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import branch_main_check as b  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
GIT = "https://github.com/ext-sakamoro/ALICE-Zip"


def manifest(dep: str, table: str = "dependencies") -> str:
    return f'[package]\nname = "x"\nversion = "0.1.0"\n\n[{table}]\n{dep}\nserde = "1"\n'


def lock(*pkgs: tuple) -> str:
    out = "version = 4\n\n"
    for name, version, source in pkgs:
        out += f'[[package]]\nname = "{name}"\nversion = "{version}"\n' + (f'source = "{source}"\n' if source else "") + "\n"
    return out


class Manifest(unittest.TestCase):
    def test_a_main_branch_dependency_passes(self):
        self.assertEqual(b.manifest_findings(manifest(f'alice-zip = {{ git = "{GIT}", branch = "main", version = "0.11" }}')), [])

    def test_a_rev_a_tag_another_branch_or_no_branch_is_found(self):
        for spec, why in ((f'{{ git = "{GIT}", rev = "{SHA}", version = "0.11" }}', "rev"),
                          (f'{{ git = "{GIT}", tag = "v0.11.0", version = "0.11" }}', "tag"),
                          (f'{{ git = "{GIT}", branch = "dev", version = "0.11" }}', "branch"),
                          (f'{{ git = "{GIT}", version = "0.11" }}', "no branch")):
            found = b.manifest_findings(manifest(f"alice-zip = {spec}"))
            self.assertEqual(len(found), 1, why)
            self.assertIn("alice-zip", found[0], why)

    def test_a_registry_alice_dependency_is_found(self):
        self.assertEqual(len(b.manifest_findings(manifest('alice-zip = "0.11"'))), 1)
        self.assertEqual(len(b.manifest_findings(manifest('alice-zip = { version = "0.11", features = ["std"] }'))), 1)

    def test_every_dependency_table_is_read(self):
        dep = f'alice-zip = {{ git = "{GIT}", rev = "{SHA}", version = "0.11" }}'
        for table in ("dev-dependencies", "build-dependencies", "target.'cfg(unix)'.dependencies",
                      "workspace.dependencies"):
            self.assertEqual(len(b.manifest_findings(manifest(dep, table))), 1, table)

    def test_a_renamed_dependency_is_read_by_its_package(self):
        dep = f'zip = {{ package = "alice-zip", git = "{GIT}", rev = "{SHA}", version = "0.11" }}'
        self.assertEqual(len(b.manifest_findings(manifest(dep))), 1)

    def test_a_path_dependency_and_a_workspace_reference_are_left_to_their_own_checks(self):
        self.assertEqual(b.manifest_findings(manifest('alice-zip = { path = "../zip" }')), [])
        self.assertEqual(b.manifest_findings(manifest('alice-zip = { workspace = true }')), [])

    def test_other_crates_are_not_judged(self):
        self.assertEqual(b.manifest_findings(manifest(f'blake3 = {{ git = "https://example.invalid/b", rev = "{SHA}" }}')), [])


class Lock(unittest.TestCase):
    def test_main_branch_sources_and_path_packages_pass(self):
        self.assertEqual(b.lock_findings(lock(("alice-zip", "0.11.0", f"git+{GIT}?branch=main#{SHA}"),
                                              ("alice-own", "0.1.0", None),
                                              ("serde", "1.0.0", "registry+https://github.com/rust-lang/crates.io-index"))), [])

    def test_a_rev_tag_other_branch_or_registry_source_is_found(self):
        for src in (f"git+{GIT}?rev={SHA}#{SHA}", f"git+{GIT}?tag=v1#{SHA}", f"git+{GIT}?branch=dev#{SHA}",
                    f"git+{GIT}#{SHA}", "registry+https://github.com/rust-lang/crates.io-index"):
            found = b.lock_findings(lock(("alice-zip", "0.11.0", src)))
            self.assertEqual(len(found), 1, src)

    def test_libasp_is_an_alice_crate(self):
        self.assertEqual(len(b.lock_findings(lock(("libasp", "1.0.0", f"git+{GIT}?rev={SHA}#{SHA}")))), 1)


class Repo(unittest.TestCase):
    def test_the_tracked_files_of_a_checkout_are_read_and_counted(self):
        with tempfile.TemporaryDirectory() as d:
            g = Path(d)
            subprocess.run(["git", "init", "-q", str(g)], check=True)
            (g / "Cargo.toml").write_text(manifest(f'alice-zip = {{ git = "{GIT}", rev = "{SHA}", version = "0.11" }}'))
            (g / "Cargo.lock").write_text(lock(("alice-zip", "0.11.0", f"git+{GIT}?rev={SHA}#{SHA}")))
            (g / "target").mkdir()
            (g / "target" / "Cargo.toml").write_text(manifest('alice-zip = "0.1"'))  # untracked: not read
            subprocess.run(["git", "-C", str(g), "add", "Cargo.toml", "Cargo.lock"], check=True)
            found, n = b.check_repo(g)
            self.assertEqual(n, 2)
            self.assertEqual(len(found), 2)


if __name__ == "__main__":
    unittest.main()
