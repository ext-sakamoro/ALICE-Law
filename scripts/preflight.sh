#!/usr/bin/env bash
# Local run of the checks CI makes on every push, before pushing:
#   scripts/preflight.sh [--quick]
# (--quick is accepted and runs the same checks: all of them take seconds; the conformance
# roster reads the release-train repositories through `gh api`, the others need no network.)
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
python3 scripts/test_readme_roster_check.py
python3 scripts/test_conformance_docs_schema_check.py
python3 scripts/test_conformance_roster_check.py
python3 scripts/test_main_mode.py
python3 scripts/test_gen_law_status.py
python3 scripts/test_readme_parity_check.py
python3 scripts/test_gen_law_readme_block.py
python3 scripts/test_workflow_concurrency.py
python3 scripts/workflow_concurrency.py
python3 scripts/test_measure_conformance.py
python3 scripts/test_shared_files_check.py
python3 scripts/test_gen_shared_files.py

echo "== the shared-file table reads, and this repository's copies equal it (the comparison with
# every listed repository's main runs on every push to main and daily, conformance-live.yml)"
python3 scripts/shared_files_check.py --table conformance/shared-files.toml
python3 scripts/shared_files_check.py --table conformance/shared-files.toml --repo . --name ALICE-Law

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
