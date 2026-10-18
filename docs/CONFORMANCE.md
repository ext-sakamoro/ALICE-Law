# Conformance

A Law-bearing crate computes and persists floating-point values whose bits must
mean the same thing wherever they are produced or read back. This document is
the human-readable version of what that requires; `conformance/gates.toml` is
the machine-readable version a generator reads to build a matrix of which
crate meets which requirement, with what evidence.

The crates this applies to are the crates in the release train
(`release-train.toml`), not a fixed list repeated here: a crate joins or
leaves conformance tracking by joining or leaving the train, in one place.

Contents: [Gates](#gates) · [Manifest](#manifest) · [Definition changes](#definition-changes-2026-10-14) · [Out of scope](#out-of-scope)

## Gates

Each gate below is: what it bans or requires, how a crate's own CI checks it,
and what evidence a `pass` entry in that crate's manifest must point at.

### G1 — no-libm-direct

A platform's transcendental functions (`sin`, `cos`, `powf`, `exp`, `ln`, ...)
are not required by IEEE 754 to round the same way on every platform, so a
crate that calls them directly computes a different answer depending on where
it runs. A Law-bearing crate calls them only through `alice-det-math`, which
pins its own algorithm instead of delegating to the platform.

Checked with a type-resolved `clippy::disallowed_methods` lint (the type
resolution matters: the same method name can be banned on `f32`/`f64` and
allowed on an unrelated type) plus a canary test that injects a direct call
and confirms the lint catches it in a crate this is not yet wired into.

The methods G1 rules on are one list, owned by this repository: the
`methods` array of G1 in `conformance/gates.toml`, each with its verdict
(`banned`, `allowed`, `pending`) and the reason. `sqrt` and `mul_add` are
allowed (IEEE 754 requires both correctly rounded; on a target without a
hardware fma, `mul_add` calls the platform library's `fma`, which must be
correctly rounded, and the cross-target measurement below includes it). `powi` is pending: whether an integer power gives the same
bits on every target is being measured, and its default is banned: a crate
bans it, or allows it with the reason in its manifest (alice-det-math's
`powi` covers `f32` only). A probe of 11,352 points found no difference between
aarch64 and x86_64 code translated to run on the same arm64 floating-point
unit, so it does not settle real x86_64 hardware.

**Claim.** No code of the crate calls an `f32` / `f64` method the G1 list in `conformance/gates.toml` marks `banned` except through alice-det-math, in every target and feature combination the crate builds.

**Evidence for `pass`.** the clippy configuration with the method list; the CI job running `cargo clippy -- -D clippy::disallowed_methods` for each feature set the crate builds (the job names); the canary test.

**Mutant** (turns the evidence red). add `x.sin()` to a library source file, and once more inside a feature-gated module: the CI job fails on both.

**Look at.** the crate's clippy `disallowed-methods` against the G1 list: every `banned` entry for `f32` and `f64` is there (a missing one is a gap unless the manifest justifies it), and the crate's choice for each `pending` entry is stated; the CI job's feature sets against `[features]` in Cargo.toml (a feature the job does not build is not covered).

**Checked by.** machine

### G2 — ieee-max-min

`f32::max`/`f32::min` (and their `f64` equivalents) are not the IEEE 754
`maximumNumber`/`minimumNumber` operations: their handling of equal-magnitude
zeros of different sign is platform-dependent, so the same comparison can
return `+0.0` on one architecture and `-0.0` on another. A Law-bearing crate
uses the Law's own `maximumNumber`/`minimumNumber` definition instead, at
every width it computes in — scalar and every SIMD width the crate builds
for, not scalar alone.

The mechanism is Law helper functions plus named `raw_*` helpers for the rare
site that must call the platform operation directly, tracked by a manifest
that defaults to deny and ratchets down, not a blanket allow on the
containing module (a module-wide allow also hides an unrelated G1 violation
in the same module, since both lints share a name).

**Claim.** Every `f32` / `f64` max and min the crate computes, at scalar width and at each SIMD width it builds, goes through a Law helper with maximumNumber / minimumNumber semantics; a site that must use the platform operation is a named `raw_*` helper listed in a default-deny manifest.

**Evidence for `pass`.** the clippy ban on `f32::max` / `f32::min` / `f64::max` / `f64::min` in library code; the manifest check script and its CI step; for each SIMD width the crate builds, one test with the `(+0, -0)` and `(-0, +0)` pairs (test names per width); and for each class of scalar site (the public functions whose result is a max or min of float inputs, grouped by the computation they share), one test through the public API that checks the result's bits for `(+0, -0)`, `(-0, +0)` and a NaN operand (a NaN result being the canonical quiet NaN of G4) (the class and its test name, one evidence entry each). The ban sees a call, not a rewrite: `if x > 0.0 { x } else { 0.0 }` is a max of `x` and `+0` that no lint names, and only a test of the result's bits sees what it returns for `-0`.

**Mutant** (turns the evidence red). replace one Law max with `f32::max` (the lint fails); add a `raw_*` site the manifest does not list (the manifest check fails); swap the operand order in one SIMD Law max (that width's test fails); rewrite one scalar site's Law max as a comparison (`if a > b { a } else { b }`) (that class's tie test fails).

**Look at.** the list of SIMD widths the crate builds (from its `cfg(target_feature)` and SIMD types), one test per width; the list of scalar site classes, one tie test per class (its completeness is audited).

**Checked by.** machine and audit (the width list)

### G3 — no-outer-fneg-of-fma

`-a.mul_add(b, c)` (negating the whole result of a fused multiply-add) is a
different instruction sequence at different optimisation levels: unoptimised
code computes the fma then negates the rounded result, while an optimising
compiler can rewrite it to `a.mul_add(-b, -c)`, a single instruction whose
cancellation case (`a*b + c` rounding to exactly zero) returns the opposite
sign of zero. This is `fneg(fma(x,y,z)) -> fma(x,-y,-z)`, a transform that is
only value-preserving under a "no signed zero" assumption Rust's default
code generation does not make, so the rewrite is already a bug independent of
which sign a given call site should produce.

The Law-conforming form negates the fma's *operands* instead of its result,
`(-a).mul_add(b, -c)`: this has no outer negation for the optimiser to sink,
computes the same value at every optimisation level, and — because it keeps
the fma's single rounding — carries the sign of an exact-cancellation input
through to the output, rather than forcing every cancellation to one fixed
sign.

Checked with a syntax-tree checker (not a text pattern) covering every form
the outer negation can take: a bare unary negation, `.neg()`, a block-wrapped
negation, subtraction from negative zero, multiplication by negative one (on
either side), and the same shape split across a `let` binding and a later
statement in the same block. `tests/` and `examples/` may allowlist a
specific, reasoned exception (an independent closed-form reference computing
its own values with the platform's own arithmetic, not the implementation
under test); `src/` allows none. A macro invocation whose tokens mention a
fma call is a hard failure rather than silently unchecked, since the checker
cannot see inside a macro's expansion.

**Residual**: the checker is shape-limited by construction — it recognises
syntactic negation of a fma call, not a fma's result that reaches a negation
after being returned from a function, stored in and read back from a field,
or passed through other data flow the checker does not trace. The dynamic
backstop for whatever the static check misses is G4's cancellation lattice,
run on the one leg that can actually execute a hardware fma instruction
(see G4 and G5) — a required leg in every conforming crate's test matrix, not
an optional one.

A universal alternative was evaluated and not adopted: wrapping every fma
call's result in a normalising `+ 0.0` (forcing every exact-cancellation zero
to `+0.0`, regardless of which sign the computation would otherwise produce)
would also catch every outer-negation case, without needing to enumerate its
syntactic forms. Measured against one crate's call sites, it is both a
measurable per-call cost and, at one already-conforming call site, a direct
regression: a Law-bearing formula in that crate deliberately uses the
negate-operands form specifically to carry an exact-cancellation input's sign
through to its output, and the universal `+0.0` would overwrite that output
regardless of input sign. A context-free rewrite cannot distinguish "an
unwanted platform-dependent cancellation" from "a correct, sign-carrying
cancellation" — the two look identical at the single call site a text or
AST rewrite can see. The shape-limited static checker plus the G4 dynamic
backstop stay the gate.

**Claim.** No library source negates the whole result of a fused multiply-add in any syntactic form, and no macro invocation in library code mentions a fma call.

**Evidence for `pass`.** the syntax-tree checker and its CI step; its own tests over every form listed above.

**Mutant** (turns the evidence red). insert `-(a.mul_add(b, c))` (and `.neg()`, `-0.0 - fma`, `-1.0 * fma`) in a library source file: the checker fails on each.

**Look at.** the checker's file set covers `src/` of every workspace member that is a published crate.

**Checked by.** machine

### G4 — debug-release-arch-goldens

A Law-bearing crate's test suite checks every signed-zero and
exact-cancellation case it has identified against a fixed expected bit
pattern, run under both `debug` and `release` profiles on both `x86_64` and
`aarch64`. Agreement across profile and architecture is the property G1–G3
exist to guarantee; this gate is where that guarantee is actually exercised
against concrete inputs, including ones no static checker reaches.

A NaN that reaches a Law output is the canonical quiet NaN: `0x7fc00000` for
`f32`, `0x7ff8000000000000` for `f64`. IEEE 754 leaves the sign and the
payload of a NaN result unspecified, and they do change with the optimisation
level and the architecture (a `sign * x` the optimiser turns into a select and
a negation gives `0x7fc00000` in debug and `0xffc00000` in release), so a
crate whose output can be NaN canonicalises it before the value leaves; the
sign and payload are not part of the output.

**Claim.** A named test checks the crate's signed-zero and exact-cancellation cases, and a NaN case wherever an output can be NaN, against fixed expected bit patterns (a NaN output being the canonical quiet NaN), and CI runs it on all of {debug, release} x {x86_64, aarch64} as required jobs.

**Evidence for `pass`.** the test name(s); the CI matrix showing the four combinations, without `continue-on-error`.

**Mutant** (turns the evidence red). flip one expected bit (the test fails on all four legs); rewrite one Law site to the outer-negated fma form of G3 (the test fails on the aarch64 release leg); remove the canonicalisation at one output that can be NaN (the NaN case fails at least on the leg where the sign or payload differs).

**Look at.** that each case named in the test reaches a library function (not a copy of it in the test).

**Checked by.** machine

### G5 — release-profile-test-leg

A crate's CI runs its full Law-bearing test suite under the `release`
profile, not debug alone — many of the miscompiles G1–G4 guard against (fma
rewriting in particular) only appear once optimisation is enabled. The
`aarch64` release leg is required specifically because it is the one
widely-available target whose default code generation uses a hardware fma
instruction at all: the equivalent `x86_64` default target lowers
`mul_add` to a library call with no native fma, so that leg alone cannot
demonstrate — or catch a regression of — the class of bug G3 and G4 exist
for.

**Claim.** CI runs the crate's Law-bearing test suite with `--release` on an aarch64 runner, as a required job.

**Evidence for `pass`.** the workflow job and its runner label.

**Mutant** (turns the evidence red). remove the aarch64 release leg from the workflow: the required check is missing and the gate's workflow check fails.

**Look at.** that the release leg runs the same test set as the debug legs (no filter that drops the Law tests).

**Checked by.** machine

### G6 — semantics-id

Each crate computes an identifier that folds its own Law-bearing constants
("pins") together with the identifiers of every upstream Law-bearing crate it
depends on, and exposes it so a consumer can tell whether it is talking to
the arithmetic it was built against. The identifier is guarded against going
stale silently: it is tied to the crate's declared version and to the
toolchain it was computed under, so a dependency bump or a toolchain change
that could change the arithmetic is visible as an identifier change rather
than an unremarked one.

An identifier matching does not by itself mean the two sides reached that
arithmetic the same way — that every call site actually routes through the
Law-conforming path is what G1 (and G2, G3) check; G6 is the fingerprint of
the result those gates protect, not a substitute for them.

**Claim.** The crate exports a constant identifier (32 bytes) folded from its own Law pins and the identifiers of the Law-bearing crates it depends on; a test pins it to its hexadecimal value, and changing any pin changes it.

**Evidence for `pass`.** `pub const SEMANTICS_ID` (or the crate's named equivalent, `<CRATE>_SEMANTICS_ID`, for example `PHYSICS_SEMANTICS_ID`) with its file:line; a constant the crate re-exports from an upstream crate, under any name, is that upstream's identifier and is documented as such, not this gate's evidence; the test asserting its hex; a test that changing one pin changes the identifier.

**Mutant** (turns the evidence red). change one pin constant: the hex test fails.

**Look at.** the fold's inputs (its own pins and each upstream identifier) against the crate's Law-bearing dependencies; the tie to version and toolchain is described in the note and audited.

**Checked by.** machine and audit (the version / toolchain tie)

### G7 — handshake-carries-id

Whenever one Law-bearing crate's output is consumed as another's input over
a boundary within one exchange (a network call, a message, an FFI call, a file written and read in the same run), the producer's G6
identifier travels with the value, and the consumer compares it against its
own. A mismatch is a distinct, explicit error (not a silent fallback, not a
generic parse failure) carrying both identifiers, so the two sides of a
mismatch are always named.

A value passed in memory as a concrete Rust type does not carry the
identifier. Rust's types are nominal per crate version: `alice_zip::X` from
one version is a different type from the same name in another version, so
two versions cannot exchange such a value without a compile error, even when
a build links both (Cargo does link semver-incompatible versions side by
side). And Cargo unifies the features of one crate version across a build,
so one version has one configuration. A value that type-checks as the
consumer's expected type therefore comes from the same crate version in the
same configuration, which has the same G6 identifier by construction. This
holds only while the type is kept: any boundary that erases it (serialized
bytes, a file, FFI / C ABI, wasm and JavaScript, Python bindings, `dyn Any`,
a registry of trait objects keyed by name, and a value computed at build
time: a `build.rs` output or a procedural macro's expansion, which Cargo
compiles as a separate instance for the host, possibly with other features,
and which reaches the target binary as source text) still carries the
per-value identifier and returns the distinct error.

A durable form read back later, possibly by another release of the reader (a saved file, a database
row, a wire format meant to be stored), is G8's, not G7's: there the reader compares against the
identifier recorded when the value was written, by G8's rule, not by equality with its own G6.

**Claim.** A Law-bearing value crossing a crate boundary is checked against the producer's identifier: at every boundary that erases the Rust type (serialized bytes or a file within one exchange, a network message, FFI / C ABI, wasm and JavaScript, Python bindings, `dyn Any`, a registry of trait objects, a value computed at build time by `build.rs` or a procedural macro) the value carries the producer's G6 identifier and the receiving function compares it, returning a dedicated error variant that names both identifiers; a value passed in memory as a concrete Rust type needs no per-value identifier (see the reason above).

**Evidence for `pass`.** the list of the crate's type-erasing boundaries that receive another Law-bearing crate's values (an empty list is the evidence that this part does not apply); for each, the error variant (two identifiers) and a test feeding a mismatched identifier.

**Mutant** (turns the evidence red). remove the comparison in one reading function: its mismatch test fails.

**Look at.** every public decode / read / from_bytes / open entry point, every `extern "C"` / `#[no_mangle]` function, every wasm-bindgen and PyO3 export, every `dyn Any` or name-keyed registry, and every `build.rs` and procedural-macro crate that calls Law functions (the list is the evidence; its completeness is audited).

**Checked by.** machine and audit (completeness of the entry point list)

### G8 — persisted-semantics-id

Where a crate's Law-bearing values are written to a durable form (a file
format, a database row, a wire format meant to be read back later), that
persisted form carries its own identifier, separate from G6's in-process one:
a value read back after a crate upgrade is compared against the identifier
recorded when it was written, not against the identifier of whichever crate
version happens to be reading it now. The reader accepts the stored
identifier when it is the reader's own or one the reader can verify as an
earlier identifier with the same bits on everything it covered (for the
arithmetic: `alice_det_math::check_semantics` returning `Verified` or
`VerifiedPrevious`); any other identifier is the explicit error naming both.
This is the rule for a saved file read back later; G7's equality applies only
within one exchange. This identifier is about the meaning
of the stored bytes, not about which platform produced them.

Data written before the format carried an identifier has none to compare. A
reader may still read it, but only visibly: the result carries a typed status
that the caller sees (for example `Unpinned`), and a policy exists that
refuses such data (for example `RequirePinned` or a strict mode). Reading it
as if it had been verified, with no status, is a fail.

**Claim.** Every durable format the crate writes Law-bearing values in carries an identifier of the meaning of the stored bytes, and the reader compares the stored identifier, not its own build's: it accepts its own identifier or a verified earlier one (`check_semantics`), and returns the explicit error for any other.

**Evidence for `pass`.** the list of the crate's encode / write functions; the identifier field in each format; a round-trip test that reads a value written with another identifier and gets the mismatch; where data without an identifier is readable, the typed status the reader returns for it and the policy that refuses it, by name, with a test of each.

**Mutant** (turns the evidence red). drop the identifier from one format's writer: the reader's test fails.

**Look at.** every public encode / write / save entry point (the list is the evidence; its completeness is audited).

**Checked by.** machine and audit (completeness of the entry point list)

### G9 — persisted-meaning-change-is-major

The G8 identifier is defined against a crate's reference platform's bit
pattern, not against every platform's: bringing a second architecture's
output to bit-for-bit agreement with the reference platform is a bug fix on
that architecture, not a change to what the stored bytes mean, and does not
move the identifier. An identifier move is reserved for an actual change in
meaning — folding in a changed upstream Law, or changing the reference
platform's own computed value — and is recorded as a semver-major change
when it happens, with the identifiers either side of the change named.

**Claim.** (unchanged) The G8 identifier is defined against the crate's reference platform; bringing another architecture to bit-for-bit agreement is a fix, not a change of the identifier; an actual meaning change moves the identifier and is released as semver-major, naming the identifiers on either side.

**Evidence for `pass`.** the audit of the identifier's history: every commit that changed the identifier's definition (`git log -S` over it), each with the release it shipped in, showing each was a meaning change released as a major version whose CHANGELOG names the old and new identifiers, and that no cross-architecture fix moved it.

Where the crate has no G8 identifier to audit, this gate takes G8's status
and cites it: G8 `n/a` (no durable form of Law-bearing values) gives `n/a`,
with G8's evidence and the mutant of adding a durable writer (which makes G8,
and so this gate, apply); G8 `fail-known` (a durable form without an
identifier) gives `fail-known`, its note naming G8's site (this gate cannot be
met before G8); G8 `unverified` gives `unverified`. Where some durable forms
carry an identifier and others do not, the identifier that exists is audited
as above, and the entry is `fail-known` while any form lacks one.

