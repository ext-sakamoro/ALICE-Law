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
- `release_train.py publish-gate`: refuses a publish from a checkout that is not the commit, a crate that is not `prepared`, a manifest that overrides an `alice-*` crate or takes one from another registry, a version already on crates.io, and an `alice-*` requirement on a crate that is not published or not accepted; also `must-contain` commits, `must-contain-patch` patch ids (`git patch-id --stable`), `changelog-has` lines in the `## [<version>]` section, `required-ci` jobs (exactly the expected legs of the named workflow's latest run, all completed with success), and a package that carries `.github/`, `scripts/`, `docs/` or private notes; on go it builds the package with `cargo package` and prints its sha256 and the `cargo publish` command
- `release_train.py publish-verify` and `roster-mark published`: after a publish, crates.io's version, yank state, checksum (against the gate's sha256), license and owners, the downloaded `.crate`'s commit (`.cargo_vcs_info.json`) and the remote tag are compared with the published commit; the roster state changes to `published` only when that passes
- `release_train.py followups`: every `Cargo.toml` of the roster's repositories with a requirement on a crate that does not accept its roster version
- `scripts/expected_failure.py`: a CI step that fails until a release is listed under `[expected-failure.<name>]` (`step`, `until`, `expect`, `why`); only a failure whose own error lines are all explained by the expected text passes, and the steps that need it are skipped; every run lists the entries in the job summary and `--check` fails on an entry no workflow runs; the end to end workflows use it until alice-lol 0.4.0
- `scripts/public_text_check.py`: file contents, paths and commit messages checked for internal vocabulary (private names held as SHA-256 hashes, parts split at `-` `_` `.` too); run by `scripts/preflight.sh` before a push and by `.github/workflows/public-text.yml` on every push
- `e2e/`: read a law, compute, save, restore and compare every value bit for bit, against the published releases and against the main branches (`scripts/main_mode.py`), with mutations the test must catch (`scripts/e2e_teeth.py`)
- `docs/CONFORMANCE.md` and `conformance/gates.toml`: the 14 gates a Law-bearing crate's arithmetic must meet (G1 banning a direct platform transcendental call through G14 banning a build flag that changes floating-point semantics), in human-readable and generator-readable form; each gate's definition, the kinds of evidence a passing crate must cite, and the manifest shape (`status`/`evidence`/`since`/`note`/`reference_repo`) a crate's own `alice-law-conformance.toml` follows
- `scripts/readme_roster_check.py`: fails when this file's own crate list in README.md falls behind `release-train.toml`'s roster

### Changed
- This repository requires alice-det-math 0.5, alice-zip 0.10, alice-analytics 0.5 and alice-db 0.4, the latest releases
