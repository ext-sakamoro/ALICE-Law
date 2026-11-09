//! Issue, sign, carry, receive, evaluate, store, restore, and compare bit for bit
//!
//! One pass through the trust path, using only the crates' public APIs:
//!
//! 1. issue: each device's master key (`alice-sign`) issues it a device certificate
//!    (`alice_identity::device_cert::DeviceCert`) naming its signing key and its Noise key
//! 2. sign: the sender names a LOL program by its law reference
//!    (`alice_lol::program_law::program_law_ref`, an `alice_zip::law_ref::LawRef`) and signs
//!    SHA-256(law reference || payload digest) with its device key
//!    (`alice_identity::artifact::sign_artifact`)
//! 3. carry: the two devices open an authenticated channel (`alice-channel`, through
//!    `alice_sync::secure_transport::SecureTransport`) and a `SyncSession` on each side
//! 4. receive: the receiver accepts the bytes only after each side has accepted the other's
//!    Hello (the Sync semantics identifier); bytes before it are refused
//! 5. evaluate: the receiver checks the signature against the sender's master key, rebuilds the
//!    law from its reference (the identifier is recomputed from the bytes) and evaluates it
//! 6. store and restore: signature, reference, values and identifiers go into an
//!    `alice_db::AliceDB`, which is written to bytes and opened again
//! 7. compare: the signature bytes, the law identifier, every value (`to_bits`) and each
//!    crate's semantics identifier, sender against receiver against restored
//!
//! Each comparison counts what it compared and the run fails on zero. The values are also held to
//! the signs the program fixes at six points (not only to themselves). A tampered signature, a
//! tampered law reference (by its identifier and by the signature's binding) and bytes before the
//! handshake must each be refused, each for its own reason. The semantics identifiers are compared
//! across the store in one build: they show that storing keeps them, not that two devices agree.

#![allow(clippy::unwrap_used, clippy::float_cmp)]

use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::time::Duration;

use alice_channel::noise::Keypair;
use alice_channel::{Credential, PeerPolicy};
use alice_db::{AliceDB, StorageConfig};
use alice_identity::artifact::{ArtifactError, Revocation, SignedArtifact, Verified};
use alice_identity::device_cert::DeviceCert;
use alice_identity::did::Did;
use alice_lol::program_law::{program_law_ref, resolve_program};
use alice_lol::Vec3;
use alice_sign::{KeyPair, PublicKey};
use alice_sync::protocol::{HandshakeRefused, Message};
use alice_sync::secure_transport::SecureTransport;
use alice_sync::semantics::IdStatus;
use alice_sync::session::{SessionBuilder, SyncSession};
use alice_sync::transport::{Frame, SyncTransport, TransportError};
use alice_sync::world::WorldHash;
use alice_zip::law_ref::LawRef;
use tokio::time::timeout;

/// The certificates' window covers it; every check reads this clock
const NOW: u64 = 1_800_000_100;
const WAIT: Duration = Duration::from_secs(10);
/// The purpose the signature is made for (a signature for another purpose does not verify)
const CONTEXT: &[u8] = b"alice-law/e2e/trust-path/v1";
/// The law: a LOL program, named by its law reference (no mesh travels)
const PROGRAM: &str = "union(sphere(10.0), translate(12.0, 0.0, 0.0, box3d(4.0, 4.0, 4.0)))";

struct Device {
    master: KeyPair,
    seed: u8,
    did: Did,
}

fn device(seed: u8, name: &str) -> Device {
    Device {
        master: KeyPair::from_seed([seed; 32]),
        seed,
        did: Did::parse(&format!("did:alice:{name}")).unwrap(),
    }
}

impl Device {
    fn device_key(&self) -> KeyPair {
        KeyPair::from_seed([self.seed.wrapping_add(100); 32])
    }

    fn cert(&self) -> DeviceCert {
        let noise = Keypair::from_secret([self.seed.wrapping_add(200); 32]);
        DeviceCert::issue(
            &self.master,
            &self.did,
            self.device_key().public().0,
            noise.public(),
            1_800_000_000,
            1_800_086_400,
            [self.seed; 16],
        )
        .unwrap()
    }

    fn credential(&self) -> Credential {
        let noise = Keypair::from_secret([self.seed.wrapping_add(200); 32]);
        Credential::new(self.device_key(), self.cert(), noise).unwrap()
    }
}

struct Policy(BTreeMap<String, PublicKey>);

impl PeerPolicy for Policy {
    fn master_of(&self, subject: &Did) -> Option<PublicKey> {
        self.0.get(&subject.to_string()).copied()
    }
    fn revocation(&self) -> Revocation<'_> {
        Revocation::NoListAvailable
    }
    fn now(&self) -> u64 {
        NOW
    }
}