**Mutant** (turns the evidence red). none (a property of the history); the audit list is re-done at each major release.

**Look at.** the history of the identifier's definition and the CHANGELOG of each release it moved in.

**Checked by.** audit

### G10 — no-run-varying-artifacts

A Law-bearing crate's output must not depend on anything that varies between
runs for reasons unrelated to its inputs: an unseeded random number
generator, a freshly generated UUID, or the wall-clock time embedded directly
in a Law-bearing value. Where randomness or a timestamp is genuinely part of
what is being computed, it is an explicit input the caller supplies (and
which the gates above then apply to), not an ambient source the function
reaches for on its own.

**Claim.** No library code reachable from a Law-bearing output reads an unseeded random source, generates a UUID, or reads the wall clock or a monotonic clock; randomness or time that is part of the computation is a parameter.

**Evidence for `pass`.** a lint (`disallowed_methods` / `disallowed_types` for `SystemTime::now`, `Instant::now`, `Uuid::new_v4` and similar, `thread_rng`, `rand::random`, unseeded `from_entropy`) or a scan step in CI with its canary.

**Mutant** (turns the evidence red). add `SystemTime::now()` to a library source file: the lint or scan fails.

**Look at.** `src/` excluding `cfg(test)`, benches and examples; the list of banned paths against the crate's dependencies.

