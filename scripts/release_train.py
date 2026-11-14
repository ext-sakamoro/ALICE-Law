#!/usr/bin/env python3
"""The core ALICE crates at commits of their main branches (release-train.toml), checked against those commits.

Nothing is published to crates.io: each row names a commit of its repository's main branch (`sha`) and
the version the crate's manifest has there. Until 2026-11-13 this script was the crates.io publish gate
(`next`, `publish-gate`, `publish-verify`, `roster-mark`, with the package / consumer build / semver /
secrets / owners checks they ran); restore them from the history of this file if a registry release is
ever wanted again.

    check --repos-root DIR [--provenance P] [--no-ci]
                                         every row that is not exempt: its commit is on main (an
                                         ancestor of origin/main in DIR/<repo>), the manifest there
                                         (`manifest-path`, default Cargo.toml) is the crate at the
                                         row's version, a repository the e2e records (P, default
                                         e2e/golden/values.provenance) is at that commit, the crate
                                         comes after the roster crates its manifest depends on, no
                                         workflow at the commit can publish to a registry, and every
                                         push run of the commit on main is green (a red scheduled or
                                         dispatched run is a note; no push run is a problem unless every
                                         workflow is filtered out for the commit's change); no crate
                                         compared exits 2
    record --repos-root DIR [--provenance P] [--no-ci]
                                         writes each row's `sha` (the provenance commit for the
                                         repositories the e2e records, else origin/main) and `version`
                                         (the manifest's there), keeping the rest of the file; refuses,
                                         writing nothing, when a commit's push CI is not green
    followups <crate> --repos-root DIR [--at REF]
                                         the consumers whose requirement does not accept the crate's
                                         roster version
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
Exit 0 when the answer is "go", 1 when it is "no", 2 when the roster, a manifest or the provenance is
unreadable or nothing was compared.
"""
from __future__ import annotations

import argparse
import os
import importlib.util
import json
import re
import shlex
import subprocess
import sys
import tomllib
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

STATES = ("main", "exempt")
DEFAULT_ROSTER = HERE.parent / "release-train.toml"
DEFAULT_PROVENANCE = HERE.parent / "e2e" / "golden" / "values.provenance"


class RosterError(ValueError):
    pass


def load_roster(text: str, need_sha: bool = True) -> dict[str, dict]:
    """the crates of the roster, validated; `need_sha=False` lets `record` read a row it has not written yet"""
    data = tomllib.loads(text)
    for gone in ("variables", "self-hosted", "secrets"):
        if gone in data:
            raise RosterError(f"[{gone}.*] belonged to the publish gate (the repositories' Actions secrets); nothing "
                              f"is published, and check refuses a workflow that could publish")
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
        for key in REMOVED_KEYS:
            if key in c:
                raise RosterError(f"{name}: `{key}` belonged to the crates.io release train; the roster names a "
                                  f"main commit (`sha`) instead")
        if "required-ci" in c:
            raise RosterError(f"{name}: `required-ci` is gone: check reads every workflow run of the commit on main")
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
            raise RosterError(f"{name}: state {c['state']!r} is not one of {STATES} (the roster names main "
                              f"commits; nothing is published)")
        if c["state"] == "main":
            if (need_sha or "sha" in c) and (not isinstance(c.get("sha"), str) or not SHA_RE.fullmatch(c["sha"])):
                raise RosterError(f"{name}: a `main` row needs `sha`, the full commit of its repository's main "
                                  f"(`release_train.py record` writes it)")
        dc.Version(c["version"])  # raises on a malformed version
        if c["order"] in orders:
            raise RosterError(f"{name}: order {c['order']} is used twice")
        orders.add(c["order"])
    return crates


def alice_name(name: str) -> str | None:
    """`name` as a crates.io crate name if it is an ALICE crate (crates.io treats `-` and
    `_` as the same name), else None"""
    n = name.replace("_", "-")
    return n if n.startswith("alice-") else None


