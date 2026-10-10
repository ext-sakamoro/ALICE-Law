# ALICE-Law

Integration checks across the core ALICE crates: ALICE-LOL, ALICE-DetMath, ALICE-SDF, ALICE-Physics, ALICE-DB, ALICE-Zip, ALICE-Crypto and ALICE-Analytics.

Everything here is built from the versions published on crates.io, with no path dependencies. Two kinds of check are planned:

- dependency consistency: every `alice-*` crate resolves to exactly one version in `Cargo.lock`, and each crate's requirements accept the latest published versions of the others
- end to end: read a law, compute, save, restore, and compare every value bit for bit

Status: initial repository, no checks yet.
