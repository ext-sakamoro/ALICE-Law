//! Read a law, compute, save, restore, and compare every value bit for bit
//!
//! One pass through the core crates, using only their public APIs:
//!
//! 1. read: Newton's law of cooling as text, through the `alice-lol` research law
//!    reader, evaluated on a time grid (the arithmetic is `alice-det-math`)
//! 2. compute: a thermal field law through `alice-lol` (solved by
//!    `alice_physics::transient_thermal`), a rod cooling directly with
//!    `alice_physics::transient_thermal`, and two bodies falling in a `PhysicsWorld`
//! 3. fit: an `alice_zip::law::SignalLaw` to the cooling curve, summarised by
//!    `alice-analytics`
//! 4. save: every value through the exact series of `alice-db` (`AliceDB::series`,
//!    not the fitted model store) and the law through its law store, to a directory
//! 5. restore: reopen the directory and read everything back
//! 6. compare: every value by `to_bits`, the law record byte for byte, the
//!    `alice-crypto` digest of the whole run, the residual summary, and the arithmetic
//!    identifiers (`SEMANTICS_ID`) the crates were built with
//!
//! Each comparison counts what it compared and fails on zero. A golden digest of the
//! computed run (`golden/run.digest`) is a change detector, separate from the
//! round trip: the round trip says that saving and restoring loses nothing, the
//! golden says that the crates still compute what they computed when it was recorded.
//! `ALICE_LAW_E2E_BLESS=1` writes the current digest (for the linked alice-det-math release)
//! instead of comparing.

#![allow(clippy::float_cmp, clippy::unwrap_used, clippy::cast_precision_loss)]

use std::path::{Path, PathBuf};

use alice_db::law_store::encode_law_record;
use alice_db::AliceDB;
use alice_lol::law::{CheckConfig, LawSet};
use alice_lol::research_law::{Param, ResearchLaw, Var};
use alice_lol::{SdfNode, Vec3};
use alice_physics::math::{Fix128, Vec3Fix};
use alice_physics::solver::{PhysicsConfig, PhysicsWorld, RigidBody};
use alice_physics::transient_thermal::{stable_dt_1d, transient_step_1d, ThermalMaterial};
use alice_zip::law::{Provenance, SignalLaw, ValidRange};


const LAW_NAME: &str = "newton-cooling";
/// The law alice-edge fits on a sensor window, stored and restored next to the first
const EDGE_LAW_NAME: &str = "edge-window";
/// The identifier of the arithmetic every crate here is built with (alice-det-math 0.5):
/// alice-edge publishes it as its own, and it is recorded with every stored law
/// Each arithmetic identifier of alice-det-math, with the release it starts at, oldest first.
/// A release that does not change the identifier (its set of functions and their bits are the
/// same) has no row of its own: it is built with the row of the latest release listed at or
/// before it ([`semantics_entry`]), and so are its golden digests. The locked release's row must
/// be `alice_det_math::SEMANTICS_ID` itself, and each row before it an identifier
/// `alice_det_math::check_semantics` accepts as a verified earlier one. A release whose identifier
/// changes adds its row.
const KNOWN_SEMANTICS: &[(&str, &str)] = &[
    ("0.5.0", "135c407d4249c1c200e4d1be79a12e848ce5c355fc3a918da965849e697ef000"),
    ("0.5.1", "85e0884623b154a3b2b264374907b6cbac09307ca3f9a08339e48c567d3cb315"),
    // 0.6.0 names the identifier 0.5.2 carried at its last revision (0.5.2 was never published and had two)
    ("0.6.0", "a2a11b9b826227fa7a9ed8ab2c1ce63dbb07ad4c39aa2c46a363ddea6b47dbe9"),
    // 0.6.1 adds tanh64 / hypot64: the identifier covers the new pins, the bits of the others are unchanged
    ("0.6.1", "6fc214e924c53557f33775df51f1f8dfe2f246d67a9e493d6fb5837d999022f2"),
];
/// `major.minor.patch` of a release (a pre-release or a malformed version is refused).
fn release(version: &str) -> (u64, u64, u64) {
    let parts: Vec<u64> = version
        .split('.')
        .map(|p| p.parse().unwrap_or_else(|_| panic!("alice-det-math {version}: not major.minor.patch")))
        .collect();
    match parts[..] {
        [major, minor, patch] => (major, minor, patch),
        _ => panic!("alice-det-math {version}: not major.minor.patch"),
    }
}

/// The row of [`KNOWN_SEMANTICS`] a release of alice-det-math is built with: the latest one that
/// starts at or before it, or `None` for a release older than every row.
fn semantics_entry(version: &str) -> Option<usize> {
    let v = release(version);
    KNOWN_SEMANTICS.iter().rposition(|(start, _)| release(start) <= v)
}

/// 0 to 600 s every 0.25 s: on about one point in ten, `alice_det_math::exp64` and
/// a platform's `exp` round differently, so the grid shows which one was used
const COOLING_POINTS: i64 = 2401;
const ROD_CELLS: usize = 64;
const ROD_SNAPSHOTS: usize = 10;
const ROD_STEPS_PER_SNAPSHOT: usize = 20;
const WORLD_FRAMES: usize = 120;
const WORLD_BODIES: [usize; 2] = [1, 2];

/// Everything the run computes, in the order it is digested
#[derive(Clone)]
struct Run {
    cooling: Vec<f64>,
    thermal_residuals: Vec<f32>,
    rod: Vec<f32>,
    ties: Vec<f32>,
    edge_fit: Vec<i32>,
    edge_record: Vec<u8>,
    world: Vec<f64>,
    law_record: Vec<u8>,
    summary: Vec<u8>,
}

