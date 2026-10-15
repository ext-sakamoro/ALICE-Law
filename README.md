# ALICE-Law

Integration checks across the crates in the release train (`release-train.toml`): ALICE-LOL, ALICE-DetMath, ALICE-SDF, ALICE-Physics, ALICE-DB, ALICE-Zip, ALICE-Crypto, ALICE-Analytics, ALICE-Edge, ALICE-ML, ALICE-LLM, ALICE-Kinematics and ALICE-Sync. `scripts/readme_roster_check.py` fails this file's own check if this sentence falls behind the roster.

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
   - when a `must-contain` commit is not an ancestor of the commit, or the CHANGELOG has no `## [<version>] - YYYY-MM-DD` release heading (a real date, not later than tomorrow; checked for every crate) or its section lacks a `changelog-has` line;
   - when no recent commit carries a `must-contain-patch` patch;
   - when a `required-ci` job does not have exactly its expected legs in the latest run of its workflow on the commit, or a leg is not completed with success;
   - when the repository's Actions secrets (repository and environments, read from GitHub; reading nothing refuses) include a name the roster's `[secrets."<repository>"]` does not declare with its purpose (or the roster declares one the repository does not have), or a publishing credential outside an environment with required reviewers; when a job receives a secret (`secrets.<name>` other than `GITHUB_TOKEN`, `secrets: inherit`, an OIDC token) on a release, tag, dispatch or schedule trigger outside such an environment, unless every secret it receives is declared as not publishing; `secrets: inherit` only into a local workflow, which is read too; the repository's Actions variables (repository and environments) must be declared in `[variables."<repository>"]` with their purpose (a variable never holds a credential), Dependabot secrets in `[secrets]` as `dependabot:<name>`, and every list is read page by page and must hold as many names as GitHub counts; a job that may run on a self-hosted runner (a runner group, a label that is not one of the exact GitHub-hosted image labels, a label of a runner registered to the repository (read from GitHub), or an expression other than a matrix value whose literal values, or the literal lists an expression chooses between, are all GitHub-hosted images) on a release, tag, dispatch or schedule trigger runs under an environment with required reviewers or is declared in `[self-hosted."<repository>"]` (`"<workflow>:<job>"`, `registry-credentials-on-host = false`);
   - when a workflow of the repository at the commit can publish outside this gate (a tag push would publish without it): read by job, comments removed and continued lines joined, a crates.io credential (a crates.io token as a secret or in `CARGO_REGISTRY_TOKEN` / `CARGO_REGISTRIES_<name>_TOKEN`, the crates.io auth action, an OIDC token in a job that runs cargo) or a crates.io publish command (any `cargo … publish` without `--dry-run`, release-plz, publish-crates) anywhere; a PyPI / npm token, OIDC token or publish command in a job whose `environment` has no required reviewers (read from GitHub); reading no workflow compares nothing;
   - when the package (`cargo package --list`) carries files under `.github/`, `scripts/`, `docs/` or `logs/`, or private notes (file and directory names held as sha256 in the script): the repository's tools and notes, not the crate. Set `include` in the manifest.

   On go it runs `cargo package` (which builds the packaged crate as `cargo publish` does, `--locked` when a `Cargo.lock` is committed) and prints the commit, the sha256 of the `.crate` and the exact `cargo publish` command. After packaging it reads every file of the package with `scripts/public_text_check.py` (paths taken back to the repository, with its `.public-text.toml` and allow-list; a package that carries internal text is refused, one with no file compares nothing) and it checks the checkout again (`cargo publish` packages the working tree, so packaging must not have changed a tracked file, such as a lock rewritten because a sibling checkout has another version). Before go it also builds that `.crate` as a crates.io consumer does: unpacked into a fresh directory with no `Cargo.lock`, `Cargo.toml` or `.cargo/config` above it, with an empty `CARGO_HOME` (its registry cache is the gate's own directory) and a minimal environment (no `RUSTFLAGS`, `RUSTC_WRAPPER` or `CARGO_*` of the machine), as the path dependency of an empty crate, once with the default features, once with none, and once with every feature (features that cannot all be on together are refused, unless the roster names the sets to build in `consumer-features`; a feature whose library another tool links, such as a Python extension, is compiled with `cargo check` from `consumer-check-features`; `consumer-features-why` gives the reason, and a known failure of one set is listed in `consumer-expected-failure` with the error it must show (`expect = {code, file, text, count, label?, message?}`: every error of the crate itself in cargo's JSON diagnostics must have that code, file, highlighted source text and, when given, the primary span's label ("expected `…`, found `…`"), and there must be exactly `count` of them, so another error next to it, another error with the same code in the same file, one more of the same, or the code only in a comment is refused) and the release it is fixed by, after which it is refused) (with `consumer-features-why`, the features left out and why: features built by another tool, such as a Python extension, or for one platform's hardware); a crate with no library is built on its own, from a directory outside it; a `.crate` carrying a Cargo configuration (`.cargo/config`, `.cargo/config.toml`) is refused. A failed build, or one that compiled no library (or binary) of the crate, is refused (`cargo package` verifies inside the repository, where a build script reading a lock above the crate still finds the repository's). With a committed `Cargo.lock` the `.crate` is the same bytes as the one `cargo publish` uploads; without one, cargo resolves the lock it puts in the package at that moment, so publish right after the gate.
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

This repository is public, and so is everything it publishes: file contents, paths and commit messages. `scripts/public_text_check.py` checks them for internal vocabulary (development-process words, internal tracker and note names, instruction sources, device names (owned equipment, in any case, including the embedded board families; a platform or architecture class such as a CPU family, a GPU memory class, a wheel target or a CI runner architecture is technical text and not a device, so it is deliberately not in the list) and their SDK / firmware details (the SDK name with a version, the board support package name, a release string of the form R<major>.<minor> on or next to a line naming a device), host names of private machines (held as SHA-256), private network addresses, private names held only as SHA-256 hashes, and id-like tokens (two hex digits) next to a word naming a review or a session (`レビュー`, `review`, `指摘`, `検証`, `session` / `セッション` before or after), and the labels of review findings, an F and a number named as a review; no id is listed, since a list of two-digit ids is public even when hashed); also the word for a work period used as a unit of the work (written right after Japanese text, at the start of a line before Japanese text, in its multi- form, with a letter, or as the next-one phrase split over two comment lines), any `[[name]]` link with an underscore or a hyphen (not a TOML array-of-tables header at the start of a line in a `.toml` file or `Cargo.lock`), private note names in prose (after the word for the note store, `<prefix>_<name>.md`, the frontmatter keys of a note), a user's home directory (`/Users/<name>/`, `/home/<name>/` where a path starts, or after `file://`, not inside a web URL; the CI runner's home and placeholders such as `<you>` or `$USER` are not a name), and a home-directory path into a private repository (`~/<repo>/`, `/Users/<user>/<repo>/`; the repository names are held as SHA-256 inside the script, so a repository that fetches the script alone gets them too; an empty name set stops the check with exit 2 instead of comparing nothing). In a repository whose own instruction package lives in one directory, a path that starts with that directory (in any file of that repository, also after `./` or `../`) names the package and is not the agent word; each entry is `sha256(<repository> NUL <path prefix>)`, and any other such path, the same path in another repository and the word in prose stay findings. The agent word that is also the name of the Cloudflare runtime crate is not a finding only in that crate's places (`crates/<that name>/`, `migrations/`, `wrangler.toml`, a `Cargo.lock` package line) and under the `technical-paths` a repository lists in its `.public-text.toml`; that file can also declare `upstream-fork = true` (with `upstream-fork-why` and `upstream-base`, the full SHA of the fork point, an ancestor of the scanned commit): in a fork, `--range` reads every commit after the fork point whoever wrote it and `--tree` reads the files added or changed after it. The same file can allow a kind of finding in a scope at a pinned number of lines, with a reason (a vocabulary linter's own word list and fixtures): an `allow` array of tables under `[public-text]` with `path` (a file, or a directory ending in `/`), `class` (agent process, work session, session name, session id, internal tracker, note link, internal note, instruction source, device or private address; session name, session id, device and private address only for a single file, the linter that defines them; never a private name or a private path), `count` and `why`; in `--tree` another number of lines fails, and `--suggest-allow <ref> <scope>` prints the entries with the counts found there. In `--range`, a commit that changes `.public-text.toml` is a finding ("policy config changed", with the keys), and the summary prints the settings in force. A binary file is read as its printable runs of six or more characters, in ASCII and in UTF-16, keeping the tokens of four or more characters (compressed text, such as a PNG zTXt chunk or a PDF stream, is not decompressed), with the vocabulary of ALICE-Physics `scripts/docs_lint.py`. A finding names the place and the kind of word, not the word. A product that names another company's product as a feature (a fixed agent-instruction file name, a plugin manifest name, an extension's entry point) is allowed per repository through `ALLOWED` in the script: each entry is `sha256(<scope> NUL <name, agent word or [[link]], lower case>)` with the number of lines it allows, where the scope is a repository-relative file or a directory prefix ending in `/` (whole path components), built with `--allow-hash <scope> <name>`, so it neither names the product nor allows it elsewhere; in `--tree` an entry that allows another number of lines than its count (1 or more) fails (a new or removed mention, a mistyped entry), commit messages are never allowed, and development-process mentions are never listed. `--range` reports the files it read next to the commits.

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
2. compute: a thermal field law checked by alice-lol (solved by alice-physics), a rod cooling with `alice_physics::transient_thermal`, two bodies falling in a `PhysicsWorld`; equal zeros (+0 and -0) meeting a min or a max on public paths of alice-physics (`SdfUnion`, `ScalarField3D::max_value`) and alice-sdf (union, intersection and subtraction of coincident boxes), where `f32::max` / `f32::min` leave the sign to the target and x86_64 and aarch64 differ; and alice-edge's fixed-point line fit of an integer sensor window and the `SignalLaw` it makes of it (`alice_edge::law::linear_law`)
3. fit: an alice-zip `SignalLaw` to the cooling curve, and its residual summary from alice-analytics
4. save: every value through the exact series of alice-db (`AliceDB::series`, not the fitted model store) and both laws (the cooling law and alice-edge's) through its law store, to a directory
5. restore: reopen the directory and read everything back
6. compare: every value by `to_bits`, the law coefficients, domain, record and identifier, every field of the residual summary, and the alice-crypto digest of the whole run

It also checks that the crates were built with one arithmetic (the `SEMANTICS_ID` of alice-det-math as alice-zip, alice-db, alice-analytics, alice-edge and alice-lol see it, equal to the identifier recorded for the linked alice-det-math release in `KNOWN_SEMANTICS`, with the identifiers of the earlier releases there accepted by `alice_det_math::check_semantics` as verified earlier ones), that every `alice-*` crate is locked at one version, and that the nine core crates it names are in the build (`e2e/tests/law_to_storage_roundtrip.rs`; not derived from the roster, so a repository joining the train does not reach this check on its own). Each comparison fails when it compared nothing. A golden digest of the computed run (`e2e/golden/run.digest`, one line `<alice-det-math version> <digest>` per release: the law records carry identifiers derived from the arithmetic, so each release has its own digest) says whether the crates still compute what they computed when it was recorded; the round trip says that saving and restoring loses nothing. In `.github/workflows/e2e-main.yml` the same run is made on an aarch64 and an x86_64 runner, each writes its digest (`ALICE_LAW_E2E_DIGEST_OUT`), and a last job requires the two to be equal to each other and to the golden line of their alice-det-math release.

```sh
cd e2e
cargo test --release -- --nocapture             # published releases (resolved against crates.io)
ALICE_LAW_E2E_BLESS=1 cargo test --release      # record the golden digest of the linked alice-det-math release again
```

Two modes run the same test:

| mode | crates | workflow | meaning |
|---|---|---|---|
| published | crates.io releases | `e2e.yml` | the gate: red until every crate it needs is released at versions that fit together |
| main | the main branches, side by side, patched in by `scripts/main_mode.py` | `e2e-main.yml` | early warning: what the next releases would change, and which crate on main is not taken because its version does not fit the others |

`scripts/e2e_teeth.py` (main mode, the teeth job of `e2e-main.yml`) changes one line at a time in the checkouts and requires the test to fail with the expected message: a law coefficient decoded one ulp off, an f64 saved through f32, a restore that returns nothing, a flipped digest byte, `exp` from the platform libm, a slightly longer thermal step, a different arithmetic identifier, a residual one ulp off, and a second version of alice-zip in the build.

## Steps that fail until a release

A CI step that cannot pass before a release (for example the end to end crate resolving a crate version from crates.io before it is published, or the main branches patched in together while one of them still requires the previous major of another) is wrapped in `scripts/expected_failure.py <step> -- <command>` and listed in `release-train.toml` under `[expected-failure.<name>]` with the release it waits for (`until`), the text of the error lines its failure has (`expect`) and why (`why`). The failure is read from its own error lines (after terminal colour codes are removed, a line starting with `error` in any case followed by `:`, `[...]`, a space or the end of the line, `fatal:`, or a Rust panic; an entry can also name the exit status with `exit`): every one must contain an `expect` text and every `expect` text must be in one, so a second, unrelated error or a log line that only quotes the text does not pass. It is then reported as expected and the steps that need it are skipped; every run lists the active entries (step, until, why) in the job summary, and `expected_failure.py --check` fails on an entry no workflow runs and on an entry wrapping a `cargo` command without `exit = 101`. The step is red when it fails another way, when it still fails after its release is `published` in the roster, and when it passes while it is still listed (remove the entry). So a red run is a problem to fix.

## Status

Measured on 2026-10-13 (the committed `Cargo.lock`): **the check passes with no expected problems**. alice-lol 0.4.0, alice-lol-macro 0.3.0, alice-physics 3.0.0, alice-sdf 6.0.0 and alice-edge 0.2.0 are published, every `alice-*` crate is linked at one version, and this repository's own requirements accept every latest release (alice-det-math 0.5, alice-zip 0.10, alice-analytics 0.5, alice-db 0.4, alice-crypto 0.4, alice-physics 3, alice-sdf 6, alice-lol 0.4, alice-edge 0.2 with its sdf, db and dashboard features).

End to end, measured on 2026-10-13:

- published: passes, 3897 values and records, the same digest as main on aarch64 and x86_64 (`966a7878…`, `e2e/golden/run.digest`; without the alice-edge records `58ad34a7…`)
- main: passes, 3897 values and records, the same digest on aarch64 and x86_64 (`966a7878…`, `e2e/golden/run.digest`; without the alice-edge records `58ad34a7…`)

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
