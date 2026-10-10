# ALICE-Law

Integration checks across the core ALICE crates: ALICE-LOL, ALICE-DetMath, ALICE-SDF, ALICE-Physics, ALICE-DB, ALICE-Zip, ALICE-Crypto and ALICE-Analytics.

Everything here is built from the versions published on crates.io, with no path dependencies. Two kinds of check:

- dependency consistency (in place): every `alice-*` crate resolves to exactly one version in `Cargo.lock`, all eight core crates are present, and each `alice-*` requirement accepts the latest published version of the crate it names
- end to end (planned): read a law, compute, save, restore, and compare every value bit for bit

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

## License

Apache-2.0 (see [LICENSE](LICENSE)).
