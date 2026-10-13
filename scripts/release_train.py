#!/usr/bin/env python3
"""The release train of the core ALICE crates (release-train.toml), checked against crates.io.

    next                                 the crate to publish now: the first in `order`
                                         that is not published (or exempt)
    publish-gate <crate> --repo DIR --ref SHA [--manifest-path P]
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
      - the package (`cargo package --list` of the manifest) carries a file under
        .github/, scripts/ or docs/, or a file or directory of private notes (named by
        hash in PRIVATE_NAME_SHA256): the repository's own tooling and notes, not the
        crate; a package that lists no file exits 2
      - after those pass, the crate `cargo package` built is unpacked into a fresh
        directory with no Cargo.lock or Cargo.toml above it and built as a dependency of
        an empty crate there, as a crates.io consumer builds it (`cargo package` verifies
        inside the repository, where a build script that reads a Cargo.lock above the
        crate still finds the repository's); a failed build, or one that did not compile
        the crate's library, is refused

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

# what the published package must not carry: the repository's CI, tools and notes
PACKAGE_EXCLUDED_DIRS = (".github/", "scripts/", "docs/")
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
        if "consumer-features" in c:
            fs = c["consumer-features"]
            if (not isinstance(fs, list) or not fs
                    or not all(isinstance(x, list) and all(isinstance(f, str) and f.strip() for f in x) for x in fs)):
                raise RosterError(f"{name}: `consumer-features` must be a non-empty list of feature lists")
            why = c.get("consumer-features-why")
            if not isinstance(why, str) or not why.strip():
                raise RosterError(f"{name}: `consumer-features` needs `consumer-features-why` (which features are "
                                  f"left out of the consumer build and why)")
        elif "consumer-features-why" in c:
            raise RosterError(f"{name}: `consumer-features-why` without `consumer-features`")
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


PATCH_DEPTH = 2000


def patch_ids(repo: Path, ref: str) -> set[str]:
    """the stable patch ids of the last PATCH_DEPTH non-merge commits of ref"""
    log = subprocess.run(["git", "-C", str(repo), "log", "-p", "--no-merges", "-n", str(PATCH_DEPTH), ref],
                         check=True, capture_output=True, env=GIT_ENV).stdout
    out = subprocess.run(["git", "-C", str(repo), "patch-id", "--stable"], input=log, check=True,
                         capture_output=True, env=GIT_ENV).stdout.decode()
    return {line.split()[0] for line in out.splitlines() if line.strip()}


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
    if wanted:
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
        for line in wanted:
            compared += 1
            if line not in section:
                problems.append(f"{crate}: the [{c['version']}] section of CHANGELOG.md does not contain {line!r}")
    return problems, compared


def gh_json_lines(path: str, jq: str) -> list[dict]:
    out = subprocess.run(["gh", "api", "--paginate", path, "--jq", jq], check=True, capture_output=True, text=True).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def github_actions_jobs(owner_repo: str, sha: str) -> dict[str, list[dict]]:
    """{workflow name: the jobs (name, status, conclusion) of its latest run on the commit}
    from the GitHub Actions API (a job of another workflow or app cannot stand in)"""
    runs = gh_json_lines(f"repos/{owner_repo}/actions/runs?head_sha={sha}&per_page=100",
                         ".workflow_runs[] | {id, name, created_at}")
    latest: dict[str, dict] = {}
    for r in runs:
        if r["name"] not in latest or r["created_at"] > latest[r["name"]]["created_at"]:
            latest[r["name"]] = r
    return {name: gh_json_lines(f"repos/{owner_repo}/actions/runs/{r['id']}/jobs?per_page=100",
                                ".jobs[] | {name, status, conclusion}")
            for name, r in latest.items()}


def ci_problems(crate: str, c: dict, owner_repo: str, commit: str, actions) -> tuple[list[str], int]:
    """(problems, compared) for the roster's `required-ci` jobs of `crate` on `commit`"""
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


def feature_specs(manifest: dict, declared: list[list[str]] | None) -> list[tuple[str, str]]:
    """(label, dependency spec) of each build: the default features, none, and either the
    roster's declared feature sets or every feature at once"""
    specs = [("default features", ""), ("no default features", ", default-features = false")]
    if declared is not None:
        for fs in declared:
            specs.append((f"features {fs}", ", default-features = false, features = " + json.dumps(fs)))
    elif [f for f in manifest.get("features", {}) if f != "default"]:
        specs.append(("all features", ", features = " +
                      json.dumps(sorted(f for f in manifest["features"] if f != "default"))))
    return specs


