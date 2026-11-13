# ALICE-Law

Integration checks across the crates in the release train (`release-train.toml`): ALICE-LOL, ALICE-DetMath, ALICE-SDF, ALICE-Physics, ALICE-DB, ALICE-Zip, ALICE-Crypto, ALICE-Analytics, ALICE-Edge, ALICE-ML, ALICE-LLM, ALICE-Kinematics, ALICE-Sync, ALICE-Identity, ALICE-Channel, ALICE-Sign, ALICE-Font and ALICE-OS-Keystore. `scripts/readme_roster_check.py` fails this file's own check if this sentence falls behind the roster.

[日本語](README_JP.md)

No path dependencies. Two kinds of check:

- dependency consistency (in place, from the core crates' main branches): every `alice-*` crate resolves to exactly one version in `Cargo.lock`, every crate `scripts/dep_consistency.py`'s own `CORE` list names is present, and every `alice-*` package of the lock file comes from its repository's main branch
- end to end (from the core crates' main branches): read a law, compute, save, restore, and compare every value bit for bit

It also holds the roster of the core crates at their main commits (and the scripts that record and check it: [runbook](#runbook-record-and-check)) and the checks of the text this repository publishes.

Contents: [Dependency consistency](#dependency-consistency) · [Release train](#release-train) · [Public text](#public-text) · [Conformance](#conformance) · [End to end](#end-to-end) · [Steps that fail until a version](#steps-that-fail-until-a-version) · [Status](#status) · [License](#license)

## Dependency consistency

`Cargo.toml` names each core crate from its repository's main branch (git, `branch = "main"`, with the version on main), with the features that turn on their dependencies on each other, so the lock file shows every link between them.

```sh
cargo generate-lockfile                 # resolve the main branches as they are today
python3 scripts/dep_consistency.py      # Markdown report; exit 1 on a problem, 2 when a check compared nothing
```

The checks:

1. one version per crate: two versions of one crate in a lock file are two copies of its types and its arithmetic, and a value saved through one is not the same type as the value the other reads back
2. presence: every crate `CORE` names is in the lock file
3. main branches: every `alice-*` package of the lock file comes from its repository's main branch (`git+https://github.com/ext-sakamoro/<repository>?branch=main#<commit>`), and every `alice-*` dependency of this repository's `Cargo.toml` names one (`git`, `branch = "main"`, a version). A crates.io release, a commit or tag pin, or another branch of a crate is a second generation of it beside main

A problem that a release still to come removes is listed in `release-train.toml` under `[pending.<name>]`, with that release (`until = "<crate> <version>"`) and the problems it removes (`linked = [<crate>, ...]`, `source = [<crate>, ...]`). With `--roster release-train.toml` (as CI runs it) the report counts them as expected per entry. A problem that is not listed fails the run, as does an entry whose release the roster marks `published` (stale: remove it) and a listed item that matches no problem (out of date). This repository's own dependencies and a missing core crate cannot be listed: they are changed or added, not waited for. So a red run is a problem to fix, not a release still to come.

Each check reports how many comparisons it made, and a check that made none fails the run (exit 2), as does a dependency of this repository on an `alice-*` crate by path or without a version. This repository's own package is not one of the crates checked.

The requirement matching (used by the release train below) follows Cargo's rules (carets, tildes, ranges, wildcards such as `1.2.*` and `1.x`, pre-releases). `scripts/semver_differential.py` compares it with the [semver](https://crates.io/crates/semver) crate (`tools/semver-oracle`) on generated requirement / version pairs and on a fixed grid of every operator and requirement shape against the versions one step away, and fails on any disagreement, including a requirement that one of the two reads and the other does not.

CI (`.github/workflows/dependency-consistency.yml`) runs the tests, the comparison with the semver crate and the check every Monday, on demand, and on a push that changes the check. The report is in the job summary.

## Release train

`release-train.toml` names each core crate at a commit of its repository's main branch: for each crate, `sha` (that commit), `version` (the version its manifest has there), its place in the order (a crate comes after every roster crate its manifest depends on), its state (`main`, or `exempt` for this repository) and its repository, with the manifest path where the repository holds more than one crate. Nothing is published to crates.io: the consumers take each crate from its repository's main branch. What a crate requires of the others is read from its own `Cargo.toml` at `sha`, so there is one source for it. `sha` and `version` are written by `record`, never by hand.

### Runbook: record and check

```sh
S=scripts/release_train.py
python3 $S record --repos-root <dir with every roster checkout>     # 1. write sha and version (refuses a commit whose main CI is not green)
python3 $S check --repos-root <dir with every roster checkout>      # 2. exit 0 = every row is at a green main commit
python3 $S followups <crate> --repos-root <dir with every roster checkout>   # 3. who has to raise a requirement
```

Then commit `release-train.toml`, after the e2e bless when `e2e/golden/values.provenance` changed (`record` takes the provenance commit for the repositories it records).

1. `record --repos-root <dir> [--provenance <file>] [--no-ci]` sets each row's `sha` to the commit `e2e/golden/values.provenance` records for its repository, or to `origin/main` of the checkout for the others, and `version` to its manifest's version there; the rest of the file is kept. It writes nothing when a commit's push runs on main are not all green.
2. `check --repos-root <dir> [--provenance <file>] [--no-ci]` refuses a row whose commit is not on main (not an ancestor of `origin/main`, so fetch the history first), whose manifest there is another crate or version, whose repository the provenance records at another commit, that comes before a roster crate its manifest depends on, whose workflows at the commit can publish to a registry (a crates.io credential or publish command anywhere; a PyPI / npm one without an environment that has required reviewers), or whose push runs on main are not all completed with success. A red scheduled or dispatched run is printed as a note (its own gate tracks it); no push run at all is a refusal unless every workflow of the commit is filtered out for its change, which is printed. A private repository is refused (its CI is not readable here).
3. `followups <crate> --repos-root <dir> [--at origin/main]` reads every `Cargo.toml` at `--at` in the checkouts of every roster repository (fetch first) and lists each requirement on the crate (normal, dev, build, target, workspace) that does not accept its roster version, and each one without a version outside the crate's own repository. Exit 1 while there is one to raise.

A command that compared nothing (no roster crate, no manifest read) exits 2. The crates.io publish gate (`publish-gate`, `publish-verify`, `roster-mark`, `next`) was removed on 2026-11-13 and is in this file's history. CI runs the tests (`scripts/test_release_train.py`, `scripts/test_release_train_main.py`, checkouts and GitHub replaced by fixtures) and `check` on fresh checkouts of every roster repository.

## Public text

This repository is public, and so is everything it publishes: file contents, paths and commit messages. `scripts/public_text_check.py` checks them for internal vocabulary (development-process words, internal tracker and note names, instruction sources, device names (owned equipment, in any case, including the embedded board families; a platform or architecture class such as a CPU family, a GPU memory class, a wheel target or a CI runner architecture is technical text and not a device, so it is deliberately not in the list) and their SDK / firmware details (the SDK name with a version, the board support package name, a release string of the form R<major>.<minor> on or next to a line naming a device), host names of private machines (held as HMAC-SHA256), private network addresses, private names held only as HMAC-SHA256 (under a key kept outside the repository, `ALICE_PRIVATE_NAME_HMAC_KEY`: this repository's own runs fail without it, and another repository that runs the script without it gets every other check and a notice that the private names were not checked). Who holds the key: this repository's `Public text` on a push and a manual run, `Public text baseline (ci)`, the public-text job of Conformance (live), and a local preflight with the key file; a pull request from a fork and every other repository run without it. The skip is safe because Conformance (live) reads every public repository daily with the key (`scripts/public_text_live.py`: a repository it could not read, or nothing compared, is red). The allow table (`ALLOWED`) keys an entry the same way when its word is a private name or a note link, or when the repository is private (`k:` before the HMAC); an entry for a public word in a public repository's own path, and the path prefixes of a product's own package directory (above), stay SHA-256, since each joins two things that repository already publishes, and a repository without the key still applies them, and id-like tokens (two hex digits) next to a word naming a review or a session (`レビュー`, `review`, `指摘`, `検証`, `session` / `セッション` before or after), and the labels of review findings, an F and a number named as a review; no id is listed, since a list of two-digit ids is public even when hashed); also the word for a work period used as a unit of the work (written right after Japanese text, at the start of a line before Japanese text, in its multi- form, with a letter, or as the next-one phrase split over two comment lines), any `[[name]]` link with an underscore or a hyphen (not a TOML array-of-tables header at the start of a line in a `.toml` file or `Cargo.lock`), private note names in prose (after the word for the note store, `<prefix>_<name>.md`, the frontmatter keys of a note), a user's home directory (`/Users/<name>/`, `/home/<name>/` where a path starts, or after `file://`, not inside a web URL; the CI runner's home and placeholders such as `<you>` or `$USER` are not a name), and a home-directory path into a private repository (`~/<repo>/`, `/Users/<user>/<repo>/`; the repository names are held as HMAC-SHA256 inside the script, so a repository that fetches the script alone gets them too; an empty name set stops the check with exit 2 instead of comparing nothing). In a repository whose own instruction package lives in one directory, a path that starts with that directory (in any file of that repository, also after `./` or `../`) names the package and is not the agent word; each entry is `sha256(<repository> NUL <path prefix>)`, and any other such path, the same path in another repository and the word in prose stay findings. The agent word that is also the name of the Cloudflare runtime crate is not a finding only in that crate's places (`crates/<that name>/`, `migrations/`, `wrangler.toml`, a `Cargo.lock` package line) and under the `technical-paths` a repository lists in its `.public-text.toml`; that file can also declare `upstream-fork = true` (with `upstream-fork-why` and `upstream-base`, the full SHA of the fork point, an ancestor of the scanned commit): in a fork, `--range` reads every commit after the fork point whoever wrote it and `--tree` reads the files added or changed after it. The same file can allow a kind of finding in a scope at a pinned number of lines, with a reason (a vocabulary linter's own word list and fixtures): an `allow` array of tables under `[public-text]` with `path` (a file, or a directory ending in `/`), `class` (agent process, work session, session name, session id, internal tracker, note link, internal note, instruction source, device or private address; session name, session id, device and private address only for a single file, the linter that defines them; never a private name or a private path), `count` and `why`; in `--tree` another number of lines fails, and `--suggest-allow <ref> <scope>` prints the entries with the counts found there. In `--range`, a commit that changes `.public-text.toml` is a finding ("policy config changed", with the keys), and the summary prints the settings in force. A binary file is read as its printable runs of six or more characters, in ASCII and in UTF-16, keeping the tokens of four or more characters (compressed text, such as a PNG zTXt chunk or a PDF stream, is not decompressed), with the vocabulary of ALICE-Physics `scripts/docs_lint.py`. A finding names the place and the kind of word, not the word. A product that names another company's product as a feature (a fixed agent-instruction file name, a plugin manifest name, an extension's entry point) is allowed per repository through `ALLOWED` in the script: each entry is `sha256(<scope> NUL <name, agent word or [[link]], lower case>)` with the number of lines it allows, where the scope is a repository-relative file or a directory prefix ending in `/` (whole path components), built with `--allow-hash <scope> <name>`, so it neither names the product nor allows it elsewhere; in `--tree` an entry that allows another number of lines than its count (1 or more) fails (a new or removed mention, a mistyped entry), commit messages are never allowed, and development-process mentions are never listed. `--range` reports the files it read next to the commits.

```sh
python3 scripts/public_text_check.py --tree                     # every tracked file at HEAD
python3 scripts/public_text_check.py --range origin/main..HEAD  # the messages, added lines and added paths of the commits
scripts/preflight.sh                                            # the script tests and both scans, before a push
```

CI (`.github/workflows/public-text.yml`) runs the tests and both scans on every push and pull request. A scan that compared nothing (no file, no commit) fails.

`README_JP.md` is the Japanese text of `README.md`. The prose is translated by hand; `scripts/readme_parity_check.py` fails when the two differ in what a translation must keep: the headings (paired in order in `scripts/readme_headings.toml`, so a section moved, dropped or renamed in one file is found), the number of paragraphs and list items in each section, the code blocks, the inline code, the values, the names and the links. It does not compare the meaning of the prose: a reworded sentence, an added negation, or a single digit or a number written as a word changed inside a paragraph that keeps its place passes.

## Conformance

`docs/CONFORMANCE.md` is the human-readable specification of what a Law-bearing crate's arithmetic must guarantee — 15 gates, from banning a direct call to a platform transcendental function (G1) through banning a build flag that changes floating-point semantics (G14) and checking the surfaces a crate says it can be used from against what it has and what CI builds (G15, phased in until 2026-12-12). `conformance/gates.toml` is the same 15 gates in the form a generator reads: id, name, one-line definition, and the kinds of evidence a passing crate must cite.

Each crate in the release train carries its own `alice-law-conformance.toml` at its repository root, one entry per gate (`status`: `pass` / `fail-known` / `unverified` / `n/a`, `evidence`, `mutant`, `since`, `note`, `reference_repo`); this repository does not hold those manifests itself, only the gates they answer against. `scripts/conformance_roster_check.py` reads every release-train crate's manifest at its main (through `gh api`: the file tree and the manifest) and fails on a crate with no manifest, a gate with no entry, an unknown gate or a key spelled like one in another form (`g15`, `G01`), a status outside the gate's vocabulary (case-sensitive), an evidence entry whose first word is a path that is not at main (after backticks, a `label:` prefix, a leading `./` and a `:<line>` / `::<name>` suffix are removed; a bare file name must match exactly one file), a mutant that is not a string, or a `fail-known` entry that does not locate its site: its note or evidence must name a file at main by its path (not a directory, and not a root file describing the crate such as `Cargo.toml` or `README.md`), or by a bare name that matches exactly one file and has a line number after it, or that names one workflow (where a step or a leg is absent); a path written as another repository's (`ALICE-Law's scripts/...`) does not count, and a line number is not checked against the file's length. A `pass` / `n/a` entry is counted as `unverified` unless an evidence entry names a path at main and its mutant is a non-empty string. A crate whose manifest is not written yet is listed by name in `conformance/missing-manifests.toml` (with the repository whose change adds it), and an entry whose manifest has landed fails as stale. The status counts of each repository's manifest, with the crates it is read for (crates that share a repository share its one manifest), are computed from the mains on each run and published in the Conformance (live) job summary (`--summary`); this repository records none of them, so a crate's landing needs no change here. Nothing compared exits 2.

## End to end

`e2e/` is a separate crate with one test, `tests/law_to_storage_roundtrip.rs`, that goes through the core crates with their public APIs. `e2e/Cargo.toml` names each core crate from its repository's main branch (git, `branch = "main"`, with the version on main):

1. read: Newton's law of cooling, written as text, through the alice-lol research law reader, evaluated on a time grid
2. compute: a thermal field law checked by alice-lol (solved by alice-physics), a rod cooling with `alice_physics::transient_thermal`, two bodies falling in a `PhysicsWorld`; equal zeros (+0 and -0) meeting a min or a max on public paths of alice-physics (`SdfUnion`, `ScalarField3D::max_value`) and alice-sdf (union, intersection and subtraction of coincident boxes), where `f32::max` / `f32::min` leave the sign to the target and x86_64 and aarch64 differ; and alice-edge's fixed-point line fit of an integer sensor window and the `SignalLaw` it makes of it (`alice_edge::law::linear_law`)
3. fit: an alice-zip `SignalLaw` to the cooling curve, and its residual summary from alice-analytics
4. save: every value through the exact series of alice-db (`AliceDB::series`, not the fitted model store) and both laws (the cooling law and alice-edge's) through its law store, to a directory
5. restore: reopen the directory and read everything back
6. compare: every value by `to_bits`, the law coefficients, domain, record and identifier, every field of the residual summary, and the alice-crypto digest of the whole run

It also checks that the crates were built with one arithmetic (the linked alice-det-math's `SEMANTICS_ID` equal to the identifier recorded for its release in `KNOWN_SEMANTICS`, with the identifiers of the earlier releases there accepted by `alice_det_math::check_semantics` as verified earlier ones; the identifiers of alice-zip, alice-db, alice-analytics and alice-edge each the fold of their own pins and of the pins of the alice-det-math functions they call, each of those the linked alice-det-math's own, and alice-lol pinning each alice-det-math function it calls), that every `alice-*` crate is locked at one version, and that the nine core crates it names are in the build (`e2e/tests/law_to_storage_roundtrip.rs`; not derived from the roster, so a repository joining the train does not reach this check on its own). Each comparison fails when it compared nothing. A golden digest of the computed values (`e2e/golden/values.digest`, one line `<alice-det-math version> <digest>`; `e2e/golden/values.provenance` names the main commit of each repository it was recorded from) says whether the crates still compute what they computed when it was recorded; the round trip says that saving and restoring loses nothing. In `.github/workflows/e2e-main.yml` the same run is made on an aarch64 and an x86_64 runner, each writes its digest (`ALICE_LAW_E2E_DIGEST_OUT`), and a last job requires the two to be equal to each other and to the golden line of their alice-det-math release.

```sh
cd e2e
cargo test --release -- --nocapture             # the main branches (resolved by git)
ALICE_LAW_E2E_BLESS=1 cargo test --release      # record the golden digest of the linked alice-det-math release again (update values.provenance with it)
```

In CI (`e2e-main.yml`) the repositories are checked out side by side and `scripts/main_mode.py` patches the git sources with the checkouts, so a run names the commit of each repository. It fails when a core crate on main is not taken (its version does not fit what another core crate requires, so a second copy is linked beside the checkout) and when `e2e/Cargo.toml` does not accept a new major on main.

`scripts/e2e_teeth.py` (the teeth job of `e2e-main.yml`) changes one line at a time in the checkouts and requires the test to fail with the expected message: a law coefficient decoded one ulp off, an f64 saved through f32, a restore that returns nothing, a flipped digest byte, `exp` from the platform libm, a slightly longer thermal step, a different arithmetic identifier, a residual one ulp off, and a second version of alice-zip in the build.

## Steps that fail until a version

A CI step that cannot pass before a version is on main (for example the main branches patched in together while one of them still requires the previous major of another) is wrapped in `scripts/expected_failure.py <step> -- <command>` and listed in `release-train.toml` under `[expected-failure.<name>]` with the version it waits for (`until`, a roster crate and its version), the text of the error lines its failure has (`expect`) and why (`why`). The failure is read from its own error lines (after terminal colour codes are removed, a line starting with `error` in any case followed by `:`, `[...]`, a space or the end of the line, `fatal:`, or a Rust panic; an entry can also name the exit status with `exit`): every one must contain an `expect` text and every `expect` text must be in one, so a second, unrelated error or a log line that only quotes the text does not pass. It is then reported as expected and the steps that need it are skipped; every run lists the active entries (step, until, why) in the job summary, and `expected_failure.py --check` fails on an entry no workflow runs and on an entry wrapping a `cargo` command without `exit = 101`. The step is red when it fails another way, when it still fails once the roster records that version or a later one, and when it passes while it is still listed (remove the entry). So a red run is a problem to fix.

## Status

<!-- law-status:begin -->

Generated by `scripts/gen_law_status.py` from `release-train.toml`, `e2e/golden/values.digest` and `conformance/` (do not edit by hand; CI fails when it differs). The CI results are the workflows' runs.

### Crates at main commits

| crate | version | main commit | order | repository |
|---|---|---|---|---|
| alice-det-math | 0.6.1 | d80446ebce73 | 0 | ALICE-DetMath |
| alice-crypto | 0.7.0 | 33496b48d145 | 1 | ALICE-Crypto |
| alice-zip | 0.11.0 | c498e1769a71 | 2 | ALICE-Zip |
| alice-zip-cli | 4.0.0 | c498e1769a71 | 3 | ALICE-Zip |
| alice-analytics | 0.7.0 | cf16e1928fae | 4 | ALICE-Analytics |
| alice-db | 0.6.0 | 5db2b15cb148 | 5 | ALICE-DB |
| alice-llm | 5.0.0 | be521bda68c5 | 6 | ALICE-LLM |
| alice-lol-macro | 0.4.0 | 82a015737b94 | 7 | ALICE-LOL |
| alice-law-integration | 0.0.0 | (exempt) | 8 | ALICE-Law |
| alice-ml | 0.4.0 | f3cccc24e63b | 9 | ALICE-ML |
| alice-physics | 4.0.0 | 8ea9fb861946 | 10 | ALICE-Physics |
| alice-sdf | 7.0.0 | 0d5830e3f2c6 | 11 | ALICE-SDF |
| alice-edge | 0.4.0 | 5de16c5a1980 | 12 | ALICE-Edge |
| alice-sign | 0.1.0 | 41b19c231f36 | 13 | ALICE-Sign |
| alice-lol | 0.5.0 | 82a015737b94 | 14 | ALICE-LOL |
| alice-kinematics | 0.4.0 | 6919b0feddc4 | 15 | ALICE-Kinematics |
| alice-identity | 3.0.0 | 0e1809b95fdb | 16 | ALICE-Identity |
| alice-channel | 0.1.0 | 5f2b92cbd4ef | 17 | ALICE-Channel |
| alice-sync | 0.8.0 | ef7472614c9d | 18 | ALICE-Sync |
| alice-font | 0.8.0 | 00a0193ffd4d | 19 | ALICE-Font |
| alice-os-keystore | 0.2.0 | 99ac2a4e2df0 | 20 | ALICE-OS-Keystore |

### Steps expected to fail until a version

None.

### End to end values digest per alice-det-math release (from the main branches)

| alice-det-math | values digest |
|---|---|
| 0.6.1 | `66b99010e871b2bc099ad5a4dd0f7c34cc5d6731f1c2a0459c0dd58284b081d7` |

### Conformance manifests (15 gates)

The status counts of each repository's manifest at main are computed from the mains by the Conformance (live) workflow and shown in its job summary (they are not committed here).

### Crates whose manifest is not written yet

| crate | added in | why |
|---|---|---|
| alice-os-keystore | ALICE-OS-Keystore | the manifest has not been written yet |

<!-- law-status:end -->

## License

`LicenseRef-Proprietary`. The text is [LICENSE-Proprietary](LICENSE-Proprietary).

Enquiries: https://extoria.co.jp/

## Licences of the linked releases

The integration crate at the root links published releases, which keep their own
licences at the versions in Cargo.lock: alice-physics 3.1.0, alice-db 0.5.1
and alice-crypto 0.4.1 are `AGPL-3.0-or-later OR LicenseRef-Commercial`;
alice-lol 0.4.1, alice-zip 0.10.0, alice-analytics 0.5.0, alice-sdf 6.0.0
and alice-det-math 0.5.1 are `Apache-2.0`. The end to end test crate links the core
crates from their main branches, each under the licence of its repository.
