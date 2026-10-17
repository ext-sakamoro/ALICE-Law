#!/usr/bin/env python3
"""The release train of the core ALICE crates (release-train.toml), checked against crates.io.

    next                                 the crate to publish now: the first in `order`
                                         that is not published (or exempt)
    feature-coverage --repos-root DIR
                                         every crate that declares consumer feature sets:
                                         each feature Cargo has for it (cargo metadata of the
                                         checkout DIR/<repo>, implicit ones included) is
                                         enabled by a set, directly or through another
                                         feature, or is in `exclude-features` with a reason,
                                         and the sets name only its features; the working
                                         tree is read (no change to a tracked file); no
                                         crate compared or cargo metadata failing exits 2
    semver-config-drift --repos-root DIR [--at REF]
                                         every run of scripts/semver_gate.py in the
                                         workflows of a roster repository at REF: its
                                         --config carries the roster's consumer sets, reason
                                         and excluded features for the crate of its
                                         --manifest (none where the roster declares none);
                                         every roster repository must be checked out; no
                                         run compared exits 2
    publish-gate <crate> --repo DIR --ref SHA [--manifest-path P] [--repos-root DIR] [--consumers-at REF]
                                         run right before `cargo publish` of <crate> from
                                         the checkout DIR; the manifest (P, default
                                         Cargo.toml) and the repository's root Cargo.toml
                                         are read at SHA; refuses when
      - DIR is not checked out at SHA, or has changes to tracked files (cargo publish
        packages the working tree, so it must be the commit that was checked)
      - <crate> is not in the roster, or its state is not `prepared`
      - the manifest is not <crate> at its roster version
      - the manifest or the root manifest overrides an `alice-*` crate ([patch.*],
        [replace]) or names one with another registry (`registry`, `registry-index`)
      - <crate> is already on crates.io at that version
      - a consumer requires <crate> from the registry (a version, neither `path` nor `git`) with a
        requirement that does not accept the roster version: every Cargo.toml (workspace members too)
        at --consumers-at (default origin/main; fetch first) of every git checkout directly under
        --repos-root (default the parent of DIR), one per remote; a repository of the roster with no
        checkout there, or no manifest read at all, is refused too (path and git dependencies build
        against the repository's state and are checked when that state is sent, not here)
      - an `alice-*` dependency of the manifest (normal or build, optional or not, any
        target; read from the manifest itself, so the roster cannot list a different
        set) has no crates.io version requirement, or
          - is a roster crate that is not `published` in the roster, is missing or yanked
            on crates.io at its roster version, or whose roster version the requirement
            does not accept (Cargo's rules: "0.4" does not accept 0.5.0)
          - is not a roster crate and its latest release on crates.io is missing, yanked
            or not accepted by the requirement
      - the roster lists `must-contain` commits for <crate> and one of them is not an
        ancestor of SHA (or is not in the repository)
      - the roster lists `must-contain-patch` patch ids (`git patch-id --stable`) for
        <crate> and none of the last PATCH_DEPTH non-merge commits of SHA has that patch
        (a rebased or cherry-picked copy of a change counts; a conflict-resolved one does not)
      - the roster lists `changelog-has` lines for <crate> and the CHANGELOG.md next to
        the manifest (else the repository's) at SHA has no `## [<version>]` section, or
        the section does not contain each of them
      - the roster lists `required-ci` jobs for <crate> ({workflow, job, legs, since}; an
        entry with `since` applies from that version of the crate on) and, in
        the latest GitHub Actions run of that workflow on SHA, the job does not have
        exactly the expected legs (a count of "<job>" / "<job> (...)" names, or the exact
        names), or one leg is not completed with success (skipped is not success)
      - a workflow of the repository at SHA publishes to crates.io (a `cargo publish` step
        without --dry-run, or a publish action): a tag push would publish outside this gate
      - the package (`cargo package --list` of the manifest) carries a file under
        .github/, scripts/, docs/ or logs/, or a file or directory of private notes (named by
        hash in PRIVATE_NAME_SHA256): the repository's own tooling and notes, not the
        crate; a package that lists no file exits 2
      - the roster declares consumer feature sets for <crate> and a feature Cargo has for it
        at SHA (cargo metadata of DIR, the implicit feature of an optional dependency
        included) is enabled by none of them, directly or through another feature, nor in
        `exclude-features`, or a set names a feature it does not have (a declared set does
        not grow with the manifest: a feature added later would be neither built nor
        semver-checked); cargo metadata failing exits 2
      - after those pass, the crate `cargo package` built is unpacked into a fresh
        directory with no Cargo.lock or Cargo.toml above it and built as a dependency of
        an empty crate there, as a crates.io consumer builds it (`cargo package` verifies
        inside the repository, where a build script that reads a Cargo.lock above the
        crate still finds the repository's); a failed build, or one that did not compile
        the crate's library, is refused
      - then `cargo semver-checks` compares the checkout with the latest release on
        crates.io, once per feature set of the consumer build (default features, none,
        and the roster's sets or every feature): refused when it finds a change the
        version step does not allow (a minor version with a breaking change), when a
        minor or patch step compared 0 checks, when the step it reports is not the one
        the two versions make, or when it did not run; a major step is not compared
        (any change is allowed), and a crate with no release yet has nothing to compare

usage: release_train.py [--roster release-train.toml] {next | publish-gate <crate> --repo DIR --ref SHA [--manifest-path P]}
Exit 0 when the answer is "go" (next: a crate was found), 1 when it is "no", 2 when the
roster or the manifest is unreadable, nothing was compared, or crates.io could not be
read or answered something other than the version asked for.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import importlib.util
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("dep_consistency", HERE / "dep_consistency.py")
dc = importlib.util.module_from_spec(_spec)
sys.modules["dep_consistency"] = dc
_spec.loader.exec_module(dc)
_sg_spec = importlib.util.spec_from_file_location("semver_gate", HERE / "semver_gate.py")
sg = importlib.util.module_from_spec(_sg_spec)
sys.modules["semver_gate"] = sg
_sg_spec.loader.exec_module(sg)
# the feature sets and the semver step are shared with the crates' own CI (scripts/semver_gate.py,
# byte-identical in each repository): one definition, read here under the same names
feature_specs = sg.feature_specs
ANSI = sg.ANSI
version_step = sg.version_step
version_precedence = sg.version_precedence
semver_feature_args = sg.semver_feature_args
run_semver_checks = sg.run_semver_checks
semver_verdict = sg.semver_verdict
previous_release = sg.previous_release
semver_problems = sg.semver_problems

# what the published package must not carry: the repository's CI, tools and notes
PACKAGE_EXCLUDED_DIRS = (".github/", "scripts/", "docs/", "logs/")
# file / directory names of private notes, anywhere in the package: the sha256 of the
# lower-case name (the names themselves are not written in this public repository)
PRIVATE_NAME_SHA256 = frozenset({
    "e026f56c08dcdea441a2aa620ab410ce91162062609b2430f5a13bc9b7cf93f1",
    "bccc86317c281a19e56f3f941077ea61b3fb37d5bd527c518b92b188c4461251",
})

STATES = ("planned", "prepared-local", "prepared", "published", "exempt")
DEFAULT_ROSTER = HERE.parent / "release-train.toml"


class RosterError(ValueError):
    pass


class RegistryError(RuntimeError):
    """crates.io answered, but not with the version asked for"""


def load_roster(text: str) -> dict[str, dict]:
    data = tomllib.loads(text)
    for repo_name, table in data.get("variables", {}).items():
        if not isinstance(table, dict) or not all(
                isinstance(d, dict) and set(d) == {"purpose"} and isinstance(d["purpose"], str) and d["purpose"].strip()
                for d in table.values()):
            raise RosterError(f"[variables.{repo_name}] entries must be {{ purpose = \"...\" }} (a variable is never "
                              f"a credential)")
    for repo_name, table in data.get("self-hosted", {}).items():
        if not isinstance(table, dict) or not all(
                isinstance(d, dict) and set(d) == {"purpose", "registry-credentials-on-host"}
                and isinstance(d["purpose"], str) and d["purpose"].strip() and d["registry-credentials-on-host"] is False
                and re.fullmatch(r"[A-Za-z0-9_.\-]+\.ya?ml:[A-Za-z0-9_.\-]+", key)
                for key, d in table.items()):
            raise RosterError(f"[self-hosted.{repo_name}] entries must be \"<workflow file>:<job>\" = {{ purpose = "
                              f"\"...\", registry-credentials-on-host = false }}")
    for repo_name, table in data.get("secrets", {}).items():
        if not isinstance(table, dict):
            raise RosterError(f"[secrets.{repo_name}] must be a table of secret names")
        for key, d in table.items():
            if (not isinstance(d, dict) or set(d) != {"purpose", "publishing"} or not isinstance(d["purpose"], str)
                    or not d["purpose"].strip() or not isinstance(d["publishing"], bool)):
                raise RosterError(f"[secrets.{repo_name}] {key} must be {{ purpose = \"...\", publishing = true|false }}")
    crates = data.get("crate", {})
    if not crates:
        raise RosterError("the roster lists no crate (compared nothing)")
    orders = set()
    for name, c in crates.items():
        for key in ("version", "state", "order"):
            if key not in c:
                raise RosterError(f"{name}: missing `{key}`")
        if "requires" in c:
            raise RosterError(f"{name}: `requires` is read from the crate's Cargo.toml, not the roster")
        if "law-dependency" in c and not isinstance(c["law-dependency"], bool):
            raise RosterError(f"{name}: `law-dependency` must be true or false")
        for key in ("must-contain", "must-contain-patch", "changelog-has"):
            if key in c and (not isinstance(c[key], list) or not c[key]
                             or not all(isinstance(x, str) and x.strip() for x in c[key])):
                raise RosterError(f"{name}: `{key}` must be a non-empty list of non-empty strings")
        if "required-ci" in c:
            jobs = c["required-ci"]
            if not isinstance(jobs, list) or not jobs:
                raise RosterError(f"{name}: `required-ci` must be a non-empty list of {{workflow, job, legs}}")
            for j in jobs:
                legs = j.get("legs", 1) if isinstance(j, dict) else None
                since = j.get("since") if isinstance(j, dict) else None
                if since is not None:
                    if not isinstance(since, str):
                        raise RosterError(f"{name}: required-ci `since` must be a version string")
                    dc.Version(since)  # raises on a malformed version
                if (not isinstance(j, dict) or set(j) - {"workflow", "job", "legs", "since"}
                        or not all(isinstance(j.get(k), str) and j[k].strip() for k in ("workflow", "job"))
                        or not ((isinstance(legs, int) and not isinstance(legs, bool) and legs > 0)
                                or (isinstance(legs, list) and legs and all(isinstance(x, str) and x for x in legs)))):
                    raise RosterError(f"{name}: required-ci entry {j!r} is not {{workflow, job, legs (a count > 0 "
                                      f"or the exact leg names)}}")
        for key in ("consumer-features", "consumer-check-features"):
            if key in c:
                fs = c[key]
                if (not isinstance(fs, list) or not fs
                        or not all(isinstance(x, list) and all(isinstance(f, str) and f.strip() for f in x) for x in fs)):
                    raise RosterError(f"{name}: `{key}` must be a non-empty list of feature lists")
        declared = "consumer-features" in c or "consumer-check-features" in c
        if declared:
            why = c.get("consumer-features-why")
            if not isinstance(why, str) or not why.strip():
                raise RosterError(f"{name}: `consumer-features` needs `consumer-features-why` (how the features are "
                                  f"built and why)")
        elif "consumer-features-why" in c:
            raise RosterError(f"{name}: `consumer-features-why` without `consumer-features`")
        if "exclude-features" in c:
            ex = c["exclude-features"]
            if not declared:
                raise RosterError(f"{name}: `exclude-features` without `consumer-features` (with no declared set "
                                  f"every feature is built at once, so there is nothing to exclude from)")
            if not isinstance(ex, dict) or not ex or not all(
                    isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip() for k, v in ex.items()):
                raise RosterError(f"{name}: `exclude-features` must be a table of feature = \"why it is not built\"")
            if "default" in ex:
                raise RosterError(f"{name}: `exclude-features` may not name `default` (it is not in the feature universe)")
            in_sets = set().union(*(set(x) for x in c.get("consumer-features", []) + c.get("consumer-check-features", [])))
            both = sorted(set(ex) & in_sets)
            if both:
                raise RosterError(f"{name}: {both} are both excluded and in a declared set")
        if "consumer-expected-failure" in c:
            sets = [sorted(x) for x in c.get("consumer-features", []) + c.get("consumer-check-features", [])]
            efs = c["consumer-expected-failure"]
            if not isinstance(efs, list) or not efs:
                raise RosterError(f"{name}: `consumer-expected-failure` must be a non-empty list")
            for e in efs:
                ex = e.get("expect") if isinstance(e, dict) else None
                if (not isinstance(e, dict) or not {"features", "expect", "until", "why"} <= set(e)
                        or set(e) - {"features", "expect", "until", "why", "command"}
                        or e.get("command", "build") not in ("build", "check")
                        or not isinstance(e["features"], list) or sorted(e["features"]) not in sets
                        or not isinstance(ex, dict) or set(ex) - {"code", "file", "text", "count", "label", "message"}
                        or not isinstance(ex.get("label", ""), str)
                        or not all(isinstance(ex.get(k), str) and ex[k].strip() for k in ("code", "file", "text"))
                        or not isinstance(ex.get("count"), int) or isinstance(ex.get("count"), bool) or ex["count"] < 1
                        or not isinstance(ex.get("message", ""), str)
                        or not all(isinstance(e[k], str) and e[k].strip() for k in ("until", "why"))
                        or len(e["until"].split()) != 2 or e["until"].split()[0] != name):
                    raise RosterError(f"{name}: consumer-expected-failure entry {e!r} is not {{features (one of the "
                                      f"declared sets), expect = {{code, file, text, count, label?, message?}}, until = \"{name} "
                                      f"<version>\", why, command?}}")
                dc.Version(e["until"].split()[1])
        if c["state"] not in STATES:
            raise RosterError(f"{name}: state {c['state']!r} is not one of {STATES}")
        dc.Version(c["version"])  # raises on a malformed version
        if c["order"] in orders:
            raise RosterError(f"{name}: order {c['order']} is used twice")
        orders.add(c["order"])
    return crates


def manifest_alice_requirements(text: str) -> tuple[str, str, dict[str, str]]:
    """(package name, package version, {alice crate: requirement}) of a Cargo.toml

    Every table cargo resolves at publish time is read: `[dependencies]` and
    `[build-dependencies]`, also under `[target.<cfg>]`; optional dependencies
    included; a renamed dependency (`package = "alice-…"`) under its crate name.
    A dependency without a version requirement (path only, git, workspace) is an
    error: crates.io would refuse it or resolve it from somewhere this cannot see.
    """
    data = tomllib.loads(text)
    pkg = data.get("package", {})
    tables = [data.get("dependencies", {}), data.get("build-dependencies", {})]
    for target in data.get("target", {}).values():
        tables += [target.get("dependencies", {}), target.get("build-dependencies", {})]
    out: dict[str, str] = {}
    for deps in tables:
        for key, spec in deps.items():
            name = alice_name(spec.get("package", key) if isinstance(spec, dict) else key)
            if name is None:
                continue
            if isinstance(spec, str):
                req = spec
            elif spec.get("workspace") or "git" in spec or not spec.get("version"):
                raise ValueError(f"{name}: no crates.io version requirement in the manifest: {spec!r}")
            elif "registry" in spec or "registry-index" in spec:
                raise ValueError(f"{name}: comes from another registry: {spec!r}")
            else:
                req = spec["version"]
            dc.parse_requirement(req)
            if name in out and out[name] != req:
                raise ValueError(f"{name}: two different requirements ({out[name]!r}, {req!r})")
            out[name] = req
    return pkg.get("name", ""), pkg.get("version", ""), out


def alice_name(name: str) -> str | None:
    """`name` as a crates.io crate name if it is an ALICE crate (crates.io treats `-` and
    `_` as the same name), else None"""
    n = name.replace("_", "-")
    return n if n.startswith("alice-") else None


def alice_overrides(text: str) -> list[str]:
    """`alice-*` crates a manifest overrides with [patch.<source>] or [replace]"""
    data = tomllib.loads(text)
    found = []
    for source, entries in data.get("patch", {}).items():
        for key, spec in entries.items():
            name = alice_name(spec.get("package", key) if isinstance(spec, dict) else key)
            if name:
                found.append(f"[patch.{source}] {name}")
    for key in data.get("replace", {}):
        name = alice_name(key.split(":")[0])
        if name:
            found.append(f"[replace] {key}")
    return found


def latest(fetch: dc.Fetch, crate: str) -> str:
    """the latest release of a crate; a body without it is a RegistryError"""
    try:
        return dc.latest_release(fetch, crate)
    except (KeyError, TypeError) as e:
        raise RegistryError(f"{crate}: crates.io answered without a latest release ({e!r})") from None


# the gate only reads the repository: no optional locks (git status would otherwise
# refresh the index), so a check leaves the checkout as it found it
GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout


def checkout_problems(repo: Path, ref: str) -> list[str]:
    """the checkout must be the commit `ref`, with no change to a tracked file"""
    problems = []
    head = git(repo, "rev-parse", "HEAD").strip()
    want = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    if head != want:
        problems.append(f"the checkout is at {head[:12]}, not {ref} ({want[:12]})")
    dirty = git(repo, "status", "--porcelain", "--untracked-files=no").strip()
    if dirty:
        problems.append(f"the checkout has changes to tracked files: {dirty.splitlines()[0]} ...")
    return problems


def cargo_package_list(repo: Path, manifest_path: str) -> list[str]:
    """the files `cargo package` puts in the crate, relative to the package root"""
    out = subprocess.run(["cargo", "package", "--list", "--manifest-path", str(repo / manifest_path)],
                         cwd=repo, check=True, capture_output=True, text=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def package_problems(crate: str, files: list[str]) -> list[str]:
    """the files of the package that belong to the repository, not the crate; an empty
    list is a ValueError (nothing was compared)"""
    if not files:
        raise ValueError(f"{crate}: cargo package --list listed no file")
    leaked: dict[str, int] = {}
    for f in files:
        f = f.replace("\\", "/")
        top = next((d for d in PACKAGE_EXCLUDED_DIRS if f.startswith(d)), None)
        if top is None:
            parts = f.split("/")
            for i, part in enumerate(parts):
                if hashlib.sha256(part.lower().encode()).hexdigest() in PRIVATE_NAME_SHA256:
                    top = "/".join(parts[:i + 1]) + ("/" if i + 1 < len(parts) else "")
                    break
        if top is not None:
            leaked[top] = leaked.get(top, 0) + 1
    if not leaked:
        return []
    listed = ", ".join(f"{k} ({n})" for k, n in sorted(leaked.items()))
    return [f"{crate}: the package carries {sum(leaked.values())} of {len(files)} files that are "
            f"not the crate's: {listed}; set `include` (or `exclude`) in the manifest"]


def changelog_section(text: str, version: str) -> str | None:
    """the body of the `## [version]` section of a Keep a Changelog file, or None"""
    lines = text.splitlines()
    head = f"## [{version}]"
    for i, line in enumerate(lines):
        if line.startswith(head):
            body = []
            for rest in lines[i + 1:]:
                if rest.startswith("## "):
                    break
                body.append(rest)
            return "\n".join(body)
    return None


def release_heading_problem(text: str, version: str) -> str | None:
    """why the `## [version]` heading is not a release heading (`## [version] - YYYY-MM-DD`
    with a real date, not after tomorrow in UTC), or None"""
    import datetime
    for line in text.splitlines():
        if line.startswith(f"## [{version}]"):
            m = re.fullmatch(rf"## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})\s*", line)
            if not m:
                return f"the CHANGELOG.md heading {line!r} is not `## [{version}] - YYYY-MM-DD`"
            try:
                day = datetime.date.fromisoformat(m.group(1))
            except ValueError:
                return f"the CHANGELOG.md heading {line!r} has no real date"
            if day > datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=1):
                return f"the CHANGELOG.md heading {line!r} is dated in the future"
            return None
    return f"CHANGELOG.md has no [{version}] heading"


PATCH_DEPTH = 2000


def patch_ids(repo: Path, ref: str) -> set[str]:
    """the stable patch ids of the last PATCH_DEPTH non-merge commits of ref"""
    log = subprocess.run(["git", "-C", str(repo), "log", "-p", "--no-merges", "-n", str(PATCH_DEPTH), ref],
                         check=True, capture_output=True, env=GIT_ENV).stdout
    out = subprocess.run(["git", "-C", str(repo), "patch-id", "--stable"], input=log, check=True,
                         capture_output=True, env=GIT_ENV).stdout.decode()
    return {line.split()[0] for line in out.splitlines() if line.strip()}


# A job can publish only with a credential, so the credential is the first signal and the
# command the second. crates.io: a crates.io token (as a secret, in CARGO_REGISTRY_TOKEN or
# CARGO_REGISTRIES_<name>_TOKEN), the crates.io auth action, an OIDC token in a job that runs
# cargo, or a publish command (cargo [+toolchain] [workspaces|release] publish without
# --dry-run, release-plz, publish-crates). Another registry (PyPI, npm): its token, an OIDC
# token, or its publish command, allowed only in a job whose environment has required reviewers.
CRATES_SIGNAL_RE = re.compile(r"secrets\.CARGO_REGISTRY_TOKEN|\bCARGO_REGISTRY_TOKEN\b|\bCARGO_REGISTRIES_[A-Z0-9_]+_TOKEN\b"
                              r"|crates-io-auth-action|release-plz|publish-crates"
                              r"|\bcargo(?:\s+\+\S+)?\s+(?:workspaces\s+|release\s+)?publish\b(?![^\n]*--dry-run)")
OTHER_SIGNAL_RE = re.compile(r"\bPYPI_API_TOKEN\b|\bTWINE_PASSWORD\b|\bNODE_AUTH_TOKEN\b|\bNPM_TOKEN\b"
                             r"|pypa/gh-action-pypi-publish|\btwine\s+upload\b|\bmaturin\s+(?:publish|upload)\b"
                             r"|\bnpm\s+publish\b(?![^\n]*--dry-run)")
ID_TOKEN_RE = re.compile(r"id-token\s*:\s*write")
JOB_RE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*$")


def workflow_text(lines: list[str]) -> str:
    """the lines without comments (a `#` that starts a comment) and with `\\` continuations joined"""
    out = []
    for line in lines:
        code = re.sub(r"(^|\s)#.*$", "", line)
        if out and out[-1].endswith("\\"):
            out[-1] = out[-1][:-1] + " " + code.strip()
        else:
            out.append(code)
    # a `run: |` block keeps one command per line; join them too so a command split over lines is read
    return "\n".join(out) + "\n" + " ".join(l.strip() for l in out)


def job_environment(block: list[str]) -> str | None:
    for k, l in enumerate(block):
        m = re.match(r"^    environment:\s*(\S.*)?$", l)
        if m:
            value = (m.group(1) or "").strip().strip("'\"")
            if value:
                return value
            for l2 in block[k + 1:k + 4]:
                m2 = re.match(r"^      name:\s*(\S+)", l2)
                if m2:
                    return m2.group(1).strip("'\"")
    return None


def environment_reviewers(owner_repo: str, name: str) -> int:
    """the number of required reviewers of a repository's deployment environment (0 when it has none
    or does not exist)"""
    r = subprocess.run(["gh", "api", f"repos/{owner_repo}/environments/{name}"], capture_output=True, text=True)
    if r.returncode != 0:
        return 0
    rules = json.loads(r.stdout).get("protection_rules", [])
    return sum(len(rule.get("reviewers", [])) for rule in rules if rule.get("type") == "required_reviewers")


def workflow_publish_problems(repo: Path, ref: str, crate: str, owner_repo: str | None = None,
                              reviewers=environment_reviewers, declared: dict | None = None,
                              env_reviewers: dict | None = None, hosts: dict | None = None,
                              runner_labels: list[set] | None = None) -> tuple[list[str], int]:
    """a workflow of the repository at ref that can publish outside this gate: a crates.io signal
    anywhere, or another registry's signal in a job whose environment has no required reviewers
    (a tag push would publish without the gate); (problems, workflow files read)"""
    try:
        names = [n for n in git(repo, "ls-tree", "--name-only", f"{ref}:.github/workflows").splitlines()
                 if n.endswith((".yml", ".yaml"))]
    except subprocess.CalledProcessError:
        names = []
    problems = []
    texts = {n: git(repo, "show", f"{ref}:.github/workflows/{n}") for n in names}
    local = {n: workflow_text(t.splitlines()) for n, t in texts.items()}
    for n in names:
        lines = texts[n].splitlines()
        jobs_at = next((k for k, l in enumerate(lines) if l.rstrip() == "jobs:"), len(lines))
        starts = [k for k in range(jobs_at + 1, len(lines)) if JOB_RE.match(lines[k])]
        blocks = [("(workflow)", lines[:jobs_at + 1])] + [
            (JOB_RE.match(lines[a]).group(1), lines[a:b]) for a, b in zip(starts, starts[1:] + [len(lines)])]
        preamble = workflow_text(lines[:jobs_at + 1])
        workflow_id_token = bool(ID_TOKEN_RE.search(preamble))
        release_trigger = bool(RELEASE_TRIGGER_RE.search(preamble))
        for job, block in blocks:
            text = workflow_text(block)
            if job != "(workflow)" and declared is not None:
                problems += job_secret_problems(crate, n, job, text + ("\nid-token: write" if workflow_id_token else ""),
                                                release_trigger, job_environment(block), declared,
                                                env_reviewers or {}, local, hosts, runner_labels)
            id_token = workflow_id_token or bool(ID_TOKEN_RE.search(text))
            runs_cargo = bool(re.search(r"\bcargo\b", text))
            if CRATES_SIGNAL_RE.search(text) or (job != "(workflow)" and id_token and runs_cargo):
                problems.append(f"{crate}: .github/workflows/{n} job {job} can publish to crates.io outside this "
                                f"gate (a crates.io credential or publish command; a tag push would publish "
                                f"without it): remove it")
            elif OTHER_SIGNAL_RE.search(text) or (job != "(workflow)" and id_token and not runs_cargo
                                                    and re.search(r"pypi|npm|twine|maturin", text)):
                env = job_environment(block)
                if not env or not owner_repo or reviewers(owner_repo, env) < 1:
                    problems.append(f"{crate}: .github/workflows/{n} job {job} publishes to a package registry "
                                    f"without an environment that has required reviewers"
                                    + (f" ({env})" if env else ""))
    if not names:
        problems.append(f"{crate}: no workflow read at {ref} (.github/workflows; compared nothing)")
    return problems, len(names)


RELEASE_TRIGGER_RE = re.compile(r"\brelease\b|\btags\s*:|workflow_dispatch|\bschedule\b")
SECRET_REF_RE = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")


def secret_inventory(owner_repo: str) -> dict:
    """the repository's Actions secrets and variables by name: {"repo": [secret names],
    "environments": {env: [secret names]}, "reviewers": {env: n}, "dependabot": [secret names],
    "variables": [names], "env_variables": {env: [names]}}; every list is read page by page and
    must hold as many names as GitHub counts; raises RuntimeError when GitHub cannot be read (a
    gate that read nothing must not pass)"""
    if not owner_repo:
        raise RuntimeError("no [train] github / repo in the roster, so the secrets cannot be read")

    def api(path):
        r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"gh api {path}: {r.stderr.strip()[:200]}")
        return json.loads(r.stdout)

    def listed(path, key):
        names, page = [], 1
        while True:
            data = api(f"{path}?per_page=100&page={page}")
            items = data.get(key, [])
            names += [x["name"] for x in items]
            total = data.get("total_count", len(names))
            if len(names) >= total or not items:
                break
            page += 1
        if len(names) != total:
            raise RuntimeError(f"gh api {path}: read {len(names)} of {total}")
        return names

    base = f"repos/{owner_repo}"
    out = {"repo": listed(f"{base}/actions/secrets", "secrets"), "environments": {}, "reviewers": {},
           "dependabot": listed(f"{base}/dependabot/secrets", "secrets"),
           "variables": listed(f"{base}/actions/variables", "variables"), "env_variables": {}, "runners": []}
    page = 1
    while True:
        data = api(f"{base}/actions/runners?per_page=100&page={page}")
        out["runners"] += [[lab["name"] for lab in r.get("labels", [])] for r in data.get("runners", [])]
        if len(out["runners"]) >= data.get("total_count", 0) or not data.get("runners"):
            break
        page += 1
    if len(out["runners"]) != data.get("total_count", len(out["runners"])):
        raise RuntimeError(f"gh api {base}/actions/runners: read {len(out['runners'])} of {data.get('total_count')}")
    envs = api(f"{base}/environments?per_page=100")
    if len(envs.get("environments", [])) != envs.get("total_count", len(envs.get("environments", []))):
        raise RuntimeError(f"gh api {base}/environments: more environments than one page")
    for env in envs.get("environments", []):
        name = env["name"]
        out["environments"][name] = listed(f"{base}/environments/{name}/secrets", "secrets")
        out["env_variables"][name] = listed(f"{base}/environments/{name}/variables", "variables")
        out["reviewers"][name] = sum(len(rule.get("reviewers", [])) for rule in env.get("protection_rules", [])
                                     if rule.get("type") == "required_reviewers")
    return out


def variable_problems(crate: str, repo_name: str, declared: dict, inventory: dict) -> list[str]:
    """every Actions variable of the repository and its environments is declared in the roster with
    its purpose (a variable is plain text and never holds a credential)"""
    present = set(inventory.get("variables", [])) | {f"{e}:{n}" for e, ns in inventory.get("env_variables", {}).items()
                                                     for n in ns}
    problems = [f"{crate}: the variable {key} of {repo_name} is not declared in the roster "
                f"([variables.\"{repo_name}\"] {key} = {{ purpose }})" for key in sorted(present - set(declared))]
    problems += [f"{crate}: the roster declares the variable {key} of {repo_name}, which the repository does not have "
                 f"(remove the declaration)" for key in sorted(set(declared) - present)]
    return problems


def secret_problems(crate: str, repo_name: str, declared: dict, inventory: dict) -> list[str]:
    """every secret of the repository and its environments is declared in the roster with its
    purpose; a publishing secret may exist only in an environment with required reviewers"""
    problems = []
    present = (set(inventory["repo"]) | {f"{e}:{n}" for e, ns in inventory["environments"].items() for n in ns}
               | {f"dependabot:{n}" for n in inventory.get("dependabot", [])})
    for key in sorted(set(declared) - present):
        problems.append(f"{crate}: the roster declares the secret {key} of {repo_name}, which the repository does not "
                        f"have (remove the declaration)")
    for where, names in ([("repository", inventory["repo"])] + [(f"environment {e}", n)
                                                                 for e, n in inventory["environments"].items()]
                         + [("dependabot", inventory.get("dependabot", []))]):
        for name in names:
            key = (name if where == "repository" else f"dependabot:{name}" if where == "dependabot"
                   else f"{where.split(' ', 1)[1]}:{name}")
            d = declared.get(key)
            if d is None:
                problems.append(f"{crate}: the {where} secret {name} of {repo_name} is not declared in the roster "
                                f"([secrets.\"{repo_name}\"] {key} = {{ purpose, publishing }})")
            elif d["publishing"] and (where in ("repository", "dependabot")
                                      or inventory["reviewers"].get(key.split(":")[0], 0) < 1):
                problems.append(f"{crate}: the {where} secret {name} of {repo_name} is a publishing credential outside "
                                f"an environment with required reviewers: a workflow could publish without this gate")
    return problems


# the GitHub-hosted runner images by their exact labels; any other label (a custom label of a
# registered runner such as "windows-unity", or an unknown one) is a self-hosted runner
GITHUB_HOSTED = {
    "ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04", "ubuntu-24.04-arm", "ubuntu-22.04-arm", "ubuntu-slim",
    "windows-latest", "windows-2025", "windows-2022", "windows-11-arm",
    "macos-latest", "macos-26", "macos-15", "macos-14", "macos-13", "macos-15-intel", "macos-13-intel",
    "macos-latest-large", "macos-15-large", "macos-14-large", "macos-13-large",
    "macos-latest-xlarge", "macos-15-xlarge", "macos-14-xlarge", "macos-13-xlarge",
}


def runs_on_value(text: str) -> list[str] | None:
    """the labels of the job's runs-on (a label, a list on one line or below it, or a mapping with
    `labels:`); None for a runner group (`group:`) or nothing found"""
    lines = text.splitlines()
    for k, line in enumerate(lines):
        m = re.match(r"^(\s*)runs-on:\s*(.*)$", line)
        if not m:
            continue
        indent, value = len(m.group(1)), m.group(2).strip()
        if value:
            if value.startswith("{"):
                if "group:" in value:
                    return None
                lab = re.search(r"labels:\s*\[?([^\]}]*)", value)
                value = lab.group(1) if lab else ""
            return [x.strip(" '\"") for x in value.strip("[]").split(",") if x.strip(" '\"")]
        labels = []
        for nxt in lines[k + 1:]:
            if not nxt.strip():
                continue
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break
            item = nxt.strip()
            if item.startswith("group:"):
                return None
            if item.startswith("labels:"):
                rest = item[len("labels:"):].strip()
                if rest:
                    labels += [x.strip(" '\"") for x in rest.strip("[]").split(",") if x.strip(" '\"")]
                continue
            labels.append(item.lstrip("- ").strip(" '\""))
        return labels
    return []


def matrix_values(text: str, key: str) -> list[str] | None:
    """the literal values of matrix.<key> written in the job (a list, a block list, or include
    entries); None when it cannot be read literally"""
    values = []
    lines = text.splitlines()
    for k, line in enumerate(lines):
        flow = re.match(r"^\s*-\s*\{(.*)\}\s*$", line)
        if flow:  # an include entry written as a flow mapping: `- { os: ubuntu-latest, target: x }`
            fm = re.search(rf"(?:^|,)\s*{re.escape(key)}:\s*([^,]+)", flow.group(1))
            if fm:
                values.append(fm.group(1).strip(" '\""))
            continue
        m = re.match(rf"^(\s*)(?:-\s*)?{re.escape(key)}:\s*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if value.startswith("${{"):
            # an expression that only chooses between literal JSON lists (by event or a needs output)
            # resolves to the labels of those lists; one that can take a value from inputs, vars or
            # env, or has no literal list, cannot be read
            if re.search(r"\b(?:inputs|vars|env)\.|github\.event\.inputs", value):
                return None
            lists = re.findall(r"'\[([^\]']*)\]'", value)
            if not lists:
                return None
            values += [x.strip(' "') for lst in lists for x in lst.split(",") if x.strip(' "')]
        elif value.startswith("["):
            values += [x.strip(" '\"") for x in value.strip("[]").split(",") if x.strip(" '\"")]
        elif value:
            values.append(value.strip(" '\""))
        else:
            for nxt in lines[k + 1:]:
                if not nxt.strip().startswith("-"):
                    break
                values.append(nxt.strip()[1:].strip(" '\""))
    if not values or any("${{" in v for v in values):
        return None
    return values


def self_hosted(text: str, runner_labels: list[set] | None = None) -> bool:
    """the job may run on a runner that is not GitHub-hosted: a runner group, a label that is not a
    GitHub-hosted image (a custom label of a registered runner included), or an expression other
    than a matrix value written literally in the job whose values are all GitHub-hosted images"""
    if "runs-on:" not in text:
        return False
    labels = runs_on_value(text)
    if labels is None:
        return True
    resolved = []
    for label in labels or ["(none)"]:
        exprs = re.findall(r"\$\{\{\s*([^}]*?)\s*\}\}", label)
        if not exprs:
            resolved.append(label)
            continue
        m = re.fullmatch(r"matrix\.([A-Za-z0-9_-]+)", exprs[0]) if len(exprs) == 1 and label.strip().startswith("${{") else None
        values = matrix_values(text, m.group(1)) if m else None
        if values is None:
            return True
        resolved += values
    if any(set(resolved) <= rl for rl in (runner_labels or []) if resolved):
        return True
    return any(x not in GITHUB_HOSTED for x in resolved)


def job_secret_problems(crate: str, n: str, job: str, text: str, release_trigger: bool, env: str | None,
                        declared: dict, reviewers: dict, local_workflows: dict,
                        hosts: dict | None = None, runner_labels: list[set] | None = None) -> list[str]:
    """a job that receives a secret (a `secrets.<name>` other than GITHUB_TOKEN, `secrets: inherit`,
    an OIDC token) on a release / tag / dispatch / schedule trigger runs under an environment with
    required reviewers, unless every secret it receives is declared as not publishing"""
    names = {m for m in SECRET_REF_RE.findall(text) if m != "GITHUB_TOKEN"}
    inherit = bool(re.search(r"secrets:\s*inherit", text))
    oidc = bool(ID_TOKEN_RE.search(text))
    problems = []
    if inherit:
        called = re.search(r"uses:\s*(\S+)", text)
        target = called.group(1) if called else ""
        if not target.startswith("./.github/workflows/") or target[len("./.github/workflows/"):] not in local_workflows:
            problems.append(f"{crate}: .github/workflows/{n} job {job} passes `secrets: inherit` to {target or 'a call'}, "
                            f"which is not a local workflow this gate reads")
        else:
            inner = local_workflows[target[len("./.github/workflows/"):]]
            names |= {m for m in SECRET_REF_RE.findall(inner) if m != "GITHUB_TOKEN"}
            oidc = oidc or bool(ID_TOKEN_RE.search(inner))
    protected = env is not None and reviewers.get(env, 0) >= 1
    if release_trigger and self_hosted(text, runner_labels) and not protected and f"{n}:{job}" not in (hosts or {}):
        problems.append(f"{crate}: .github/workflows/{n} job {job} runs on a self-hosted runner on a release / tag / "
                        f"dispatch / schedule trigger outside an environment with required reviewers (the host can "
                        f"hold credentials; or declare it in [self-hosted.\"<repository>\"] \"{n}:{job}\")")
    if not release_trigger or not (names or inherit or oidc):
        return problems
    env_key = lambda nm: f"{env}:{nm}" if env else nm
    harmless = names and all((declared.get(env_key(nm)) or declared.get(nm) or {}).get("publishing") is False
                             for nm in names) and not oidc
    if not protected and not harmless:
        what = ", ".join(sorted(names)) + (" (an OIDC token)" if oidc else "") + (" (inherited)" if inherit else "")
        problems.append(f"{crate}: .github/workflows/{n} job {job} receives {what} on a release / tag / dispatch / "
                        f"schedule trigger outside an environment with required reviewers")
    return problems


def history_problems(repo: Path, ref: str, crate: str, c: dict, manifest_path: str) -> tuple[list[str], int]:
    """(problems, compared) for the roster's `must-contain` commits and `changelog-has`
    lines of `crate`, read at `ref`"""
    problems: list[str] = []
    compared = 0
    for commit in c.get("must-contain", []):
        compared += 1
        known = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
                               capture_output=True, env=GIT_ENV).returncode == 0
        if not known:
            problems.append(f"{crate}: must contain {commit}, which is not in the repository")
            continue
        r = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, ref],
                           capture_output=True, text=True, env=GIT_ENV)
        if r.returncode == 1:
            problems.append(f"{crate}: {ref} does not contain {commit}")
        elif r.returncode != 0:
            raise subprocess.CalledProcessError(r.returncode, ["git", "merge-base"], stderr=r.stderr)
    if c.get("must-contain-patch"):
        have = patch_ids(repo, ref)
        for pid in c["must-contain-patch"]:
            compared += 1
            if pid not in have:
                problems.append(f"{crate}: no commit in the last {PATCH_DEPTH} of {ref} has the patch {pid}")
    wanted = c.get("changelog-has", [])
    if True:  # the release heading is checked for every crate; `changelog-has` adds lines
        here = str(Path(manifest_path).parent).replace("\\", "/")
        candidates = ([f"{here}/CHANGELOG.md"] if here not in ("", ".") else []) + ["CHANGELOG.md"]
        text = None
        for path in candidates:
            try:
                text = git(repo, "show", f"{ref}:{path}")
                break
            except subprocess.CalledProcessError:
                continue
        if text is None:
            return problems + [f"{crate}: no CHANGELOG.md at {ref}"], compared + 1
        section = changelog_section(text, c["version"])
        if section is None:
            return problems + [f"{crate}: CHANGELOG.md has no [{c['version']}] section"], compared + 1
        compared += 1
        heading = release_heading_problem(text, c["version"])
        if heading:
            problems.append(f"{crate}: {heading}")
        for line in wanted:
            compared += 1
            if line not in section:
                problems.append(f"{crate}: the [{c['version']}] section of CHANGELOG.md does not contain {line!r}")
    return problems, compared


def gh_json_lines(path: str, jq: str) -> list[dict]:
    out = subprocess.run(["gh", "api", "--paginate", path, "--jq", jq], check=True, capture_output=True, text=True).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def github_actions_jobs(owner_repo: str, sha: str) -> dict[str, list[dict]]:
    """{workflow name: the jobs (name, status, conclusion, and the event of the run) of its latest
    run on the commit} from the GitHub Actions API (a job of another workflow or app cannot stand in)"""
    runs = gh_json_lines(f"repos/{owner_repo}/actions/runs?head_sha={sha}&per_page=100",
                         ".workflow_runs[] | {id, name, created_at, event}")
    latest: dict[str, dict] = {}
    for r in runs:
        if r["name"] not in latest or r["created_at"] > latest[r["name"]]["created_at"]:
            latest[r["name"]] = r
    return {name: [{**j, "event": r.get("event")}
                   for j in gh_json_lines(f"repos/{owner_repo}/actions/runs/{r['id']}/jobs?per_page=100",
                                          ".jobs[] | {name, status, conclusion}")]
            for name, r in latest.items()}


def ci_problems(crate: str, c: dict, owner_repo: str, commit: str, actions,
                dispatched: list[str] | None = None) -> tuple[list[str], int]:
    """(problems, compared) for the roster's `required-ci` jobs of `crate` on `commit`. A required
    job the run skipped (a path filter that the commit did not match, which also leaves a matrix
    unexpanded: `Fuzz — ${{ matrix.target }}`) is never green: it is refused with the workflow to
    dispatch on the commit. The green jobs that came from a dispatched run are added to
    `dispatched` ("<workflow>: <job>"), for the gate's record"""
    wanted = [w for w in c.get("required-ci", [])
              if "since" not in w or dc.Version(c["version"]) >= dc.Version(w["since"])]
    if not wanted:
        return [], 0
    try:
        by_workflow = actions(owner_repo, commit)
    except subprocess.CalledProcessError as e:
        return [f"{crate}: the CI jobs of {commit[:12]} could not be read: {(e.stderr or '').strip()[:200]}"], 1
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [f"{crate}: the CI jobs of {commit[:12]} could not be read: {e!r}"[:300]], 1
    problems: list[str] = []
    compared = 0
    for w in wanted:
        workflow, job, legs = w["workflow"], w["job"], w.get("legs", 1)
        compared += 1
        where = f"{crate}: required CI job {job!r} of workflow {workflow!r} on {commit[:12]}"
        if workflow not in by_workflow:
            problems.append(f"{where}: the workflow has no run")
            continue
        mine = [j for j in by_workflow[workflow]
                if j.get("name") == job or str(j.get("name", "")).startswith(job + " (")]
        names = sorted({str(j.get("name")) for j in mine})
        unexpanded = [j for j in by_workflow[workflow]
                      if "${{" in str(j.get("name", "")) and j.get("conclusion") == "skipped"]
        if (mine and all(j.get("conclusion") == "skipped" for j in mine)) or (not mine and unexpanded):
            problems.append(f"{where}: skipped by the run (not green): dispatch {workflow} on {commit}")
            continue
        if isinstance(legs, int):
            if len(names) != legs:
                problems.append(f"{where}: {len(names)} legs ran, the roster expects {legs}: {names}")
        else:
            missing = sorted(set(legs) - set(names))
            extra = sorted(set(names) - set(legs))
            if missing or extra:
                problems.append(f"{where}: legs missing {missing}, not in the roster {extra}")
        bad = sorted({f"{j.get('name')}: {j.get('status')}/{j.get('conclusion')}" for j in mine
                      if j.get("status") != "completed" or j.get("conclusion") != "success"})
        if bad:
            problems.append(f"{where}: not green: {'; '.join(bad)}")
        elif dispatched is not None and mine and all(j.get("event") == "workflow_dispatch" for j in mine):
            dispatched.append(f"{workflow}: {job} ({len(names)} legs)")
    return problems, compared


