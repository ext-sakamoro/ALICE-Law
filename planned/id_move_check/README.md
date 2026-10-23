# id_move_check (planned bytes, not wired)

The next revision of the shared `scripts/id_move_check.py` and its tests, kept here until the
shared-files move lands in the repositories. Nothing in this repository runs these files.

- base: `scripts/id_move_check.py` of ALICE-Physics (git blob 03e6bfc1) and its tests (dc8ec15d)
- `--base-from bump` / `auto`: the base is the commit before the newest version bump on main
  (`release_baseline.py`, the same bytes as ALICE-DetMath's), judged together with the newest tag
  when the tag is an ancestor of that commit; each problem names the base it came from
- previous-tests run in a copy under a fresh parent that holds the copy under the repository's own
  name and a link to every sibling repository the manifests reach by path dependency (read from
  every `Cargo.toml`, also the siblings' own), so a sibling that points back at this repository
  builds against the copy; a named sibling that is not checked out fails
