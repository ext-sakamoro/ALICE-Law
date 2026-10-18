#!/usr/bin/env bash
# The strict shared-file check, on its own (ALICE-Law G11 (c)):
#   scripts/shared_files_strict.sh
# Every listed repository's main against the table, with an unlisted holder of the same bytes a finding, and the
# table compared with the one regenerated in memory, also strict. It reads the mains from GitHub (blob-less, no
# build) and takes about two minutes, so it runs right before a push to main (on the tree to be pushed) and on
# every push to main in CI (conformance-live.yml). The preflight runs the same checks without --strict: there an
# unlisted same-bytes holder (a rollout that landed in another repository during the run) is a counted note, and
# everything else (other bytes, a missing copy, a pending row past its time, nothing compared) stays a finding.
#
# exit 0: everything compared is equal; 1: a finding; 2: not checked (the mains could not be read, the input could
# not be read, or nothing was compared): never a pass and never a finding
set -uo pipefail
cd "$(dirname "$0")/.."

worst=0
for check in "shared_files_check.py --table conformance/shared-files.toml --live --strict" \
             "gen_shared_files.py --table conformance/shared-files.toml --check --strict"; do
  err=$(mktemp)
  # shellcheck disable=SC2086
  python3 scripts/$check 2> >(tee "$err" >&2)
  rc=$?
  wait
  case $rc in
    0) ;;
    1) echo "shared-files strict: finding ($check)" >&2; [ $worst -eq 0 ] && worst=1 ;;
    2)
      if grep -q "compared nothing" "$err"; then
        echo "shared-files strict: nothing compared ($check): not a pass" >&2
      else
        echo "shared-files strict: infra: the check could not read its input ($check): not a pass, not a finding" >&2
      fi
      worst=2 ;;
    *) echo "shared-files strict: infra: $check exited $rc: not a pass" >&2; worst=2 ;;
  esac
  rm -f "$err"
done
[ $worst -eq 0 ] && echo "shared-files strict: ok"
exit $worst