**Checked by.** machine

### G11 — gate-self-tests

A gate that can silently stop checking anything is worse than no gate, since
a passing CI run then looks identical whether the gate is working or not.
Every gate above carries its own tests that a change breaking the gate itself
would catch:

- (a) a canary case the gate is expected to flag, confirming the checker
  still fires rather than having been quietly disabled or miscompiled away
- (b) a check that the gate actually compared something: a run that scans
  zero files or finds zero call sites fails rather than reporting success by
  omission
- (c) where a gate's script or helper is meant to be identical across more
  than one crate (a shared checker, a shared helper source file), a
  cross-repository check that the copies have not silently diverged

For (c) this repository holds `conformance/shared-files.toml`, generated by
`scripts/gen_shared_files.py`: for each shared file its path and the repositories that
carry it (with a path for one that keeps it elsewhere, and any repository exempted with its
reason), and either the SHA-256 of its bytes with a commit on its source repository's main
that has them, or, while the copies differ, a `pending` record: the variants found, the
target bytes and the repository that owns them, the date it became pending and what ends
it. A crate meets (c) by running `scripts/shared_files_check.py --repo . --name
<repository>` from this repository at a revision it pins together with the SHA-256 of the
checker and of the table (as for `scripts/public_text_check.py`): every listed copy must be
present and equal (or, on a pending row, one of its variants), a table path present in the
crate but neither listed nor exempted for it is a finding (except a copy with exactly a uniform
entry's bytes or a pending row's target: a crate adopting a shared file cannot be listed before
its main has it, so that copy is a counted "adopting" note, and the strict check on this
repository's landing path makes the next table list it), and a run that compared nothing
fails. A crate with a pending row is not a pass for that file: its cell is `fail-known`
naming the row. `.github/workflows/conformance-live.yml` compares every listed repository's
main with the table on every push to main and daily (count-locked on the copies of uniform
rows, which move only with the table; the table is regenerated immediately before a change
to it lands), fails a pending row
older than 21 days and a file under `scripts/`, `tools/` or `.github/workflows/` that is
byte-identical in two table repositories and has no entry (a shared copy elsewhere, such as
under `tests/` or `src/`, is checked once a row names it, not found by this scan),
and checks that the table is the generated one; the branch checks refuse a change that adds
an unplanned pending row. A rollout is a planned row instead: the shared-file registry names
the new bytes (`next=`, a git blob id) before any repository moves, and once the owner's main
has them `scripts/gen_shared_files.py --next-from <registry>` writes the row as pending with
`planned` = that id (kept on later regenerations). The live check fails a planned row whose
owner's main does not hold the target (both its SHA-256 and the planned blob id), one whose
every copy already has the target (it has converged: regenerate), and one older than 14 days;
the ratchet counts planned and unplanned rows separately, and only the unplanned count may
not grow.
A planned row re-rolled to bytes no main holds yet (some mains already took the earlier
bytes) stays planned: the registry's `next=` names the new blob and
`--target PATH=SHA256@ALICE-X` gives their SHA-256 and owner, and the row records
`moved_from = { planned, target }` of the bytes it moves from, in a table-only commit before
any main takes the new bytes. Its owner may hold either pair; a copy may also hold an older
variant the table recorded before the move (a re-roll found mid-rollout: listed as a stale
copy, with how many copies hold the target), and any other value is a finding; the branch
check fails a moving row that records a variant its base table did not have, and a planned id that changed without `moved_from` naming the
base's planned id and target. The generator refuses such a move without the SHA-256, and a
planned row whose bytes left every main, instead of dropping either to an unplanned row.
A rollout that lands before the table is regenerated does not turn that job red: a copy on a
main the table does not list, with the bytes of its row (or one of a pending row's
variants), is counted as an unlisted holder with the date it appeared, while a copy with
other bytes stays a finding. The table must still be regenerated before the next publish:
this repository's own checks (`scripts/preflight.sh`) and the publish gate
(`scripts/release_train.py publish-gate`) run the comparison with `--strict`, where an
unlisted holder is a finding.

The per-gate entries are kept as a table, `conformance/g11.toml` in the crate, one `[[row]]` per
checker: `gate`, `checker` (a path, or `path#token`), `canary` and `zero` (references), and
`shared` (a list of paths in `conformance/shared-files.toml`, or `"n/a: <reason>"`). A reference
is `<path>.rs::<fn>` (a `#[test]` that runs: not `#[ignore]`, not under `#[cfg(any())]`, under
`tests/` or after `#[cfg(test)]`, and a command of a workflow or `scripts/preflight.sh` (comments
dropped) that runs it: `cargo test` / `cargo nextest run` / `scripts/run_tests.sh` /
`scripts/cargo_test_nonzero.sh` for its package, selecting the lib's unit tests (no target selection,
`--lib`, `--tests` or `--all-targets`) or, for `tests/<t>.rs`, that target (`--test <t>`), whose
test-name filters select it (a filter is a substring of its module path `a::b::tests::fn`, or equal to it
under `--exact`; a matching `--skip` rejects the command; with a filter and a file whose module path
cannot be derived, the command does not count), with every
feature it needs on that command: `#[cfg(feature = ...)]` on it or an enclosing module and the
target's `required-features`), `<path>.py::<test>` (one `def test...` of that name, in a file a
workflow or `scripts/preflight.sh` command runs) or `<path>.py::<Class>.<method>`, `<path>#<token>` (any file, workflows and `scripts/preflight.sh`
included), or any of these as `law@<pin>:<reference>`, read in this repository at the revision
`.github/<pin>` names (a checker the crate fetches from here is tested here).
`scripts/g11_table_check.py --repo . --name ALICE-<crate>` reads the table: a gate the manifest
marks `pass` (other than G11) with no row (F1), a row for a gate that is not `pass` (F2), a
checker path or anchor that is not in the tree (F3), a canary or zero reference that does not
resolve (F4), a row whose canary and zero are one reference (F5), and a shared path that the
shared-file table at the crate's `.github/shared-files-pin` revision does not list for the crate,
or `"n/a"` where it lists the checker (F6), each exit 1; no row or no `pass` gate exits 2. A crate
fetches the checker at a revision it pins with the checker's SHA-256, as it does the shared-file
checker. The table is a prerequisite: it does not make G11 `pass` by itself, and the evidence
below is still required.