fn cooling_law() -> ResearchLaw {
    ResearchLaw::new(
        "Newton's law of cooling",
        "Ta + (T0 - Ta) * exp(-k * t)",
        Var::new("T", "K"),
        &[Var::new("t", "s")],
        &[
            Param::new("Ta", 293.15, 0.0, "K"),
            Param::new("T0", 373.15, 0.0, "K"),
            Param::new("k", 0.01, 0.0, "1/s"),
        ],
        &[("t", ValidRange { lo: 0.0, hi: 600.0 })],
        Provenance::new("closed form", "stated"),
    )
    .unwrap()
}

fn cooling_time(i: i64) -> f64 {
    0.25 * i as f64
}

fn thermal_residuals() -> Vec<f32> {
    // A PLA sphere of radius 5 mm (1 unit = 1 cm) with a 5 W source in the middle,
    // held to 25 °C: 150 J into about 0.6 J/K raises it far above that within 30 s,
    // so the law is violated and the violation carries the solved temperature
    let report = LawSet::new()
        .thermal_field(
            "hot spot",
            1.0,
            SdfNode::sphere(0.5),
            vec![(Vec3::ZERO, 5.0)],
            ThermalMaterial::pla_polymer(),
            0.01,
            20.0,
            25.0,
            30.0,
        )
        .check(&CheckConfig {
            aabb_min: Vec3::splat(-0.6),
            aabb_max: Vec3::splat(0.6),
            resolution: 12,
        });
    assert_eq!(report.total_laws, 1);
    assert!(
        report.unresolved.is_empty(),
        "the thermal field law was not decided: {:?}",
        report.unresolved
    );
    assert_eq!(
        report.violations.len(),
        1,
        "the thermal field law should be violated (passed {})",
        report.passed
    );
    report
        .violations
        .iter()
        .flat_map(|v| [v.residual, v.point.x, v.point.y, v.point.z])
        .collect()
}

/// A distance field at a fixed distance (a signed zero here), so two of them meet
/// with equal zeros in a union
struct ConstantField(f32);

impl alice_physics::sdf_collider::SdfField for ConstantField {
    fn distance(&self, _x: f32, _y: f32, _z: f32) -> f32 {
        std::hint::black_box(self.0)
    }
    fn normal(&self, _x: f32, _y: f32, _z: f32) -> (f32, f32, f32) {
        (0.0, 1.0, 0.0)
    }
}

/// Equal zeros (+0 and -0) meeting a max or a min on public paths: `f32::max` /
/// `f32::min` leave the sign of the result unspecified and x86_64 and aarch64 differ,
/// so these values are the same bits on both only when the crates resolve the tie
/// themselves. Each value is kept (stored, restored, compared, and in the digest).
fn ties() -> Vec<f32> {
    use alice_physics::sdf_collider::{SdfField, SdfUnion};
    use alice_physics::sim_field::ScalarField3D;
    let mut out = Vec::new();
    // alice-physics: the union of two fields is the min of their distances
    for (a, b) in [(0.0, -0.0), (-0.0, 0.0), (0.0, 0.0), (-0.0, -0.0)] {
        out.push(SdfUnion::new(ConstantField(a), ConstantField(b)).distance(1.0, 2.0, 3.0));
    }
    // alice-physics: the maximum of a field holding +0 and -0, in both orders
    let mut field = ScalarField3D::new(2, 2, 2, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0));
    for order in [[-0.0_f32, 0.0], [0.0, -0.0]] {
        for (i, v) in field.data.iter_mut().enumerate() {
            *v = order[i % 2];
        }
        out.push(field.max_value());
    }
    field.data.fill(-0.0);
    out.push(field.max_value());
    // alice-sdf: union / intersection / subtraction of coincident boxes, on and inside
    // their faces (the tree evaluator)
    let cube = || SdfNode::box3d(1.0, 1.0, 1.0);
    let scenes = [
        cube().union(cube()),
        cube().intersection(cube()),
        cube().subtract(cube()),
        cube().subtract(SdfNode::box3d(1.0, 2.0, 2.0)),
    ];
    let c = [-0.5_f32, 0.0, 0.5];
    for scene in &scenes {
        for &x in &c {
            for &y in &c {
                for &z in &c {
                    out.push(alice_sdf::eval(scene, Vec3::new(x, y, z)));
                }
            }
        }
    }
    out
}

/// A sensor window in integer counts (a ramp with a bounded wobble), as alice-edge reads it
fn edge_samples() -> Vec<i32> {
    (0..64_i32).map(|i| 1000 + 37 * i + (i * i * 7) % 23 - 11).collect()
}

/// alice-edge's fixed-point fit of the window (slope and intercept in Q16.16) and the law it
/// makes of it, through its public API
fn edge_law(samples: &[i32]) -> (Vec<i32>, SignalLaw) {
    let (slope_q, intercept_q) = alice_edge::q16_linear::fit_linear_fixed(samples);
    let law = alice_edge::law::linear_law(samples, Provenance::new("alice-edge", "Q16.16 linear fit")).unwrap();
    (vec![slope_q, intercept_q], law)
}

fn rod() -> Vec<f32> {
    let material = ThermalMaterial::pla_polymer();
    let dx = 0.001_f32;
    let mut t: Vec<f32> = (0..ROD_CELLS)
        .map(|i| if i < ROD_CELLS / 2 { 100.0 } else { 20.0 })
        .collect();
    let dt = 0.5 * stable_dt_1d(&t, &material, dx);
    let mut out = Vec::with_capacity(ROD_CELLS * ROD_SNAPSHOTS);
    for _ in 0..ROD_SNAPSHOTS {
        for _ in 0..ROD_STEPS_PER_SNAPSHOT {
            transient_step_1d(&mut t, &material, dx, dt);
        }
        out.extend_from_slice(&t);
    }
    out
}