# the gate only reads the repository: no optional locks (git status would otherwise
# refresh the index), so a check leaves the checkout as it found it
GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV, encoding="utf-8").stdout


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
    r = subprocess.run(["gh", "api", f"repos/{owner_repo}/environments/{name}"], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        return 0
    rules = json.loads(r.stdout).get("protection_rules", [])
    return sum(len(rule.get("reviewers", [])) for rule in rules if rule.get("type") == "required_reviewers")


def workflow_publish_problems(repo: Path, ref: str, crate: str, owner_repo: str | None = None,
                              reviewers=environment_reviewers) -> tuple[list[str], int]:
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
    for n in names:
        lines = texts[n].splitlines()
        jobs_at = next((k for k, l in enumerate(lines) if l.rstrip() == "jobs:"), len(lines))
        starts = [k for k in range(jobs_at + 1, len(lines)) if JOB_RE.match(lines[k])]
        blocks = [("(workflow)", lines[:jobs_at + 1])] + [
            (JOB_RE.match(lines[a]).group(1), lines[a:b]) for a, b in zip(starts, starts[1:] + [len(lines)])]
        preamble = workflow_text(lines[:jobs_at + 1])
        workflow_id_token = bool(ID_TOKEN_RE.search(preamble))
        for job, block in blocks:
            text = workflow_text(block)
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


# ---------------------------------------------------------------- after the gate: package, verify, mark


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

# ---------------------------------------------------------------- the main-commit roster (check / record)

SHA_RE = re.compile(r"[0-9a-f]{40}")
# keys of the crates.io release train the main-commit roster no longer has (scripts/release_train.py before
# 2026-11-13 held publish-gate / publish-verify / roster-mark / next on them; restore from that history if needed)
REMOVED_KEYS = ("must-contain", "must-contain-patch", "changelog-has", "tag")


def read_provenance(text: str) -> dict[str, tuple[str, dict[str, str]]]:
    """{repository: (commit, {crate: version})} of e2e/golden/values.provenance ("<repository> <commit>
    <crate> <version>[, <crate> <version>]..." per line, `#` comments)"""
    out: dict[str, tuple[str, dict[str, str]]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) != 3 or not SHA_RE.fullmatch(parts[1]):
            raise ValueError(f"values.provenance: {line!r} is not \"<repository> <commit> <crate> <version>, ...\"")
        crates = {}
        for item in parts[2].split(","):
            cv = item.split()
            if len(cv) != 2:
                raise ValueError(f"values.provenance: {item.strip()!r} is not \"<crate> <version>\"")
            crates[cv[0]] = cv[1]
        out[parts[0]] = (parts[1], crates)
    return out


def manifest_alice_names(text: str) -> set[str]:
    """the `alice-*` crates a Cargo.toml depends on (normal and build, any target, renamed ones under their
    crate name), whatever their source"""
    data = tomllib.loads(text)
    tables = [data.get("dependencies", {}), data.get("build-dependencies", {})]
    for target in data.get("target", {}).values():
        tables += [target.get("dependencies", {}), target.get("build-dependencies", {})]
    names = set()
    for deps in tables:
        for key, spec in deps.items():
            name = alice_name(spec.get("package", key) if isinstance(spec, dict) else key)
            if name:
                names.add(name)
    return names


def gh_glob_re(pattern: str) -> re.Pattern:
    """a GitHub Actions filter pattern (`**` any path, `*` within a segment, `?` one character) as a regex"""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def filter_matches(patterns: list[str], path: str) -> bool:
    """whether `path` is selected by a GitHub filter list (later patterns win; `!` negates)"""
    hit = False
    for p in patterns:
        neg = p.startswith("!")
        if gh_glob_re(p[1:] if neg else p).match(path):
            hit = not neg
    return hit


def push_runs_for(text: str, changed: list[str]) -> bool | None:
    """whether a workflow (its YAML text) runs on a push to main that changes `changed`: None when it has no
    push trigger for main, else True / False by its branches, paths and paths-ignore filters"""
    lines = text.splitlines()
    sec = on_section(lines)
    if sec is None:
        m = next((re.match(r"^(?:on|'on'|\"on\"|true):\s*(\S.*)$", l) for l in lines
                  if re.match(r"^(?:on|'on'|\"on\"|true):\s*\S", l)), None)
        if m and "push" in re.findall(r"[\w-]+", m.group(1)):
            return True
        return None
    a, b = sec
    keys = block_keys(lines, a + 1, b, 2)
    if "push" not in keys:
        return None
    value, at = keys["push"]
    end = next((j for j in range(at + 1, b) if lines[j].strip() and not lines[j].lstrip().startswith("#")
                and indent_of(lines[j]) <= 2), b)
    sub = block_keys(lines, at + 1, end, 4)
    if "branches" in sub and not filter_matches(list_value(lines, *sub["branches"]), "main"):
        return None
    if "branches-ignore" in sub and filter_matches(list_value(lines, *sub["branches-ignore"]), "main"):
        return None
    if "paths" in sub:
        pats = list_value(lines, *sub["paths"])
        return any(filter_matches(pats, f) for f in changed)
    if "paths-ignore" in sub:
        pats = list_value(lines, *sub["paths-ignore"])
        return any(not filter_matches(pats, f) for f in changed)
    return True


def gh_main_runs(owner_repo: str, sha: str) -> list[dict]:
    """the GitHub Actions runs of `sha` on branch main (workflowName, databaseId, event, status, conclusion);
    a private repository answers one entry {"private": True} instead"""
    private = subprocess.run(["gh", "api", f"repos/{owner_repo}", "--jq", ".private"], check=True,
                             capture_output=True, text=True, encoding="utf-8").stdout.strip()
    if private != "false":
        return [{"private": True}]
    out = subprocess.run(["gh", "run", "list", "--repo", owner_repo, "--commit", sha, "--branch", "main",
                          "--limit", "100", "--json",
                          "workflowName,databaseId,status,conclusion,headSha,headBranch,event"],
                         check=True, capture_output=True, text=True, encoding="utf-8").stdout
    return [r for r in json.loads(out) if r.get("headSha") == sha and r.get("headBranch") == "main"]


def main_ci_problems(name: str, repo: Path, owner_repo: str, sha: str, runs=gh_main_runs) -> list[str]:
    """the main CI of `sha`: every push run of it on main completed with success (the jobs a run skipped
    inside are its own decision); no push run at all is a problem unless every workflow of the commit is
    filtered out for its change, which is said; a red scheduled / dispatched run is a note, not a problem
    (it takes whatever head main has then, and its own gate tracks it)"""
    where = f"{name} ({owner_repo} {sha[:12]})"
    try:
        found = runs(owner_repo, sha)
    except subprocess.CalledProcessError as e:
        return [f"{where}: the runs of the commit could not be read: {(e.stderr or '').strip()[:200]}"]
    except (OSError, ValueError) as e:
        return [f"{where}: the runs of the commit could not be read: {e!r}"[:300]]
    if any(r.get("private") for r in found):
        return [f"{where}: the repository is private: the roster takes public repositories (their main CI is "
                f"read here)"]
    pushed = [r for r in found if r.get("event") == "push"]
    for r in found:
        if r.get("event") != "push" and (r.get("status") != "completed" or r.get("conclusion") != "success"):
            # a scheduled / dispatched run takes whatever head main has then; its own gate tracks it
            print(f"note: {where}: {r.get('event')} run {r.get('workflowName')} {r.get('databaseId', '')} is "
                  f"{r.get('status')}/{r.get('conclusion')} (not blocking)")
    if pushed:
        bad = sorted(f"{r.get('workflowName')}: {r.get('status')}/{r.get('conclusion')}" for r in pushed
                     if r.get("status") != "completed" or r.get("conclusion") != "success")
        return [f"{where}: the main CI of the commit (its push runs) is not green: {'; '.join(bad)}"] if bad else []
    parents = git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]
    changed = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", *(parents[:1] or []), sha
                  ).split() if parents else git(repo, "ls-tree", "-r", "--name-only", sha).split()
    try:
        names = [n for n in git(repo, "ls-tree", "--name-only", f"{sha}:.github/workflows").splitlines()
                 if n.endswith((".yml", ".yaml"))]
    except subprocess.CalledProcessError:
        names = []
    would = [n for n in names if push_runs_for(git(repo, "show", f"{sha}:.github/workflows/{n}"), changed)]
    if would:
        return [f"{where}: no workflow run on main for the commit, but {', '.join(would)} run on its change: "
                f"record another commit, or wait for its CI"]
    print(f"note: {where}: no workflow runs on main for the change of the commit ({len(changed)} file(s), "
          f"{len(names)} workflow(s) filtered out)")
    return []