**Claim.** Each machine-checked gate of the crate has (a) a canary its checker must flag, (b) a failure when it compared nothing, and (c) where its checker or helper is shared with other crates, a check that the copies are identical.

**Evidence for `pass`.** per gate, the names of the canary test, the zero-compared test and (where shared) the copy check.

**Mutant** (turns the evidence red). disable one canary's assertion (its test fails); make one checker scan an empty set (it fails).

**Look at.** every gate the manifest marks `pass` by machine has its three entries.

**Checked by.** machine

### G12 — public-repo-text-rules

A Law-bearing crate's public repository follows the text rules this
repository's own `scripts/public_text_check.py` enforces on itself (no
internal process vocabulary, no private names, checked by hash rather than
by a plaintext list a check could itself leak). This gate does not redefine
those rules; it requires that a conforming crate run the equivalent check
against its own repository.

**Claim.** The crate's CI runs ALICE-Law's `scripts/public_text_check.py` on its repository (the file fetched from ALICE-Law at a named revision, or a copy byte-identical to it), failing when it compared nothing; a crate's own checker counts only if it is that file.

**Evidence for `pass`.** the CI step that fetches or carries the script and the revision or hash it checks; a run's output showing the files compared.

**Mutant** (turns the evidence red). add an internal word to a tracked document: the step fails.