/// Positions as `f64` for the series, and the raw fixed-point words for the digest
fn world() -> (Vec<f64>, Vec<u8>) {
    let config = PhysicsConfig {
        substeps: 4,
        iterations: 8,
        gravity: Vec3Fix::new(Fix128::ZERO, Fix128::from_int(-10), Fix128::ZERO),
        damping: Fix128::from_ratio(99, 100),
        ..Default::default()
    };
    let mut world = PhysicsWorld::new(config);
    world.add_body(RigidBody::new_static(Vec3Fix::new(
        Fix128::ZERO,
        Fix128::from_int(-1),
        Fix128::ZERO,
    )));
    world.add_body(RigidBody::new_dynamic(
        Vec3Fix::new(Fix128::ZERO, Fix128::from_int(10), Fix128::ZERO),
        Fix128::ONE,
    ));
    world.add_body(
        RigidBody::new_dynamic(
            Vec3Fix::new(Fix128::from_int(2), Fix128::from_int(15), Fix128::ZERO),
            Fix128::from_int(2),
        )
        .with_restitution(Fix128::from_ratio(8, 10)),
    );
    let dt = Fix128::from_ratio(1, 60);
    let mut values = Vec::new();
    let mut raw = Vec::new();
    for _ in 0..WORLD_FRAMES {
        world.step(dt);
        for b in WORLD_BODIES {
            let p = world.bodies[b].position;
            for c in [p.x, p.y, p.z] {
                values.push(c.to_f64());
                raw.extend_from_slice(&c.hi.to_le_bytes());
                raw.extend_from_slice(&c.lo.to_le_bytes());
            }
        }
    }
    (values, raw)
}

fn digest(run: &Run, world_raw: &[u8]) -> [u8; 32] {
    digest_of(run, world_raw, true)
}

/// The digest of the run; without the alice-edge records (`q`, `e`) it is the digest the run
/// had before they were added, printed so a re-recording can be split into its parts
fn digest_of(run: &Run, world_raw: &[u8], with_edge: bool) -> [u8; 32] {
    let mut bytes = Vec::new();
    for (tag, n) in [
        (b'c', run.cooling.len()),
        (b'h', run.thermal_residuals.len()),
        (b'r', run.rod.len()),
        (b't', run.ties.len()),
        (b'q', run.edge_fit.len()),
        (b'e', run.edge_record.len()),
        (b'w', run.world.len()),
        (b'l', run.law_record.len()),
        (b's', run.summary.len()),
    ] {
        if !with_edge && (tag == b'q' || tag == b'e') {
            continue;
        }
        bytes.push(tag);
        bytes.extend_from_slice(&(n as u64).to_le_bytes());
    }
    run.cooling.iter().for_each(|v| bytes.extend_from_slice(&v.to_bits().to_le_bytes()));
    run.thermal_residuals.iter().for_each(|v| bytes.extend_from_slice(&v.to_bits().to_le_bytes()));
    run.rod.iter().for_each(|v| bytes.extend_from_slice(&v.to_bits().to_le_bytes()));
    run.ties.iter().for_each(|v| bytes.extend_from_slice(&v.to_bits().to_le_bytes()));
    if with_edge {
        run.edge_fit.iter().for_each(|v| bytes.extend_from_slice(&v.to_le_bytes()));
        bytes.extend_from_slice(&run.edge_record);
    }
    run.world.iter().for_each(|v| bytes.extend_from_slice(&v.to_bits().to_le_bytes()));
    bytes.extend_from_slice(&run.law_record);
    bytes.extend_from_slice(&run.summary);
    bytes.extend_from_slice(world_raw);
    alice_crypto::hash(&bytes).0
}

/// Every field of a residual summary, as bits
fn summary_bytes(s: &alice_analytics::law::ResidualSummary) -> Vec<u8> {
    let mut b = s.law_id.to_vec();
    for n in [s.count, s.out_of_range, s.non_finite, s.outside_accuracy] {
        b.extend_from_slice(&n.to_le_bytes());
    }
    let mut f = vec![s.relative_accuracy, s.accurate_range.0, s.accurate_range.1];
    if let Some(d) = s.distribution {
        f.extend([d.mean, d.min, d.max, d.max_abs, d.p50, d.p90, d.p99]);
    }
    b.push(u8::from(s.distribution.is_some()));
    f.iter().for_each(|v| b.extend_from_slice(&v.to_bits().to_le_bytes()));
    b
}

/// Each `alice-*` package in `Cargo.lock` with the versions it is locked at
///
/// Two versions of one crate are two copies of its types and its arithmetic: a value
/// saved through one is not the value the other reads back.
fn locked_alice_versions() -> Vec<(String, Vec<String>)> {
    let lock = std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.lock"))
        .expect("Cargo.lock next to Cargo.toml");
    let mut found: std::collections::BTreeMap<String, Vec<String>> = std::collections::BTreeMap::new();
    let mut name: Option<String> = None;
    // only `[[package]]` entries: `[[patch.unused]]` lists patches Cargo did not use
    let mut in_package = false;
    for line in lock.lines() {
        if line.starts_with("[[") || line.starts_with('[') {
            in_package = line == "[[package]]";
            name = None;
        } else if !in_package {
        } else if let Some(v) = line.strip_prefix("name = \"") {
            name = Some(v.trim_end_matches('"').to_string());
        } else if let (Some(n), Some(v)) = (&name, line.strip_prefix("version = \"")) {
            if n.starts_with("alice-") {
                found.entry(n.clone()).or_default().push(v.trim_end_matches('"').to_string());
            }
        }
    }
    found.into_iter().collect()
}

