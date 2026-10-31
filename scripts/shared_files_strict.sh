#!/usr/bin/env bash
# The strict shared-file check, on its own (ALICE-Law G11 (c)):
#   scripts/shared_files_strict.sh [--daily]
#
# Two modes. The default is the landing gate of a change in this repository (right before a push to main, and the
# receipt the push hook asks for), compared with the base (origin/main, or LAW_BASE_REF): an unlisted holder of the
# same bytes that the base does not list either (a rollout that landed elsewhere) is a note there, so a landing in
# another repository while this change is checked does not fail it; a holder the change dropped, other bytes, a
# missing copy, a table that differs in anything else, and nothing compared stay findings. A pending row whose
# copies moved is reported in both modes (its copies move by definition). --daily (the scheduled run) is strict:
# the unlisted holder is a finding too, and the owner regenerates the table. The base must not be the tree under check (with
# the committed files as their own base nothing reads as changed). What follows describes the --daily mode.
# Every listed repository's main against the table, with an unlisted holder of the same bytes a finding, and the
# table compared with the one regenerated in memory, also strict. It reads the mains from GitHub (blob-less, no
# build) and takes about two minutes, so it runs right before a push to main (on the tree to be pushed) and on
# every push to main in CI (conformance-live.yml). The preflight runs the same checks without --strict: there an
# unlisted same-bytes holder (a rollout that landed in another repository during the run) is a counted note, and
# everything else (other bytes, a missing copy, a pending row past its time, nothing compared) stays a finding.
# It also checks this repository's own conformance inputs (scripts/conformance_roster_check.py --internal) and the
# README Status blocks (scripts/gen_law_status.py --check). The crates' manifests at their mains are not part of a
# landing here: Law records none of their counts, and another repository's manifest never refuses this push (the
# Conformance (live) workflow reports them).
#
# exit 0: everything compared is equal; 1: a finding in any check (also when another one could not run);
# 2: not checked (the mains could not be read, the input could not be read, or nothing was compared): never a pass
set -uo pipefail
cd "$(dirname "$0")/.."

finding=0
unchecked=0
case "${1:-}" in
  "") mode=landing ;;
  --daily) mode=daily ;;
  *) echo "shared-files strict: unknown argument $1 (scripts/shared_files_strict.sh [--daily]): not a pass" >&2; exit 2 ;;
esac
if [ "$mode" = daily ]; then
  table_args="--strict"; gen_args="--strict"
else
  # the landing mode compares with the base (LAW_BASE_REF, default origin/main; LAW_BASE_TABLE names the file
  # directly): what this change itself edited must equal what the mains give, and only what it left as the base
  # has it may lag behind a landing elsewhere
  base_dir=$(mktemp -d)
  trap 'rm -rf "$base_dir"' EXIT
  base_table=${LAW_BASE_TABLE:-$base_dir/shared-files.toml}
  for pair in "conformance/shared-files.toml:$base_table"; do
    rel=${pair%%:*}; out=${pair#*:}
    if [ ! -f "$out" ] && ! git show "${LAW_BASE_REF:-origin/main}:$rel" > "$out" 2>/dev/null; then
      echo "shared-files strict: infra: the base ${LAW_BASE_REF:-origin/main}:$rel could not be read: not a pass" >&2
      exit 2
    fi
  done
  table_args="--base-table $base_table"; gen_args="--base-table $base_table"
fi
for check in "shared_files_check.py --table conformance/shared-files.toml --live $table_args" \
             "gen_shared_files.py --table conformance/shared-files.toml --check $gen_args --lock-file .github/workflows/conformance-live.yml" \
             "conformance_roster_check.py --internal" \
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