**Look at.** the script's hash against ALICE-Law's at the named revision.

**Checked by.** machine

### G13 — no-unspecified-order

Where Rust itself leaves an order unspecified — `HashMap`/`HashSet`
iteration, the order a parallel reduction combines its partial results in,
anything that depends on thread count or on the relative order memory
addresses happen to land in — a Law-bearing crate's output must not depend
on it. Where an order matters to the result, the crate uses an
order-preserving collection or an explicitly associative, order-independent
reduction, not an incidental order that today's standard library or runtime
happens to produce.

**Claim.** No Law-bearing output depends on `HashMap` / `HashSet` iteration order, on the order a parallel reduction combines partial results, or on thread count or address order.

**Evidence for `pass`.** a lint banning `HashMap` / `HashSet` iteration in the crate's Law modules, or a reasoned allowlist of sites where the collection is used for membership only (each with why its order cannot reach an output); parallel reductions over floats either absent (scan) or ordered (named test with two thread counts).

**Mutant** (turns the evidence red). iterate a `HashMap` into a Law-bearing output: the lint fails; run the ordered-reduction test with a different thread count after making the reduction unordered: it fails.

**Look at.** each allowlisted site's reason.

**Checked by.** machine and audit (the allowlist reasons)

### G14 — float-environment-invariants

A Law-bearing crate's shipped artifacts are not built with flags that change
floating-point semantics from the ones the gates above assume: no
fast-math-style flag that permits reassociation or flushing subnormals to
zero, no `target-cpu=native` (which can select different instructions,
including fma, on the machine that happens to build it versus the machine
that runs it). The crate's tests assert the assumptions those flags would
violate directly — round-to-nearest-even, and that denormals are not flushed
to zero — rather than relying on nobody having set the flag.

**Claim.** No build configuration of the crate (`.cargo/config.toml`, CI `RUSTFLAGS`, `build.rs`) sets a fast-math-style flag or `target-cpu=native`, and the crate's tests assert round-to-nearest-even and that subnormals are not flushed to zero.

**Evidence for `pass`.** the scan step over those files in CI; the two test names.

**Mutant** (turns the evidence red). set `-C target-cpu=native` in the CI environment (the scan fails); a subnormal input in the flush test (it fails under a flush-to-zero mode).

**Look at.** every place a flag can come from: `.cargo/config*`, workflow `env`, `build.rs`, `Cargo.toml` profiles.

**Checked by.** machine

## Manifest

Each crate's own manifest (`alice-law-conformance.toml` at its repository
root) has one entry per gate, with these fields:

