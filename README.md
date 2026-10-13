# ALICE-Law

Integration checks across the crates in the release train (`release-train.toml`): ALICE-LOL, ALICE-DetMath, ALICE-SDF, ALICE-Physics, ALICE-DB, ALICE-Zip, ALICE-Crypto, ALICE-Analytics, ALICE-Edge and ALICE-ML. `scripts/readme_roster_check.py` fails this file's own check if this sentence falls behind the roster.

Everything here is built from the versions published on crates.io, with no path dependencies. Two kinds of check:

- dependency consistency (in place): every `alice-*` crate resolves to exactly one version in `Cargo.lock`, every crate `scripts/dep_consistency.py`'s own `CORE` list names is present, and each `alice-*` requirement accepts the latest published version of the crate it names
- end to end: read a law, compute, save, restore, and compare every value bit for bit

It also holds the release train of the core crates (the plan, and the scripts run before and after each publish: [runbook](#runbook-one-crate)) and the checks of the text this repository publishes.

Contents: [Dependency consistency](#dependency-consistency) · [Release train](#release-train) · [Public text](#public-text) · [Conformance](#conformance) · [End to end](#end-to-end) · [Steps that fail until a release](#steps-that-fail-until-a-release) · [Status](#status) · [License](#license)

## Dependency consistency

`Cargo.toml` depends on the latest release of each core crate, with the features that turn on their dependencies on each other, so the lock file shows every link between them.

```sh
cargo generate-lockfile                 # resolve against crates.io as it is today
python3 scripts/dep_consistency.py      # Markdown report; exit 1 on a problem, 2 when a check compared nothing
```

The checks:

1. one version per crate: two versions of one crate in a lock file are two copies of its types and its arithmetic, and a value saved through one is not the same type as the value the other reads back
2. presence: every crate `CORE` names is in the lock file
3. requirement vs latest: the `alice-*` requirements of this repository and of every core crate at the version the lock file resolved must accept the latest release of the crate they name (the latest stable release, or the latest pre-release when a crate has no stable one). A requirement that does not is how a second version gets into a user's lock file

A problem that a release still to come removes is listed in `release-train.toml` under `[pending.<name>]`, with that release (`until = "<crate> <version>"`) and the problems it removes (`linked = [<crate>, ...]`, `requires = ["<crate> <version> -> <dependency> <requirement>", ...]`). With `--roster release-train.toml` (as CI runs it) the report counts them as expected per entry. A problem that is not listed fails the run, as does an entry whose release the roster marks `published` (stale: remove it and raise the requirements that release allows) and a listed item that matches no problem (out of date). This repository's own requirements and a missing core crate cannot be listed: they are raised or added, not waited for. So a red run is a problem to fix, not a release still to come.

Each check reports how many comparisons it made, and a check that made none fails the run (exit 2), as does a dependency of this repository on an `alice-*` crate by path or git. This repository's own package is not one of the crates checked.

The requirement matching follows Cargo's rules (carets, tildes, ranges, wildcards such as `1.2.*` and `1.x`, pre-releases). `scripts/semver_differential.py` compares it with the [semver](https://crates.io/crates/semver) crate (`tools/semver-oracle`) on generated requirement / version pairs and on a fixed grid of every operator and requirement shape against the versions one step away, and fails on any disagreement, including a requirement that one of the two reads and the other does not.

CI (`.github/workflows/dependency-consistency.yml`) runs the tests, the comparison with the semver crate and the check every Monday, on demand, and on a push that changes the check. The report is in the job summary.

## Release train

`release-train.toml` is the plan for releasing the core crates together: for each crate, the version it is being released at, its place in the order (a crate comes after everything it requires), its state (`planned`, `prepared-local`, `prepared`, `published`, or `exempt` for this repository), its repository and, where the repository holds more than one crate, its manifest path and tag name (`tag`, default `v{version}`). `[train] owners` lists the crates.io owners every release must have. What a crate requires of the others is not listed there: it is read from the crate's own `Cargo.toml` at the release checkout, so there is one source for it. A crate can also list `must-contain` (commits the published commit must include), `changelog-has` (lines its `## [<version>]` CHANGELOG section must contain) `must-contain-patch` (`git patch-id --stable` ids of changes the published commit must carry, as the change or a rebased copy, among its last 2000 non-merge commits) and `required-ci` (`{ workflow, job, legs, since }`; an entry with `since` applies from that version of the crate on: in the latest GitHub Actions run of that workflow on the published commit, the job's legs `<job>` / `<job> (...)` must be exactly the expected ones, a count or the exact names, and all completed with success; a missing or extra leg, or one running, skipped or failed, refuses, and a CI that cannot be read is a refusal too).

### Runbook: one crate

```sh
S=scripts/release_train.py
python3 $S next                                                      # 1. the crate to publish now
git clone <its repository> /tmp/rel && git -C /tmp/rel checkout <sha> # 2. a clean checkout of the release commit
python3 $S publish-gate <crate> --repo /tmp/rel --ref <sha>          # 3. exit 0 = go; prints the .crate sha256 and the publish command
(cd /tmp/rel && cargo publish --locked --manifest-path Cargo.toml)    # 4. the command the gate printed, run by a person
git -C /tmp/rel tag -a <tag> <sha> -m "<crate> <version>" && git -C /tmp/rel push origin <tag>   # 5. the tag at the published commit
python3 $S roster-mark published <crate> --repo /tmp/rel --ref <sha> --expect-sha256 <from 3>   # 6. verify, then mark the roster
python3 $S followups <crate> --repos-root <dir with every roster checkout>                      # 7. who has to raise a requirement
```

Then commit `release-train.toml`, and land the requirement bumps that `followups` lists (the next crate in the order is one of them).

1. `next` names the first crate in the order that is not published.
2. `cargo publish` packages the working tree, so the gate runs on a clone at the commit, not on a working copy.
3. `publish-gate <crate> --repo <checkout> --ref <sha> [--manifest-path <path>]` reads the crate's manifest (and the repository's root `Cargo.toml`) at that commit, and refuses:
   - when the checkout is not that commit, or has changes to tracked files;
   - when the crate is not `prepared`, or the manifest is another crate or version than the roster's;
   - when a manifest overrides an `alice-*` crate (`[patch.*]`, `[replace]`) or takes one from another registry;
   - when that version is already on crates.io;
   - when an `alice-*` dependency of the manifest (normal or build, optional or not, any target, renamed or not, `-` or `_` in the name) has no crates.io version requirement, or names a crate that is not `published` in the roster, is missing or yanked on crates.io, or is not accepted by the requirement (Cargo's rules: `"0.4"` does not accept 0.5.0). A dependency outside the roster is checked against its latest release;
   - when a `must-contain` commit is not an ancestor of the commit, or the CHANGELOG has no `## [<version>]` section with every `changelog-has` line;
   - when no recent commit carries a `must-contain-patch` patch;
   - when a `required-ci` job does not have exactly its expected legs in the latest run of its workflow on the commit, or a leg is not completed with success;
   - when the package (`cargo package --list`) carries files under `.github/`, `scripts/` or `docs/`, or private notes (file and directory names held as sha256 in the script): the repository's tools and notes, not the crate. Set `include` in the manifest.

   On go it runs `cargo package` (which builds the packaged crate as `cargo publish` does, `--locked` when a `Cargo.lock` is committed) and prints the commit, the sha256 of the `.crate` and the exact `cargo publish` command. Before go it also builds that `.crate` as a crates.io consumer does: unpacked into a fresh directory with no `Cargo.lock`, `Cargo.toml` or `.cargo/config` above it, with an empty `CARGO_HOME` (its registry cache is the gate's own directory) and a minimal environment (no `RUSTFLAGS`, `RUSTC_WRAPPER` or `CARGO_*` of the machine), as the path dependency of an empty crate, once with the default features, once with none, and once with every feature (each feature alone when they cannot all be on together) or with the roster's `consumer-features` sets instead (with `consumer-features-why`, the features left out and why: features built by another tool, such as a Python extension, or for one platform's hardware); a crate with no library is built on its own. A failed build, or one that compiled no library (or binary) of the crate, is refused (`cargo package` verifies inside the repository, where a build script reading a lock above the crate still finds the repository's). With a committed `Cargo.lock` the `.crate` is the same bytes as the one `cargo publish` uploads; without one, cargo resolves the lock it puts in the package at that moment, so publish right after the gate.
4. Publishing is the one step the scripts do not take.
5. `publish-verify` reads the tag from the remote (`--remote`, default `origin`).
6. `roster-mark published` runs `publish-verify` and changes the crate's `state = "prepared"` line to `published` only when every check passed. `publish-verify` (the same checks without the edit) refuses:
   - when the version is not on crates.io, or is yanked;
   - when its checksum is not the sha256 the gate printed;
   - when its license is not the manifest's, or its owners are not `[train] owners`;
   - when the downloaded `.crate` does not hash to that checksum, or its `.cargo_vcs_info.json` names another commit, a dirty checkout or another directory;
   - when the tag is missing on the remote or names another commit.

   It also prints the docs.rs build status, as information (docs.rs builds after the publish).
7. `followups <crate> --repos-root <dir> [--at origin/main]` reads every `Cargo.toml` at `--at` in the checkouts of every roster repository (fetch first) and lists each requirement on the crate (normal, dev, build, target, workspace) that does not accept its roster version, and each one without a version outside the crate's own repository. Exit 1 while there is one to raise.

Only an HTTP 404 counts as "not on crates.io"; any other answer (an error body, another version than the one asked for, a broken connection) stops a command with exit 2, as does a command that compared nothing (no roster crate, no package file, no manifest read, no `[train] owners`). The requirement matching is the one in `scripts/dep_consistency.py`. CI runs the tests (`scripts/test_release_train.py`, crates.io, cargo and the remote replaced by fixtures) and prints `next`.

## Public text

This repository is public, and so is everything it publishes: file contents, paths and commit messages. `scripts/public_text_check.py` checks them for internal vocabulary (development-process words, internal tracker and note names, instruction sources, device names, private network addresses, private names held only as SHA-256 hashes, and id-like tokens (two hex digits) next to a word naming a review or a session (`レビュー`, `review`, `指摘`, `検証`, `session` / `セッション` before or after), and the labels of review findings, an F and a number named as a review; no id is listed, since a list of two-digit ids is public even when hashed), with the vocabulary of ALICE-Physics `scripts/docs_lint.py`. A finding names the place and the kind of word, not the word.

```sh
python3 scripts/public_text_check.py --tree                     # every tracked file at HEAD
python3 scripts/public_text_check.py --range origin/main..HEAD  # the messages, added lines and added paths of the commits
scripts/preflight.sh                                            # the script tests and both scans, before a push
```

CI (`.github/workflows/public-text.yml`) runs the tests and both scans on every push and pull request. A scan that compared nothing (no file, no commit) fails.

## Conformance

`docs/CONFORMANCE.md` is the human-readable specification of what a Law-bearing crate's arithmetic must guarantee — 14 gates, from banning a direct call to a platform transcendental function (G1) through banning a build flag that changes floating-point semantics (G14). `conformance/gates.toml` is the same 14 gates in the form a generator reads: id, name, one-line definition, and the kinds of evidence a passing crate must cite.

Each crate in the release train carries its own `alice-law-conformance.toml` at its repository root, one entry per gate (`status`: `pass` / `missing` / `n/a`, `evidence`, `since`, `note`, `reference_repo`); this repository does not hold those manifests itself, only the gates they answer against.

## End to end

`e2e/` is a separate crate with one test, `tests/law_to_storage_roundtrip.rs`, that goes through the core crates with their public APIs:

1. read: Newton's law of cooling, written as text, through the alice-lol research law reader, evaluated on a time grid
2. compute: a thermal field law checked by alice-lol (solved by alice-physics), a rod cooling with `alice_physics::transient_thermal`, two bodies falling in a `PhysicsWorld`
3. fit: an alice-zip `SignalLaw` to the cooling curve, and its residual summary from alice-analytics
4. save: every value through the exact series of alice-db (`AliceDB::series`, not the fitted model store) and the law through its law store, to a directory
5. restore: reopen the directory and read everything back
6. compare: every value by `to_bits`, the law coefficients, domain, record and identifier, every field of the residual summary, and the alice-crypto digest of the whole run

It also checks that the crates were built with one arithmetic (the `SEMANTICS_ID` of alice-det-math as alice-zip, alice-db, alice-analytics and alice-lol see it), that every `alice-*` crate is locked at one version, and that the eight core crates it names are in the build (`e2e/tests/law_to_storage_roundtrip.rs`; not derived from the roster, so a repository joining the train does not reach this check on its own). Each comparison fails when it compared nothing. A golden digest of the computed run (`e2e/golden/run.digest`) says whether the crates still compute what they computed when it was recorded; the round trip says that saving and restoring loses nothing.

```sh
cd e2e
cargo test --release -- --nocapture             # published releases (resolved against crates.io)
ALICE_LAW_E2E_BLESS=1 cargo test --release      # record the golden digest again
```

Two modes run the same test:

| mode | crates | workflow | meaning |
|---|---|---|---|
| published | crates.io releases | `e2e.yml` | the gate: red until every crate it needs is released at versions that fit together |
| main | the main branches, side by side, patched in by `scripts/main_mode.py` | `e2e-main.yml` | early warning: what the next releases would change, and which crate on main is not taken because its version does not fit the others |

`scripts/e2e_teeth.py` (main mode, the teeth job of `e2e-main.yml`) changes one line at a time in the checkouts and requires the test to fail with the expected message: a law coefficient decoded one ulp off, an f64 saved through f32, a restore that returns nothing, a flipped digest byte, `exp` from the platform libm, a slightly longer thermal step, a different arithmetic identifier, a residual one ulp off, and a second version of alice-zip in the build.

## Steps that fail until a release

A CI step that cannot pass before a release (the end to end crate resolving alice-lol 0.4 from crates.io before it is published, or the main branches patched in together while alice-lol on main still requires the previous alice-physics) is wrapped in `scripts/expected_failure.py <step> -- <command>` and listed in `release-train.toml` under `[expected-failure.<name>]` with the release it waits for (`until`), the text of the error lines its failure has (`expect`) and why (`why`). The failure is read from its own error lines (after terminal colour codes are removed, a line starting with `error` in any case followed by `:`, `[...]`, a space or the end of the line, `fatal:`, or a Rust panic; an entry can also name the exit status with `exit`): every one must contain an `expect` text and every `expect` text must be in one, so a second, unrelated error or a log line that only quotes the text does not pass. It is then reported as expected and the steps that need it are skipped; every run lists the active entries (step, until, why) in the job summary, and `expected_failure.py --check` fails on an entry no workflow runs and on an entry wrapping a `cargo` command without `exit = 101`. The step is red when it fails another way, when it still fails after its release is `published` in the roster, and when it passes while it is still listed (remove the entry). So a red run is a problem to fix.

## Status

Measured on 2026-10-13 (the committed `Cargo.lock`): **the check passes with 7 expected problems**, in one pending entry (alice-lol 0.4.0: 7). alice-physics 3.0.0, alice-sdf 6.0.0 and alice-edge 0.2.0 are published, and this repository's own requirements accept every latest release (alice-det-math 0.5, alice-zip 0.10, alice-analytics 0.5, alice-db 0.4, alice-crypto 0.4, alice-physics 3, alice-sdf 6, alice-edge 0.2 with its sdf, db and dashboard features); what remains comes from the published alice-lol 0.3.0. The end to end test does not exercise alice-edge yet.

Linked at two or more versions:

| crate | versions | brought in by |
|---|---|---|
| alice-physics | 0.14.0-preview.8, 3.0.0 | alice-lol 0.3.0 (`^0.14.0-preview.4`, optional) |
| alice-sdf | 1.13.0, 6.0.0 | alice-lol 0.3.0 (`^1.9.0`) |

The release that would remove them: alice-lol 0.4.0 (alice-sdf 6, alice-physics 3, alice-lol-macro 0.3); then this repository's alice-lol requirement is raised to it.

End to end, measured on 2026-10-12:

- published: **expected failure until alice-lol 0.4.0** (`expected-failure.e2e-published-lol-0-4`): fails to resolve, alice-lol 0.4 (the first release with the research law reader the test uses) is not on crates.io yet. A green after alice-lol 0.4 is released covers less than the planned check: alice-physics stays at 2.2.0, used with `std` only (the thermal step and the rigid body world), and its metrics sink into alice-db is not part of the test until alice-physics 3.0.0, whose sink writes to the exact series of alice-db, is released
- main: **expected failure until alice-lol 0.4.0** (`expected-failure.e2e-main-lol-physics-3`), fails before the test: alice-lol on main requires `alice-physics ^2.0`, and alice-physics on main is 3.0.0, so the checkouts cannot be patched in together until alice-lol on main raises the requirement (the last passing run compared 3776 values and records, digest `2a0f3d06…`)

## License

`AGPL-3.0-or-later OR LicenseRef-Commercial`: dual-licensed, pick either.

| Option | Terms | Use it when |
|--------|-------|-------------|
| **AGPL-3.0-or-later** | [LICENSE-AGPL](LICENSE-AGPL), free | your project is itself AGPL-compatible open source, or you run the checks internally |
| **Commercial License** | [LICENSE-COMMERCIAL.md](LICENSE-COMMERCIAL.md), paid, removes the copyleft | closed-source products or pipelines, proprietary SaaS, redistribution in a proprietary tool |

AGPL is a strong copyleft: a product, pipeline or service that incorporates these
checks and is distributed or served to users must be released under the AGPL as well.
Of the core crates the end to end test crate links, three are under the same dual
licence: alice-physics, alice-db and alice-crypto. alice-lol, alice-zip and
alice-analytics are `Apache-2.0` and alice-sdf is `MIT OR Apache-2.0`; alice-det-math
is `MIT OR Apache-2.0` in the published 0.4.0 the test builds against, and
`Apache-2.0` from 0.5.0.

Commercial licence enquiries: <contact@extoria.co.jp>
