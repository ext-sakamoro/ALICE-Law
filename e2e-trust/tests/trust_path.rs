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
use alice_identity::artifact::{
    sha256_payload, sign_artifact, verify_artifact, ArtifactError, ArtifactSignature, PayloadDigest,
    Revocation,
};
use alice_identity::device_cert::DeviceCert;
use alice_identity::did::Did;
use alice_lol::program_law::{program_law_ref, resolve_program};
use alice_lol::Vec3;
use alice_sign::{hash_data, KeyPair, PublicKey};
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

/// The digest the signature covers: the law reference and the payload together, so the same
/// payload under another law reference has another signature
fn bound_digest(law_ref: &[u8], payload: &[u8]) -> PayloadDigest {
    let mut both = law_ref.to_vec();
    both.extend_from_slice(sha256_payload(payload).bytes());
    PayloadDigest::sha256(hash_data(&both).0)
}

/// The points the law is evaluated at: inside, on and outside both parts
fn grid() -> Vec<Vec3> {
    let mut out = Vec::new();
    for x in [-12.0f32, -10.0, -5.0, 0.0, 5.0, 9.5, 10.0, 12.0, 14.0, 16.0, 20.0] {
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
    grid().into_iter().map(|p| alice_lol::eval(&shape, p).to_bits()).collect()
}

/// `u32 length || bytes` for each part
fn frame(parts: &[&[u8]]) -> Vec<u8> {
    let mut out = Vec::new();
    for p in parts {
        out.extend_from_slice(&u32::try_from(p.len()).unwrap().to_le_bytes());
        out.extend_from_slice(p);
    }
    out
}

fn unframe(mut bytes: &[u8]) -> Vec<Vec<u8>> {
    let mut parts = Vec::new();
    while !bytes.is_empty() {
        let (len, rest) = bytes.split_at(4);
        let len = u32::from_le_bytes(len.try_into().unwrap()) as usize;
        parts.push(rest[..len].to_vec());
        bytes = &rest[len..];
    }
    parts
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
    let build = |node, t| SessionBuilder::new().node_id(node).discovery(false).tick_rate(0).build(t);
    let sa = build(1, ta);
    let sb = build(2, tb);
    let _serve = sa.transport().clone().serve();
    let addr_a = sa.transport().local_addr().unwrap();
    sb.transport().connect(addr_a, receiver.cert()).await.unwrap();
    (sa, sb, addr_a)
}

/// `from`'s Hello accepted by `to`; the address `to` knows `from` by
async fn hello(from: &Session, to: &Session, to_addr: SocketAddr) -> SocketAddr {
    from.handshake(to_addr, WorldHash(0x7a11)).await.unwrap();
    match timeout(WAIT, to.recv_frame()).await.unwrap().unwrap() {
        Frame::Message(env) => {
            assert!(matches!(env.message, Message::Hello { .. }), "expected the Hello");
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
    let digest = bound_digest(&law_ref_bytes, PROGRAM.as_bytes());
    let signature = sign_artifact(&sender.device_key(), &sender.cert(), CONTEXT, &digest).unwrap();
    let signature_bytes = signature.to_bytes();
    let message = frame(&[&law_ref_bytes, &signature_bytes, PROGRAM.as_bytes()]);

    // 3. the channel, and a session on each side
    let (sa, sb, addr_a) = pair(&receiver, &sender).await;

    // 4. bytes before the handshake are refused both ways
    refused_before_handshake(sb.send_bytes(addr_a, &message).await);
    sb.transport().send_bytes_to(&message, addr_a).await.unwrap();
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
    let parts = unframe(&received);
    assert_eq!(parts.len(), 3);
    let (got_ref, got_sig, got_payload) = (&parts[0], &parts[1], &parts[2]);
    let got_signature = ArtifactSignature::from_bytes(got_sig).unwrap();
    let got_digest = bound_digest(got_ref, got_payload);
    verify_artifact(&got_signature, &sender.master.public(), CONTEXT, &got_digest, NOW, Revocation::NoListAvailable)
        .unwrap();
    let got_law = LawRef::from_bytes(got_ref).unwrap();
    compared.bits("law identifier", law_ref.law_id(), got_law.law_id());
    compared.bits("law semantics identifier", law_ref.semantics_id(), got_law.semantics_id());
    let got_values = evaluate(&got_law);
    assert_eq!(got_values.len(), grid().len());
    // the values are the program's, not a constant: the evaluator agrees with the signs the
    // program fixes, and the grid gives many distinct values
    let shape = resolve_program(&got_law).unwrap();
    for ((x, y, z), inside) in SIGNS {
        let d = alice_lol::eval(&shape, Vec3::new(x, y, z));
        assert_eq!(d < 0.0, inside, "the law's value at ({x}, {y}, {z}) is {d}: not the program's shape");
        compared.0.entry("sign from the program").and_modify(|n| *n += 1).or_insert(1);
    }
    // the grid is symmetric about y = 0 and partly about x = 0, so values repeat; a constant would not
    let distinct: std::collections::BTreeSet<u32> = got_values.iter().copied().collect();
    assert!(distinct.len() > 1, "every value of the grid is the same: {distinct:?}");
    for (a, b) in sent_values.iter().zip(&got_values) {
        compared.bits("value", &a.to_le_bytes(), &b.to_le_bytes());
    }

    // the refusals: a flipped signature byte, a flipped law reference byte
    let mut tampered = got_sig.clone();
    let last = tampered.len() - 1;
    tampered[last] ^= 1;
    // the last bytes of the encoding are the Ed25519 signature: only the signature check refuses it
    let tampered = ArtifactSignature::from_bytes(&tampered).unwrap();
    let verdict =
        verify_artifact(&tampered, &sender.master.public(), CONTEXT, &got_digest, NOW, Revocation::NoListAvailable);
    assert!(
        matches!(verdict, Err(ArtifactError::BadSignature)),
        "a tampered signature verified, or was refused for another reason: {verdict:?}"
    );
    // one digit of the program changed inside the reference: the bytes still parse, and only the
    // identifier recomputed from them can refuse it
    let mut tampered_ref = got_ref.clone();
    let at = tampered_ref.windows(4).position(|w| w == b"10.0").expect("the program is in the reference");
    tampered_ref[at + 1] = b'1';
    assert!(LawRef::from_bytes(&tampered_ref).is_err(), "a tampered law reference was accepted");
    // and the signature binds the reference: the untouched signature does not cover the same payload
    // under the changed reference
    let rebound = bound_digest(&tampered_ref, got_payload);
    let verdict =
        verify_artifact(&got_signature, &sender.master.public(), CONTEXT, &rebound, NOW, Revocation::NoListAvailable);
    assert!(
        matches!(verdict, Err(ArtifactError::DigestMismatch)),
        "the signature covered another law reference: {verdict:?}"
    );

    // 6. store, write to bytes, open again
    let identifiers: [(&str, [u8; 32]); 5] = [
        ("identity", alice_identity::semantics::IDENTITY_SEMANTICS_ID),
        ("sign", alice_sign::semantics::SIGN_SEMANTICS_ID),
        ("sync", alice_sync::semantics::SYNC_SEMANTICS_ID),
        ("db", alice_db::semantics::DB_SEMANTICS_ID),
        ("lol geometry", alice_lol::geometry_id::LOL_GEOMETRY_SEMANTICS_ID),
    ];
    let db = AliceDB::in_memory(StorageConfig::default()).unwrap();
    db.put_blob(b"signature", got_sig).unwrap();
    db.put_blob(b"law_ref", got_ref).unwrap();
    db.put_blob(b"values", &values_bytes(&got_values)).unwrap();
    for (name, id) in &identifiers {
        db.put_blob(format!("semantics/{name}").as_bytes(), id).unwrap();
    }
    let restored = AliceDB::from_bytes(StorageConfig::default(), &db.to_bytes().unwrap()).unwrap();

    // 7. compare what was restored with what was sent
    compared.bits("restored signature", &signature_bytes, &restored.get_blob(b"signature").unwrap().unwrap());
    let restored_ref = LawRef::from_bytes(&restored.get_blob(b"law_ref").unwrap().unwrap()).unwrap();
    compared.bits("restored law identifier", law_ref.law_id(), restored_ref.law_id());
    compared.bits("restored values", &values_bytes(&sent_values), &restored.get_blob(b"values").unwrap().unwrap());
    compared.bits("restored re-evaluation", &values_bytes(&sent_values), &values_bytes(&evaluate(&restored_ref)));
    for (name, id) in &identifiers {
        let got = restored.get_blob(format!("semantics/{name}").as_bytes()).unwrap().unwrap();
        compared.bits("semantics identifier", id, &got);
    }

    for (kind, n) in &compared.0 {
        println!("trust path: compared {n} {kind}");
    }
    let total: usize = compared.0.values().sum();
    assert_eq!(compared.0.get("value"), Some(&grid().len()), "not every value was compared");
    assert_eq!(compared.0.get("semantics identifier"), Some(&identifiers.len()));
    assert_eq!(compared.0.get("sign from the program"), Some(&SIGNS.len()));
    assert!(total > 0, "nothing was compared");
    println!("trust path: {total} comparisons, 0 differences");
}
