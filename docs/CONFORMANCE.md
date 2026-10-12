# Conformance

A Law-bearing crate computes and persists floating-point values whose bits must
mean the same thing wherever they are produced or read back. This document is
the human-readable version of what that requires; `conformance/gates.toml` is
the machine-readable version a generator reads to build a matrix of which
crate meets which requirement, with what evidence.

The crates this applies to are the crates in the release train
(`release-train.toml`), not a fixed list repeated here: a crate joins or
leaves conformance tracking by joining or leaving the train, in one place.

Contents: [Gates](#gates) · [Manifest](#manifest) · [Out of scope](#out-of-scope)

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

Out of scope: `sqrt` (IEEE 754 requires it correctly rounded, so every
conforming platform already agrees) and `powi` (repeated squaring from an
integer exponent, portable by construction, not a transcendental lookup).

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

### G4 — debug-release-arch-goldens

A Law-bearing crate's test suite checks every signed-zero and
exact-cancellation case it has identified against a fixed expected bit
pattern, run under both `debug` and `release` profiles on both `x86_64` and
`aarch64`. Agreement across profile and architecture is the property G1–G3
exist to guarantee; this gate is where that guarantee is actually exercised
against concrete inputs, including ones no static checker reaches.

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

### G7 — handshake-carries-id

Whenever one Law-bearing crate's output is consumed as another's input
(directly, or over a boundary such as a saved file or a network call), the
producer's G6 identifier travels with the value, and the consumer compares
it against its own. A mismatch is a distinct, explicit error (not a silent
fallback, not a generic parse failure) carrying both identifiers, so the two
sides of a mismatch are always named.

### G8 — persisted-semantics-id

Where a crate's Law-bearing values are written to a durable form (a file
format, a database row, a wire format meant to be read back later), that
persisted form carries its own identifier, separate from G6's in-process one:
a value read back after a crate upgrade is compared against the identifier
recorded when it was written, not against the identifier of whichever crate
version happens to be reading it now. This identifier is about the meaning
of the stored bytes, not about which platform produced them.

### G9 — persisted-meaning-change-is-major

The G8 identifier is defined against a crate's reference platform's bit
pattern, not against every platform's: bringing a second architecture's
output to bit-for-bit agreement with the reference platform is a bug fix on
that architecture, not a change to what the stored bytes mean, and does not
move the identifier. An identifier move is reserved for an actual change in
meaning — folding in a changed upstream Law, or changing the reference
platform's own computed value — and is recorded as a semver-major change
when it happens, with the identifiers either side of the change named.

### G10 — no-run-varying-artifacts

A Law-bearing crate's output must not depend on anything that varies between
runs for reasons unrelated to its inputs: an unseeded random number
generator, a freshly generated UUID, or the wall-clock time embedded directly
in a Law-bearing value. Where randomness or a timestamp is genuinely part of
what is being computed, it is an explicit input the caller supplies (and
which the gates above then apply to), not an ambient source the function
reaches for on its own.

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

### G12 — public-repo-text-rules

A Law-bearing crate's public repository follows the text rules this
repository's own `scripts/public_text_check.py` enforces on itself (no
internal process vocabulary, no private names, checked by hash rather than
by a plaintext list a check could itself leak). This gate does not redefine
those rules; it requires that a conforming crate run the equivalent check
against its own repository.

### G13 — no-unspecified-order

Where Rust itself leaves an order unspecified — `HashMap`/`HashSet`
iteration, the order a parallel reduction combines its partial results in,
anything that depends on thread count or on the relative order memory
addresses happen to land in — a Law-bearing crate's output must not depend
on it. Where an order matters to the result, the crate uses an
order-preserving collection or an explicitly associative, order-independent
reduction, not an incidental order that today's standard library or runtime
happens to produce.

### G14 — float-environment invariants

A Law-bearing crate's shipped artifacts are not built with flags that change
floating-point semantics from the ones the gates above assume: no
fast-math-style flag that permits reassociation or flushing subnormals to
zero, no `target-cpu=native` (which can select different instructions,
including fma, on the machine that happens to build it versus the machine
that runs it). The crate's tests assert the assumptions those flags would
violate directly — round-to-nearest-even, and that denormals are not flushed
to zero — rather than relying on nobody having set the flag.

## Manifest

Each crate's own manifest (`alice-law-conformance.toml` at its repository
root) has one entry per gate, with these fields:

| field | meaning |
|---|---|
| `status` | `pass`, `missing`, or `n/a` |
| `evidence` | mandatory when `status = "pass"`: the test file(s) or CI job that exercises the gate |
| `since` | the version or date the gate first passed |
| `note` | mandatory unless `status = "pass"`: why it is missing, or why it does not apply |
| `reference_repo` | the crate whose implementation this gate's mechanism was taken from or checked against, where one exists |

An `n/a` entry is not a silent skip: its `note` states what makes the gate
not apply (for example, a crate with no floating-point Law arithmetic at all
is `n/a` for G1–G3, with evidence that the same scanners that catch a
violation elsewhere find zero relevant call sites in this crate — absence
demonstrated, not assumed).

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
