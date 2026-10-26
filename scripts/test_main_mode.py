#!/usr/bin/env python3
"""Tests for main_mode.py: the requirement raise of the CI copy and what verify counts as a problem."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("main_mode", HERE / "main_mode.py")
mm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm)

MANIFEST = '''[package]
name = "alice-law-e2e"
version = "0.0.0"

[dependencies]
alice-db = "0.5"
alice-zip = "0.10"
alice-sdf = "6"
alice-edge = { version = ">=0.2, <0.4", default-features = false, features = ["std", "law"] }
serde = "1"

[features]
main-mode = []

[workspace]
'''


def pkg(name: str, version: str, source: str | None) -> dict:
    return {"name": name, "version": version, "source": source}


REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
DETMATH_URL = "https://github.com/ext-sakamoro/ALICE-DetMath"
DETMATH_GIT = f"git+{DETMATH_URL}?rev=6384856c#6384856c"


def graph(edges: list[tuple[str, str, str | None, str | None]]) -> dict:
    """`cargo metadata` output for edges (from, crate, declared source, resolved source): the declared
    source is what the requiring manifest names (None for a path dependency), the resolved one where
    Cargo took the crate from (None for a path, as a patched checkout is)"""
    packages: dict[str, dict] = {}

    def node(name: str, source: str | None) -> dict:
        pid = f"{name} 1.0.0 {source or 'path'}"
        return packages.setdefault(pid, {"id": pid, "name": name, "version": "1.0.0", "source": source,
                                         "dependencies": []})

    deps: dict[str, list[str]] = {}
    for frm, crate, declared, resolved in edges:
        a, b = node(frm, None), node(crate, resolved)
        a["dependencies"].append({"name": crate, "source": declared, "req": "*", "kind": None})
        deps.setdefault(a["id"], []).append(b["id"])
    nodes = [{"id": pid, "deps": [{"name": packages[t]["name"].replace("-", "_"), "pkg": t}
                                  for t in deps.get(pid, [])]} for pid in packages]
    return {"packages": list(packages.values()), "resolve": {"nodes": nodes}}


class Accepts(unittest.TestCase):
    def test_cargo_rules_for_the_forms_e2e_uses(self):
        cases = [
            ("0.5", "0.5.1", True), ("0.5", "0.6.0", False), ("3", "3.9.0", True), ("3", "4.0.0", False),
            ("^0.0.3", "0.0.3", True), ("^0.0.3", "0.0.4", False), ("0.4.1", "0.4.0", False),
            (">=0.4.1, <0.6", "0.5.0", True), (">=0.4.1, <0.6", "0.6.0", False), (">=0.2, <0.4", "0.4.0", False),
            ("=0.4.1", "0.4.1", True), ("=0.4.1", "0.4.2", False), (">0.4", "0.4.1", True), ("<=1", "1.0.0", True),
        ]
        for req, version, want in cases:
            self.assertEqual(mm.accepts(req, version), want, f"{req!r} accepts {version}")

    def test_a_comparator_this_does_not_read_is_refused_not_guessed(self):
        with self.assertRaises(ValueError):
            mm.accepts("~1.2", "1.2.3")
        with self.assertRaises(ValueError):
            mm.accepts("*", "1.2.3")


class Raise(unittest.TestCase):
    def test_only_the_requirements_that_reject_main_are_raised_and_the_feature_is_on(self):
        versions = {"alice-db": "0.6.0", "alice-zip": "0.11.0", "alice-sdf": "6.1.0", "alice-edge": "0.4.0"}
        out, changed = mm.raise_requirements(MANIFEST, versions)
        self.assertEqual(changed, ['alice-db: "0.5" -> "0.6.0"', 'alice-zip: "0.10" -> "0.11.0"',
                                   'alice-edge: ">=0.2, <0.4" -> "0.4.0"'])
        self.assertIn('alice-db = "0.6.0"\n', out)
        self.assertIn('alice-sdf = "6"\n', out)  # 6.1.0 accepted: left as it is
        self.assertIn('alice-edge = { version = "0.4.0", default-features = false, features = ["std", "law"] }', out)
        self.assertIn('serde = "1"\n', out)  # not a core crate
        self.assertIn('[features]\ndefault = ["main-mode"]\nmain-mode = []', out)

    def test_nothing_to_raise_still_turns_the_feature_on(self):
        out, changed = mm.raise_requirements(MANIFEST, {"alice-db": "0.5.1"})
        self.assertEqual(changed, [])
        self.assertIn('default = ["main-mode"]', out)

    def test_a_manifest_without_the_feature_is_refused(self):
        with self.assertRaises(ValueError):
            mm.raise_requirements(MANIFEST.replace("main-mode = []\n", ""), {"alice-db": "0.6.0"})


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def package(name: str, version: str = "1.0.0", extra: str = "") -> str:
    return f'[package]\nname = "{name}"\nversion = "{version}"\n{extra}'


def checkouts(d: Path, physics: str = "split") -> None:
    """every core repository side by side: the single-package ones at their root, ALICE-LOL a
    virtual workspace, ALICE-Physics either one package at the root ("one") or the core at the
    root with the alice-physics facade as a member ("split")"""
    for crate, repo in mm.CRATES.items():
        if repo not in ("ALICE-LOL", "ALICE-Physics"):
            write(d / repo / "Cargo.toml", package(crate))
    write(d / "ALICE-LOL" / "Cargo.toml", '[workspace]\nmembers = ["alice-lol-macro", "alice-lol", "tools/*"]\n')
    write(d / "ALICE-LOL" / "alice-lol" / "Cargo.toml", package("alice-lol", "0.9.0"))
    write(d / "ALICE-LOL" / "alice-lol-macro" / "Cargo.toml", package("alice-lol-macro", "0.9.0"))
    write(d / "ALICE-LOL" / "tools" / "ui" / "Cargo.toml", package("alice-lol-ui"))
    if physics == "one":
        write(d / "ALICE-Physics" / "Cargo.toml", package("alice-physics", "3.1.0"))
    else:
        write(d / "ALICE-Physics" / "Cargo.toml", package(
            "alice-physics-core", "4.0.0", '\n[workspace]\nmembers = [".", "crates/alice-physics", "crates/ffi"]\n'))
        write(d / "ALICE-Physics" / "crates" / "alice-physics" / "Cargo.toml", package("alice-physics", "4.0.0"))
        write(d / "ALICE-Physics" / "crates" / "ffi" / "Cargo.toml", package("alice-physics-ffi", "4.0.0"))


class CrateDir(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def test_a_package_at_the_root_is_found_there(self):
        checkouts(self.d, physics="one")
        self.assertEqual(mm.crate_dir(self.d / "ALICE-Physics", "alice-physics"), self.d / "ALICE-Physics")

    def test_a_crate_moved_from_the_root_to_a_member_is_found_by_its_name(self):
        # the root package is renamed (alice-physics-core) and alice-physics becomes a member: the root
        # is not the crate any more, and the other member whose name starts the same is not it either
        checkouts(self.d)
        self.assertEqual(mm.crate_dir(self.d / "ALICE-Physics", "alice-physics"),
                         self.d / "ALICE-Physics" / "crates" / "alice-physics")

    def test_the_root_package_of_a_workspace_is_a_candidate(self):
        checkouts(self.d)
        self.assertEqual(mm.crate_dir(self.d / "ALICE-Physics", "alice-physics-core"), self.d / "ALICE-Physics")

    def test_the_root_listed_as_a_member_too_is_one_package(self):
        # members = [".", "crates/*"]: the root is listed and globbed members sit beside it; "." is the
        # root package, not a second one
        checkouts(self.d)
        write(self.d / "ALICE-Physics" / "Cargo.toml", package(
            "alice-physics-core", "4.0.0", '\n[workspace]\nmembers = [".", "crates/*"]\n'))
        phys = self.d / "ALICE-Physics"
        self.assertEqual(mm.crate_dir(phys, "alice-physics-core"), phys)
        self.assertEqual(mm.crate_dir(phys, "alice-physics"), phys / "crates" / "alice-physics")

    def test_two_packages_with_the_name_are_refused(self):
        checkouts(self.d)
        write(self.d / "ALICE-Physics" / "crates" / "ffi" / "Cargo.toml", package("alice-physics"))
        with self.assertRaises(LookupError) as e:
            mm.crate_dir(self.d / "ALICE-Physics", "alice-physics")
        self.assertIn("2 packages named alice-physics", str(e.exception))

    def test_members_of_a_virtual_workspace_and_globbed_members(self):
        checkouts(self.d)
        lol = self.d / "ALICE-LOL"
        self.assertEqual(mm.crate_dir(lol, "alice-lol"), lol / "alice-lol")
        self.assertEqual(mm.crate_dir(lol, "alice-lol-macro"), lol / "alice-lol-macro")
        self.assertEqual(mm.crate_dir(lol, "alice-lol-ui"), lol / "tools" / "ui")

    def test_no_package_of_that_name_is_refused_naming_what_is_there(self):
        checkouts(self.d)
        with self.assertRaises(LookupError) as e:
            mm.crate_dir(self.d / "ALICE-Physics", "alice-physic")
        self.assertIn("alice-physics-core", str(e.exception))
        self.assertIn("alice-physics-ffi", str(e.exception))

    def test_an_excluded_member_is_not_read(self):
        checkouts(self.d)
        write(self.d / "ALICE-LOL" / "Cargo.toml",
              '[workspace]\nmembers = ["alice-lol-macro", "alice-lol", "tools/*"]\nexclude = ["tools/ui"]\n')
        with self.assertRaises(LookupError):
            mm.crate_dir(self.d / "ALICE-LOL", "alice-lol-ui")

    def test_a_member_manifest_without_a_package_is_refused_as_a_lookup(self):
        checkouts(self.d)
        write(self.d / "ALICE-Physics" / "crates" / "ffi" / "Cargo.toml", "[lib]\n")
        with self.assertRaises(LookupError) as e:  # not a bare KeyError from the missing table
            mm.crate_dir(self.d / "ALICE-Physics", "alice-physics")
        self.assertIn("crates/ffi/Cargo.toml has no [package] name", str(e.exception))

    def test_a_listed_member_without_a_manifest_is_refused(self):
        checkouts(self.d)
        (self.d / "ALICE-Physics" / "crates" / "ffi" / "Cargo.toml").unlink()
        with self.assertRaises(LookupError):
            mm.crate_dir(self.d / "ALICE-Physics", "alice-physics")


def every_core_crate_from(declared: str | None = REGISTRY) -> list[tuple[str, str, str | None, str | None]]:
    """the e2e crate requiring every core crate from `declared`, each resolved from its checkout"""
    return [("alice-law-e2e", c, declared, None) for c in mm.CRATES]


class Patch(unittest.TestCase):
    def run_patch(self, physics: str, change=None, graphs=None) -> tuple[int, str, str]:
        # the e2e paths point into a temporary directory: a patch must never write the repository's own
        import contextlib
        import io
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        checkouts(d / "core", physics)
        if change:
            change(d / "core")
        for name in ("ROOT", "E2E", "CONFIG"):
            self.addCleanup(setattr, mm, name, getattr(mm, name))
        mm.ROOT, mm.E2E, mm.CONFIG = d, d / "e2e", d / "e2e" / ".cargo" / "config.toml"
        write(mm.E2E / "Cargo.toml", MANIFEST)
        # the resolved graph after each written patch: a function of the config text, so a git entry
        # appears in the graph only once the patch for it is written (cargo is not run here)
        graphs = graphs or (lambda config: graph(every_core_crate_from()))
        self.addCleanup(setattr, mm, "metadata", mm.metadata)
        mm.metadata = lambda: graphs(mm.CONFIG.read_text(encoding="utf-8"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = mm.patch(d / "core")
        config = mm.CONFIG.read_text(encoding="utf-8") if mm.CONFIG.is_file() else ""
        return code, config, err.getvalue()

    def test_the_facade_member_is_patched_after_the_rename(self):
        code, config, err = self.run_patch("split")
        self.assertEqual(code, 0, err)
        line = next(x for x in config.splitlines() if x.startswith("alice-physics ="))
        self.assertTrue(line.endswith('ALICE-Physics/crates/alice-physics" }'), line)
        self.assertEqual(sum(x.split(" = ")[0] in mm.CRATES for x in config.splitlines()), len(mm.CRATES))

    def test_a_git_source_of_a_core_repository_is_patched_too(self):
        # a consumer on main names alice-det-math by a git commit: Cargo applies [patch.crates-io] only to
        # crates.io requirements, so without an entry under the repository's URL that copy is linked
        # beside the checkout (two alice-det-math in one build)
        def graphs(config: str) -> dict:
            patched = f'[patch."{DETMATH_URL}"]' in config
            return graph(every_core_crate_from() + [("alice-db", "alice-det-math", DETMATH_GIT,
                                                     None if patched else DETMATH_GIT)])
        code, config, err = self.run_patch("split", graphs=graphs)
        self.assertEqual(code, 0, err)
        table = config.split(f'[patch."{DETMATH_URL}"]\n', 1)
        self.assertEqual(len(table), 2, config)
        self.assertTrue(table[1].startswith('alice-det-math = { path = "'), table[1])
        self.assertTrue(table[1].splitlines()[0].endswith('ALICE-DetMath" }'), table[1])

    def test_only_the_sources_the_graph_uses_get_an_entry(self):
        # no requirement names a crate from crates.io or by git: no entry for it (an entry nothing uses
        # is a finding of verify, so the patch writes none)
        def graphs(config: str) -> dict:
            return graph([e for e in every_core_crate_from() if e[1] != "alice-edge"]
                         + [("alice-lol", "alice-edge", None, None)])
        code, config, err = self.run_patch("split", graphs=graphs)
        self.assertEqual(code, 0, err)
        self.assertNotIn("alice-edge =", config)
        self.assertNotIn("[patch.\"https://", config)

    def test_a_source_that_keeps_appearing_stops_the_patch(self):
        # each pass names a git source the last one did not: no fixed point, so the patch refuses
        # instead of writing a config the graph does not match
        passes = []

        def graphs(config: str) -> dict:
            passes.append(config)
            return graph(every_core_crate_from() + [
                ("alice-db", "alice-det-math", f"git+{DETMATH_URL}-{len(passes)}?rev=1#1", None)])
        code, config, err = self.run_patch("split", graphs=graphs)
        self.assertEqual(code, 2)
        self.assertIn("no fixed point", err)
        self.assertEqual(config, "")  # no config the graph does not match is left for the build

    def test_the_root_package_is_patched_before_the_rename(self):
        code, config, err = self.run_patch("one")
        self.assertEqual(code, 0, err)
        self.assertIn('ALICE-Physics" }', next(x for x in config.splitlines() if x.startswith("alice-physics =")))

    def test_a_crate_two_packages_name_fails_the_patch(self):
        def twin(core: Path) -> None:
            write(core / "ALICE-Physics" / "crates" / "ffi" / "Cargo.toml", package("alice-physics"))
        code, config, err = self.run_patch("split", twin)
        self.assertEqual(code, 2)
        self.assertEqual(config, "")
        self.assertIn("2 packages named alice-physics", err)

    def test_a_crate_no_member_names_fails_the_patch(self):
        def rename(core: Path) -> None:
            write(core / "ALICE-Physics" / "crates" / "alice-physics" / "Cargo.toml", package("alice-physics-facade"))
        code, config, err = self.run_patch("split", rename)
        self.assertEqual(code, 2)
        self.assertEqual(config, "")  # nothing written for the crates before or after it
        self.assertIn("0 packages named alice-physics", err)


class Problems(unittest.TestCase):
    def all_from_main(self) -> list[dict]:
        return [pkg(c, "1.0.0", None) for c in mm.CRATES]

    def test_every_core_crate_from_its_checkout_is_clean(self):
        _, problems, compared = mm.problems_of(self.all_from_main())
        self.assertEqual(problems, [])
        self.assertEqual(compared, len(mm.CRATES))

    def test_a_member_rejecting_another_members_new_major_stays_red(self):
        # alice-db main still requires alice-zip 0.10 while alice-zip main is 0.11: the patch is used
        # for the e2e crate's own (raised) requirement and the registry release comes in for alice-db
        packages = self.all_from_main() + [pkg("alice-zip", "0.10.0", REGISTRY)]
        _, problems, _ = mm.problems_of(packages)
        self.assertEqual(problems, [f"alice-zip 0.10.0: from {REGISTRY}, not from main"])

    def test_a_core_crate_never_resolved_from_main_is_a_problem(self):
        packages = [p for p in self.all_from_main() if p["name"] != "alice-edge"]
        _, problems, _ = mm.problems_of(packages)
        self.assertEqual(problems, ["alice-edge: not resolved from main (0 times)"])
        packages.append(pkg("alice-edge", "0.3.0", REGISTRY))
        _, problems, _ = mm.problems_of(packages)
        self.assertIn("alice-edge: not resolved from main (0 times)", problems)
        self.assertIn(f"alice-edge 0.3.0: from {REGISTRY}, not from main", problems)

    def test_no_core_crate_in_the_graph_compares_nothing(self):
        _, problems, compared = mm.problems_of([pkg("serde", "1.0.0", REGISTRY)])
        self.assertEqual(compared, 0)
        self.assertEqual(len(problems), len(mm.CRATES))


class Requirers(unittest.TestCase):
    def test_the_crates_that_do_not_accept_main_are_named(self):
        def dep(name, req):
            return {"name": name, "req": req, "kind": None}
        packages = [
            {"name": "alice-db", "dependencies": [dep("alice-zip", "^0.10")]},
            {"name": "alice-edge", "dependencies": [dep("alice-zip", "^0.10")]},
            {"name": "alice-lol", "dependencies": [dep("alice-zip", ">=0.10, <0.12")]},
            {"name": "alice-sdf", "dependencies": [dep("alice-det-math", "^0.5")]},
        ]
        self.assertEqual(mm.requirers(packages, "alice-zip", "0.11.0"), ["alice-db (^0.10)", "alice-edge (^0.10)"])
        self.assertEqual(mm.requirers(packages, "alice-zip", "0.10.3"), [])


class Entries(unittest.TestCase):
    """which patch entries the resolved graph used: an entry is (crate, source), the source "crates-io"
    or a git URL, and it is used when a requirement naming that crate from that source resolved to the
    checkout (a path package)"""

    def test_a_requirement_resolved_to_the_checkout_uses_its_entry(self):
        meta = graph([("alice-law-e2e", "alice-zip", REGISTRY, None),
                      ("alice-db", "alice-det-math", DETMATH_GIT, None)])
        self.assertEqual(mm.used_entries(meta), {("alice-zip", "crates-io"), ("alice-det-math", DETMATH_URL)})

    def test_a_requirement_resolved_elsewhere_uses_no_entry(self):
        # the git patch dropped (or its version not accepted): the git commit is linked, not main
        meta = graph([("alice-db", "alice-det-math", DETMATH_GIT, DETMATH_GIT)])
        self.assertEqual(mm.used_entries(meta), set())
        self.assertEqual(mm.sources_named(meta), {("alice-det-math", DETMATH_URL)})

    def test_path_requirements_and_other_crates_are_not_entries(self):
        meta = graph([("alice-lol", "alice-sdf", None, None), ("alice-law-e2e", "serde", REGISTRY, REGISTRY)])
        self.assertEqual(mm.used_entries(meta), set())
        self.assertEqual(mm.sources_named(meta), set())

    def test_an_unused_entry_is_a_finding_and_none_used_fails(self):
        meta = graph([("alice-db", "alice-det-math", DETMATH_GIT, DETMATH_GIT)])
        written = {("alice-det-math", DETMATH_URL), ("alice-det-math", "crates-io")}
        problems, used = mm.entry_problems(written, meta)
        self.assertEqual(used, 0)
        self.assertEqual(problems, ["patch entry alice-det-math from crates-io: not used",
                                    f"patch entry alice-det-math from {DETMATH_URL}: not used"])
        problems, used = mm.entry_problems({("alice-det-math", DETMATH_URL)},
                                           graph([("alice-db", "alice-det-math", DETMATH_GIT, None)]))
        self.assertEqual((problems, used), ([], 1))

    def test_the_written_entries_are_read_back_from_the_config(self):
        text = ('[patch.crates-io]\nalice-zip = { path = "/c/ALICE-Zip" }\n\n'
                f'[patch."{DETMATH_URL}"]\nalice-det-math = {{ path = "/c/ALICE-DetMath" }}\n')
        self.assertEqual(mm.written_entries(text), {("alice-zip", "crates-io"), ("alice-det-math", DETMATH_URL)})


class Verify(unittest.TestCase):
    def run_verify(self, config: str, meta: dict) -> tuple[int, str]:
        """verify() over a written config and a resolved graph (cargo is not run), its exit code and output"""
        import contextlib
        import io
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name in ("ROOT", "CONFIG", "metadata"):
            self.addCleanup(setattr, mm, name, getattr(mm, name))
        mm.ROOT = Path(tmp.name)
        mm.CONFIG = mm.ROOT / "e2e" / ".cargo" / "config.toml"
        write(mm.CONFIG, config)
        mm.metadata = lambda: meta
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = mm.verify()
        return code, out.getvalue()

    def config(self, git: bool) -> str:
        text = "[patch.crates-io]\n" + "".join(f'{c} = {{ path = "/c/{c}" }}\n' for c in mm.CRATES)
        if git:
            text += f'\n[patch."{DETMATH_URL}"]\nalice-det-math = {{ path = "/c/alice-det-math" }}\n'
        return text

    def test_every_entry_used_and_every_crate_from_main_passes(self):
        code, out = self.run_verify(self.config(git=True), graph(
            every_core_crate_from() + [("alice-db", "alice-det-math", DETMATH_GIT, None)]))
        self.assertEqual(code, 0, out)
        self.assertIn(f"{len(mm.CRATES) + 1} patch entries used, 0 problem(s)", out)

    def test_a_written_entry_the_graph_does_not_use_fails_and_is_named(self):
        # the git entry is written but no requirement names the commit any more
        code, out = self.run_verify(self.config(git=True), graph(every_core_crate_from()))
        self.assertEqual(code, 1, out)
        self.assertIn(f"patch entry alice-det-math from {DETMATH_URL}: not used", out)

    def test_no_entry_used_fails_with_exit_2(self):
        # every requirement resolved elsewhere: the checkouts were never taken
        code, out = self.run_verify(self.config(git=False), graph(
            [("alice-law-e2e", c, REGISTRY, REGISTRY) for c in mm.CRATES]))
        self.assertEqual(code, 2, out)
        self.assertIn("no patch entry was used", out)

    def test_without_the_patch_it_says_so_instead_of_reading_every_crate_as_from_crates_io(self):
        import contextlib
        import io
        import tempfile
        saved = mm.CONFIG
        with tempfile.TemporaryDirectory() as d:
            mm.CONFIG = Path(d) / ".cargo" / "config.toml"
            self.addCleanup(setattr, mm, "CONFIG", saved)
            mm_root = mm.ROOT
            mm.ROOT = Path(d)
            self.addCleanup(setattr, mm, "ROOT", mm_root)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = mm.verify()
        self.assertEqual(code, 2)
        self.assertIn("the patch did not run", err.getvalue())


class Packages(unittest.TestCase):
    def setUp(self):
        saved = (mm.E2E, mm.CONFIG, mm.CRATES)
        self.addCleanup(lambda: (setattr(mm, "E2E", saved[0]), setattr(mm, "CONFIG", saved[1]),
                                 setattr(mm, "CRATES", saved[2])))

    def test_the_trust_package_has_its_own_directory_config_and_crates(self):
        mm.select("e2e-trust")
        self.assertEqual(mm.E2E, mm.ROOT / "e2e-trust")
        self.assertEqual(mm.CONFIG, mm.ROOT / "e2e-trust" / ".cargo" / "config.toml")
        for crate in ("alice-identity", "alice-sign", "alice-channel", "alice-sync", "alice-lol"):
            self.assertIn(crate, mm.CRATES)
        self.assertNotIn("alice-physics", mm.CRATES)

    def test_the_default_package_is_the_round_trip(self):
        self.assertEqual(mm.CRATES, {c: mm.REPOSITORY[c] for c in mm.PACKAGES["e2e"]})
        self.assertNotIn("alice-sync", mm.CRATES)

    def test_every_package_crate_has_a_repository(self):
        for name, crates in mm.PACKAGES.items():
            self.assertGreater(len(crates), 0, name)
            self.assertEqual([c for c in crates if c not in mm.REPOSITORY], [], name)

    def test_an_unknown_package_is_refused(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(mm.main(["main_mode.py", "--package", "nope", "verify"]), 2)
        self.assertIn("no end to end package 'nope'", err.getvalue())


if __name__ == "__main__":
    unittest.main()