def check_problems(roster: dict[str, dict], repos_root: Path, provenance_text: str, runs=gh_main_runs,
                   github: str = "ext-sakamoro") -> tuple[list[str], int]:
    """(problems, crates compared): every row that is not exempt names a commit of its repository's main
    branch (an ancestor of origin/main in repos_root/<repo>) whose manifest is the crate at the row's version;
    a repository the e2e records (values.provenance) is at that commit; a crate comes after the roster crates
    its manifest depends on; no workflow at the commit can publish to a registry; the main CI of the commit is
    green (main_ci_problems, when `runs` reads it)"""
    prov = read_provenance(provenance_text)
    problems: list[str] = []
    compared = 0
    for name, c in sorted(roster.items(), key=lambda kv: kv[1]["order"]):
        if c["state"] == "exempt":
            continue
        repo_name, sha = c.get("repo"), c["sha"]
        where = f"{name} {c['version']} ({repo_name} {sha[:12]})"
        repo = repos_root / repo_name if repo_name else None
        if repo is None:
            problems.append(f"{where}: the row names no `repo`")
            continue
        if not (repo / ".git").exists():
            problems.append(f"{where}: no checkout of {repo_name} under {repos_root}")
            continue
        try:
            git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        except subprocess.CalledProcessError:
            problems.append(f"{where}: the commit is not in the checkout (fetch the full history of main)")
            continue
        compared += 1
        if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, "origin/main"],
                          capture_output=True, env=GIT_ENV).returncode != 0:
            problems.append(f"{where}: the commit is not on main (not an ancestor of origin/main)")
        mpath = c.get("manifest-path", "Cargo.toml")
        try:
            text = git(repo, "show", f"{sha}:{mpath}")
            pkg = tomllib.loads(text).get("package", {})
        except (subprocess.CalledProcessError, tomllib.TOMLDecodeError) as e:
            problems.append(f"{where}: {mpath} cannot be read at the commit ({type(e).__name__})")
            continue
        if pkg.get("name") != name:
            problems.append(f"{where}: {mpath} at the commit is the package {pkg.get('name')!r}, not {name}")
        if pkg.get("version") != c["version"]:
            problems.append(f"{where}: {mpath} at the commit has version {pkg.get('version')!r}, the roster "
                            f"{c['version']}: run `release_train.py record`")
        if repo_name in prov:
            psha, pcrates = prov[repo_name]
            if psha != sha:
                problems.append(f"{where}: the e2e provenance records {repo_name} at {psha[:12]}, the roster at "
                                f"{sha[:12]} (re-record both: `release_train.py record` after the e2e bless)")
            if name in pcrates and pcrates[name] != c["version"]:
                problems.append(f"{where}: the e2e provenance records {name} {pcrates[name]}, the roster "
                                f"{c['version']}")
        for dep in sorted(manifest_alice_names(text)):
            if dep in roster and dep != name and roster[dep]["order"] >= c["order"]:
                problems.append(f"{where}: depends on {dep} (order {roster[dep]['order']}), which is not before it "
                                f"in order ({c['order']})")
        wf, _ = workflow_publish_problems(repo, sha, name)
        problems += [p.replace(" outside this gate", "").replace(" without it): remove it",
                                                                     "): nothing is published, remove it")
                     for p in wf]
        if runs is not None:
            problems += main_ci_problems(name, repo, f"{github}/{repo_name}", sha, runs)
    return problems, compared