| field | meaning |
|---|---|
| `status` | `pass`, `fail-known`, `unverified`, or `n/a` |
| `evidence` | a list of strings: mandatory for `pass` and `n/a`; an entry whose first word is a path (it contains `/` or ends in a file extension) must exist in the repository; a place in a file is written as an anchor `` `path#token` `` (the path from the repository root, and a string that occurs in that file exactly once; or `` `path#A>B` ``: A once, then the first B after it) instead of a line number, which moves when the file changes, and each anchor is checked against the file at the ref |
| `mutant` | mandatory for `pass` and `n/a`: the change that turns the evidence red (the gate's **Mutant** above, as carried out in this crate) |
| `since` | the version or commit the gate first passed |
| `note` | mandatory for `fail-known` (what is unmet, with file:line where there is a site) and for `n/a` (what makes the gate not apply) |
| `reference_repo` | the crate whose implementation this gate's mechanism was taken from or checked against, where one exists |
| `tie_tests` | G2 only, required for `pass`: one string per class of scalar site, `"<class>: <path>.rs::<test>"`, each a test that runs (a `fn` with `#[test]`, not `#[ignore]` and not under `#[cfg(any())]`, in a file under `tests/` or after a `#[cfg(test)]`); `[]` states the crate has no scalar float max / min site |
| `writers` | G8 only, required for `pass`: every function writing a durable form of Law-bearing values, `"<path>.rs::<fn>"`; an empty list compared nothing and does not count |
| `legacy` / `legacy_status` / `legacy_policy` | G8 only, required for `pass`: `legacy = "none"` when no data without an identifier is readable; otherwise `legacy_status` (the typed status such data is read with) and `legacy_policy` (the policy refusing it), each `"<path>.rs::<name>"` |

The roster check reads every `<path>.rs::<name>` at the same ref as the
manifest and looks for its definition (`fn`, `struct`, `enum`, `trait`,
`type`, `const`, `static`, `mod`, or an enum variant), with comments and
string literals removed. A G2 or G8 `pass` whose fields are missing, or name
something that is not there, is counted as `unverified`, with a note saying
what is missing.

The four statuses keep "not looked at" apart from "looked and unmet":

- `pass`: checked; the evidence exists and the mutant turns it red.
- `fail-known`: checked, and the requirement is not met; the note says where.
- `unverified`: not checked yet. A gate the manifest does not list, and every
  gate of a crate with no manifest, is `unverified`.
- `n/a`: the gate does not apply. This is not a silent skip: its evidence is
  the same scanner that catches a violation elsewhere finding zero relevant
  sites in this crate, and its mutant is adding one such site (absence
  demonstrated, not assumed). For example, a crate with no floating-point Law
  arithmetic at all is `n/a` for G1–G3.

A `pass` or `n/a` entry without `evidence` or without `mutant` is read as
`unverified`: a generated view never shows it as met. An `n/a` whose evidence
names no scanner (a checker under `scripts/` that exists at the ref, has its own
test `scripts/test_<name>.py`, is run by a workflow at the ref, and is not a
generic driver such as `scripts/preflight.sh`), or whose mutant does not say
it was `measured`, is read as `unverified` too: source files alone, a driver
that runs everything, or a described mutant, do not show an absence.

A form that no layer of the gate reads (a checker hole: a spelling, a path or a
construct none of the gate's checks sees) leaves the cell `unverified` for what
it cannot see, not `pass`; a gate with several layers (a lexical guard and a
type-resolved lint, for example) stays verified for a form any one layer
covers. `fail-known` requires the unmet requirement to be located: the file and
line of the offending code, or the place where the required thing is absent
(the identifier, the release leg, the canary or the scan that is missing, named
in the workflow, file or API that lacks it); a suspicion without either is
`unverified`. A cell moves to `pass` when the missing check lands together
with its canary (the mutant that the new check turns red).

Transition: the earlier vocabulary had `missing` in place of the split between
`fail-known` and `unverified`. Until the release-train crates' manifests are
rewritten, `missing` is read as `unverified` with a warning; after that it is
refused.

### Features and the guarantee

The manifest may also say, per Cargo feature of the crate, whether the
feature is inside or outside the guarantee, in a `[features]` table:

```toml
[features.std]
guarantee = "inside"
evidence = ["tests/law_oracle.rs", ".github/workflows/ci.yml determinism job"]
note = "same arithmetic as the default build"

[features.simd]
guarantee = "outside"
note = "lane order of the reductions differs from the scalar path"
```

| field | meaning |
|---|---|
| `guarantee` | `inside` or `outside` |
| `evidence` | mandatory when `guarantee = "inside"`: the test file(s) or CI job that shows it, as for a gate's `pass` |
| `note` | what the feature changes, and for `outside`, why it is outside |

A feature is **inside** the guarantee only when all of these hold with the
feature enabled, and the evidence shows it:

1. **The same arithmetic generation**: the crate built with the feature reports
   the same arithmetic identifier (`SEMANTICS_ID` of alice-det-math, as the
   crate re-exports or pins it) as without it, and the feature adds no
   floating-point path the gates of this document do not cover (G1–G3 and G13
   apply to its code as to the rest).
2. **The same values**: a computation the feature reaches produces the same bits
   as the same computation without the feature, or, where the feature adds a
   computation of its own, that computation is pinned by a golden that holds on
   every architecture the crate's own G4 matrix runs.
3. **Covered**: the feature is exercised either by the end to end check of this
   repository (`e2e/`, with the feature in the dependency's feature list) or by a
   named oracle in the crate's own suite that compares against an external
   reference (an analytic solution or an independent implementation), not
   against the crate's own output.

Anything else is `outside`, and its note says which condition it does not
meet. A feature the table does not list is not declared, and the README block
(`scripts/gen-law-readme-block.py`) shows it as such, never as inside; the
generator refuses a feature marked `inside` without evidence, and a listed
feature the crate does not have.

### Line-number references

A place is named by an anchor, never by a line number. The line numbers that manifests
still hold are counted against `conformance/line-refs-baseline.toml`, which records each
crate's current count. A reference is `(line 3)`, `lines 3 to 9`, a GitHub `#L42` /
`#L3-L9` anchor, a path followed by `L42`, or any file extension followed by `:42`
(a version, a date, a ratio or a URL's port is not one). The roster check (at every main)
fails a count above its baseline (a new line reference). A count below it is slack that
would let the crate add references back up to the baseline: in the live check it is a
note naming the crate, the baseline and the count while the crate's main has been below
for at most 3 days (dated by the manifest's commits on main), and a finding after that,
when the date is not known, and always under `--strict` (the receipt before a push to
main). `scripts/land_law_state.py` (the gate before a crate lands) refuses a candidate
whose count is above its baseline (exit 2). A candidate below its baseline moves nothing
(the Law commit comes before the landing, and a baseline lowered then would be below the
crate's main until it lands); once it has landed, another repository's main below its
baseline is a move for the next landing, whose `--write` lowers that baseline with the state. `scripts/line_refs_ratchet.py` (branch CI and the preflight)
fails a baseline that rises against main's, a new entry above 0, and the removal of the
file. With every baseline at 0 no manifest may hold a line reference at all (a crate the
file does not list counts as 0).
`python3 scripts/line_refs_propose.py --repo <clone of the crate> [--ref REF]` proposes,
for each reference, an anchor naming the construct the line is part of: the definition the
line is (`fn`, a TOML table, a YAML job or step, a preflight step, a Markdown heading), a
scope under the enclosing definition with a token of the line, or the whole line (a token
that merely occurs once is not proposed, nor a comment). Every proposal passes the roster's
own rule and an oracle: the anchor resolves to the referenced line (or to its enclosing
definition when the manifest's text names that construct), and a word of the reference's
own text (after the previous reference, and after a `, ` or `(` when a word remains) names
the enclosing definition, or occurs on the line when no definition encloses it, so a line
number that has drifted into a neighbouring construct is reported instead of anchored.
The word check is a heuristic; a proposal is read before it is applied. A range is not proposed (an anchor names one place). It
lists what it cannot resolve, writes nothing, and exits 1 when references exist and no
proposal could be compared. The manifests are converted crate by crate; on the day a
crate's conversion lands, a Law commit that changes only the baseline file lowers that
crate's baseline to its new count.

### Checking a manifest before it lands

A branch's own CI does not read its manifest the way this repository's roster
check reads it at main, so a manifest can pass there and be refused once it
lands. Before a change to a manifest lands, run the same check against the
branch, from a clone of this repository:

    python3 scripts/conformance_roster_check.py --check-manifest <clone of the crate> --ref <branch or commit>

It applies the roster check's own rules (gate ids, status vocabulary, evidence
paths at that ref, a located `fail-known`, `pass` / `n/a` without evidence or a
mutant read as `unverified`) and prints the counts the state file would
record. Exit 1 on a finding, 2 when nothing was compared.

### Whether a landing moves this repository's state

`conformance/manifest-state.toml` and the README Status blocks record the
counts of every release-train manifest at its main, and the live check of this
repository fails when they differ. A crate commit that changes its manifest's
statuses therefore needs a state commit here. Before it lands, the lander runs,
from a clone of this repository:

    python3 scripts/land_law_state.py --candidate ALICE-X=<clone of the crate>@<commit> [--candidate ...]

It reads each candidate's manifest at that commit and every other repository's
at its main, with the roster check's rules, and prints `state moves: no` or
`state moves: yes` with each changed count. Exit 0: no move; 1: the state
moves, so a state commit here must follow the landing; 2: not computed (a
manifest with findings, a candidate the roster does not name, nothing
compared). With `--write` it writes the state file and the README blocks that
the landing makes true. The strict shared-file receipt taken before a push to
this repository's main (`scripts/shared_files_strict.sh`) also runs the state
check and `scripts/gen_law_status.py --check`, so a push never carries a state
the crates' mains no longer match.

The state commit lands here first, before the crate: `--write` also records
each moving candidate in `conformance/pending-state.toml` (its commit and the
date). The rule is on the counts, not on the history, because a landing may
rebase the commit and other commits may land on the crate's main first. For a
repository a pending entry names: when main's counts are the recorded ones, the
entry has landed; otherwise the state file must record the counts computed from
the manifest at the entry's commit (read there, not trusted). The following are
findings: a difference no entry explains, a recorded state that is neither
main's nor the entry's commit's, and an entry older than 3 days. Once the crate
lands, the entry is a counted note in the live check (this repository stays
green across the landing). It is a finding under
`--strict` (the receipt before a push here) and after 3 days, so the next
change here drops it. `land_law_state.py --write` and
`conformance_roster_check.py --write` drop landed entries. A re-rolled
candidate keeps the date its entry was first recorded. For `land_law_state.py`,
a move is a change of the recorded state or of which repositories are pending:
a rebased candidate whose counts are already recorded (another sha or date in
its entry) is not a move, so a landing that rebased needs no second Law change.

### The README block

A crate states its ALICE-Law status in one place: the block between
`<!-- law-integration:begin law=<sha> -->` and `<!-- law-integration:end -->` in
README.md and in the Japanese README, which `scripts/gen-law-readme-block.py`
writes from the pinned revision of this repository and checks in the crate's
CI. Outside the block a README does not name ALICE-Law or this file at all,
neutral wording included ("not yet checked against ALICE-Law"), except a line
that is only a link to this file whose link text makes no claim; the word "Law"
alone is the crate's own noun and is a finding only next to a claim word. The
check reads every line outside the block and the code fences.

### G7 and G8 for stored files (2026-10-15)

G7 named "a saved file" as a boundary where the consumer compares against its own G6 identifier, and
G8 required a value read back later to be compared against the identifier recorded when it was
written; for a file read back by another release the two disagreed. G7 now covers boundaries within
one exchange, and a durable form read back later is G8's, whose reader accepts its own identifier or a
verified earlier one (`check_semantics`).

## Definition changes (2026-10-17)

| gate | change |
|---|---|
| G2 | **meaning** (tightening): besides the ban and the SIMD-width tests, one bit-level tie test through the public API (`(+0, -0)`, `(-0, +0)`, a NaN operand) per class of scalar site, because the ban cannot see a max or min written as a comparison. A G2 `pass` without `tie_tests` naming tests that exist reads as `unverified` (the roster check) until the manifest adds them |
| G4 | **meaning** (tightening): a NaN at a Law output is the canonical quiet NaN (`f32` `0x7fc00000`, `f64` `0x7ff8000000000000`), its sign and payload not exposed; a crate whose output can be NaN pins it with a NaN case in the goldens |
| G6 | wording: the named equivalent is `<CRATE>_SEMANTICS_ID`; a re-exported upstream identifier is that upstream's, not this gate's evidence |
| G8 | **meaning** (tightening): data without an identifier is readable only with a typed status the caller sees and a policy that refuses it; silent acceptance is a fail. A G8 `pass` without `writers` and the legacy fields, each naming something that exists, reads as `unverified` until the manifest adds them |
| G9 | wording: where there is no G8 identifier, the entry takes G8's status and cites it |

## Definition changes (2026-10-14)

Each gate above gained **Claim**, **Evidence for `pass`**, **Mutant**, **Look
at** and **Checked by**, so that one pass over a crate decides each gate. The
claims restate the earlier definitions (`conformance/gates.toml`); where the
meaning changed, it is marked.

| gate | earlier definition (verbatim) | change |
|---|---|---|
| G1 | Platform transcendental functions are banned outside alice-det-math; sqrt (correctly rounded by IEEE 754) and powi (portable by construction) are out of scope. | **meaning**: one method list owned by `conformance/gates.toml` with a verdict and reason per method (it names `hypot`, `cbrt`, `exp_m1`, `ln_1p` and the others as banned, `mul_add` as allowed); `powi` moves from out of scope to pending a cross-target measurement, banned by default (2026-10-15: a crate allowing it states the reason); a crate's clippy list must contain every banned entry; the evidence must cover each feature set |
| G2 | f32/f64 max/min must implement IEEE 754 maximumNumber/minimumNumber, at scalar width and at every SIMD width the crate builds for, via Law helpers and a default-deny manifest of named raw_* exceptions. | wording: one test per SIMD width named as evidence |
| G3 | Negating the whole result of a fused multiply-add (a.mul_add(b,c)) is banned in every syntactic form; the Law-conforming form negates the fma's operands instead, (-a).mul_add(b, -c). | wording |
| G4 | Signed-zero and exact-cancellation test cases are checked against fixed expected bit patterns under {debug, release} x {x86_64, aarch64}. | **meaning** (tightening): the four legs must be required jobs, not only run |
| G5 | The full Law-bearing test suite runs under the release profile in CI; the aarch64 release leg is required (the one common target whose default codegen uses a hardware fma instruction). | wording |
| G6 | Each crate computes an identifier folding its own Law pins with upstream crates' identifiers, guarded by its own version and toolchain so staleness is visible; the identifier attests the fingerprint, not that every call site routes through it (G1 attests that). | wording: names what to look at (the exported constant, its hex test, a pin-change test); the version and toolchain tie is an audit |
| G7 | A Law-bearing value crossing a crate boundary carries its producer's G6 identifier; a mismatch against the consumer's own identifier is an explicit, distinctly-typed error naming both identifiers. | **meaning** (relaxation, accepted 2026-10-14 by the coordinator of the release train, open to the owner's veto): a value passed in memory as a concrete Rust type no longer carries a per-value identifier. Reason: Rust's types are nominal per crate version (two versions cannot exchange such a value without a compile error, even when a build links both) and Cargo unifies features per crate version, so the same type means the same version and configuration, hence the same G6 identifier. Carve-out: every type-erasing boundary (serialized bytes, files, FFI / C ABI, wasm / JavaScript, Python bindings, `dyn Any`, trait-object registries, values computed at build time by `build.rs` or a procedural macro, which Cargo builds as a separate host instance) still carries and compares the identifier; the manifest lists the crate's erased boundaries |
| G8 | Law-bearing values written to a durable form carry their own identifier, distinct from G6's in-process one, describing the meaning of the stored bytes rather than the platform that wrote them. | wording |
| G9 | The G8 identifier is defined against the crate's reference platform; bringing another architecture to bit-for-bit agreement with it is a fix, not a bump. An actual meaning change moves the identifier and is recorded as semver-major. | none: the claim is kept as it was; the history audit (each move a major release naming both identifiers, no cross-architecture fix among them) is its evidence |
| G10 | Law-bearing output must not depend on an unseeded RNG, a freshly generated UUID, or an ambient wall-clock read; where randomness or time is part of the computation it is an explicit input. | wording: the evidence is a lint or scan with a canary |
| G11 | Every gate's own checker carries: (a) a canary case it must flag, (b) a zero-compared-fails check, and (c) where a checker or helper is meant to be shared across crates, a cross-repository byte-identity check. | wording |
| G12 | A crate's public repository runs the equivalent of this repository's own public_text_check against its own text; this gate references that requirement rather than redefining it. | **meaning**: "the equivalent" is defined as ALICE-Law's own script (fetched at a named revision or byte-identical); a crate's own checker no longer counts unless it is that file |
| G13 | Law-bearing output must not depend on HashMap/HashSet iteration order, parallel-reduction combination order, or thread-count/address-dependent ordering. | wording: membership-only uses may be allowlisted with a reason |
| G14 | Shipped Law-bearing artifacts are not built with fast-math-style flags or target-cpu=native; round-to-nearest-even and no-flush-to-zero are asserted by test, not assumed. | wording |
| manifest | `status` = `pass`, `missing`, or `n/a` | **meaning**: `missing` split into `fail-known` (checked, unmet) and `unverified` (not checked); `mutant` mandatory for `pass` and `n/a`; a `pass` without evidence or mutant reads as `unverified` |

## Out of scope

The following are explicitly not covered by these gates, each for a stated
reason:

- **LLM/inference numerics** — an inference runtime's own floating-point
  behaviour is outside a crate's control in the way a Law-bearing crate's own
  arithmetic is not; it is not persisted or compared bit-for-bit by this
  repository's checks.
- **ML float activations** — a model's internal activations are not
  Law-bearing values in this repository's sense: they are not pinned
  constants or persisted, compared values, and a platform difference in them
  is not a conformance question this document answers.
- **Telemetry floats** — a value reported only for observability, never read
  back or compared bit-for-bit, does not carry the correctness requirement
  these gates exist for.
