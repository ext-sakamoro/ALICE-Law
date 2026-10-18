#!/usr/bin/env bash
# The strict shared-file check, on its own (ALICE-Law G11 (c)):
#   scripts/shared_files_strict.sh
# Every listed repository's main against the table, with an unlisted holder of the same bytes a finding, and the
# table compared with the one regenerated in memory, also strict. It reads the mains from GitHub (blob-less, no
# build) and takes about two minutes, so it runs right before a push to main (on the tree to be pushed) and on
# every push to main in CI (conformance-live.yml). The preflight runs the same checks without --strict: there an
# unlisted same-bytes holder (a rollout that landed in another repository during the run) is a counted note, and
# everything else (other bytes, a missing copy, a pending row past its time, nothing compared) stays a finding.
# It also checks Law's conformance state against the crates' mains (conformance/manifest-state.toml with
# scripts/conformance_roster_check.py, and the README Status blocks with scripts/gen_law_status.py --check): a crate
# whose gate flipped since the last state commit makes the state stale, and a Law push must not carry a stale state
# (scripts/land_law_state.py tells a crate's lander before landing whether the state will move).
#
# exit 0: everything compared is equal; 1: a finding in any check (also when another one could not run);
# 2: not checked (the mains could not be read, the input could not be read, or nothing was compared): never a pass
set -uo pipefail
cd "$(dirname "$0")/.."

finding=0
unchecked=0
for check in "shared_files_check.py --table conformance/shared-files.toml --live --strict" \
             "gen_shared_files.py --table conformance/shared-files.toml --check --strict" \
             "conformance_roster_check.py --strict" \
             "gen_law_status.py --check"; do
  err=$(mktemp)
  # (stderr to a file, then shown: no process substitution, whose end bash before 4.4 cannot wait for)
  # shellcheck disable=SC2086
  python3 scripts/$check 2> "$err"
  rc=$?
  cat "$err" >&2
  case $rc in
    0) ;;
    1) echo "shared-files strict: finding ($check)" >&2; finding=1 ;;
    2)
      if grep -qE "compared nothing|nothing was compared|no README was compared" "$err"; then
        echo "shared-files strict: nothing compared ($check): not a pass" >&2
      else
        echo "shared-files strict: infra: the check could not read its input ($check): not a pass, not a finding" >&2
      fi
      unchecked=1 ;;
    *) echo "shared-files strict: infra: $check exited $rc: not a pass" >&2; unchecked=1 ;;
  esac
  rm -f "$err"
done
# (a finding is reported as one even when the other check could not run)
if [ $finding -eq 1 ]; then
  exit 1
fi
if [ $unchecked -eq 1 ]; then
  exit 2
fi
echo "shared-files strict: ok"
exit 0