def record_text(text: str, roster: dict[str, dict], repos_root: Path, provenance_text: str) -> tuple[str, list[str]]:
    """the roster text with each non-exempt row's `sha` set to its repository's commit (the e2e provenance
    for the repositories it records, else origin/main of repos_root/<repo>) and `version` to the version
    the manifest has there; the rest of the text (comments, other keys) is kept; (text, changes)"""
    prov = read_provenance(provenance_text)
    lines = text.splitlines(keepends=True)
    changes = []
    for name, c in roster.items():
        if c["state"] == "exempt":
            continue
        repo_name = c["repo"]
        repo = repos_root / repo_name
        sha = prov[repo_name][0] if repo_name in prov else git(repo, "rev-parse", "origin/main").strip()
        version = tomllib.loads(git(repo, "show", f"{sha}:{c.get('manifest-path', 'Cargo.toml')}"))["package"]["version"]
        head = next(i for i, line in enumerate(lines) if line.strip() == f"[crate.{name}]")
        end = next((i for i in range(head + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        seen = set()
        for i in range(head + 1, end):
            for key, value in (("version", version), ("sha", sha)):
                if re.match(rf"{key}\s*=", lines[i]):
                    lines[i] = f'{key} = "{value}"\n'
                    seen.add(key)
        if "sha" not in seen:
            at = next(i for i in range(head + 1, end) if re.match(r"version\s*=", lines[i]))
            lines.insert(at + 1, f'sha = "{sha}"\n')
        if (c["version"], c.get("sha")) != (version, sha):
            changes.append(f"{name}: {c['version']} {str(c.get('sha'))[:12]} -> {version} {sha[:12]}")
    return "".join(lines), changes


def main(argv: list[str] | None = None, runs=gh_main_runs, metadata=sg.cargo_metadata) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("check", "record"):
        p = sub.add_parser(cmd)
        p.add_argument("--repos-root", type=Path, required=True,
                       help="the directory holding a checkout of every roster repository (origin/main fetched, "
                            "with the history back to each row's commit)")
        p.add_argument("--provenance", type=Path, default=DEFAULT_PROVENANCE,
                       help="the main commits the e2e golden was recorded from")
        p.add_argument("--no-ci", action="store_true", help="do not read the main CI of each commit from GitHub")
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
        roster = load_roster(roster_text, need_sha=a.cmd != "record")
    except (OSError, ValueError) as e:
        print(f"error: {a.roster}: {e}", file=sys.stderr)
        return 2
    if a.cmd in ("check", "record"):
        try:
            provenance_text = a.provenance.read_text(encoding="utf-8")
            github = tomllib.loads(roster_text).get("train", {}).get("github", "ext-sakamoro")
            if a.cmd == "record":
                text, changes = record_text(roster_text, roster, a.repos_root, provenance_text)
                recorded = load_roster(text)
                refused = [] if a.no_ci else [
                    p for n, c in recorded.items() if c["state"] == "main"
                    for p in main_ci_problems(n, a.repos_root / c["repo"], f"{github}/{c['repo']}", c["sha"], runs)]
                if refused:
                    for p in refused:
                        print(f"refused: {p}")
                    print("record: nothing written (a commit to record has no green main CI)")
                    return 1
                a.roster.write_text(text, encoding="utf-8")
                for line in changes:
                    print(f"recorded: {line}")
                print(f"record: {len(changes)} row(s) changed; commit {a.roster}")
                return 0
            problems, compared = check_problems(roster, a.repos_root, provenance_text,
                                                None if a.no_ci else runs, github)
        except (OSError, ValueError, StopIteration, KeyError, subprocess.CalledProcessError) as e:
            print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
            return 2
        for p in problems:
            print(f"refused: {p}")
        print(f"check: {compared} crate(s) compared at their main commits, {len(problems)} problem(s)")
        if compared == 0:
            print("error: no crate was compared", file=sys.stderr)
            return 2
        return 1 if problems else 0
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
    # followups
    if a.crate not in roster:
        print(f"error: {a.crate} is not in the roster", file=sys.stderr)
        return 2
    try:
        bump, ok, read = followups(roster, a.crate, a.repos_root, a.at)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
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

if __name__ == "__main__":
    sys.exit(main())