fn unhex(text: &str) -> [u8; 32] {
    let mut out = [0u8; 32];
    for (k, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&text[2 * k..2 * k + 2], 16).expect("hex");
    }
    out
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn scratch_dir() -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!("alice-law-e2e-{}-{nanos}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// Compares two value lists by `to_bits`, returns how many it compared
fn compare_bits<T: Copy + std::fmt::Debug, B: PartialEq + std::fmt::Debug>(
    what: &str,
    written: &[T],
    read: &[T],
    bits: impl Fn(T) -> B,
) -> usize {
    assert_eq!(read.len(), written.len(), "{what}: read {} of {} values", read.len(), written.len());
    for (i, (w, r)) in written.iter().zip(read).enumerate() {
        assert_eq!(bits(*w), bits(*r), "{what}[{i}]: wrote {w:?}, read {r:?}");
    }
    assert!(!written.is_empty(), "{what}: nothing was compared");
    written.len()
}

fn save(dir: &Path, run: &Run, law: &SignalLaw, edge: &SignalLaw) -> (u64, u64) {
    let db = AliceDB::open(dir).unwrap();
    let cooling = db.series("cooling").unwrap();
    for (i, v) in run.cooling.iter().enumerate() {
        cooling.put_f64(i as i64, *v).unwrap();
    }
    let thermal = db.series("thermal").unwrap();
    for (i, v) in run.thermal_residuals.iter().enumerate() {
        thermal.put_f32(i as i64, *v).unwrap();
    }
    let rod = db.series("rod").unwrap();
    for (i, v) in run.rod.iter().enumerate() {
        rod.put_f32(i as i64, *v).unwrap();
    }
    let ties = db.series("ties").unwrap();
    for (i, v) in run.ties.iter().enumerate() {
        ties.put_f32(i as i64, *v).unwrap();
    }
    let world = db.series("world").unwrap();
    for (i, v) in run.world.iter().enumerate() {
        world.put_f64(i as i64, *v).unwrap();
    }
    let version = db.put_law(LAW_NAME, law, &cooling_semantics()).unwrap();
    let edge_version = db.put_law(EDGE_LAW_NAME, edge, &alice_edge::SEMANTICS_ID).unwrap();
    db.flush().unwrap();
    db.close().unwrap();
    (version, edge_version)
}

fn values_f64(db: &AliceDB, name: &str) -> Vec<f64> {
    let s = db.series(name).unwrap();
    let rows = s.scan_f64(i64::MIN, i64::MAX).unwrap();
    rows.iter().enumerate().for_each(|(i, (k, _))| assert_eq!(*k, i as i64, "{name}: key {k} at {i}"));
    rows.into_iter().map(|(_, v)| v).collect()
}

fn values_f32(db: &AliceDB, name: &str) -> Vec<f32> {
    let s = db.series(name).unwrap();
    let rows = s.scan_f32(i64::MIN, i64::MAX).unwrap();
    rows.iter().enumerate().for_each(|(i, (k, _))| assert_eq!(*k, i as i64, "{name}: key {k} at {i}"));
    rows.into_iter().map(|(_, v)| v).collect()
}

/// The published releases re-export alice-det-math's identifier: each must be the one
/// this build linked.
#[cfg(not(feature = "main-mode"))]
fn each_crate_folds_this_det_math(semantics: &[u8; 32]) -> usize {
    let identifiers = [
        ("alice-zip", alice_zip::law::SEMANTICS_ID),
        ("alice-db", alice_db::SEMANTICS_ID),
        ("alice-analytics", alice_analytics::SEMANTICS_ID),
        ("alice-edge", alice_edge::SEMANTICS_ID),
    ];
    for (name, id) in identifiers {
        assert_eq!(&id, semantics, "{name} was built with a different alice-det-math");
    }
    identifiers.len()
}

/// How a crate's identifier takes in alice-det-math: through its own `alice-det-math` pin, or
/// through the pin of another checked crate (named after it, holding that crate's identifier)
/// which takes it in itself, `depth` crates down.
#[derive(Debug, PartialEq, Eq)]
enum DetMathBinding {
    Direct,
    Via { crate_name: String, depth: usize },
}

/// One checked crate: its name, its pins and its identifier.
type Checked<'a> = (&'a str, &'a [(&'a str, [u8; 32])], [u8; 32]);