def release_state(fetch: dc.Fetch, crate: str, version: str) -> str:
    """'missing' (HTTP 404), 'yanked' or 'present' for one version on crates.io; anything
    else crates.io answers is a RegistryError"""
    try:
        data = fetch(f"{crate}/{version}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "missing"
        raise
    v = data.get("version") if isinstance(data, dict) else None
    if not isinstance(v, dict) or "num" not in v:
        raise RegistryError(f"{crate}/{version}: crates.io answered without a version: {str(data)[:200]}")
    if v["num"] != version:
        raise RegistryError(f"{crate}/{version}: crates.io answered version {v['num']}")
    return "yanked" if v.get("yanked") else "present"


def library_crates(roster: dict[str, dict]) -> list[str]:
    """the crates this repository links (scripts/dep_consistency.py CORE): every roster crate
    that is not exempt and not marked `law-dependency = false`"""
    return sorted(n for n, c in roster.items() if c["state"] != "exempt" and c.get("law-dependency", True))


def next_crate(roster: dict[str, dict]) -> tuple[str | None, int]:
    """(crate, unpublished count): the first crate in `order` that is not published / exempt
    (the publish-gate refuses it while an upstream is not published yet)"""
    pending = [(c["order"], n) for n, c in roster.items() if c["state"] not in ("published", "exempt")]
    if not pending:
        return None, 0
    return min(pending)[1], len(pending)


def publish_gate(roster: dict[str, dict], crate: str, manifest_text: str,
                 fetch: dc.Fetch, root_text: str | None = None) -> tuple[list[str], int]:
    """(problems, compared) for publishing `crate` now from the given manifest (and the
    repository's root manifest, when it is another file)"""
    if crate not in roster:
        return [f"{crate} is not in the roster"], 1
    c = roster[crate]
    problems: list[str] = []
    compared = 1
    name, version, requires = manifest_alice_requirements(manifest_text)
    for text in (manifest_text,) if root_text is None else (manifest_text, root_text):
        for o in alice_overrides(text):
            problems.append(f"{crate}: the manifest overrides an ALICE crate: {o}")
    if name != crate or version != c["version"]:
        problems.append(f"the manifest is {name or '?'} {version or '?'}, the roster has {crate} {c['version']}")
    if c["state"] != "prepared":
        problems.append(f"{crate}: state is {c['state']!r}, a publish needs 'prepared'")
    own = release_state(fetch, crate, c["version"])
    if own != "missing":
        problems.append(f"{crate} {c['version']} is already on crates.io ({own})")
    for dep, req in sorted(requires.items()):
        compared += 1
        if dep in roster:
            d = roster[dep]
            if d["state"] != "published":
                problems.append(f"{crate} requires {dep} {req!r}: {dep} {d['version']} is {d['state']!r}, not published")
                continue
            target = d["version"]
        else:
            target = latest(fetch, dep)
        state = release_state(fetch, dep, target)
        if state != "present":
            problems.append(f"{crate} requires {dep} {req!r}: {dep} {target} is {state} on crates.io")
            continue
        if not dc.matches(req, target):
            problems.append(f"{crate} requires {dep} {req!r}, which does not accept the published {target}")
    return problems, compared


# ---------------------------------------------------------------- after the gate: package, verify, mark

DOCS_RS = "https://docs.rs/crate"
STATIC_CRATES = "https://static.crates.io/crates"


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": dc.USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            time.sleep(1)  # crates.io asks for at most one request per second
            return body
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def docs_rs_status(crate: str, version: str) -> dict:
    return json.loads(http_get(f"{DOCS_RS}/{crate}/{version}/status.json"))


def lock_tracked(repo: Path, manifest_path: str) -> bool:
    """a Cargo.lock is committed next to the manifest or at the repository root"""
    here = Path(manifest_path).parent
    for p in {str(here / "Cargo.lock"), "Cargo.lock"}:
        r = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", p], capture_output=True, env=GIT_ENV)
        if r.returncode == 0:
            return True
    return False


def cargo_package(repo: Path, manifest_path: str, name: str, version: str, target_dir: Path) -> Path:
    """`cargo package` (which builds the packaged crate, as `cargo publish` does) into
    target_dir; the .crate it wrote"""
    cmd = ["cargo", "package", "--manifest-path", str(repo / manifest_path), "--target-dir", str(target_dir)]
    if lock_tracked(repo, manifest_path):
        cmd.append("--locked")
    subprocess.run(cmd, cwd=repo, check=True, capture_output=True, text=True)
    return target_dir / "package" / f"{name}-{version}.crate"


CONSUMER_MANIFEST = """[package]
name = "release-gate-consumer"
version = "0.0.0"
edition = "2021"
publish = false

[dependencies]
{name} = {{ path = "../pkg/{name}-{version}"{spec} }}

[workspace]
"""
LIB_KINDS = ("lib", "rlib", "proc-macro", "dylib", "cdylib", "staticlib")
# the only variables the consumer build inherits: where the toolchain is, and how the
# system finds programs and temporary files; no RUSTFLAGS, RUSTC_WRAPPER, CARGO_* or
# configuration of the machine the gate runs on reaches it
CONSUMER_ENV_KEEP = ("PATH", "RUSTUP_HOME", "RUSTUP_TOOLCHAIN", "TMPDIR", "TEMP", "TMP",
                     "SYSTEMROOT", "LANG", "LC_ALL")


def manifests_above(path: Path) -> list[Path]:
    """files in the ancestors of path that Cargo or a build script would read from outside
    the crate: Cargo.lock / Cargo.toml (a lock above the crate, a workspace) and Cargo
    configuration (.cargo/config, .cargo/config.toml)"""
    names = ("Cargo.lock", "Cargo.toml", ".cargo/config", ".cargo/config.toml")
    return [d / f for d in path.resolve().parents for f in names if (d / f).exists()]


def compiled_lib(stdout: str, name: str, version: str, kinds=LIB_KINDS) -> bool:
    """the JSON messages of a cargo build carry a compiler artifact of the crate's library
    (or, with kinds=("bin",), of its binary)"""
    for line in stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("reason") != "compiler-artifact":
            continue
        pid = msg.get("package_id", "")
        got = msg.get("target", {}).get("kind", [])
        if (pid.endswith(f"#{name}@{version}") or pid.startswith(f"{name} {version} ")) and \
                any(k in kinds for k in got):
            return True
    return False


def consumer_env(home: Path, registry: Path) -> dict[str, str]:
    """a minimal environment with an empty CARGO_HOME (its registry cache is the gate's own
    directory, which holds no configuration) and HOME in the build directory"""
    cargo_home = home / "cargo-home"
    cargo_home.mkdir(parents=True, exist_ok=True)
    registry.mkdir(parents=True, exist_ok=True)
    (cargo_home / "registry").symlink_to(registry, target_is_directory=True)
    env = {k: os.environ[k] for k in CONSUMER_ENV_KEEP if k in os.environ}
    if "RUSTUP_HOME" not in env:  # rustup finds its toolchains under the real HOME
        real = Path(os.environ.get("HOME", str(Path.home()))) / ".rustup"
        if real.is_dir():
            env["RUSTUP_HOME"] = str(real)
    env["HOME"] = str(home)
    env["CARGO_HOME"] = str(cargo_home)
    return env


def expected_failure(entry: dict | None, name: str, version: str, features: list[str] | None,
                     cmd: str) -> dict | None:
    """the roster's `consumer-expected-failure` entry for this feature set (and command, when
    the entry names one), if any"""
    for e in (entry or {}).get("consumer-expected-failure", []):
        if features is not None and sorted(e["features"]) == sorted(features) and e.get("command", cmd) == cmd:
            return e
    return None


def crate_errors(stdout: str, name: str, version: str) -> list[dict]:
    """the error diagnostics of the crate itself in cargo's JSON messages (`--message-format=json`):
    {code, file (of the primary span), message}; the closing "aborting due to" count is not one"""
    out = []
    for line in stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        pid = msg.get("package_id", "")
        if msg.get("reason") != "compiler-message" or not (pid.endswith(f"#{name}@{version}")
                                                          or pid.startswith(f"{name} {version} ")):
            continue
        m = msg.get("message", {})
        if m.get("level") != "error" or (not m.get("spans") and m.get("message", "").startswith("aborting due to")):
            continue
        primary = [sp for sp in m.get("spans", []) if sp.get("is_primary")]
        out.append({"code": (m.get("code") or {}).get("code"),
                    "file": primary[0].get("file_name", "") if primary else None,
                    "text": span_text(primary[0]) if primary else "",
                    "label": (primary[0].get("label") or "") if primary else "",
                    "message": m.get("message", "")})
    return out


def span_text(span: dict) -> str:
    """the source text a span highlights (its lines joined by newlines), not its line number"""
    lines = span.get("text") or []
    parts = []
    for i, t in enumerate(lines):
        start = t.get("highlight_start", 1) - 1
        end = t.get("highlight_end", len(t.get("text", "")) + 1) - 1
        parts.append(t.get("text", "")[start:end] if len(lines) == 1 else
                     t.get("text", "")[start:] if i == 0 else t.get("text", "")[:end] if i == len(lines) - 1
                     else t.get("text", ""))
    return "\n".join(parts)


def matches_expected(err: dict, expect: dict) -> bool:
    """the error has the expected code, in the expected file (the path inside the crate), on the
    expected source text (what its primary span highlights), with the primary span's label
    ("expected `…`, found `…`") and the message fragment when the entry names them"""
    f = (err["file"] or "").replace("\\", "/")
    return (err["code"] == expect["code"] and (f == expect["file"] or f.endswith("/" + expect["file"]))
            and err.get("text") == expect["text"] and expect.get("message", "") in err["message"]
            and ("label" not in expect or err.get("label") == expect["label"]))


def first_errors(r) -> str:
    """the first error messages of a failed build, for the report"""
    msgs = []
    for line in r.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        m = msg.get("message", {}) if msg.get("reason") == "compiler-message" else {}
        if m.get("level") == "error":
            msgs.append(((m.get("code") or {}).get("code") or "") + " " + m.get("message", ""))
    tail = [l.strip() for l in r.stderr.strip().splitlines()[-2:]]
    return "; ".join([x.strip() for x in msgs[:2]] + tail)


def consumer_build(body: bytes, name: str, version: str, target_dir: Path | None,
                   entry: dict | None = None, universe: list[str] | None = None) -> list[str]:
    """build the packaged crate the way a crates.io consumer does: unpacked into a fresh
    directory with no Cargo.lock, Cargo.toml or Cargo configuration above it, with an empty
    CARGO_HOME and a minimal environment, as the path dependency of an empty crate, once per
    feature set (feature_specs); a crate with no library is built on its own, from a
    directory outside it, and must produce its binary; a .crate carrying a Cargo configuration
    (.cargo/config, .cargo/config.toml) is refused. The problems: none when every build compiled the crate"""
    root = Path(tempfile.mkdtemp(prefix="release-gate-consumer-"))
    try:
        above = manifests_above(root / "consumer")
        if above:
            return [f"{name} {version}: the consumer build directory {root} has {above[0]} above it"]
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as t:
            t.extractall(root / "pkg", filter="data")
        pkg = root / "pkg" / f"{name}-{version}"
        if not (pkg / "Cargo.toml").is_file():
            return [f"{name} {version}: the .crate has no {name}-{version}/Cargo.toml"]
        shipped = [c for c in (".cargo/config", ".cargo/config.toml") if (pkg / c).exists()]
        if shipped:
            return [f"{name} {version}: the .crate carries {shipped[0]} (Cargo configuration of the repository, "
                    f"which a consumer never reads; set `include` or `exclude`)"]
        manifest = tomllib.loads((pkg / "Cargo.toml").read_text(encoding="utf-8"))
        if universe is None and not ((entry or {}).get("consumer-features") or (entry or {}).get("consumer-check-features")):
            # the every-feature set of the package as Cargo defines it (implicit features included)
            universe = sg.feature_universe(pkg / "Cargo.toml")
        has_lib = "lib" in manifest or (pkg / "src" / "lib.rs").is_file()
        cache = (target_dir or root) / "consumer-cache"
        env = consumer_env(root / "home", cache / "registry")
        target = ["--target-dir", str(cache / "target")]
        problems, built = [], 0
        if has_lib:
            consumer = root / "consumer"
            (consumer / "src").mkdir(parents=True)
            (consumer / "src" / "lib.rs").write_text("", encoding="utf-8")

            def build(spec: str, cmd: str):
                (consumer / "Cargo.toml").write_text(
                    CONSUMER_MANIFEST.format(name=name, version=version, spec=spec), encoding="utf-8")
                return subprocess.run(["cargo", cmd, "--message-format=json", *target],
                                      cwd=consumer, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", env=env)

            # every feature at once fails when some features cannot be on together: that is
            # a refusal (a consumer enabling them breaks) unless the roster names the feature
            # sets to build instead (`consumer-features`, with the reason)
            runs = [(label, build(spec, cmd), fs, cmd) for label, spec, fs, cmd in feature_specs(manifest, entry, universe)]
        else:
            # from a directory outside the package: Cargo reads configuration from the working
            # directory up, and `cargo install` of a published crate does not start inside it
            outside = root / "bin"
            outside.mkdir()
            runs = [("its binary", subprocess.run(
                ["cargo", "build", "--message-format=json", "--manifest-path",
                 str(pkg / "Cargo.toml"), *target], cwd=outside,
                capture_output=True, text=True, encoding="utf-8", errors="replace", env=env), None, "build")]
        for label, r, fs, cmd in runs:
            ef = expected_failure(entry, name, version, fs, cmd)
            if ef is not None:
                until_crate, until_version = ef["until"].split()
                if until_crate == name and dc.Version(version) >= dc.Version(until_version):
                    problems.append(f"{name} {version}: the expected failure of {label} was until {ef['until']} "
                                    f"(remove the entry; the build must pass)")
                elif r.returncode == 0:
                    problems.append(f"{name} {version}: {label} is listed as an expected failure but builds "
                                    f"(remove the entry)")
                else:
                    errors = crate_errors(r.stdout, name, version)
                    other = [e for e in errors if not matches_expected(e, ef["expect"])]
                    held = len(errors) - len(other)
                    if not other and held != ef["expect"]["count"]:
                        problems.append(f"{name} {version}: {label} fails with {held} of the expected "
                                        f"{ef['expect']['code']} in {ef['expect']['file']}, the entry's count is "
                                        f"{ef['expect']['count']}")
                    elif not errors or other:
                        shown = other[0] if other else None
                        problems.append(f"{name} {version}: {label} fails, but not only with the expected "
                                        f"{ef['expect']['code']} in {ef['expect']['file']} ("
                                        + (f"{shown['code']} in {shown['file']}: {shown['message']}" if shown
                                           else f"no error of the crate itself: {first_errors(r)}") + ")")
                    else:
                        built += 1  # failed as the roster says, until its release
                continue
            if r.returncode != 0:
                tail = first_errors(r)
                problems.append(f"{name} {version}: the unpacked .crate does not build ({label}) as a crates.io "
                                f"consumer builds it, with no Cargo.lock or configuration above it ({tail})")
            elif not compiled_lib(r.stdout, name, version, LIB_KINDS if has_lib else ("bin",)):
                problems.append(f"{name} {version}: the consumer build ({label}) compiled no "
                                f"{'library' if has_lib else 'binary'} of {name} {version}")
            else:
                built += 1
        if built == 0 and not problems:
            problems.append(f"{name} {version}: the consumer build ran no build (compared nothing)")
        return problems
    finally:
        shutil.rmtree(root, ignore_errors=True)


def load_public_text():
    spec = importlib.util.spec_from_file_location("public_text_check", HERE / "public_text_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def package_text_problems(body: bytes, name: str, version: str, repo: Path, ref: str, repo_name: str | None,
                          manifest_dir: str) -> tuple[list[str], int]:
    """the published text of the package read by scripts/public_text_check.py (each file's path
    and text, binary files by their printable runs and the build paths in them), with the source
    repository's settings and allow-list (paths taken back to the repository: <manifest dir>/<path
    in the crate>); (problems, files read)"""
    ptc = load_public_text()
    problem = ptc.private_sets_problem()
    if problem:
        return [f"{name} {version}: the package text check: {problem}"], 0
    ptc.CONFIG.update(ptc.load_config(str(repo), ref))
    ptc.ALLOW_STATE["allowed"] = dict(ptc.ALLOWED.get(repo_name, {})) if repo_name else {}
    ptc.ALLOW_STATE["repo"] = repo_name
    ptc.ALLOW_STATE["used"] = {}
    ptc.CLASS_USED.clear()
    problems, files = [], 0
    prefix = f"{name}-{version}/"
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as t:
        for m in t.getmembers():
            if not m.isfile() or not m.name.startswith(prefix):
                continue
            inner = m.name[len(prefix):]
            if inner == ".cargo_vcs_info.json":
                continue
            files += 1
            path = f"{manifest_dir}/{inner}" if manifest_dir else inner
            data = t.extractfile(m).read()
            found = ptc.scan_text(path, inner, path) + ptc.scan_text(path, ptc.text_of(data), path)
            # a compiled file, a container or an LFS pointer in the package: its build paths, or unscanned
            found += ptc.file_findings(path, data, path)
            for f in found:
                problems.append(f"{name} {version}: the package carries internal text: {f}")
    if files == 0:
        problems.append(f"{name} {version}: the package text check read no file (compared nothing)")
    return problems, files


def publish_command(repo: Path, manifest_path: str, locked: bool) -> str:
    return (f"(cd {repo} && cargo publish{' --locked' if locked else ''} --manifest-path {manifest_path})")


def crate_files(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        return [m.name for m in t.getmembers() if m.isfile()]


def vcs_info(data: bytes, name: str, version: str) -> dict | None:
    """the .cargo_vcs_info.json cargo writes into a .crate (the commit it was packaged from)"""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        try:
            f = t.extractfile(f"{name}-{version}/.cargo_vcs_info.json")
        except KeyError:
            return None
        return json.load(f) if f else None


def remote_tag_commit(repo: Path, remote: str, tag: str) -> str | None:
    """the commit the tag names on the remote (an annotated tag is peeled), or None"""
    out = git(repo, "ls-remote", remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    direct = peeled = None
    for line in out.splitlines():
        sha, ref = line.split("\t")
        if ref.endswith("^{}"):
            peeled = sha
        else:
            direct = sha
    return peeled or direct


def tag_name(c: dict) -> str:
    return c.get("tag", "v{version}").replace("{version}", c["version"])


def publish_verify(roster: dict[str, dict], owners: list[str], crate: str, commit: str,
                   manifest_text: str, manifest_dir: str, expect_sha256: str, fetch: dc.Fetch,
                   download, tag_commit) -> tuple[list[str], int]:
    """(problems, compared) for the release of `crate` at its roster version, read back
    from crates.io and the repository after `cargo publish`"""
    if crate not in roster:
        return [f"{crate} is not in the roster"], 1
    c = roster[crate]
    version = c["version"]
    problems: list[str] = []
    compared = 1
    try:
        data = fetch(f"{crate}/{version}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return [f"{crate} {version} is not on crates.io"], compared
        raise
    v = data.get("version") if isinstance(data, dict) else None
    if not isinstance(v, dict) or v.get("num") != version:
        raise RegistryError(f"{crate}/{version}: crates.io answered {str(data)[:200]}")
    if v.get("yanked"):
        problems.append(f"{crate} {version} is yanked")
    checksum = v.get("checksum")
    compared += 1
    if not checksum or checksum != expect_sha256:
        problems.append(f"{crate} {version}: crates.io has checksum {checksum}, the gate packaged {expect_sha256}")
    compared += 1
    license_here = tomllib.loads(manifest_text).get("package", {}).get("license")
    if v.get("license") != license_here:
        problems.append(f"{crate} {version}: crates.io has license {v.get('license')!r}, the manifest {license_here!r}")
    compared += 1
    users = fetch(f"{crate}/owners").get("users")
    if not isinstance(users, list):
        raise RegistryError(f"{crate}/owners: crates.io answered without users")
    found = sorted(u.get("login", "?") for u in users)
    if found != sorted(owners):
        problems.append(f"{crate}: crates.io owners are {found}, the roster has {sorted(owners)}")
    compared += 1
    body = download(f"{STATIC_CRATES}/{crate}/{crate}-{version}.crate")
    got = hashlib.sha256(body).hexdigest()
    if got != checksum:
        problems.append(f"{crate} {version}: the downloaded .crate has sha256 {got}, crates.io lists {checksum}")
    else:
        info = vcs_info(body, crate, version)
        compared += 1
        if info is None:
            problems.append(f"{crate} {version}: the .crate has no .cargo_vcs_info.json (packaged outside git)")
        else:
            sha1 = info.get("git", {}).get("sha1")
            if sha1 != commit:
                problems.append(f"{crate} {version}: the .crate was packaged from {sha1}, not {commit}")
            if info.get("git", {}).get("dirty"):
                problems.append(f"{crate} {version}: the .crate was packaged from a dirty checkout")
            if info.get("path_in_vcs", "") != manifest_dir:
                problems.append(f"{crate} {version}: the .crate was packaged from {info.get('path_in_vcs')!r}, "
                                f"not {manifest_dir!r}")
    compared += 1
    tag = tag_name(c)
    at = tag_commit(tag)
    if at is None:
        problems.append(f"{crate}: tag {tag} is not on the remote")
    elif at != commit:
        problems.append(f"{crate}: tag {tag} names {at}, not {commit}")
    return problems, compared


def mark_published(text: str, crate: str) -> str:
    """the roster text with `crate`'s state changed from prepared to published"""
    head = f"[crate.{crate}]"
    lines = text.splitlines(keepends=True)
    block = False
    changed = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("["):
            block = s == head
            continue
        if block and re.fullmatch(r'state\s*=\s*"prepared"\s*(#.*)?', s):
            lines[i] = line.replace('"prepared"', '"published"', 1)
            changed += 1
    if changed != 1:
        raise RosterError(f"{crate}: the roster has no single `state = \"prepared\"` line to mark")
    new = "".join(lines)
    if load_roster(new)[crate]["state"] != "published":
        raise RosterError(f"{crate}: marking it published did not take")
    return new


# ---------------------------------------------------------------- after the publish: who has to follow

DEP_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")


def registry_requirements_on(text: str, crate: str) -> list[tuple[str, str]]:
    """(table, requirement) of every dependency of a manifest on `crate` that cargo resolves from the
    registry: a version with neither `path` nor `git` (a path or git dependency builds against the
    repository's state, which the push gate of that repository checks; it is not decided by a publish)"""
    data = tomllib.loads(text)
    tables = [(t, data.get(t, {})) for t in DEP_TABLES]
    for cfg, target in data.get("target", {}).items():
        tables += [(f"target.{cfg}.{t}", target.get(t, {})) for t in DEP_TABLES]
    tables.append(("workspace.dependencies", data.get("workspace", {}).get("dependencies", {})))
    out = []
    for table, deps in tables:
        for key, spec in deps.items():
            name = alice_name(spec.get("package", key) if isinstance(spec, dict) else key)
            if name != crate:
                continue
            if isinstance(spec, str):
                out.append((table, spec))
            elif spec.get("workspace") or "path" in spec or "git" in spec:
                continue  # the workspace's own entry is read in its table; path / git: not the registry
            elif spec.get("version"):
                out.append((table, spec["version"]))
    return out


def registry_consumer_problems(roster: dict[str, dict], crate: str, version: str, repos_root: Path,
                               at: str) -> tuple[list[str], int]:
    """(problems, manifests read): every Cargo.toml at `at` of every git checkout directly under
    `repos_root` (one per remote; workspace members included) that requires `crate` from the registry with a
    requirement `version` does not satisfy. Publishing then would leave that consumer unable to take the
    release until its requirement is widened (the publish-wave widening), so the publish is refused.
    Every repository of the roster must have a checkout there (else the read would silently cover less than
    the train); no manifest read is a refusal too (a scan that compared nothing)"""
    problems: list[str] = []
    missing = sorted(r for r in {c["repo"] for c in roster.values() if "repo" in c}
                     if not (repos_root / r / ".git").exists())
    if missing:
        problems.append(f"{crate}: no checkout of {', '.join(missing)} under {repos_root} "
                        "(the consumer read must cover every repository of the train; give --repos-root)")
    read = 0
    seen: set[str] = set()
    for repo in sorted(d for d in repos_root.iterdir() if (d / ".git").exists()):
        try:
            remote = git(repo, "remote", "get-url", "origin").strip().lower().removesuffix(".git")
        except subprocess.CalledProcessError:
            remote = str(repo.resolve())
        if remote in seen:
            continue  # another clone of the same repository
        seen.add(remote)
        try:
            names = git(repo, "ls-tree", "-r", "--name-only", at).splitlines()
        except subprocess.CalledProcessError:
            continue  # no `at` in this checkout (not a published repository)
        for path in sorted(n for n in names if n == "Cargo.toml" or n.endswith("/Cargo.toml")):
            text = git(repo, "show", f"{at}:{path}")
            read += 1
            try:
                if tomllib.loads(text).get("package", {}).get("name") == crate:
                    continue
                reqs = registry_requirements_on(text, crate)
            except tomllib.TOMLDecodeError as e:
                problems.append(f"{repo.name}/{path}: not readable TOML ({e})")
                continue
            for table, req in reqs:
                if not dc.matches(req, version):
                    problems.append(f"{repo.name}/{path} [{table}] {crate} = {req!r} does not accept {version} "
                                    "(registry consumer: widen it before publishing)")
    if read == 0:
        problems.append(f"{crate}: no Cargo.toml was read under {repos_root} at {at} (the consumer read compared nothing)")
    return problems, read


def requirements_on(text: str, crate: str) -> list[tuple[str, str | None]]:
    """(table, requirement or None when there is no version) of every dependency of a
    manifest on `crate`, in every table cargo reads (normal, dev, build, target, workspace)"""
    data = tomllib.loads(text)
    tables = [(t, data.get(t, {})) for t in DEP_TABLES]
    for cfg, target in data.get("target", {}).items():
        tables += [(f"target.{cfg}.{t}", target.get(t, {})) for t in DEP_TABLES]
    tables.append(("workspace.dependencies", data.get("workspace", {}).get("dependencies", {})))
    out = []
    for table, deps in tables:
        for key, spec in deps.items():
            name = alice_name(spec.get("package", key) if isinstance(spec, dict) else key)
            if name != crate:
                continue
            if isinstance(spec, str):
                out.append((table, spec))
            elif spec.get("workspace"):
                continue  # the requirement is the workspace's, listed under workspace.dependencies
            else:
                out.append((table, spec.get("version")))
    return out


def package_features(metadata, checkout: Path, manifest_path: str, name: str) -> dict:
    """the `features` map Cargo has for the crate `name` of the manifest in a checkout (the
    [features] table and the implicit feature of each optional dependency no feature names as
    `dep:`), from `cargo metadata --no-deps` through `metadata` (scripts/semver_gate.py
    cargo_metadata); semver_gate.MetadataError when it cannot be read (never a fallback to the
    [features] keys)"""
    return sg.features_from_metadata(metadata(checkout / manifest_path), name)


def feature_coverage_problems(name: str, entry: dict, features: dict) -> tuple[list[str], int]:
    """(problems, 1 when compared): a crate that declares consumer feature sets builds and
    semver-checks only those, so every feature of its universe (cargo metadata, implicit ones
    included, `default` aside) must be enabled by a set, directly or through another feature
    (semver_gate.feature_closure), or be in `exclude-features` (with the reason), and the sets
    may name only features the crate has; a crate that declares none builds every feature at
    once and is not compared (0)"""
    sets = entry.get("consumer-features", []) + entry.get("consumer-check-features", [])
    if not sets:
        return [], 0
    excluded = set(entry.get("exclude-features", {}))
    declared = set().union(*(set(s) for s in sets))
    problems = []
    missing = sg.uncovered_features(sg.universe_from_features(features), features, sets, excluded)
    if missing:
        problems.append(f"{name}: the crate's feature(s) {missing} are enabled by no consumer set and not in "
                        f"exclude-features: a declared set does not grow with the manifest, so they would be "
                        f"neither built nor semver-checked")
    unknown = sg.unknown_features(features, declared | excluded)
    if unknown:
        problems.append(f"{name}: the consumer sets or exclude-features name {unknown}, which the crate "
                        f"does not have")
    return problems, 1


def feature_coverage(roster: dict[str, dict], repos_root: Path, metadata) -> tuple[list[str], list[str], int]:
    """(problems, crates covered, crates compared) of feature_coverage_problems for every roster
    crate that declares consumer sets, read from the working tree of its repository's checkout
    (cargo metadata reads files, so the tree must have no change to a tracked file)"""
    problems, ok, compared = [], [], 0
    for name, c in sorted(roster.items()):
        if not (c.get("consumer-features") or c.get("consumer-check-features")):
            continue
        if "repo" not in c:
            problems.append(f"{name}: declares consumer sets but names no repository")
            continue
        repo = repos_root / c["repo"]
        if not (repo / ".git").exists():
            problems.append(f"{name}: {repo} is not a checkout")
            continue
        if git(repo, "status", "--porcelain", "--untracked-files=no").strip():
            problems.append(f"{name}: {repo} has changes to tracked files (the commit is what is checked)")
            continue
        path = c.get("manifest-path", "Cargo.toml")
        head = git(repo, "rev-parse", "--short", "HEAD").strip()
        p, n = feature_coverage_problems(name, c, package_features(metadata, repo, path, name))
        problems += p
        compared += n
        if not p:
            ok.append(f"{name}: {c['repo']}/{path} at {head}")
    return problems, ok, compared


# the keys a repository's semver-gate config copies from its roster entry (equal-version is
# the repository's own: how its CI treats a version not raised yet)
SEMVER_CONFIG_COPIED = ("consumer-features", "consumer-check-features", "consumer-features-why",
                        "exclude-features")


def feature_sets_key(sets: list) -> list:
    """the declared sets with order ignored: within a set and between sets"""
    return sorted(sorted(s) for s in sets)


def workflow_command_lines_at(text: str) -> list[tuple[str, int]]:
    """(logical line, index of the physical line it starts on) of a workflow or script: a
    `run: |` block gives its lines and a `run: >` block one folded line (YAML's two block
    styles), a line continued with a backslash is joined to the next"""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        m = re.match(r"^(\s*)(?:-\s+)?run:\s*([|>])[-+]?\s*$", lines[i])
        if not m:
            out.append((lines[i], i))
            i += 1
            continue
        base, style = len(m.group(1)), m.group(2)
        block = []
        i += 1
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > base):
            block.append((lines[i].strip(), i))
            i += 1
        if style == ">":
            kept = [x for x in block if x[0]]
            if kept:
                out.append((" ".join(x for x, _ in kept), kept[0][1]))
        else:
            out += block
    joined, cur, start = [], "", None
    for line, k in out:
        if start is None:
            start = k
        if line.rstrip().endswith("\\"):
            cur += line.rstrip()[:-1] + " "
            continue
        joined.append((cur + line, start))
        cur, start = "", None
    if cur:
        joined.append((cur, start))
    return joined


def workflow_command_lines(text: str) -> list[str]:
    """the logical lines of workflow_command_lines_at, without their positions"""
    return [line for line, _ in workflow_command_lines_at(text)]


SHELL_SEPARATORS = {"&&", "||", ";", "|", "&", "|&", ";;", "(", ")"}


def semver_gate_invocations(repo: Path, at: str) -> tuple[list[tuple[str, str, str | None, int]], list[str]]:
    """((workflow, manifest, config or None, line) of every run of scripts/semver_gate.py in the
    repository's workflows at `at`, problems): a run is any command whose argv has an element
    named semver_gate.py, whatever the interpreter and its flags (--manifest defaults to
    Cargo.toml; no --config means the every-feature set); a --manifest or --config that is not
    a literal path, a line that cannot be split, and semver_gate.py named anywhere but a run,
    a step name or a path filter are problems (an unknown form fails closed)"""
    names = git(repo, "ls-tree", "--name-only", f"{at}:.github/workflows").split()
    found, problems = [], []
    for wf in sorted(n for n in names if n.endswith((".yml", ".yaml"))):
        for line, pos in workflow_command_lines_at(git(repo, "show", f"{at}:.github/workflows/{wf}")):
            if not re.search(r"(?<![\w-])semver_gate\.py\b", line):
                continue
            stripped = line.strip()
            if (stripped.startswith("#") or re.match(r"^(?:-\s+)?name:", stripped)
                    or re.fullmatch(r"-\s+['\"]?[\w./*-]*semver_gate\.py['\"]?", stripped)):
                continue
            where = f".github/workflows/{wf}: {stripped[:120]}"
            try:
                lex = shlex.shlex(re.sub(r"^(?:-\s+)?run:\s*", "", stripped), posix=True, punctuation_chars=True)
                lex.whitespace_split = True
                lex.commenters = "#"
                argv = list(lex)
            except ValueError as e:
                problems.append(f"{where}: cannot split the command ({e})")
                continue
            idx = [k for k, x in enumerate(argv) if Path(x).name == "semver_gate.py"]
            if not idx:
                problems.append(f"{where}: names semver_gate.py outside a run of it, a step name or a path filter")
                continue
            for k in idx:
                args = []
                for x in argv[k + 1:]:
                    if x in SHELL_SEPARATORS:
                        break
                    args.append(x)
                opts = {"--manifest": "Cargo.toml", "--config": None}
                for j, x in enumerate(args):
                    for key in opts:
                        if x == key and j + 1 < len(args):
                            opts[key] = args[j + 1]
                        elif x.startswith(key + "="):
                            opts[key] = x.split("=", 1)[1]
                literal = [v for v in opts.values() if v is not None and ("$" in v or "{{" in v)]
                if literal:
                    problems.append(f"{where}: {literal} is not a literal path, so the config cannot be compared")
                    continue
                found.append((wf, opts["--manifest"], opts["--config"], pos))
    return found, problems


SEMVER_GATE_FILE = "scripts/semver_gate.py"
# where a run of the gate outside the workflows is allowed: the local copy of the CI steps
SEMVER_GATE_WRAPPERS_ALLOWED = {"scripts/preflight.sh"}
SHELL_LIKE = (".sh", ".bash", ".ps1")


def semver_gate_wrappers(repo: Path, at: str) -> list[str]:
    """tracked shell scripts (and Makefile / justfile) at `at` that run semver_gate.py as a
    command, scripts/preflight.sh aside: a workflow that runs such a wrapper reads a config
    this check cannot see"""
    names = git(repo, "ls-tree", "-r", "--name-only", at).splitlines()
    found = []
    for n in names:
        base = Path(n).name
        if n in SEMVER_GATE_WRAPPERS_ALLOWED or not (n.endswith(SHELL_LIKE) or base in ("Makefile", "justfile")):
            continue
        for line in workflow_command_lines(git(repo, "show", f"{at}:{n}")):
            if line.strip().startswith("#") or not re.search(r"(?<![\w-])semver_gate\.py\b", line):
                continue
            try:
                argv = shlex.split(line, comments=True)
            except ValueError:
                argv = line.split()
            if any(Path(x).name == "semver_gate.py" for x in argv):
                found.append(n)
                break
    return found


JOB_KEY_INDENT = 4


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def yaml_scalar(value: str) -> str:
    """a one-line YAML value without quotes, `${{ }}` and a trailing comment"""
    v = re.sub(r"\s+#.*$", "", value).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1].strip()
    m = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", v)
    return m.group(1) if m else v


def workflow_jobs(lines: list[str]) -> dict[str, tuple[int, int]]:
    """job name -> (first line, end) of the jobs under `jobs:`"""
    jobs_at = next((k for k, l in enumerate(lines) if l.rstrip() == "jobs:"), None)
    if jobs_at is None:
        return {}
    starts = [k for k in range(jobs_at + 1, len(lines)) if JOB_RE.match(lines[k])]
    return {JOB_RE.match(lines[a]).group(1): (a, b) for a, b in zip(starts, starts[1:] + [len(lines)])}


def block_keys(lines: list[str], start: int, end: int, indent: int, first: str = "") -> dict[str, tuple[str, int]]:
    """key -> (value, line) of the mapping at `indent` in lines[start:end] (`first` is the
    rest of a `- ` line that opens a list item)"""
    keys = {}
    if first:
        m = re.match(r"^([\w-]+):\s*(.*)$", first)
        if m:
            keys[m.group(1)] = (m.group(2), start)
    for k in range(start + (1 if first else 0), end):
        l = lines[k]
        if l.strip() and not l.lstrip().startswith("#") and indent_of(l) == indent:
            m = re.match(r"^\s*([\w-]+):\s*(.*)$", l)
            if m:
                keys[m.group(1)] = (m.group(2), k)
    return keys


def list_value(lines: list[str], value: str, at: int) -> list[str]:
    """a YAML list given inline (`[a, b]`), as one scalar, or as `- x` lines below `at`"""
    v = re.sub(r"\s+#.*$", "", value).strip()
    if v.startswith("["):
        return [x.strip().strip("'\"") for x in v.strip("[]").split(",") if x.strip()]
    if v:
        return [v.strip("'\"")]
    out, base = [], indent_of(lines[at])
    for l in lines[at + 1:]:
        if not l.strip() or l.lstrip().startswith("#"):
            continue
        if indent_of(l) <= base:
            break
        m = re.match(r"^\s*-\s+(.*)$", l)
        if m:
            out.append(re.sub(r"\s+#.*$", "", m.group(1)).strip().strip("'\""))
    return out


def step_of(lines: list[str], job: tuple[int, int], k: int) -> tuple[int, int, int, str]:
    """(first line, end, key indent, rest of the `- ` line) of the step holding line k"""
    a, b = job
    for s0 in range(k, a, -1):
        m = re.match(r"^(\s*)-\s+(.*)$", lines[s0])
        if m and (s0 == k or indent_of(lines[s0]) < indent_of(lines[k]) or lines[k].strip() == ""):
            dash = len(m.group(1))
            end = next((j for j in range(s0 + 1, b) if lines[j].strip() and not lines[j].lstrip().startswith("#")
                        and indent_of(lines[j]) <= dash), b)
            return s0, end, dash + 2, m.group(2)
    return k, k + 1, indent_of(lines[k]), ""


ALLOWED_EVENT_IFS = {"github.event_name == 'push'", "github.event_name == 'pull_request'"}
CHANGES_IF = "needs.changes.outputs.run == 'true'"
SOFT_AFTER = {"||", "|", "&"}
SHELL_COMPOUND_WORDS = {"if", "elif", "while", "until", "!", "then", "do", "else", "case", "for", "select",
                        "function", "{", "}", "(", ")"}


def is_shell_operator(tok: str) -> bool:
    """a control or redirection operator of a shlex token stream (punctuation_chars)"""
    return bool(tok) and all(c in "|&;<>()" for c in tok)


def is_redirection(tok: str) -> bool:
    return is_shell_operator(tok) and ("<" in tok or ">" in tok) and not any(c in "|;()" for c in tok)


def pattern_lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


def on_section(lines: list[str]) -> tuple[int, int] | None:
    k = next((i for i, l in enumerate(lines) if re.match(r"^(on|'on'|\"on\"|true):\s*$", l)), None)
    if k is None:
        return None
    end = next((j for j in range(k + 1, len(lines)) if lines[j].strip() and not lines[j].startswith((" ", "#"))),
               len(lines))
    return k, end


def semver_gate_block_problems(repo: Path, at: str, wf: str, text: str, pos: int, config: str) -> list[str]:
    """problems that would let the run of the gate at line `pos` of workflow `wf` not block:
    a soft run (anything but the gate as the whole of its step's run, one simple command:
    an operator, keyword, group, function, subshell, condition or `set +e` around it;
    continue-on-error on the step or the job; a shell other than bash / sh), an `if:` other than none, the push /
    pull_request event conditions or the changed-paths decision (which needs a non-soft
    `if: always()` gate job that needs both jobs and a decide step reading the patterns file),
    and a workflow a push to main with a change to the gate does not reach"""
    lines = text.splitlines()
    where = f".github/workflows/{wf}:{pos + 1}"
    problems = []
    jobs = workflow_jobs(lines)
    job = next((n for n, (a, b) in jobs.items() if a <= pos < b), None)
    if job is None:
        return [f"{where}: the run of semver_gate.py is not inside a job"]
    ja, jb = jobs[job]
    jkeys = block_keys(lines, ja, jb, JOB_KEY_INDENT)
    sa, se, sind, first = step_of(lines, (ja, jb), pos)
    skeys = block_keys(lines, sa, se, sind, first)
    # the gate is the whole of its step's run: one logical command after comments and blank
    # lines are left out (a group, a function, a subshell, a here-document, a condition or
    # `set +e` around it would each be a second line or an operator, and errexit does not
    # stop in all of them); the gate is in that command itself (not in an env value the run
    # expands)
    commands = []
    if "run" in skeys:
        value, rk = skeys["run"]
        if re.fullmatch(r"[|>][-+]?\s*(#.*)?", value.strip()):
            body = []
            for j in range(rk + 1, se):
                if lines[j].strip() and indent_of(lines[j]) <= indent_of(lines[rk]):
                    break
                body.append(lines[j])
            run_text = "\n".join(body)
            if value.strip().startswith(">"):
                run_text = " ".join(l.strip() for l in body if l.strip() and not l.strip().startswith("#"))
        else:
            run_text = value
        commands = [l for l, _ in workflow_command_lines_at(run_text) if l.strip() and not l.strip().startswith("#")]
    if len(commands) != 1:
        problems.append(f"{where}: the step's run has {len(commands)} commands (the run of semver_gate.py is "
                        f"the whole of its step's run, one simple command)")
    logical = commands[0] if len(commands) == 1 else ""
    # the command: no operator but a redirection after the gate, only assignments and the
    # interpreter before it
    lex = shlex.shlex(re.sub(r"^\s*(?:-\s+)?run:\s*", "", logical), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        toks = list(lex)
    except ValueError:
        toks = []
    k = next((i for i, x in enumerate(toks) if Path(x).name == "semver_gate.py"), None)
    if k is None and len(commands) == 1:
        problems.append(f"{where}: the step's run does not run semver_gate.py itself (the gate is the run, "
                        f"not a value it expands)")
    elif k is not None:
        # a simple command: only redirections after it, and only assignments and the
        # interpreter before it (errexit does not stop on a command left of `&&` or `||`,
        # negated with `!`, or in an if / elif / while / until condition)
        after = next((x for x in toks[k + 1:] if is_shell_operator(x) and not is_redirection(x)), None)
        if after is not None:
            problems.append(f"{where}: `{after}` after the run of semver_gate.py (the gate is a simple command, "
                            f"on a line of its own)")
        before = next((x for x in toks[:k] if is_shell_operator(x) or x in SHELL_COMPOUND_WORDS
                       or "$(" in x or "`" in x), None)
        if before is not None:
            problems.append(f"{where}: `{before}` before the run of semver_gate.py (the gate is a simple command, "
                            f"not in a condition, a negation or a list)")
    # continue-on-error and shell
    for what, keys in (("step", skeys), (f"job {job}", jkeys)):
        if "continue-on-error" in keys and yaml_scalar(keys["continue-on-error"][0]).lower() != "false":
            problems.append(f"{where}: continue-on-error on the {what} lets a failure pass")
    shell = yaml_scalar(skeys["shell"][0]) if "shell" in skeys else None
    if shell is not None and shell not in ("bash", "sh"):
        problems.append(f"{where}: shell {shell!r} on the step (only bash or sh, which stop on a failed command)")
    # if:
    if "if" in skeys and yaml_scalar(skeys["if"][0]) not in ALLOWED_EVENT_IFS:
        problems.append(f"{where}: the step has `if: {yaml_scalar(skeys['if'][0])}`")
    changes_gated = False
    if "if" in jkeys:
        cond = yaml_scalar(jkeys["if"][0])
        if cond == CHANGES_IF:
            changes_gated = True
        elif cond not in ALLOWED_EVENT_IFS:
            problems.append(f"{where}: job {job} has `if: {cond}`")
    # reachability on a push to main
    on = on_section(lines)
    on_push = on_pr = None
    if on is None:
        flow = next((l for l in lines if re.match(r"^on:\s*\S", l)), None)
        if flow is None or "push" not in flow:
            problems.append(f"{where}: the workflow does not run on push")
    else:
        okeys = block_keys(lines, on[0] + 1, on[1], 2)
        if "push" not in okeys:
            problems.append(f"{where}: the workflow does not run on push")
        else:
            pa = okeys["push"][1]
            pend = next((j for j in range(pa + 1, on[1]) if lines[j].strip() and not lines[j].lstrip().startswith("#")
                         and indent_of(lines[j]) <= 2), on[1])
            on_push = block_keys(lines, pa, pend, 4)
        if "pull_request" in okeys:
            pa = okeys["pull_request"][1]
            pend = next((j for j in range(pa + 1, on[1]) if lines[j].strip() and not lines[j].lstrip().startswith("#")
                         and indent_of(lines[j]) <= 2), on[1])
            on_pr = block_keys(lines, pa, pend, 4)
    gate_files = {SEMVER_GATE_FILE, config}
    if on_push is not None:
        if "branches" in on_push:
            branches = list_value(lines, *on_push["branches"])
            if "main" not in branches:
                problems.append(f"{where}: on.push.branches {branches} does not include main")
        if "paths-ignore" in on_push:
            problems.append(f"{where}: on.push has paths-ignore (a change to the gate could be ignored)")
        if "paths" in on_push:
            paths = list_value(lines, *on_push["paths"])
            missing = sorted(f for f in gate_files if f not in paths)
            if missing:
                problems.append(f"{where}: on.push.paths does not list {missing}")
        elif changes_gated is False and False:
            pass
    if changes_gated:
        problems += changes_gate_problems(repo, at, wf, lines, jobs, job, on_pr, gate_files, where)
    return problems


def changes_gate_problems(repo: Path, at: str, wf: str, lines: list[str], jobs: dict, job: str, on_pr: dict | None,
                          gate_files: set, where: str) -> list[str]:
    """a job gated by `needs.changes.outputs.run == 'true'` blocks only with a non-soft
    `if: always()` job that needs `changes` and it and runs `changed_paths.py gate`, and a
    `changes` job whose decide step reads a patterns file that lists the gate files (and
    that on.pull_request.paths repeats)"""
    problems = []
    gates = []
    for n, (a, b) in jobs.items():
        keys = block_keys(lines, a, b, JOB_KEY_INDENT)
        if "if" in keys and yaml_scalar(keys["if"][0]) == "always()":
            gates.append((n, a, b, keys))
    gate = None
    for n, a, b, keys in gates:
        needs = list_value(lines, *keys["needs"]) if "needs" in keys else []
        if "changes" in needs and job in needs:
            gate = (n, a, b, keys)
    if gate is None:
        return [f"{where}: job {job} runs only when the changes job says so, and no `if: always()` job that needs "
                f"both changes and {job} checks the outcome"]
    n, a, b, keys = gate
    if "continue-on-error" in keys and yaml_scalar(keys["continue-on-error"][0]).lower() != "false":
        problems.append(f"{where}: continue-on-error on the gate job {n}")
    gate_runs = [l for l, k in workflow_command_lines_at("\n".join(lines)) if a <= k < b and "changed_paths.py" in l]
    ran_gate = False
    for l in gate_runs:
        lex = shlex.shlex(re.sub(r"^\s*(?:-\s+)?run:\s*", "", l), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
        for i, x in enumerate(toks):
            if Path(x).name == "changed_paths.py" and i + 1 < len(toks) and toks[i + 1] == "gate":
                ran_gate = True
                after = next((y for y in toks[i + 2:] if y in SOFT_AFTER | {"&&", ";"}), None)
                if after in SOFT_AFTER:
                    problems.append(f"{where}: `{after}` after changed_paths.py gate in job {n}")
    if not ran_gate:
        problems.append(f"{where}: the gate job {n} does not run changed_paths.py gate")
    for k in range(a, b):
        m = re.match(r"^(\s*)(?:-\s+)?(if|continue-on-error):\s*(.*)$", lines[k])
        if m and indent_of(lines[k]) > JOB_KEY_INDENT:
            problems.append(f"{where}: a step of the gate job {n} has `{m.group(2)}: {yaml_scalar(m.group(3))}`")
    if "changes" not in jobs:
        return problems + [f"{where}: there is no changes job"]
    ca, cb = jobs["changes"]
    files = []
    for l, k in workflow_command_lines_at("\n".join(lines)):
        if ca <= k < cb and "changed_paths.py" in l:
            lex = shlex.shlex(re.sub(r"^\s*(?:-\s+)?run:\s*", "", l), posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            toks = list(lex)
            if any(Path(x).name == "changed_paths.py" for x in toks) and "decide" in toks:
                inline = [x for x in toks if x in ("--pattern", "--patterns") or x.startswith(("--pattern=", "--patterns="))]
                if inline:
                    problems.append(f"{where}: the decide step lists patterns inline (it must read the patterns file)")
                for i, x in enumerate(toks):
                    if x == "--patterns-file" and i + 1 < len(toks):
                        files.append(toks[i + 1])
                    elif x.startswith("--patterns-file="):
                        files.append(x.split("=", 1)[1])
    if len(files) != 1 or "$" in files[0] or "{{" in files[0]:
        return problems + [f"{where}: the decide step of the changes job reads {files or 'no'} patterns file "
                           f"(exactly one literal --patterns-file)"]
    try:
        patterns = pattern_lines(git(repo, "show", f"{at}:{files[0]}"))
    except subprocess.CalledProcessError:
        return problems + [f"{where}: the patterns file {files[0]} is not tracked"]
    missing = sorted(f for f in gate_files if f not in patterns)
    if missing:
        problems.append(f"{where}: {files[0]} does not list {missing}")
    if on_pr is not None and "paths" in on_pr and list_value(lines, *on_pr["paths"]) != patterns:
        problems.append(f"{where}: on.pull_request.paths is not the patterns of {files[0]}")
    return problems


def semver_config_drift(roster: dict[str, dict], repos_root: Path, at: str) -> tuple[list[str], list[str], int]:
    """(problems, configs that match, runs compared): every run of scripts/semver_gate.py in
    the workflows of a roster repository at `at` checks the crate of its --manifest, and the
    --config it reads carries that crate's roster sets, reason and excluded features (no
    --config, or a config without sets, where the roster declares none: the every-feature
    set); a repository with no such run is not wired yet"""
    problems, ok, compared = [], [], 0
    by_repo: dict[str, list[str]] = {}
    for name, c in roster.items():
        if "repo" in c:
            by_repo.setdefault(c["repo"], []).append(name)
    for repo_name in sorted(by_repo):
        repo = repos_root / repo_name
        if not (repo / ".git").exists():
            problems.append(f"{repo_name}: {repo} is not a checkout (every roster repository is read)")
            continue
        runs, run_problems = semver_gate_invocations(repo, at)
        problems += [f"{repo_name}/{p}" for p in run_problems]
        problems += [f"{repo_name}/{w}: runs semver_gate.py outside the workflows (a wrapper hides the config "
                     f"from this check; run it from the workflow directly)" for w in semver_gate_wrappers(repo, at)]
        # a repository that carries the gate runs it from a workflow (the integration repository,
        # whose roster crates are all exempt, uses it as a library of the publish gate)
        has_gate = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{at}:{SEMVER_GATE_FILE}"],
                                  env=GIT_ENV, capture_output=True).returncode == 0
        exempt = all(roster[n]["state"] == "exempt" for n in by_repo[repo_name])
        if has_gate and not runs and not run_problems and not exempt:
            problems.append(f"{repo_name}: {SEMVER_GATE_FILE} is tracked but no workflow runs it directly")
        # anchored on the tracked files: every tracked config is read by exactly one direct run,
        # and every run reads a tracked config
        tracked = git(repo, "ls-tree", "-r", "--name-only", at, "--", ".github").splitlines()
        configs = sorted(n for n in tracked if re.fullmatch(r"\.github/semver-gate[\w.-]*\.toml", n))
        for c in configs:
            n_runs = sum(1 for _, _, cfg, _ in runs if cfg == c)
            if n_runs != 1:
                problems.append(f"{repo_name}/{c}: read by {n_runs} direct workflow run(s) (exactly one reads a config)")
        untracked = [(wf, cfg) for wf, _, cfg, _ in runs if cfg not in configs]
        for wf, cfg in untracked:
            problems.append(f"{repo_name}/.github/workflows/{wf}: a run of semver_gate.py reads "
                            f"{'no --config' if cfg is None else cfg}, which is not a tracked .github/semver-gate*.toml")
        runs = [r for r in runs if r[2] in configs]
        for wf, manifest_path, config_path, pos in runs:
            where = f"{repo_name}/.github/workflows/{wf} (--manifest {manifest_path}, --config {config_path})"
            name = tomllib.loads(git(repo, "show", f"{at}:{manifest_path}")).get("package", {}).get("name")
            if name not in by_repo[repo_name]:
                problems.append(f"{where}: the manifest is {name!r}, which is not a roster crate of {repo_name} "
                                f"({', '.join(sorted(by_repo[repo_name]))})")
                continue
            config = tomllib.loads(git(repo, "show", f"{at}:{config_path}"))
            problems += [f"{repo_name}/{p}" for p in semver_gate_block_problems(
                repo, at, wf, git(repo, "show", f"{at}:.github/workflows/{wf}"), pos, config_path)]
            compared += 1
            entry = roster[name]
            diff = []
            for key in SEMVER_CONFIG_COPIED:
                want, have = entry.get(key), config.get(key)
                if key in ("consumer-features", "consumer-check-features"):
                    want, have = feature_sets_key(want or []), feature_sets_key(have or [])
                elif key == "exclude-features":
                    want, have = want or {}, have or {}
                if want != have:
                    diff.append(f"{key}: roster {entry.get(key)!r}, config {config.get(key)!r}")
            if diff:
                problems.append(f"{where} at {at} is not a copy of [crate.{name}]: " + "; ".join(diff))
            else:
                ok.append(f"{name}: {where}")
    return problems, ok, compared


def followups(roster: dict[str, dict], crate: str, repos_root: Path, at: str) -> tuple[list[str], list[str], int]:
    """(to bump, already accept, manifests read): every Cargo.toml at `at` in the
    repositories of the roster that requires `crate` without accepting its roster version"""
    version = roster[crate]["version"]
    own = roster[crate].get("repo")
    bump, ok = [], []
    read = 0
    for repo_name in sorted({c["repo"] for c in roster.values() if "repo" in c}):
        repo = repos_root / repo_name
        if not (repo / ".git").exists():
            raise FileNotFoundError(f"{repo}: not a checkout (followups reads every repository of the roster)")
        names = git(repo, "ls-tree", "-r", "--name-only", at).splitlines()
        for path in sorted(n for n in names if n == "Cargo.toml" or n.endswith("/Cargo.toml")):
            text = git(repo, "show", f"{at}:{path}")
            read += 1
            if tomllib.loads(text).get("package", {}).get("name") == crate:
                continue
            for table, req in requirements_on(text, crate):
                where = f"{repo_name}/{path} [{table}] {crate}"
                if req is None:
                    if repo_name == own:
                        continue  # a path to the crate in its own repository (fuzz, examples) follows by itself
                    bump.append(f"{where}: no version requirement (path or git only)")
                elif not dc.matches(req, version):
                    bump.append(f"{where} = {req!r} does not accept {version}")
                else:
                    ok.append(f"{where} = {req!r} accepts {version}")
    return bump, ok, read


# ---------------------------------------------------------------- command line

def main(argv: list[str] | None = None, fetch: dc.Fetch = dc.http_fetch,
         lister=cargo_package_list, packager=cargo_package, download=http_get,
         docs=docs_rs_status, tags=remote_tag_commit, actions=github_actions_jobs, consumer=None,
         reviewers=environment_reviewers, inventory=secret_inventory, semver=semver_problems,
         metadata=sg.cargo_metadata, consumers=registry_consumer_problems) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("next")

    def release_args(p, verify: bool):
        p.add_argument("crate")
        p.add_argument("--repo", type=Path, required=True, help="the release checkout")
        p.add_argument("--ref", required=True, help="the commit being (or that was) published")
        p.add_argument("--manifest-path", default="Cargo.toml", help="the crate's manifest, relative to the repository")
        if verify:
            p.add_argument("--expect-sha256", required=True, help="the .crate sha256 the gate printed")
            p.add_argument("--remote", default="origin", help="where the tag is read")

    g = sub.add_parser("publish-gate")
    release_args(g, False)
    g.add_argument("--repos-root", type=Path,
                   help="the directory holding a checkout of every consumer repository (default: the parent of --repo)")
    g.add_argument("--consumers-at", default="origin/main",
                   help="the commit read in each consumer checkout (fetch first)")
    g.add_argument("--target-dir", type=Path, help="where cargo package builds (default: a temporary directory)")
    release_args(sub.add_parser("publish-verify"), True)
    m = sub.add_parser("roster-mark")
    m.add_argument("state", choices=["published"])
    release_args(m, True)
    f = sub.add_parser("followups")
    f.add_argument("crate")
    f.add_argument("--repos-root", type=Path, required=True, help="the directory holding a checkout of every roster repository")
    f.add_argument("--at", default="origin/main", help="the commit read in each checkout (fetch first)")
    fc = sub.add_parser("feature-coverage")
    fc.add_argument("--repos-root", type=Path, required=True,
                    help="the directory holding a checkout of the repository of every crate with consumer sets "
                         "(its working tree is read: check out the commit to check)")
    sd = sub.add_parser("semver-config-drift")
    sd.add_argument("--repos-root", type=Path, required=True,
                    help="the directory holding a checkout of every roster repository")
    sd.add_argument("--at", default="origin/main", help="the commit read in each checkout (fetch first)")
    a = ap.parse_args(argv)
    try:
        roster_text = a.roster.read_text(encoding="utf-8")
        roster = load_roster(roster_text)
    except (OSError, ValueError) as e:
        print(f"error: {a.roster}: {e}", file=sys.stderr)
        return 2
    if a.cmd == "next":
        name, pending = next_crate(roster)
        if name is None:
            print("nothing to publish: every crate in the roster is published or exempt")
            return 1
        print(f"next: {name} {roster[name]['version']} (state {roster[name]['state']}, {pending} unpublished)")
        return 0
    if a.cmd == "feature-coverage":
        try:
            problems, ok, compared = feature_coverage(roster, a.repos_root, metadata)
        except (OSError, ValueError, sg.MetadataError, subprocess.CalledProcessError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        for line in ok:
            print(f"covered: {line}")
        for p in problems:
            print(f"refused: {p}")
        print(f"feature-coverage: {compared} crate(s) with consumer sets compared, {len(problems)} problem(s)")
        if compared == 0 and not problems:
            print("error: no crate with consumer sets was compared", file=sys.stderr)
            return 2
        return 1 if problems else 0
    if a.cmd == "semver-config-drift":
        try:
            problems, ok, compared = semver_config_drift(roster, a.repos_root, a.at)
        except (OSError, ValueError, subprocess.CalledProcessError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        for line in ok:
            print(f"copy: {line}")
        for p in problems:
            print(f"refused: {p}")
        print(f"semver-config-drift: {compared} semver_gate run(s) compared at {a.at}, {len(problems)} problem(s)")
        if compared == 0 and not problems:
            print(f"error: no workflow runs scripts/semver_gate.py at {a.at}: nothing was compared", file=sys.stderr)
            return 2
        return 1 if problems else 0
    if a.cmd != "followups" and a.crate not in roster:
        print(f"refused: {a.crate} is not in the roster")
        return 1
    try:
        if a.cmd == "followups":
            if a.crate not in roster:
                print(f"error: {a.crate} is not in the roster", file=sys.stderr)
                return 2
            bump, ok, read = followups(roster, a.crate, a.repos_root, a.at)
            if read == 0:
                print("error: no Cargo.toml was read", file=sys.stderr)
                return 2
            for line in bump:
                print(f"bump: {line}")
            for line in ok:
                print(f"ok:   {line}")
            print(f"followups for {a.crate} {roster[a.crate]['version']}: {len(bump)} to bump, "
                  f"{len(ok)} accept it ({read} manifests at {a.at})")
            return 1 if bump else 0
        problems = checkout_problems(a.repo, a.ref)
        commit = git(a.repo, "rev-parse", "--verify", f"{a.ref}^{{commit}}").strip()
        manifest_text = git(a.repo, "show", f"{a.ref}:{a.manifest_path}")
        manifest_dir = str(Path(a.manifest_path).parent).replace("\\", "/")
        manifest_dir = "" if manifest_dir == "." else manifest_dir
        if a.cmd == "publish-gate":
            root_text = None
            if manifest_dir:
                root_text = git(a.repo, "show", f"{a.ref}:Cargo.toml")
            gate_problems, compared = publish_gate(roster, a.crate, manifest_text, fetch, root_text)
            problems += gate_problems
            hist, n = history_problems(a.repo, a.ref, a.crate, roster[a.crate], a.manifest_path)
            problems += hist
            compared += n
            root = a.repos_root or a.repo.resolve().parent
            reg, n = consumers(roster, a.crate, roster[a.crate]["version"], root, a.consumers_at)
            problems += reg
            compared += n
            print(f"consumers: {n} manifests read under {root} at {a.consumers_at}")
            github = tomllib.loads(roster_text).get("train", {}).get("github")
            owner_repo = f"{github}/{roster[a.crate]['repo']}" if github and roster[a.crate].get("repo") else None
            declared_all = tomllib.loads(roster_text).get("secrets", {})
            repo_name = roster[a.crate].get("repo")
            declared = {k: v for k, v in declared_all.get(repo_name, {}).items()} if repo_name else {}
            try:
                inv = inventory(owner_repo)
            except RuntimeError as e:
                inv = None
                problems.append(f"{a.crate}: the secrets of the repository could not be read ({e})")
            roster_data = tomllib.loads(roster_text)
            declared_vars = roster_data.get("variables", {}).get(repo_name, {}) if repo_name else {}
            hosts = roster_data.get("self-hosted", {}).get(repo_name, {}) if repo_name else {}
            if inv is not None:
                problems += secret_problems(a.crate, repo_name, declared, inv)
                problems += variable_problems(a.crate, repo_name, declared_vars, inv)
                compared += 2
            wf, n = workflow_publish_problems(a.repo, a.ref, a.crate, owner_repo, reviewers, declared,
                                              (inv or {}).get("reviewers", {}), hosts,
                                              [set(x) for x in (inv or {}).get("runners", [])])
            problems += wf
            compared += n
            dispatched: list[str] = []
            if roster[a.crate].get("required-ci"):
                github = tomllib.loads(roster_text).get("train", {}).get("github")
                if not isinstance(github, str) or not github or "repo" not in roster[a.crate]:
                    print(f"error: {a.roster}: required-ci needs [train] github (the owner) and the crate's repo",
                          file=sys.stderr)
                    return 2
                ci, n = ci_problems(a.crate, roster[a.crate], f"{github}/{roster[a.crate]['repo']}", commit, actions,
                                    dispatched)
                problems += ci
                compared += n
            problems += package_problems(a.crate, lister(a.repo, a.manifest_path))
            compared += 1
            features = package_features(metadata, a.repo, a.manifest_path, a.crate)
            universe = sg.universe_from_features(features)
            cov, n = feature_coverage_problems(a.crate, roster[a.crate], features)
            problems += cov
            compared += n
            for p in problems:
                print(f"refused: {p}")
            if problems:
                return 1
            version = roster[a.crate]["version"]
            with tempfile.TemporaryDirectory() as tmp:
                crate_path = packager(a.repo, a.manifest_path, a.crate, version, a.target_dir or Path(tmp))
                body = crate_path.read_bytes()
            # `cargo publish` packages the working tree: packaging must leave it as it was (a lock
            # rewritten because a sibling checkout has another version would not be what was gated)
            after = checkout_problems(a.repo, a.ref)
            if after:
                for p in after:
                    print(f"refused: after cargo package: {p}")
                return 1
            text_problems, _ = package_text_problems(body, a.crate, version, a.repo, a.ref,
                                                     roster[a.crate].get("repo"), manifest_dir)
            if text_problems:
                for p in text_problems:
                    print(f"refused: {p}")
                return 1
            compared += 1
            consumer_problems = (consumer or consumer_build)(body, a.crate, version, a.target_dir, roster[a.crate],
                                                             universe=universe)
            for p in consumer_problems:
                print(f"refused: {p}")
            if consumer_problems:
                return 1
            compared += 1
            semver_found, n, semver_note = semver(a.repo, a.manifest_path, a.crate, version,
                                                  tomllib.loads(manifest_text), roster[a.crate], fetch,
                                                  universe=universe)
            for p in semver_found:
                print(f"refused: {p}")
            if semver_found:
                return 1
            compared += n
            digest = hashlib.sha256(body).hexdigest()
            print(f"publish-gate: go for {a.crate} {version} ({compared} checks)")
            print(f"  commit:        {commit}")
            print(f"  .crate sha256: {digest} ({len(crate_files(body))} files, built by cargo package)")
            if dispatched:
                print(f"  dispatched:    required CI from a dispatched run: {'; '.join(dispatched)}")
            print("  text:          every file of the package read by scripts/public_text_check.py")
            print("  consumer:      the unpacked .crate built as a crates.io consumer builds it (empty CARGO_HOME, "
                  "minimal environment, no Cargo.lock or configuration above it, each feature set)")
            print(f"  semver:        {semver_note}")
            print(f"  publish:       {publish_command(a.repo, a.manifest_path, lock_tracked(a.repo, a.manifest_path))}")
            print(f"  then:          python3 scripts/release_train.py roster-mark published {a.crate} "
                  f"--repo {a.repo} --ref {commit} --expect-sha256 {digest}"
                  + (f" --manifest-path {a.manifest_path}" if manifest_dir else ""))
            return 0
        # publish-verify / roster-mark
        owners = tomllib.loads(roster_text).get("train", {}).get("owners")
        if not isinstance(owners, list) or not owners:
            print(f"error: {a.roster}: [train] owners (the crates.io owners every release must have) is missing",
                  file=sys.stderr)
            return 2
        verify_problems, compared = publish_verify(
            roster, owners, a.crate, commit, manifest_text, manifest_dir, a.expect_sha256, fetch, download,
            lambda tag: tags(a.repo, a.remote, tag))
        problems += verify_problems
        version = roster[a.crate]["version"]
        try:
            d = docs(a.crate, version)
            print(f"docs.rs: {a.crate} {version} doc_status={d.get('doc_status')} (information, not a check)")
        except (OSError, ValueError, urllib.error.URLError) as e:
            print(f"docs.rs: {a.crate} {version} status not read ({e}) (information, not a check)")
    except subprocess.CalledProcessError as e:
        print(f"error: {Path(str(e.cmd[0])).name}: {(e.stderr or '').strip()}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RegistryError, sg.RegistryError, sg.MetadataError, urllib.error.URLError, TimeoutError, json.JSONDecodeError,
            tarfile.TarError) as e:
        # OSError covers a reset connection and a missing checkout; ValueError covers a
        # malformed manifest, requirement or version, and an unreadable JSON body
        print(f"error: {e}", file=sys.stderr)
        return 2
    for p in problems:
        print(f"refused: {p}")
    if problems:
        return 1
    if a.cmd == "publish-verify":
        print(f"publish-verify: {a.crate} {version} is the release of {commit} ({compared} checks)")
        return 0
    a.roster.write_text(mark_published(roster_text, a.crate), encoding="utf-8")
    print(f"roster-mark: {a.crate} {version} is published (verified, {compared} checks); commit {a.roster}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
