# Changelog

All notable changes to this repository are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This repository is not published as a crate (its package is `0.0.0`); its scripts and
workflows are the interface, so changes to what they check are recorded here.

## [Unreleased]

### Added
- `scripts/dep_consistency.py`: one version of every `alice-*` crate in `Cargo.lock`, all eight core crates present, and every `alice-*` requirement (this repository's and those of the resolved releases) accepting the latest release
- `scripts/dep_consistency.py --roster`: problems a coming release removes are listed in `release-train.toml` under `[pending.<name>]` (`until`, `linked`, `requires`) and reported as expected with a count; an unlisted problem, an entry whose release is published, and a listed item that matches nothing fail the check
- `release-train.toml` and `scripts/release_train.py`: the release plan of the core crates (version, order, state) and `next`
- `release_train.py publish-gate`: refuses a publish from a checkout that is not the commit, a crate that is not `prepared`, a manifest that overrides an `alice-*` crate or takes one from another registry, a version already on crates.io, and an `alice-*` requirement on a crate that is not published or not accepted; also `must-contain` commits, `must-contain-patch` patch ids (`git patch-id --stable`), `changelog-has` lines in the `## [<version>]` section, `required-ci` jobs (exactly the expected legs of the named workflow's latest run, all completed with success; an entry with `since` applies from that version on), and a package that carries `.github/`, `scripts/`, `docs/` or private notes; on go it builds the package with `cargo package` and prints its sha256 and the `cargo publish` command
- `release_train.py publish-verify` and `roster-mark published`: after a publish, crates.io's version, yank state, checksum (against the gate's sha256), license and owners, the downloaded `.crate`'s commit (`.cargo_vcs_info.json`) and the remote tag are compared with the published commit; the roster state changes to `published` only when that passes
- `release_train.py followups`: every `Cargo.toml` of the roster's repositories with a requirement on a crate that does not accept its roster version
- `scripts/expected_failure.py`: a CI step that fails until a release is listed under `[expected-failure.<name>]` (`step`, `until`, `expect`, `why`); only a failure whose own error lines (any case, colour codes removed) are all explained by the expected text, and whose exit status is the entry's `exit` when it names one, passes, and the steps that need it are skipped; every run lists the entries in the job summary and `--check` fails on an entry no workflow runs; the end to end workflows use it until alice-lol 0.4.0
- `scripts/public_text_check.py`: file contents, paths and commit messages checked for internal vocabulary (private names held as SHA-256 hashes, parts split at `-` `_` `.` too); run by `scripts/preflight.sh` before a push and by `.github/workflows/public-text.yml` on every push
- `e2e/`: read a law, compute, save, restore and compare every value bit for bit, against the published releases and against the main branches (`scripts/main_mode.py`), with mutations the test must catch (`scripts/e2e_teeth.py`)
- `docs/CONFORMANCE.md` and `conformance/gates.toml`: the 14 gates a Law-bearing crate's arithmetic must meet (G1 banning a direct platform transcendental call through G14 banning a build flag that changes floating-point semantics), in human-readable and generator-readable form; each gate's definition, the kinds of evidence a passing crate must cite, and the manifest shape (`status`/`evidence`/`since`/`note`/`reference_repo`) a crate's own `alice-law-conformance.toml` follows
- `scripts/readme_roster_check.py`: fails when README.md's intro paragraph (whole-name matched, not substring) falls behind `release-train.toml`'s roster
- `scripts/conformance_docs_schema_check.py`: fails when a `### G<n> — <name>` heading in `docs/CONFORMANCE.md` and the matching `[[gate]]` entry in `conformance/gates.toml` disagree in id, name, or count
- `release_train.py publish-gate`: the packaged `.crate` is unpacked into a directory with no `Cargo.lock` or `Cargo.toml` above it and built as the dependency of an empty crate (as a crates.io consumer builds it); a failed build or one that compiled no library of the crate is refused
- `release_train.py publish-gate`: the consumer build runs with an empty `CARGO_HOME` and a minimal environment, refuses a `.cargo/config` above the build directory, and builds the default features, no default features and every feature (or each one alone, or the roster's `consumer-features`); a crate with no library is built on its own
- `scripts/public_text_check.py`: an id-like token (two hex digits, not glued to a digit, letter, `#`, `_`, `-` or `.`) next to a word naming a review or a session, on either side, and the labels of review findings, in file contents and commit messages; no list of ids is held (two-digit ids are enumerable even hashed)

### Changed
- The dependency consistency check covers alice-edge (this repository requires alice-edge 0.1 with its sdf, db and dashboard features) and alice-lol-macro; the core crate list is the roster's library crates (`release-train.toml`, every crate not exempt and not marked `law-dependency = false`), held equal by a test
- This repository requires alice-det-math 0.5, alice-zip 0.10, alice-analytics 0.5, alice-db 0.4 and alice-physics 3, the latest releases
- `release-train.toml`: alice-physics 3.0.0 is published (its pending entry removed; the problems alice-sdf 5.1.1 still brings through alice-physics 2.2 are listed until alice-sdf 6.0.0)
- This repository requires alice-sdf 6; `release-train.toml`: alice-sdf 6.0.0 is published (its pending entry removed, 17 expected problems remain until alice-lol 0.4.0 and alice-edge 0.2.0)
- `release-train.toml`: alice-edge 0.2.0 lists its required commits, CHANGELOG lines and CI jobs; alice-ml is in the roster (not a dependency of this repository) so that its next publish is refused until it requires the published alice-det-math and alice-db (alice-edge's `ml` feature takes the published alice-ml 0.2.0)
- This repository requires alice-edge 0.2; `release-train.toml`: alice-edge 0.2.0 is published (its pending entry removed, 7 expected problems remain until alice-lol 0.4.0) and alice-ml is held at its published 0.2.0
