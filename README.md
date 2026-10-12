# ALICE-Law

Integration checks across the core ALICE crates: ALICE-LOL, ALICE-DetMath, ALICE-SDF, ALICE-Physics, ALICE-DB, ALICE-Zip, ALICE-Crypto and ALICE-Analytics.

Everything here is built from the versions published on crates.io, with no path dependencies. Two kinds of check:

- dependency consistency (in place): every `alice-*` crate resolves to exactly one version in `Cargo.lock`, all eight core crates are present, and each `alice-*` requirement accepts the latest published version of the crate it names
- end to end: read a law, compute, save, restore, and compare every value bit for bit

## Dependency consistency

`Cargo.toml` depends on the latest release of each core crate, with the features that turn on their dependencies on each other, so the lock file shows every link between them.

```sh
cargo generate-lockfile                 # resolve against crates.io as it is today
python3 scripts/dep_consistency.py      # Markdown report; exit 1 on a problem, 2 when a check compared nothing
```

The checks:

1. one version per crate: two versions of one crate in a lock file are two copies of its types and its arithmetic, and a value saved through one is not the same type as the value the other reads back
2. presence: all eight core crates are in the lock file
3. requirement vs latest: the `alice-*` requirements of this repository and of every core crate at the version the lock file resolved must accept the latest release of the crate they name (the latest stable release, or the latest pre-release when a crate has no stable one). A requirement that does not is how a second version gets into a user's lock file

Each check reports how many comparisons it made, and a check that made none fails the run (exit 2), as does a dependency of this repository on an `alice-*` crate by path or git. This repository's own package is not one of the crates checked.

The requirement matching follows Cargo's rules (carets, tildes, ranges, wildcards such as `1.2.*` and `1.x`, pre-releases). `scripts/semver_differential.py` compares it with the [semver](https://crates.io/crates/semver) crate (`tools/semver-oracle`) on generated requirement / version pairs and on a fixed grid of every operator and requirement shape against the versions one step away, and fails on any disagreement, including a requirement that one of the two reads and the other does not.

CI (`.github/workflows/dependency-consistency.yml`) runs the tests, the comparison with the semver crate and the check every Monday, on demand, and on a push that changes the check. The report is in the job summary.

## Release train

`release-train.toml` is the plan for releasing the core crates together: for each crate, the version it is being released at, its place in the order (a crate comes after everything it requires) and its state (`planned`, `prepared-local`, `prepared`, `published`, or `exempt` for this repository). What a crate requires of the others is not listed there: it is read from the crate's own `Cargo.toml` at the release checkout, so there is one source for it.

```sh
python3 scripts/release_train.py next                                         # the crate to publish now
python3 scripts/release_train.py publish-gate alice-zip --repo ../ALICE-Zip --ref <sha>   # right before `cargo publish`; exit 0 = go
```

`next` names the first crate in the order that is not published. `publish-gate <crate> --repo <checkout> --ref <sha> [--manifest-path <path>]` reads the crate's manifest (and the repository's root `Cargo.toml`) at that commit, and refuses when the checkout is not that commit or has changes to tracked files (`cargo publish` packages the working tree), when the crate is not `prepared`, when the manifest is another crate or version than the roster's, when a manifest overrides an `alice-*` crate (`[patch.*]`, `[replace]`) or takes one from another registry, when that version is already on crates.io, or when an `alice-*` dependency of the manifest (normal or build, optional or not, any target, renamed or not, `-` or `_` in the name) has no crates.io version requirement, or names a crate that is not `published` in the roster, is missing or yanked on crates.io, or is not accepted by the requirement (Cargo's rules: `"0.4"` does not accept 0.5.0). A dependency outside the roster is checked against its latest release. Only an HTTP 404 counts as "not on crates.io"; any other answer (an error body, another version than the one asked for, a broken connection) stops the gate with exit 2. The publisher runs the gate first in every publish and updates the crate's `state` in the same step. The requirement matching is the one in `scripts/dep_consistency.py`. CI runs the tests (`scripts/test_release_train.py`, crates.io replaced by a fixture) and prints `next`.

## End to end

`e2e/` is a separate crate with one test, `tests/law_to_storage_roundtrip.rs`, that goes through the core crates with their public APIs:

1. read: Newton's law of cooling, written as text, through the alice-lol research law reader, evaluated on a time grid
2. compute: a thermal field law checked by alice-lol (solved by alice-physics), a rod cooling with `alice_physics::transient_thermal`, two bodies falling in a `PhysicsWorld`
3. fit: an alice-zip `SignalLaw` to the cooling curve, and its residual summary from alice-analytics
4. save: every value through the exact series of alice-db (`AliceDB::series`, not the fitted model store) and the law through its law store, to a directory
5. restore: reopen the directory and read everything back
6. compare: every value by `to_bits`, the law coefficients, domain, record and identifier, every field of the residual summary, and the alice-crypto digest of the whole run

It also checks that the crates were built with one arithmetic (the `SEMANTICS_ID` of alice-det-math as alice-zip, alice-db, alice-analytics and alice-lol see it), that every `alice-*` crate is locked at one version, and that all eight core crates are in the build. Each comparison fails when it compared nothing. A golden digest of the computed run (`e2e/golden/run.digest`) says whether the crates still compute what they computed when it was recorded; the round trip says that saving and restoring loses nothing.

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

## Status

Measured on 2026-10-11 (the committed `Cargo.lock`): **the check fails**, and is expected to until the core crates are released with requirements on each other's current versions.

Linked at two or more versions:

| crate | versions | brought in by |
|---|---|---|
| alice-analytics | 0.1.1, 0.2.0, 0.3.0 | alice-physics 2.2.0 (`^0.1`), alice-db 0.3.0-beta.2 (`^0.2`) |
| alice-crypto | 0.1.0, 0.4.1 | alice-db 0.3.0-beta.2 (`^0.1`) |
| alice-det-math | 0.3.2, 0.4.0 | alice-analytics 0.2.0 (`^0.3`) |
| alice-physics | 0.14.0-preview.8, 2.2.0 | alice-lol 0.3.0 (`^0.14.0-preview.4`) |
| alice-sdf | 1.13.0, 5.1.0 | alice-lol 0.3.0 (`^1.9.0`) |
| alice-zip | 0.7.0, 0.8.0 | alice-db 0.3.0-beta.2 (`^0.7`), alice-analytics 0.3.0 (`^0.7`, optional) |

Releases that would remove them, in dependency order: alice-analytics with `alice-zip ^0.8`; alice-db with `alice-zip ^0.8`, `alice-analytics ^0.3` and `alice-crypto ^0.4`; alice-physics with `alice-analytics ^0.3` and the new alice-db; alice-lol with `alice-sdf ^5.1` and `alice-physics ^2`.

End to end, measured on 2026-10-11:

- published: **fails to resolve**, alice-lol 0.4 (the first release with the research law reader the test uses) is not on crates.io yet. A green after alice-lol 0.4 is released covers less than the planned check: alice-physics stays at 2.2.0, used with `std` only (the thermal step and the rigid body world), and its metrics sink into alice-db is not part of the test until alice-physics 3.0.0, whose sink writes to the exact series of alice-db, is released
- main: the test passes (3776 values and records compared, digest `2a0f3d06…`); the check that every crate came from main fails, because alice-det-math 0.5.0 on main is not taken (every other crate requires `^0.4`), so the build uses 0.4.0 from crates.io

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