def consumer_build(body: bytes, name: str, version: str, target_dir: Path | None,
                   feature_sets: list[list[str]] | None = None) -> list[str]:
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
        has_lib = "lib" in manifest or (pkg / "src" / "lib.rs").is_file()
        cache = (target_dir or root) / "consumer-cache"
        env = consumer_env(root / "home", cache / "registry")
        target = ["--target-dir", str(cache / "target")]
        problems, built = [], 0
        if has_lib:
            consumer = root / "consumer"
            (consumer / "src").mkdir(parents=True)
            (consumer / "src" / "lib.rs").write_text("", encoding="utf-8")

            def build(spec: str):
                (consumer / "Cargo.toml").write_text(
                    CONSUMER_MANIFEST.format(name=name, version=version, spec=spec), encoding="utf-8")
                return subprocess.run(["cargo", "build", "--message-format=json-render-diagnostics", *target],
                                      cwd=consumer, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", env=env)

            # every feature at once fails when some features cannot be on together: that is
            # a refusal (a consumer enabling them breaks) unless the roster names the feature
            # sets to build instead (`consumer-features`, with the reason)
            runs = [(label, build(spec)) for label, spec in feature_specs(manifest, feature_sets)]
        else:
            # from a directory outside the package: Cargo reads configuration from the working
            # directory up, and `cargo install` of a published crate does not start inside it
            outside = root / "bin"
            outside.mkdir()
            runs = [("its binary", subprocess.run(
                ["cargo", "build", "--message-format=json-render-diagnostics", "--manifest-path",
                 str(pkg / "Cargo.toml"), *target], cwd=outside,
                capture_output=True, text=True, encoding="utf-8", errors="replace", env=env))]
        for label, r in runs:
            if r.returncode != 0:
                tail = "; ".join(l.strip() for l in r.stderr.strip().splitlines()[-3:])
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
         docs=docs_rs_status, tags=remote_tag_commit, actions=github_actions_jobs, consumer=None) -> int:
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
    g.add_argument("--target-dir", type=Path, help="where cargo package builds (default: a temporary directory)")
    release_args(sub.add_parser("publish-verify"), True)
    m = sub.add_parser("roster-mark")
    m.add_argument("state", choices=["published"])
    release_args(m, True)
    f = sub.add_parser("followups")
    f.add_argument("crate")
    f.add_argument("--repos-root", type=Path, required=True, help="the directory holding a checkout of every roster repository")
    f.add_argument("--at", default="origin/main", help="the commit read in each checkout (fetch first)")
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
            if roster[a.crate].get("required-ci"):
                github = tomllib.loads(roster_text).get("train", {}).get("github")
                if not isinstance(github, str) or not github or "repo" not in roster[a.crate]:
                    print(f"error: {a.roster}: required-ci needs [train] github (the owner) and the crate's repo",
                          file=sys.stderr)
                    return 2
                ci, n = ci_problems(a.crate, roster[a.crate], f"{github}/{roster[a.crate]['repo']}", commit, actions)
                problems += ci
                compared += n
            problems += package_problems(a.crate, lister(a.repo, a.manifest_path))
            compared += 1
            for p in problems:
                print(f"refused: {p}")
            if problems:
                return 1
            version = roster[a.crate]["version"]
            with tempfile.TemporaryDirectory() as tmp:
                crate_path = packager(a.repo, a.manifest_path, a.crate, version, a.target_dir or Path(tmp))
                body = crate_path.read_bytes()
            consumer_problems = (consumer or consumer_build)(body, a.crate, version, a.target_dir,
                                                             roster[a.crate].get("consumer-features"))
            for p in consumer_problems:
                print(f"refused: {p}")
            if consumer_problems:
                return 1
            compared += 1
            digest = hashlib.sha256(body).hexdigest()
            print(f"publish-gate: go for {a.crate} {version} ({compared} checks)")
            print(f"  commit:        {commit}")
            print(f"  .crate sha256: {digest} ({len(crate_files(body))} files, built by cargo package)")
            print("  consumer:      the unpacked .crate built as a crates.io consumer builds it (empty CARGO_HOME, "
                  "minimal environment, no Cargo.lock or configuration above it, each feature set)")
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
    except (OSError, ValueError, RegistryError, urllib.error.URLError, TimeoutError, json.JSONDecodeError,
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
