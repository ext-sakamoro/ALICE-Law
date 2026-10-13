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
//! `ALICE_LAW_E2E_BLESS=1` writes the current digest instead of comparing.

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
const SEMANTICS_HEX: &str = "135c407d4249c1c200e4d1be79a12e848ce5c355fc3a918da965849e697ef000";
/// 0 to 600 s every 0.25 s: on about one point in ten, `alice_det_math::exp64` and
/// a platform's `exp` round differently, so the grid shows which one was used
const COOLING_POINTS: i64 = 2401;
const ROD_CELLS: usize = 64;
const ROD_SNAPSHOTS: usize = 10;
const ROD_STEPS_PER_SNAPSHOT: usize = 20;
const WORLD_FRAMES: usize = 120;
const WORLD_BODIES: [usize; 2] = [1, 2];

/// Everything the run computes, in the order it is digested
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
        bytes.push(tag);
        bytes.extend_from_slice(&(n as u64).to_le_bytes());
    }
    run.cooling.iter().for_each(|v| bytes.extend_from_slice(&v.to_bits().to_le_bytes()));
    run.thermal_residuals.iter().for_each(|v| bytes.extend_from_slice(&v.to_bits().to_le_bytes()));
    run.rod.iter().for_each(|v| bytes.extend_from_slice(&v.to_bits().to_le_bytes()));
    run.ties.iter().for_each(|v| bytes.extend_from_slice(&v.to_bits().to_le_bytes()));
    run.edge_fit.iter().for_each(|v| bytes.extend_from_slice(&v.to_le_bytes()));
    bytes.extend_from_slice(&run.edge_record);
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
    let version = db.put_law(LAW_NAME, law, &alice_det_math::SEMANTICS_ID).unwrap();
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

#[test]
fn a_law_survives_compute_save_and_restore_bit_for_bit() {
    // the crates were built with one arithmetic: a second version of
    // alice-det-math in the build would give a crate a different identifier
    let semantics = alice_det_math::SEMANTICS_ID;
    let identifiers = [
        ("alice-zip", alice_zip::law::SEMANTICS_ID),
        ("alice-db", alice_db::SEMANTICS_ID),
        ("alice-analytics", alice_analytics::SEMANTICS_ID),
        ("alice-edge", alice_edge::SEMANTICS_ID),
    ];
    assert_eq!(hex(&semantics), SEMANTICS_HEX, "the arithmetic identifier changed");
    for (name, id) in identifiers {
        assert_eq!(id, semantics, "{name} was built with a different alice-det-math");
    }
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
    let compared_ids = identifiers.len() + 1;

    let locked = locked_alice_versions();
    let twice: Vec<_> = locked.iter().filter(|(_, v)| v.len() > 1).collect();
    assert!(twice.is_empty(), "linked at two or more versions: {twice:?}");
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
    run.law_record = encode_law_record(LAW_NAME, version, &law.to_parts()).unwrap();
    run.edge_record = encode_law_record(EDGE_LAW_NAME, edge_version, &edge.to_parts()).unwrap();
    let computed = digest(&run, &world_raw);

    // 5. restore
    let db = AliceDB::open(&dir).unwrap();
    let restored_law = db.get_law(LAW_NAME).unwrap().expect("the law was saved");
    let restored_edge = db.get_law(EDGE_LAW_NAME).unwrap().expect("the alice-edge law was saved");
    let restored = Run {
        cooling: values_f64(&db, "cooling"),
        thermal_residuals: values_f32(&db, "thermal"),
        rod: values_f32(&db, "rod"),
        ties: values_f32(&db, "ties"),
        edge_fit: edge_law(&samples).0,
        edge_record: encode_law_record(EDGE_LAW_NAME, edge_version, &restored_edge.to_parts()).unwrap(),
        world: values_f64(&db, "world"),
        law_record: encode_law_record(LAW_NAME, version, &restored_law.to_parts()).unwrap(),
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

    // golden: what the crates compute, recorded when it was last blessed
    let golden_path = Path::new(env!("CARGO_MANIFEST_DIR")).join("golden/run.digest");
    let got = hex(&computed);
    if let Some(out) = std::env::var_os("ALICE_LAW_E2E_DIGEST_OUT") {
        // for the comparison of two architectures (.github/workflows/e2e-main.yml)
        std::fs::write(out, format!("{got}\n")).unwrap();
    }
    if std::env::var_os("ALICE_LAW_E2E_BLESS").is_some() {
        std::fs::write(&golden_path, format!("{got}\n")).unwrap();
        println!("e2e: wrote {}", golden_path.display());
    } else {
        let want = std::fs::read_to_string(&golden_path).expect("golden/run.digest");
        assert_eq!(got, want.trim(), "the run computes something else than when the golden was recorded");
    }
    println!("e2e: run digest {got}");
}