fn knowing(d: &Device) -> Policy {
    Policy(BTreeMap::from([(d.did.to_string(), d.master.public())]))
}

type Session = SyncSession<SecureTransport<Policy>>;

/// The comparisons made, by kind; the run fails when a kind compared nothing
#[derive(Default)]
struct Compared(BTreeMap<&'static str, usize>);

impl Compared {
    fn bits(&mut self, kind: &'static str, a: &[u8], b: &[u8]) {
        assert_eq!(a, b, "{kind}: the bytes differ");
        *self.0.entry(kind).or_default() += 1;
    }
}

/// The points the law is evaluated at: inside, on and outside both parts
fn grid() -> Vec<Vec3> {
    let mut out = Vec::new();
    for x in [
        -12.0f32, -10.0, -5.0, 0.0, 5.0, 9.5, 10.0, 12.0, 14.0, 16.0, 20.0,
    ] {
        for y in [-6.0f32, 0.0, 2.0, 6.0] {
            out.push(Vec3::new(x, y, 0.5));
        }
    }
    out
}

/// What the program itself says at some points, independent of the evaluator: a sphere of radius 10
/// at the origin, and a box of half extent 4 centred at x = 12 (so it spans x in [8, 16])
const SIGNS: [((f32, f32, f32), bool); 6] = [
    ((0.0, 0.0, 0.5), true),    // the centre of the sphere
    ((9.5, 0.0, 0.5), true),    // inside the sphere, near its surface
    ((14.0, 0.0, 0.5), true),   // inside the box only
    ((20.0, 0.0, 0.5), false),  // outside both
    ((-12.0, 0.0, 0.5), false), // outside the sphere on the other side
    ((0.0, 12.0, 0.5), false),  // above the sphere
];

fn evaluate(law_ref: &LawRef) -> Vec<u32> {
    let shape = resolve_program(law_ref).unwrap();
    grid()
        .into_iter()
        .map(|p| alice_lol::eval(&shape, p).to_bits())
        .collect()
}

/// The one way in for a received unit and for one read back from storage: the signature first,
/// then the law reference (its identifier recomputed from its bytes)
fn open(bytes: &[u8], sender: &Device) -> Result<(Verified, LawRef), ArtifactError> {
    let v = SignedArtifact::open(
        bytes,
        &sender.master.public(),
        CONTEXT,
        NOW,
        Revocation::NoListAvailable,
    )?;
    let law = LawRef::from_bytes(v.law_ref()).expect("a signed law reference parses");
    Ok((v, law))
}

/// `bytes` with the first `from` inside the law reference part replaced by `to` (same length)
fn replace_in_law_ref(bytes: &[u8], from: &[u8], to: &[u8]) -> Vec<u8> {
    assert_eq!(from.len(), to.len());
    let mut out = bytes.to_vec();
    let at = out
        .windows(from.len())
        .position(|w| w == from)
        .expect("the text is in the unit");
    out[at..at + to.len()].copy_from_slice(to);
    out
}

/// A unit put together by hand: version, law reference, payload, signature (u32 BE lengths), as
/// someone with write access to the store but not the device key can make it
fn unit_bytes(law_ref: &[u8], payload: &[u8], signature: &[u8]) -> Vec<u8> {
    let mut b = vec![1u8];
    for part in [law_ref, payload, signature] {
        b.extend_from_slice(&u32::try_from(part.len()).unwrap().to_be_bytes());
        b.extend_from_slice(part);
    }
    b
}

fn values_bytes(values: &[u32]) -> Vec<u8> {
    values.iter().flat_map(|v| v.to_le_bytes()).collect()
}

/// Two sessions on authenticated transports: `a` (the receiver) listens, `b` (the sender) holds
/// `a`'s certificate and connects; each knows only the other's master key
async fn pair(receiver: &Device, sender: &Device) -> (Session, Session, SocketAddr) {
    let ta = SecureTransport::bind("127.0.0.1:0", receiver.credential(), knowing(sender))
        .await
        .unwrap();
    let tb = SecureTransport::bind("127.0.0.1:0", sender.credential(), knowing(receiver))
        .await
        .unwrap();
    let build = |node, t| {
        SessionBuilder::new()
            .node_id(node)
            .discovery(false)
            .tick_rate(0)
            .build(t)
    };
    let sa = build(1, ta);
    let sb = build(2, tb);
    let _serve = sa.transport().clone().serve();
    let addr_a = sa.transport().local_addr().unwrap();
    sb.transport()
        .connect(addr_a, receiver.cert())
        .await
        .unwrap();
    (sa, sb, addr_a)
}

/// `from`'s Hello accepted by `to`; the address `to` knows `from` by
async fn hello(from: &Session, to: &Session, to_addr: SocketAddr) -> SocketAddr {
    from.handshake(to_addr, WorldHash(0x7a11)).await.unwrap();
    match timeout(WAIT, to.recv_frame()).await.unwrap().unwrap() {
        Frame::Message(env) => {
            assert!(
                matches!(env.message, Message::Hello { .. }),
                "expected the Hello"
            );
            env.from
        }
        other => panic!("expected the Hello, got {other:?}"),
    }
}

fn refused_before_handshake<T: std::fmt::Debug>(r: Result<T, TransportError>) {
    match r {
        Err(TransportError::Handshake(HandshakeRefused::NotHandshaken { .. })) => {}
        other => panic!("bytes before the handshake were not refused: {other:?}"),
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_signed_law_is_carried_evaluated_stored_and_restored_bit_for_bit() {
    let receiver = device(1, "receiver");
    let sender = device(2, "sender");
    let mut compared = Compared::default();

    // 1-2. the sender names the law and signs it under its certificate
    let law_ref = program_law_ref(PROGRAM).unwrap();
    let law_ref_bytes = law_ref.to_bytes();
    let sent_values = evaluate(&law_ref);
    let unit = SignedArtifact::sign(
        &sender.device_key(),
        &sender.cert(),
        CONTEXT,
        &law_ref_bytes,
        PROGRAM.as_bytes(),
    )
    .unwrap();
    let message = unit.to_bytes();

    // (i) a parameter of the law changed after signing, before it is sent: refused by the signature
    let tampered_before_send = replace_in_law_ref(&message, b"10.0", b"11.0");
    assert!(
        matches!(
            open(&tampered_before_send, &sender),
            Err(ArtifactError::DigestMismatch)
        ),
        "a law changed after signing was accepted"
    );
    compared
        .0
        .entry("refused")
        .and_modify(|n| *n += 1)
        .or_insert(1);

    // 3. the channel, and a session on each side
    let (sa, sb, addr_a) = pair(&receiver, &sender).await;

    // 4. bytes before the handshake are refused both ways
    refused_before_handshake(sb.send_bytes(addr_a, &message).await);
    sb.transport()
        .send_bytes_to(&message, addr_a)
        .await
        .unwrap();
    refused_before_handshake(timeout(WAIT, sa.recv_frame()).await.unwrap());

    let from_b = hello(&sb, &sa, addr_a).await;
    assert_eq!(hello(&sa, &sb, from_b).await, addr_a);
    assert_eq!(sa.peer_status(from_b).await, Some(IdStatus::Current));
    assert_eq!(sb.peer_status(addr_a).await, Some(IdStatus::Current));
    sb.send_bytes(addr_a, &message).await.unwrap();
    let received = match timeout(WAIT, sa.recv_frame()).await.unwrap().unwrap() {
        Frame::Bytes { bytes, from } => {
            assert_eq!(from, from_b);
            bytes
        }
        other => panic!("expected the law, got {other:?}"),
    };
    compared.bits("carried bytes", &message, &received);

    // 5. the receiver checks the signature, rebuilds the law and evaluates it
    let (verified, got_law) = open(&received, &sender).unwrap();
    compared.bits("law reference", &law_ref_bytes, verified.law_ref());
    compared.bits("payload", PROGRAM.as_bytes(), verified.payload());
    compared.bits("law identifier", law_ref.law_id(), got_law.law_id());
    compared.bits(
        "law semantics identifier",
        law_ref.semantics_id(),
        got_law.semantics_id(),
    );
    let got_values = evaluate(&got_law);
    assert_eq!(got_values.len(), grid().len());
    // the values are the program's, not a constant: the evaluator agrees with the signs the
    // program fixes, and the grid gives many distinct values
    let shape = resolve_program(&got_law).unwrap();
    for ((x, y, z), inside) in SIGNS {
        let d = alice_lol::eval(&shape, Vec3::new(x, y, z));
        assert_eq!(
            d < 0.0,
            inside,
            "the law's value at ({x}, {y}, {z}) is {d}: not the program's shape"
        );
        compared
            .0
            .entry("sign from the program")
            .and_modify(|n| *n += 1)
            .or_insert(1);
    }
    // the grid is symmetric about y = 0 and partly about x = 0, so values repeat; a constant would not
    let distinct: std::collections::BTreeSet<u32> = got_values.iter().copied().collect();
    assert!(
        distinct.len() > 1,
        "every value of the grid is the same: {distinct:?}"
    );
    for (a, b) in sent_values.iter().zip(&got_values) {
        compared.bits("value", &a.to_le_bytes(), &b.to_le_bytes());
    }

    // the refusals on receipt: a flipped signature byte, a flipped law reference byte
    let mut tampered = received.clone();
    let last = tampered.len() - 1;
    tampered[last] ^= 1;
    // the last bytes of the unit are the Ed25519 signature: only the signature check refuses it
    let verdict = open(&tampered, &sender).map(|_| ());
    assert!(
        matches!(verdict, Err(ArtifactError::BadSignature)),
        "a tampered signature verified, or was refused for another reason: {verdict:?}"
    );
    compared
        .0
        .entry("refused")
        .and_modify(|n| *n += 1)
        .or_insert(1);

    // 6. store, write to bytes, open again
    let identifiers: [(&str, [u8; 32]); 5] = [
        ("identity", alice_identity::semantics::IDENTITY_SEMANTICS_ID),
        ("sign", alice_sign::semantics::SIGN_SEMANTICS_ID),
        ("sync", alice_sync::semantics::SYNC_SEMANTICS_ID),
        ("db", alice_db::semantics::DB_SEMANTICS_ID),
        (
            "lol geometry",
            alice_lol::geometry_id::LOL_GEOMETRY_SEMANTICS_ID,
        ),
    ];
    let db = AliceDB::in_memory(StorageConfig::default()).unwrap();
    // the signed unit is stored as it was received; values are not stored as truth (they are
    // evaluated again from the restored law)
    db.put_blob(b"artifact", &received).unwrap();
    for (name, id) in &identifiers {
        db.put_blob(format!("semantics/{name}").as_bytes(), id)
            .unwrap();
    }
    let restored = AliceDB::from_bytes(StorageConfig::default(), &db.to_bytes().unwrap()).unwrap();

    // 7. compare what was restored with what was sent
    let stored = restored.get_blob(b"artifact").unwrap().unwrap();
    compared.bits("restored unit", &message, &stored);
    // the unit read back goes through the same gate as a received one
    let (restored_unit, restored_ref) = open(&stored, &sender).unwrap();
    compared.bits(
        "restored law reference",
        &law_ref_bytes,
        restored_unit.law_ref(),
    );
    compared.bits(
        "restored law identifier",
        law_ref.law_id(),
        restored_ref.law_id(),
    );
    compared.bits(
        "restored re-evaluation",
        &values_bytes(&sent_values),
        &values_bytes(&evaluate(&restored_ref)),
    );

    // (ii) the stored row rewritten consistently: another program, its reference well formed with
    // its identifier recomputed (the identifier is an unkeyed hash, anyone can compute it), kept
    // with the old signature. It parses as a law reference, so only the signature can refuse it
    let other_program = PROGRAM.replace("10.0", "99.0");
    let other_ref = program_law_ref(&other_program).unwrap();
    assert!(
        LawRef::from_bytes(&other_ref.to_bytes()).is_ok(),
        "the rewritten reference must parse on its own"
    );
    let old_signature = &stored[stored.len() - restored_unit.signature().to_bytes().len()..];
    let rewritten = unit_bytes(
        &other_ref.to_bytes(),
        other_program.as_bytes(),
        old_signature,
    );
    let tampered_db = AliceDB::in_memory(StorageConfig::default()).unwrap();
    tampered_db.put_blob(b"artifact", &rewritten).unwrap();
    let reread =
        AliceDB::from_bytes(StorageConfig::default(), &tampered_db.to_bytes().unwrap()).unwrap();
    let verdict = open(&reread.get_blob(b"artifact").unwrap().unwrap(), &sender).map(|_| ());
    assert!(
        matches!(verdict, Err(ArtifactError::DigestMismatch)),
        "a rewritten stored artifact was accepted, or refused for another reason: {verdict:?}"
    );
    compared
        .0
        .entry("refused")
        .and_modify(|n| *n += 1)
        .or_insert(1);
    for (name, id) in &identifiers {
        let got = restored
            .get_blob(format!("semantics/{name}").as_bytes())
            .unwrap()
            .unwrap();
        compared.bits("semantics identifier", id, &got);
    }

    for (kind, n) in &compared.0 {
        println!("trust path: compared {n} {kind}");
    }
    let total: usize = compared.0.values().sum();
    assert_eq!(
        compared.0.get("value"),
        Some(&grid().len()),
        "not every value was compared"
    );
    assert_eq!(
        compared.0.get("semantics identifier"),
        Some(&identifiers.len())
    );
    assert_eq!(compared.0.get("sign from the program"), Some(&SIGNS.len()));
    assert_eq!(
        compared.0.get("refused"),
        Some(&3),
        "not every tamper was tried"
    );
    assert!(total > 0, "nothing was compared");
    println!("trust path: {total} comparisons, 0 differences");
}