/// The [`DetMathBinding`] of `name` among `crates`, against the alice-det-math identifier this
/// build linked. Every pin named after another checked crate must hold that crate's identifier,
/// whether or not the binding goes through it (a stale crate pin is an error even beside a
/// direct alice-det-math pin). A pin of alice-det-math that is not the linked one, a crate that
/// pins it more than once, a chain through a crate that does not take it in, and a cycle are
/// each an error naming the crate.
fn det_math_binding(
    crates: &[Checked<'_>],
    name: &str,
    semantics: &[u8; 32],
    stack: &mut Vec<String>,
) -> Result<DetMathBinding, String> {
    let Some((_, pins, _)) = crates.iter().find(|(n, _, _)| *n == name) else {
        return Err(format!("{name} is not a checked crate"));
    };
    if stack.iter().any(|s| s == name) {
        return Err(format!("{name}: a cycle of crate pins ({} -> {name})", stack.join(" -> ")));
    }
    for (pin, value) in pins.iter() {
        if let Some((_, _, id)) = crates.iter().find(|(n, _, _)| n == pin && *n != name) {
            if value != id {
                return Err(format!("{name}: its {pin} pin is not {pin}'s identifier"));
            }
        }
    }
    let direct: Vec<_> = pins.iter().filter(|(n, _)| *n == "alice-det-math").collect();
    match direct.len() {
        0 => {}
        1 if &direct[0].1 == semantics => return Ok(DetMathBinding::Direct),
        1 => return Err(format!("{name} was built with a different alice-det-math")),
        n => return Err(format!("{name}: its pins hold alice-det-math {n} times, not once")),
    }
    stack.push(name.to_string());
    let mut why = Vec::new();
    for (pin, value) in pins.iter() {
        let Some((_, _, id)) = crates.iter().find(|(n, _, _)| n == pin && *n != name) else {
            continue;
        };
        debug_assert_eq!(value, id, "checked above");
        match det_math_binding(crates, pin, semantics, stack) {
            Ok(b) => {
                stack.pop();
                let depth = match b {
                    DetMathBinding::Direct => 1,
                    DetMathBinding::Via { depth, .. } => depth + 1,
                };
                return Ok(DetMathBinding::Via { crate_name: (*pin).to_string(), depth });
            }
            Err(e) => why.push(e),
        }
    }
    stack.pop();
    Err(format!(
        "{name}: its pins take in alice-det-math neither directly nor through a checked crate ({})",
        if why.is_empty() { "no pin names one".to_string() } else { why.join("; ") }
    ))
}

/// On main each crate has its own identifier (ALICE-Law G6), the fold of its own pins: folding
/// them must give the crate's identifier (so pins that were not the ones folded fail here), and
/// they must take in the alice-det-math this build linked, directly or through another checked
/// crate's identifier (so an identifier that ignored alice-det-math fails here).
#[cfg(feature = "main-mode")]
fn each_crate_folds_this_det_math(semantics: &[u8; 32]) -> usize {
    let crates: [(&str, &[(&str, [u8; 32])], [u8; 32]); 4] = [
        ("alice-zip", alice_zip::semantics::ZIP_SEMANTICS_PINS, alice_zip::law::SEMANTICS_ID),
        ("alice-db", alice_db::semantics::DB_SEMANTICS_PINS, alice_db::DB_SEMANTICS_ID),
        (
            "alice-analytics",
            &alice_analytics::semantics::ANALYTICS_SEMANTICS_PINS,
            alice_analytics::semantics::ANALYTICS_SEMANTICS_ID,
        ),
        ("alice-edge", &alice_edge::EDGE_SEMANTICS_PINS, alice_edge::EDGE_SEMANTICS_ID),
    ];
    for (name, pins, id) in crates {
        assert_eq!(fold(pins), id, "{name}: its identifier is not the fold of its pins");
        let binding = det_math_binding(&crates, name, semantics, &mut Vec::new()).unwrap_or_else(|e| panic!("{e}"));
        let how = match &binding {
            DetMathBinding::Direct => "directly".to_string(),
            DetMathBinding::Via { crate_name, depth } => format!("through {crate_name} (depth {depth})"),
        };
        println!("e2e: {name} identifier {} folds {} pins, alice-det-math {how}", hex(&id), pins.len());
    }
    crates.len()
}

/// alice-det-math's fold: entries sorted by name (byte order), then SHA-256 over each
/// entry's name length (u32, big-endian), name bytes and 32 digest bytes.
#[cfg(feature = "main-mode")]
fn fold(pins: &[(&str, [u8; 32])]) -> [u8; 32] {
    use sha2::{Digest, Sha256};
    let mut sorted: Vec<_> = pins.to_vec();
    sorted.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    assert!(!sorted.is_empty(), "an empty pin table folds nothing");
    let mut h = Sha256::new();
    for (name, digest) in sorted {
        h.update((name.len() as u32).to_be_bytes());
        h.update(name.as_bytes());
        h.update(digest);
    }
    h.finalize().into()
}

/// The record alice-db writes for a law (the published releases take no identifier; on
/// main the record carries the identifier it was written under, ALICE-Law G8).
#[cfg(not(feature = "main-mode"))]
fn law_record(name: &str, version: u64, law: &SignalLaw, _semantics: &[u8; 32]) -> Vec<u8> {
    encode_law_record(name, version, &law.to_parts()).unwrap()
}

/// The arithmetic the cooling law is stamped with: the identifier its producer, alice-zip,
/// exports for the arithmetic a law is evaluated with. The published releases name
/// alice-det-math's; on main alice-zip's `law::SEMANTICS_ID` is its own fold, the identifier
/// alice-db verifies a stored record against.
#[cfg(not(feature = "main-mode"))]
fn cooling_semantics() -> [u8; 32] {
    alice_det_math::SEMANTICS_ID
}

#[cfg(feature = "main-mode")]
fn cooling_semantics() -> [u8; 32] {
    alice_zip::law::SEMANTICS_ID
}

/// On main, a law stamped with alice-det-math's identifier (the arithmetic before alice-zip
/// folded its own) reads back as that earlier arithmetic, not as the current one: the known
/// earlier-arithmetic case, so the Current checks of the stored laws compare something.
#[cfg(feature = "main-mode")]
fn earlier_arithmetic_reads_as_previous(db: &AliceDB, law: &SignalLaw) -> usize {
    db.put_law("earlier-arithmetic", law, &alice_det_math::SEMANTICS_ID).unwrap();
    let stored = db.get_law("earlier-arithmetic").unwrap().expect("stored");
    assert_eq!(
        stored.semantics,
        alice_db::law_store::RecordSemantics::VerifiedPrevious(alice_det_math::SEMANTICS_ID),
        "a law stamped with alice-det-math's identifier must read as the earlier arithmetic"
    );
    1
}

#[cfg(feature = "main-mode")]
fn law_record(name: &str, version: u64, law: &SignalLaw, semantics: &[u8; 32]) -> Vec<u8> {
    encode_law_record(name, version, &law.to_parts(), semantics).unwrap()
}

/// A stored law read back (on main also its record's semantics, which must be the
/// current one: a record the store read as unpinned or as another arithmetic fails here).
#[cfg(not(feature = "main-mode"))]
fn restore_law(db: &AliceDB, name: &str) -> SignalLaw {
    db.get_law(name).unwrap().unwrap_or_else(|| panic!("{name} was saved"))
}

#[cfg(feature = "main-mode")]
fn restore_law(db: &AliceDB, name: &str) -> SignalLaw {
    let stored = db.get_law(name).unwrap().unwrap_or_else(|| panic!("{name} was saved"));
    assert_eq!(
        stored.semantics,
        alice_db::law_store::RecordSemantics::Current,
        "{name}: the store read its record under another arithmetic"
    );
    stored.law
}

#[test]
fn a_law_survives_compute_save_and_restore_bit_for_bit() {
    // the crates were built with one arithmetic: a second version of
    // alice-det-math in the build would give a crate a different identifier
    let semantics = alice_det_math::SEMANTICS_ID;
    // one version of every alice-* crate first: the identifier below is looked up by the one
    // alice-det-math release in the build
    let locked = locked_alice_versions();
    let twice: Vec<_> = locked.iter().filter(|(_, v)| v.len() > 1).collect();
    assert!(twice.is_empty(), "linked at two or more versions: {twice:?}");
    let det_math_version = locked
        .iter()
        .find(|(name, _)| name == "alice-det-math")
        .and_then(|(_, v)| v.first().cloned())
        .expect("alice-det-math in Cargo.lock");
    let position = semantics_entry(&det_math_version).unwrap_or_else(|| {
        panic!("alice-det-math {det_math_version} is older than every row of KNOWN_SEMANTICS")
    });
    let (entry_version, entry_id) = KNOWN_SEMANTICS[position];
    // the crate's own constant, not a copy: a release that changes the identifier without a new
    // row fails here, by name
    assert_eq!(
        hex(&alice_det_math::SEMANTICS_ID),
        entry_id,
        "alice-det-math {det_math_version} has an identifier the {entry_version} row does not record: \
         a release that changes its identifier adds its row to KNOWN_SEMANTICS"
    );
    assert_eq!(hex(&semantics), entry_id);
    for (version, earlier) in &KNOWN_SEMANTICS[..position] {
        let id = unhex(earlier);
        assert_eq!(
            alice_det_math::check_semantics(&id),
            alice_det_math::SemanticsCheck::VerifiedPrevious(id),
            "alice-det-math {det_math_version} does not accept the identifier of {version} as a verified earlier one"
        );
    }
    println!(
        "e2e: alice-det-math {det_math_version} (row {entry_version}), identifier {}, {} earlier identifier(s) accepted",
        hex(&semantics),
        position
    );
    let crate_ids = each_crate_folds_this_det_math(&semantics);
    let lol_pin = alice_lol::law_id::LOL_SEMANTICS_PINS
        .iter()
        .find(|(name, _)| *name == "alice-det-math")
        .map(|(_, id)| *id)
        .expect("alice-lol pins the alice-det-math identifier");
    assert_eq!(lol_pin, semantics, "alice-lol pins a different alice-det-math");
    assert_eq!(
        alice_lol::law_id::fold_semantics_pins(alice_lol::law_id::LOL_SEMANTICS_PINS),
        alice_lol::law_id::LOL_SEMANTICS_ID
    );
    let compared_ids = crate_ids + 1;

    for core in [
        "alice-det-math", "alice-crypto", "alice-zip", "alice-analytics",
        "alice-db", "alice-physics", "alice-sdf", "alice-lol", "alice-edge",
    ] {
        assert!(locked.iter().any(|(n, _)| n == core), "{core} is not in Cargo.lock");
    }

    // 1. read
    let law_text = cooling_law();
    let cooling: Vec<f64> = (0..COOLING_POINTS)
        .map(|i| law_text.evaluate(&[("t", cooling_time(i))]).unwrap())
        .collect();
    assert_eq!(cooling[0], 373.15, "T(0) = T0");

    // 2. compute
    let thermal_residuals = thermal_residuals();
    let rod = rod();
    let ties = ties();
    assert!(ties.iter().any(|v| v.to_bits() == 0x8000_0000) && ties.iter().any(|v| v.to_bits() == 0),
            "the tie step produced no signed zero of one kind");
    let (world, world_raw) = world();
    let samples = edge_samples();
    let (edge_fit, edge) = edge_law(&samples);
    assert_eq!(edge.coefficients().len(), 2, "alice-edge fits a line");

    // 3. fit
    let points: Vec<(f64, f64)> = (0..COOLING_POINTS).map(|i| (cooling_time(i), cooling[i as usize])).collect();
    let law = SignalLaw::fit_polynomial(&points, 4, Provenance::new("alice-lol research law", "least squares, degree 4")).unwrap();
    let summary = alice_analytics::law::residual_summary(&law, &points);
    assert_eq!(summary.count, COOLING_POINTS as u64);

    // 4. save
    let dir = scratch_dir();
    let mut run = Run {
        cooling,
        thermal_residuals,
        rod,
        ties,
        edge_fit,
        edge_record: Vec::new(),
        world,
        law_record: Vec::new(),
        summary: summary_bytes(&summary),
    };
    let (version, edge_version) = save(&dir, &run, &law, &edge);
    run.law_record = law_record(LAW_NAME, version, &law, &cooling_semantics());
    run.edge_record = law_record(EDGE_LAW_NAME, edge_version, &edge, &alice_edge::SEMANTICS_ID);
    let computed = digest(&run, &world_raw);

    // 5. restore
    let db = AliceDB::open(&dir).unwrap();
    let restored_law = restore_law(&db, LAW_NAME);
    let restored_edge = restore_law(&db, EDGE_LAW_NAME);
    let restored = Run {
        cooling: values_f64(&db, "cooling"),
        thermal_residuals: values_f32(&db, "thermal"),
        rod: values_f32(&db, "rod"),
        ties: values_f32(&db, "ties"),
        edge_fit: edge_law(&samples).0,
        edge_record: law_record(EDGE_LAW_NAME, edge_version, &restored_edge, &alice_edge::SEMANTICS_ID),
        world: values_f64(&db, "world"),
        law_record: law_record(LAW_NAME, version, &restored_law, &cooling_semantics()),
        summary: Vec::new(),
    };
    let restored_points: Vec<(f64, f64)> = restored
        .cooling
        .iter()
        .enumerate()
        .map(|(i, v)| (cooling_time(i as i64), *v))
        .collect();
    let restored_summary = alice_analytics::law::residual_summary(&restored_law, &restored_points);
    let restored = Run { summary: summary_bytes(&restored_summary), ..restored };
    #[cfg(feature = "main-mode")]
    let earlier = earlier_arithmetic_reads_as_previous(&db, &restored_law);
    drop(db);
    std::fs::remove_dir_all(&dir).unwrap();

    // 6. compare
    let mut compared = 0;
    compared += compare_bits("cooling", &run.cooling, &restored.cooling, f64::to_bits);
    compared += compare_bits("thermal", &run.thermal_residuals, &restored.thermal_residuals, f32::to_bits);
    compared += compare_bits("rod", &run.rod, &restored.rod, f32::to_bits);
    compared += compare_bits("ties", &run.ties, &restored.ties, f32::to_bits);
    compared += compare_bits("world", &run.world, &restored.world, f64::to_bits);
    compared += compare_bits("law coefficients", law.coefficients(), restored_law.coefficients(), f64::to_bits);
    let domain = |l: &SignalLaw| [l.domain().lo, l.domain().hi];
    compared += compare_bits("law domain", &domain(&law), &domain(&restored_law), f64::to_bits);
    assert_eq!(run.law_record, restored.law_record, "the law record changed on the way through the store");
    assert!(!run.law_record.is_empty());
    assert_eq!(law.law_id(&semantics), restored_law.law_id(&semantics), "law id");
    compared += compare_bits("edge law coefficients", edge.coefficients(), restored_edge.coefficients(), f64::to_bits);
    compared += compare_bits("edge law domain", &domain(&edge), &domain(&restored_edge), f64::to_bits);
    #[cfg(feature = "main-mode")]
    {
        compared += earlier;
    }
    assert_eq!(run.edge_record, restored.edge_record, "the alice-edge law record changed on the way through the store");
    assert!(!run.edge_record.is_empty());
    assert_eq!(edge.law_id(&semantics), restored_edge.law_id(&semantics), "alice-edge law id");
    compared += 2; // edge law record, edge law id
    assert_eq!(summary, restored_summary, "residual summary");
    assert_eq!(run.summary, restored.summary, "residual summary bits");
    let restored_digest = digest(&restored, &world_raw);
    assert_eq!(hex(&computed), hex(&restored_digest), "digest of the run");
    compared += 4; // law record, law id, residual summary, digest

    println!(
        "e2e: compared {compared} values and records, {compared_ids} arithmetic identifiers, \
         {} locked alice-* crates; {} cooling, {} thermal, {} rod, {} world values, {} law coefficients, law record {} bytes",
        locked.len(),
        run.cooling.len(),
        run.thermal_residuals.len(),
        run.rod.len(),
        run.world.len(),
        law.coefficients().len(),
        run.law_record.len()
    );
    assert!(compared > 0 && compared_ids > 0);

    // golden: what the crates compute, recorded when it was last blessed, one line per
    // alice-det-math release ("<version> <digest>"): the run is digested with law records that
    // carry identifiers derived from the arithmetic, so each release of it has its own digest
    let got = hex(&computed);
    println!("e2e: run digest without the alice-edge records {}", hex(&digest_of(&run, &world_raw, false)));
    // the values alone (the law records left out) are the same in both modes: main mode
    // changes the record format (it carries an identifier, ALICE-Law G8), not what is computed
    let values = Run { law_record: Vec::new(), edge_record: Vec::new(), ..run.clone() };
    let values_got = hex(&digest(&values, &world_raw));
    println!("e2e: values digest {values_got}");
    if let Some(out) = std::env::var_os("ALICE_LAW_E2E_DIGEST_OUT") {
        // for the comparison of two architectures: the whole run in the published mode
        // (.github/workflows/e2e.yml, against golden/run.digest), the values on main
        // (e2e-main.yml, against golden/values.digest)
        let shown = if cfg!(feature = "main-mode") { &values_got } else { &got };
        // keyed like the golden files: by the KNOWN_SEMANTICS row the release is built with
        std::fs::write(out, format!("{entry_version} {shown}\n")).unwrap();
    }
    check_golden("golden/values.digest", entry_version, &values_got);
    // the whole run, records included, is the published releases' format; on main the records
    // are compared through the store above (written == read back, read as the current semantics)
    #[cfg(not(feature = "main-mode"))]
    check_golden("golden/run.digest", entry_version, &got);
    println!("e2e: run digest {got}");
}

/// `got` against the line of `det_math_version` in the golden file `rel` ("<version> <digest>"
/// per line), or that line written when ALICE_LAW_E2E_BLESS is set
fn check_golden(rel: &str, det_math_version: &str, got: &str) {
    let golden_path = Path::new(env!("CARGO_MANIFEST_DIR")).join(rel);
    let recorded = std::fs::read_to_string(&golden_path).unwrap_or_default();
    let mut lines: Vec<(String, String)> = Vec::new();
    for l in recorded.lines().filter(|l| !l.trim().is_empty()) {
        let (version, digest) = l.split_once(' ').unwrap_or_else(|| panic!("{rel}: \"<version> <digest>\" per line"));
        assert!(
            KNOWN_SEMANTICS.iter().any(|(known, _)| *known == version),
            "{rel} names alice-det-math {version}, which KNOWN_SEMANTICS does not list"
        );
        assert!(lines.iter().all(|(v, _)| v != version), "{rel} names {version} twice");
        lines.extend([(version.to_string(), digest.to_string())]);
    }
    if std::env::var_os("ALICE_LAW_E2E_BLESS").is_some() {
        lines.retain(|(v, _)| v != det_math_version);
        lines.extend([(det_math_version.to_string(), got.to_string())]);
        lines.sort_by_key(|(v, _)| KNOWN_SEMANTICS.iter().position(|(known, _)| known == v));
        let text: String = lines.iter().map(|(v, d)| format!("{v} {d}\n")).collect();
        std::fs::write(&golden_path, text).unwrap();
        println!("e2e: wrote {} for alice-det-math {det_math_version}", golden_path.display());
    } else {
        let want = lines
            .iter()
            .find(|(v, _)| v == det_math_version)
            .map(|(_, d)| d.clone())
            .unwrap_or_else(|| panic!("{rel} has no digest for alice-det-math {det_math_version}: record it with ALICE_LAW_E2E_BLESS=1"));
        assert_eq!(got, want, "{rel}: the run computes something else than when the golden was recorded");
    }
}

/// The transitive rule of [`det_math_binding`] on made-up crates (runs in both modes).
#[test]
fn det_math_binding_follows_crate_pins_and_refuses_the_rest() {
    let det = [1u8; 32];
    let other = [2u8; 32];
    let (zip_id, db_id, x_id) = ([10u8; 32], [11u8; 32], [12u8; 32]);
    let zip_pins = [("golden", [3u8; 32]), ("alice-det-math", det)];
    let db_pins = [("model", [4u8; 32]), ("alice-zip", zip_id)];
    let no_det = [("model", [5u8; 32])];
    let crates: [Checked<'_>; 2] = [("alice-zip", &zip_pins, zip_id), ("alice-db", &db_pins, db_id)];
    let b = |cs: &[Checked<'_>], n: &str| det_math_binding(cs, n, &det, &mut Vec::new());
    assert_eq!(b(&crates, "alice-zip"), Ok(DetMathBinding::Direct));
    assert_eq!(b(&crates, "alice-db"), Ok(DetMathBinding::Via { crate_name: "alice-zip".into(), depth: 1 }));
    // depth counts the chain
    let x_pins = [("alice-db", db_id)];
    let three: [Checked<'_>; 3] = [crates[0], crates[1], ("alice-x", &x_pins, x_id)];
    assert_eq!(b(&three, "alice-x"), Ok(DetMathBinding::Via { crate_name: "alice-db".into(), depth: 2 }));
    // the zip pin dropped: neither binding
    let dropped: [Checked<'_>; 2] = [crates[0], ("alice-db", &no_det, db_id)];
    assert!(b(&dropped, "alice-db").unwrap_err().contains("neither directly nor through"));
    // the zip pin is not the linked zip's identifier
    let stale_pins = [("model", [4u8; 32]), ("alice-zip", other)];
    let stale: [Checked<'_>; 2] = [crates[0], ("alice-db", &stale_pins, db_id)];
    assert!(b(&stale, "alice-db").unwrap_err().contains("its alice-zip pin is not alice-zip's identifier"));
    // a chain through a crate that does not take alice-det-math in
    let hollow: [Checked<'_>; 2] = [("alice-zip", &no_det, zip_id), ("alice-db", &db_pins, db_id)];
    assert!(b(&hollow, "alice-db").unwrap_err().contains("alice-zip: its pins take in alice-det-math neither"));
    // a different alice-det-math, or the same one twice
    assert!(b(&crates, "alice-zip").is_ok());
    let wrong = [("alice-det-math", other)];
    assert!(b(&[("alice-zip", &wrong, zip_id)], "alice-zip").unwrap_err().contains("a different alice-det-math"));
    let twice = [("alice-det-math", det), ("alice-det-math", det)];
    assert!(b(&[("alice-zip", &twice, zip_id)], "alice-zip").unwrap_err().contains("2 times"));
    // a stale crate pin is an error even when another route binds, and even beside a direct pin
    let an_id = [13u8; 32];
    let an_pins = [("alice-det-math", det)];
    let mixed_pins = [("alice-zip", zip_id), ("alice-analytics", other)];
    let mixed: [Checked<'_>; 3] = [crates[0], ("alice-analytics", &an_pins, an_id), ("alice-db", &mixed_pins, db_id)];
    assert!(b(&mixed, "alice-db").unwrap_err().contains("its alice-analytics pin is not alice-analytics's identifier"));
    let direct_stale = [("alice-det-math", det), ("alice-zip", other)];
    let ds: [Checked<'_>; 2] = [crates[0], ("alice-edge", &direct_stale, [14u8; 32])];
    assert!(b(&ds, "alice-edge").unwrap_err().contains("its alice-zip pin is not alice-zip's identifier"));
    // a cycle of crate pins
    let (a_pins, c_pins) = ([("alice-c", [21u8; 32])], [("alice-a", [20u8; 32])]);
    let cycle: [Checked<'_>; 2] = [("alice-a", &a_pins, [20u8; 32]), ("alice-c", &c_pins, [21u8; 32])];
    assert!(b(&cycle, "alice-a").unwrap_err().contains("cycle"));
}

/// A release without a new identifier is built with the row it started at; one older than every
/// row has none. The table is strictly ordered by release and has no identifier twice (each row is
/// a change of the identifier).
#[test]
fn a_release_is_built_with_the_row_it_started_at() {
    let at = |v: &str| semantics_entry(v).map(|i| KNOWN_SEMANTICS[i].0);
    assert_eq!(at("0.5.0"), Some("0.5.0"));
    assert_eq!(at("0.5.1"), Some("0.5.1"));
    assert_eq!(at("0.5.2"), Some("0.5.1"));
    assert_eq!(at("0.5.10"), Some("0.5.1"));
    // every row's release is built with its own row, and a release past the last row with the last one
    // (independent of how many rows follow: appending a row keeps every line true)
    for (version, _) in KNOWN_SEMANTICS {
        assert_eq!(at(version), Some(*version), "{version} is built with its own row");
    }
    let (last, _) = KNOWN_SEMANTICS.last().unwrap();
    let (major, minor, patch) = release(last);
    assert_eq!(at(&format!("{major}.{minor}.{}", patch + 1)), Some(*last));
    assert_eq!(at("0.4.9"), None);
    for pair in KNOWN_SEMANTICS.windows(2) {
        assert!(release(pair[0].0) < release(pair[1].0), "{} before {}", pair[0].0, pair[1].0);
        assert_ne!(pair[0].1, pair[1].1, "{} and {} record the same identifier", pair[0].0, pair[1].0);
    }
    let ids: std::collections::BTreeSet<_> = KNOWN_SEMANTICS.iter().map(|(_, id)| *id).collect();
    assert_eq!(ids.len(), KNOWN_SEMANTICS.len(), "an identifier recorded twice");
}

#[test]
#[should_panic(expected = "not major.minor.patch")]
fn a_pre_release_is_refused() {
    let _ = release("0.6.0-rc.1");
}
