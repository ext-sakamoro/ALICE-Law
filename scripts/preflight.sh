#!/usr/bin/env bash
# Local run of the checks CI makes on every push, before pushing:
#   scripts/preflight.sh [--quick]
# (--quick is accepted and runs the same checks; the conformance roster reads the release-train
# repositories through `gh api`, and the strict shared-file check reads every listed main from GitHub,
# about two minutes; the others need no network.)
# The end to end crate and the resolution against crates.io are not run here.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== tests of the scripts"
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 'python 3.11 or later is needed (tomllib)')"
python3 scripts/test_dep_consistency.py
python3 scripts/test_release_train.py
python3 scripts/test_semver_gate.py
python3 scripts/test_expected_failure.py
python3 scripts/expected_failure.py --check
python3 scripts/test_public_text_check.py
python3 scripts/test_licence_files_check.py
python3 scripts/test_holder_live.py
python3 scripts/licence_files_check.py --repo . --holder-base-ref "$(git merge-base origin/main HEAD)"
python3 scripts/test_readme_roster_check.py
python3 scripts/test_conformance_docs_schema_check.py
python3 scripts/test_conformance_roster_check.py
python3 scripts/test_land_law_state.py
python3 scripts/test_run_tests_shifted_clock.py
python3 scripts/run_tests_shifted_clock.py
python3 scripts/test_workflow_gh_token_check.py
python3 scripts/workflow_gh_token_check.py
python3 scripts/test_g11_table_check.py
python3 scripts/test_g11_table_gen.py
python3 scripts/test_g11_self_check.py
python3 scripts/test_subprocess_encoding_check.py
python3 scripts/subprocess_encoding_check.py
python3 scripts/test_main_mode.py
python3 scripts/test_gen_law_status.py
python3 scripts/test_readme_parity_check.py
python3 scripts/test_gen_law_readme_block.py
python3 scripts/test_workflow_concurrency.py
python3 scripts/workflow_concurrency.py
python3 scripts/test_locked_lockfile_check.py
python3 scripts/locked_lockfile_check.py
python3 scripts/test_measure_conformance.py
python3 scripts/test_shared_files_check.py
python3 scripts/test_gen_shared_files.py
python3 scripts/test_shared_files_strict.py
python3 scripts/test_conformance_live_workflow.py

echo "== the line-number reference baseline does not rise against origin/main"
python3 scripts/test_line_refs_ratchet.py
python3 scripts/test_line_refs_propose.py
line_base=$(mktemp)
if git cat-file -e origin/main:conformance/line-refs-baseline.toml 2>/dev/null; then
  git show origin/main:conformance/line-refs-baseline.toml > "$line_base"
else
  rm -f "$line_base"
fi
python3 scripts/line_refs_ratchet.py --baseline conformance/line-refs-baseline.toml --base "$line_base"
rm -f "$line_base"

echo "== the shared-file table reads, and this repository's copies equal it (the comparison with
# every listed repository's main runs on every push to main and daily, conformance-live.yml)"
if git cat-file -e origin/main:conformance/shared-files.toml 2>/dev/null; then
  base_table=$(mktemp)
  git show origin/main:conformance/shared-files.toml > "$base_table"
  python3 scripts/shared_files_check.py --table conformance/shared-files.toml --base-table "$base_table" --lock-file .github/workflows/conformance-live.yml
  rm -f "$base_table"
else
  python3 scripts/shared_files_check.py --table conformance/shared-files.toml --lock-file .github/workflows/conformance-live.yml
fi
python3 scripts/shared_files_check.py --table conformance/shared-files.toml --repo . --name ALICE-Law

echo "== the shared-file table against every listed main (other bytes, a missing copy, a pending row past its time and
# nothing compared are findings; an unlisted holder of the same bytes is a counted note here: a rollout that lands in
# another repository during this run must not make the run fail. The strict pair, where that holder is a finding
# too, is scripts/shared_files_strict.sh: it runs on the tree to be pushed right before a push to main, and
# conformance-live.yml on every push to main)"
python3 scripts/shared_files_check.py --table conformance/shared-files.toml --live
python3 scripts/gen_shared_files.py --table conformance/shared-files.toml --check --lock-file .github/workflows/conformance-live.yml

echo "== README names every release-train repository"
python3 scripts/readme_roster_check.py

echo "== conformance docs and schema agree"
python3 scripts/conformance_docs_schema_check.py

echo "== the committed conformance state is consistent (no other repository read; the comparison with
# the members' mains runs on every push to main and daily, .github/workflows/conformance-live.yml)"
python3 scripts/conformance_roster_check.py --internal

echo "== the README Status section is the generated one"
python3 scripts/gen_law_status.py --check

echo "== README.md and README_JP.md agree"
python3 scripts/readme_parity_check.py

echo "== public text: the tree"
python3 scripts/public_text_check.py --tree HEAD

echo "== public text: the commits not on origin/main"
base=$(git rev-parse --verify -q origin/main || true)
if [ -n "$base" ] && [ "$(git rev-list --count "$base..HEAD")" -gt 0 ]; then
  python3 scripts/public_text_check.py --range "$base..HEAD"
else
  echo "no commit outside origin/main"
fi
echo "preflight: ok"
