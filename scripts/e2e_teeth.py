#!/usr/bin/env python3
"""Check that the end to end test fails on each mistake it is meant to catch

Runs in main mode (scripts/main_mode.py has patched e2e/ with the checkouts in
<checkouts>). For each mutation: change one line in a checkout (or in e2e/), run the
test, require it to fail with the expected message, and put the line back. The
unmutated test must pass before and after, and every checkout must be back to its
original contents at the end.

    python3 scripts/e2e_teeth.py <checkouts>

Exit 0 when every mutation was caught, 1 when one was not (or the baseline did not
pass), 2 when a mutation could not be applied (its line is not in the source any
more: update the mutation, do not skip it).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
E2E = ROOT / "e2e"

GOLDEN = "computes something else than when the golden was recorded"

# (name, file relative to <checkouts> or "e2e/...", original text, mutated text, expected message)
MUTATIONS = [
    (
        "db: a law coefficient decoded one ulp off",
        "ALICE-DB/src/law_store.rs",
        "        coefficients.push(r.f64()?);",
        "        coefficients.push(f64::from_bits(r.f64()?.to_bits() + 1));",
        "law coefficients[",
    ),
    (
        "db: an f64 series value stored through f32 (a lossy save)",
        "ALICE-DB/src/series.rs",
        "            .put_blob(&self.key(key), &value.to_bits().to_le_bytes())\n    }\n\n    /// The value stored at `key`",
        "            .put_blob(&self.key(key), &f64::from(value as f32).to_bits().to_le_bytes())\n    }\n\n    /// The value stored at `key`",
        "cooling[",
    ),
    (
        "db: restoring an f64 series returns nothing",
        "ALICE-DB/src/series.rs",
        "        self.scan(start, end)?\n            .into_iter()\n            .map(|(k, v)| want_f64(v).map(|x| (k, x)))\n            .collect()",
        "        let _ = self.scan(start, end)?;\n        Ok(Vec::new())",
        "read 0 of",
    ),
    (
        "crypto: one byte of the digest flipped",
        "ALICE-Crypto/src/hash.rs",
        "    Hash(*blake3::hash(data).as_bytes())",
        "    let mut h = *blake3::hash(data).as_bytes();\n    h[7] ^= 1;\n    Hash(h)",
        GOLDEN,
    ),
    (
        "lol: the research law evaluates exp with the platform libm",
        "ALICE-LOL/alice-lol/src/research_law.rs",
        "                Func::Exp => alice_det_math::exp64(x),",
        "                Func::Exp => x.exp(),",
        GOLDEN,
    ),
    (
        "physics: the 1D thermal step takes a slightly longer step",
        "ALICE-Physics/src/transient_thermal.rs",
        "    let inv_dx_squared = 1.0 / (dx * dx);\n    let k: Vec<f32> = temperatures",
        "    let inv_dx_squared = 1.0 / (dx * dx) * (1.0 + f32::EPSILON);\n    let k: Vec<f32> = temperatures",
        GOLDEN,
    ),
    (
        "zip: built against a different arithmetic identifier",
        "ALICE-Zip/src/law.rs",
        "pub use alice_det_math::SEMANTICS_ID;",
        "pub const SEMANTICS_ID: [u8; 32] = [0x5a; 32];",
        "alice-zip was built with a different alice-det-math",
    ),
    (
        "analytics: the largest residual reported one ulp off",
        "ALICE-Analytics/src/law.rs",
        "                max_abs: min.abs().max(max.abs()),",
        "                max_abs: f64::from_bits(min.abs().max(max.abs()).to_bits() + 1),",
        GOLDEN,
    ),
    (
        "dependencies: a second version of alice-zip in the build",
        "e2e/Cargo.toml",
        'alice-zip = "0.10"\n',
        'alice-zip = "0.10"\nalice-zip-old = { package = "alice-zip", version = "0.9" }\n',
        "linked at two or more versions",
    ),
]


def run_test() -> tuple[bool, str]:
    env = dict(os.environ)
    env.pop("ALICE_LAW_E2E_BLESS", None)
    p = subprocess.run(
        ["cargo", "test", "--release", "--test", "law_to_storage_roundtrip"],
        cwd=E2E,
        env=env,
        capture_output=True,
        text=True,
    )
    return p.returncode == 0, p.stdout + p.stderr


def resolve(checkouts: Path, rel: str) -> Path:
    return ROOT / rel if rel.startswith("e2e/") else checkouts / rel


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    checkouts = Path(argv[1])
    originals = {}
    for _, rel, old, _, _ in MUTATIONS:
        path = resolve(checkouts, rel)
        text = originals.setdefault(path, path.read_text(encoding="utf-8"))
        if text.count(old) != 1:
            print(f"teeth: {rel}: the line to mutate is there {text.count(old)} times, not once", file=sys.stderr)
            return 2

    ok, out = run_test()
    if not ok:
        print(out[-3000:])
        print("teeth: the unmutated test does not pass")
        return 1

    caught = 0
    rows = []
    try:
        for name, rel, old, new, expect in MUTATIONS:
            path = resolve(checkouts, rel)
            path.write_text(originals[path].replace(old, new, 1), encoding="utf-8")
            try:
                ok, out = run_test()
            finally:
                path.write_text(originals[path], encoding="utf-8")
            if ok:
                verdict = "not caught (the test passed)"
            elif expect not in out:
                tail = [l for l in out.splitlines() if "panicked" in l or "error" in l][:3]
                verdict = f"failed for another reason: {tail}"
            else:
                verdict = "caught"
                caught += 1
            rows.append((name, verdict))
            print(f"| {name} | {verdict} |", flush=True)
    finally:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")

    ok, out = run_test()
    if not ok:
        print(out[-3000:])
        print("teeth: the test does not pass after the mutations were put back")
        return 1
    print(f"teeth: {caught} of {len(MUTATIONS)} mutations caught")
    return 0 if caught == len(MUTATIONS) and caught > 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
