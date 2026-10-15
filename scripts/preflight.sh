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
python3 scripts/test_expected_failure.py
python3 scripts/expected_failure.py --check
python3 scripts/test_public_text_check.py
python3 scripts/test_readme_roster_check.py
python3 scripts/test_conformance_docs_schema_check.py
python3 scripts/test_conformance_roster_check.py
python3 scripts/test_gen_law_readme_block.py
python3 scripts/test_workflow_concurrency.py
python3 scripts/workflow_concurrency.py
python3 scripts/test_measure_conformance.py

echo "== README names every release-train repository"
python3 scripts/readme_roster_check.py

echo "== conformance docs and schema agree"
python3 scripts/conformance_docs_schema_check.py

echo "== every release-train crate declares its conformance at main"
python3 scripts/conformance_roster_check.py

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
