#!/usr/bin/env python3
"""Check the text this public repository publishes for internal vocabulary.

The vocabulary is the one of ALICE-Physics `scripts/docs_lint.py` (copied; keep the
two lists the same): development-process words, internal tracker and note names,
instruction sources, device names, private network addresses, and private names
held only as HMAC-SHA256 (the name key) of lower-case 1-, 2- and 3-word phrases; id-like tokens
next to a word naming a review or a session, and review labels.

    public_text_check.py --tree [REF]        every tracked file at REF (default HEAD):
                                             its path and its text
    public_text_check.py --range BASE..HEAD  the commits in the range: each message,
                                             each added line and each added path
    public_text_check.py --allow-hash PATH NAME
                                             the allow-list entry for a product's mention
                                             of NAME in PATH (see ALLOWED)

A finding prints the place and the kind of word, never the word itself (the output of
CI is public too). This file and its tests hold the patterns and are not scanned.
Exit 0 when nothing is found, 1 on a finding, 2 when nothing was compared (no file,
no commit) or git failed.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import subprocess
import sys
import unicodedata

EXEMPT = {"scripts/public_text_check.py", "scripts/test_public_text_check.py"}

# ---------------------------------------------------------------- the name key
#
# The private names this check looks for (repository names, project names, session ids, host names) are short
# and could be recovered from a plain SHA-256 by trying names, so each table holds HMAC-SHA256 under a key that
# is not in this repository: ALICE_PRIVATE_NAME_HMAC_KEY (64 hex characters) from the environment, else the
# file NAME_KEY_FILE. ALICE-Law's own jobs pass --require-private-names and fail without the key; a repository
# that runs this file without the key gets every other check and a printed notice that the private-name checks
# did not run (ALICE-Law runs them over every public repository daily). The key is never printed.
NAME_KEY_ENV = "ALICE_PRIVATE_NAME_HMAC_KEY"
NAME_KEY_FILE = os.path.join("~", ".config", "alice", "private_name_hmac_key")
NAME_KEY: dict = {"key": None, "read": False}


def load_name_key() -> bytes | None:
    """the key from the environment or the key file, None when neither is set; a value that is not 64 hex
    characters is a ValueError that does not quote the value"""
    if NAME_KEY["read"]:
        return NAME_KEY["key"]
    raw = os.environ.get(NAME_KEY_ENV)
    where = NAME_KEY_ENV
    if not raw:
        path = os.path.expanduser(NAME_KEY_FILE)
        where = NAME_KEY_FILE
        try:
            with open(path, encoding="ascii") as f:
                raw = f.read()
        except (OSError, UnicodeDecodeError):
            raw = None
    key = None
    if raw:
        raw = raw.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", raw):
            raise ValueError(f"the private name key in {where} is not 64 hex characters")
        key = bytes.fromhex(raw)
    NAME_KEY.update(key=key, read=True)
    return key


def private_name_hit(phrase: str) -> bool:
    """a lower-case phrase is a private name: its HMAC is in PRIVATE_NAME_HASHES, or (until the set is empty) its
    plain SHA-256 is in LEGACY_PRIVATE_NAME_SHA256"""
    return name_mac(phrase) in PRIVATE_NAME_HASHES or \
        hashlib.sha256(phrase.encode("utf-8")).hexdigest() in LEGACY_PRIVATE_NAME_SHA256


def name_mac(text: str) -> str | None:
    """HMAC-SHA256 of text under the name key (hex), None without a key"""
    key = load_name_key()
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest() if key else None
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")

# ---------------------------------------------------------------- vocabulary (from docs_lint.py)

A = r"(?<![A-Za-z])"
Z = r"(?![A-Za-z])"


# A product's own package in the "skill" format lives in one directory of its repository; a path that starts
# with that directory (`skills/<its dir>/...`, in any file of that repository) names the package, not the
# process. Each entry is sha256(<repository> NUL <path prefix>), so the list names neither; every other
# `skills/<x>/` path, any such path in another repository, and the word in prose stay findings.
OWN_SKILL_PREFIX_HASHES = {
    "504b446ca023c96625390e13d4f5c8fab91eeb9faf7dc544711175be6a6c95d7",
    "5affd913400e5451bef38e69584e1856d51894ed426240de2e9d5e0fdcb11d85",
    "88b5b20ada429bc45e512c17ae1ee8659d82cadd1ddb2c8cfdddc4f747fc2209",
}
# (at the start of a path, or after "./" or "../" steps: a relative link to the same directory)
SKILL_PREFIX_RE = re.compile(r"(?:(?<![A-Za-z0-9_./-])|(?<=\.\./)|(?<=(?<![A-Za-z0-9_.-])\./))skills(/[A-Za-z0-9._-]+/)")


def own_skill_prefixes(line: str) -> list[tuple[int, int]]:
    """the spans of the paths in `line` that start with the scanned repository's own skill directory"""
    repo = ALLOW_STATE.get("repo")
    if not repo:
        return []
    return [m.span() for m in SKILL_PREFIX_RE.finditer(line)
            if hashlib.sha256(f"{repo}\0skills{m.group(1)}".encode("utf-8")).hexdigest() in OWN_SKILL_PREFIX_HASHES]


def words(*alts: str) -> str:
    return A + "(?:" + "|".join(alts) + ")" + Z


FORBIDDEN = [
    ("agent process", re.compile(
                                 # a programming term is not the process: the pool-size arguments of
                                 # Python's executors and data loaders (a closed list) and a thread
                                 # ("worker thread(s)"); any other identifier stays a finding
                                 # (any case, as a word; not inside an identifier such as `SKILL_DIR`, not
                                 # the Agent Skills file name `SKILL.md`, not the product "Cloudflare Workers")
                                 r"(?<![A-Za-z])(?<!max_)(?<!num_)(?<!n_)(?<!Cloudflare )(?i:workers?)(?![A-Za-z])"
                                 r"(?!\s+(?i:threads?)(?![A-Za-z]))(?!_threads\()|"
                                 r"(?<![A-Za-z_])(?i:subagents?|multi[- ]?agents?|skills?)(?![A-Za-z_])(?<!SKILL(?=\.md))"
                                 # `git worktree add` is a command, not a description of how the work was done; so is
                                 # the same subcommand as an argument list (`[*git_argv, "worktree", "add", ...]`): the
                                 # quoted word followed by a quoted worktree subcommand
                                 + r"|(?<!git )" + A + r"(?!(?<=[\"'])worktree[\"']\s*,\s*[\"']"
                                 + r"(?:add|list|lock|move|prune|remove|repair|unlock)[\"'])" + r"(?:worktrees?)" + Z
                                 + r"|ワーカー|マルチエージェント|エージェント|調停役|並走\s*session|他\s*session|session\s*名|この\s*(?:session|セッション)"
                                 # the same in English prose ("This session's tooling", "asked in chat")
                                 # (not the start of an identifier: `chat_template`, `session_id`, `chat2`)
                                 + r"|(?<![A-Za-z0-9_])(?i:this\s+session|in\s+chat)(?![A-Za-z0-9_])")),
    # "session" as a unit of the work: glued after Japanese ("次 session", "別 session" in a
    # table cell), at the start of a line or heading followed by Japanese ("Session 分割案"),
    # "multi-session", and a lettered session ("Session A (~1h)"); "checkout session 作成" or
    # "OpenXR Session 管理" (a word of the product before it) is not one
    ("work session", re.compile(r"[\u3005\u3040-\u30ff\u4e00-\u9fff]\s*(?i:sessions?)(?![A-Za-z0-9_.(:])"
                                r"|^[\s#*>|/\-]*(?i:sessions?)\s*[\u3005\u3040-\u30ff\u4e00-\u9fff]"
                                r"|(?<![A-Za-z])(?i:multi-sessions?)(?![A-Za-z])"
                                r"|(?<![A-Za-z])(?i:session)\s+[A-Z]\s*\(")),
    ("session name", re.compile(r"(?<![A-Za-z0-9])(?:ys|sakamoro)-[0-9a-f]{2}(?![A-Za-z0-9])")),
    # the frontmatter of a private note
    ("internal note", re.compile(r"^\s*node_type\s*:\s*memory\b|(?<![A-Za-z])originSessionId(?![A-Za-z])")),
    ("internal tracker", re.compile(words(r"Backlog") + r"|memory\s*(?:側|化|に|へ|file)|(?<![A-Za-z0-9_.])memory/")),
    # a link to a private note: any `[[name]]` with an underscore or a hyphen, in any case
    # (a single word such as `[[gate]]` names a TOML table in prose); a TOML
    # array-of-tables header at the start of a line in a .toml file is not one (kinds())
    ("note link", re.compile(r"\[\[[A-Za-z][A-Za-z0-9.]*[_\-][A-Za-z0-9_.\-]*\]\]")),
    # names of private notes in prose: the note-file prefixes, "memory `<name>`", `<name>.md`
    # (a bare `feedback_…` / `success_…` name without `.md` is NOTE_NAME_RE: in a code file it is read
    # only inside a comment or a string, since a field such as `success_count` is code, not a note)
    ("internal note", re.compile(r"(?<![A-Za-z0-9_])(?:feedback|discipline|success|handoff)_[a-z0-9_]{4,}\.md"
                                 r"|(?<![A-Za-z0-9_])(?:project|reference)_alice_[a-z0-9_]+"
                                 r"|(?<![A-Za-z0-9_])(?:project|reference)_[a-z0-9_]{4,}\.md"
                                 r"|(?<![A-Za-z\-])(?i:memory|memo)\s*`[A-Za-z0-9_\-]+(?:\.md)?`"
                                 # numbered entries of an internal pitfall catalogue / CI template
                                 r"|罠\s*#\s*\d+|canonical\s+(?:CI\s+)?template")),
    ("instruction source", re.compile(r"user\s*(?:指示|裁定|指摘|判断)")),
    # a user's home directory names the user ("/Users/<name>/", "/home/<name>/"); placeholders
    # and the CI runner's home are not a name
    # a path token only: at the start of a line or after whitespace, a quote, `=`, a bracket, `:`,
    # `,`, `;` or a backquote, or right after `file://` (a local file URL); not inside a URL
    # (`https://vrchat.com/home/world/...`)
    ("home path", re.compile(r"(?:^|(?<=[\s\"'=(\[`<,;:])|(?<=file://))/(?:Users|home)/(?!(?:runner|user|USER|username|you|me|name|Shared|<[^>/]+>|\$\{?[A-Za-z_]+\}?)/)"
                             r"[A-Za-z0-9._\-]+/")),
    # (any case: a host name is often the device name in lower case)
    ("device", re.compile(r"(?i:" + words(r"jetson", r"orin[\s-]*(?:nano|nx|agx)", r"mac\s?mini", r"macbook")
                          + r"|raspberry[\s_-]*pi)"
                          # the short names of the board, with a model number or none, as a whole
                          # token: no letter on either side, or a camelCase boundary (lower → Upper
                          # before it, a letter-to-Upper step after it), so l2_rpi5, L2-RasPi5,
                          # budgetRpi5 and RpiCache are found and grasping, rpix and RPIX are not
                          + r"|(?:(?<![A-Za-z])|(?<=[a-z])(?=[A-Z]))(?i:ras\s?pi\d*|rpi\d*)"
                          + r"(?:(?![A-Za-z])|(?<![A-Z])(?=[A-Z]))")),
    # the SDK / firmware of such a device ("JetPack 6", "JetPack R36", "L4T"); Android's
    # "Jetpack Compose" has no version and is not one
    ("device detail", re.compile(r"(?i)(?<![A-Za-z])(?:jetpack\s*(?:r?\d|sdk)|l4t)(?![A-Za-z])")),
    ("private address", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b|\b192\.168\.\d{1,3}\.\d{1,3}\b|\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
]

# Names of unrelated private projects and of internal infrastructure are not written
# here in plain text: this file
# is public, and a list of names would itself disclose them. Each entry is the
# HMAC-SHA256 (the name key) of a lower-case word, or of a two- or three-word phrase joined by one
# space; documents are tokenised and every 1-, 2- and 3-gram is hashed and looked up.
# Owned equipment and private network names (generic products stay in the
# `device` pattern above); hashed like the private names below and merged into them.
DEVICE_NAME_HASHES = {
    "016bcf64509077fe070b377615c8e3c61e6e76bf11bfcbe72b8aa7e83790ea12",
    "59615b02f6101fcf2d889deb03fb99b6b2782107c025d3ebe34d5c96293917a8",
    "5a2561359700450deaf345674a2e77fa7a21ec713515661d8300cdeda0eca924",
    "806fd20e99bc62e058db6fdf9d123785d18c3ffd0f6fe9c4a737a5e0011eb53c",
    "9ee2abdc24254df0d5b80eec5c495a40a909054db7298fb9c88954f876591cc7",
}
PRIVATE_NAME_HASHES = DEVICE_NAME_HASHES | {
    "047e4d6bac3b071068b3878286dd73921c5255ea03ca15c53869edcb4bac051c",
    "0f2ba8c3566cc881eeda8886dd15103ac7148366864bb832f3b482ef824bee43",
    "11ac19f49d851221a8235452c5bf7ede6f1f151f1428ea3d4cd92f6fa8b60c1b",
    "1915fa2af89d6fca1ff241d607dc917d0e92d49d81ae0b1e2d6111d09857fdc4",
    "24e1a28fdafefc850e91b10dfa8fbb110a541048c82744643ebdba804a7a0e7a",
    "25aa4359e38ab7219643f814776d8cfe3c204d8a6c2ec84977cb1a7de998eb9b",
    "288bfa0757ee2c56a177c2f4f104309be3377dadd9ade29fb1ff7190d79034fd",
    "2b45487ea964ba803109514fb6bf2597e25fbebb56d75884e5ec4a59d744744a",
    "3cde8d93fa7e4affad045b046c218b68c3bb8799353379fe81a3d42d502d86df",
    "40548fbade4381ca1bd259cbc242bd742884f73e4a252165db7657a371730a46",
    "41c90f0303518fb3f0184a938b9a9cf433dddb568bde0c5b73fa59d0e4424e80",
    "46749497a6df43c879b0f4f203fb8aaac0aacc3cdd7f36fce77077778067d982",
    "4985f9d60a2d9505751cba9ffe211bafb4efe2d1239e04a8c89a5218f2661ded",
    "4adbb142bc5d82e40d401d0e1ea878c77c6ff00e5f4ff3e9cb9c37d21c7c43f4",
    "4d19837b84fcd2561556529a0f03a5ccca51a3660c5d143a9ad4364ae0ac72d5",
    "50ccad39c8ce855cd0fdead34fd6cc883822d56e027f7c5fb98ca2cdf1ccd7b8",
    "561d01d0d8868619c59e990e9d1f46332f815bff728c2c3fe07eaaab44583c9b",
    "5a6054a7fa2d5c789535c0d68e02ab72eb49f65bea5478053692b75cc65d6772",
    "64c806b9f097509d223b5ebf69e1376eb222afbab167badf407ec51e70dfe06e",
    "6882cdf35d2adacd497f761de02d6327055a3a1284016e84b7593f786f3a3b5f",
    "689527f750fc3db93bd135fd2de28351ed2814463232325f24b60f89b7495144",
    "698018697de442b2fa5b8758de387f0ef664526fc56e5c56083f77f59f2cfefa",
    "6a0e4181c60f1c8eba1e115292ccf13a0a212ed49a7845337bc7407f4a9d1f71",
    "747b8475f91fafa060ab3873fc63eff25e5233a908758cbddad40a46fc1ee44a",
    "7d99b6a0f0989b47834e2634209af8cb4df955fa156bad62ea2c1a029caac289",
    "83976bc6e1350481d26206d958cc88b0fae2c9831d71bdf6aa03505c3ad224eb",
    "8524c36b956770f2a73c20e710f7494308e39a7cd5daed3942c569988fb3f0df",
    "93c52deabf8a537387dbe342739e76794cfca1a370319a133bb0ad0328f2121c",
    "9d79140b7058f61a14b16b90c1f18a934aced26fdc23e237c55270617f8c9ff6",
    "a141bc6e17b4ee8b036f7483faa9fca3fccead8d5ccbf1adda1ad2e89ebcf6bc",
    "ac954b6429081a6a1c3e99154595d663c070e47d1487a088d9c3e98d17dfdde0",
    "b11f2ae04b539d0bf293a4b47c5ecb3e208c7b5a07c0d7d7ebdb82fa1d6d49a6",
    "b4e1e3a6342786b1f95b29c98f942105186a15322070acc06edff99703310834",
    "b6e69f26b7c9279d8381a23a95beca30ceefc1525259ff2cbe2f1a92aa93201d",
    "bf9cec599bca56136bd05ddf041de658a280535d318f0ffc5c8909a52be59549",
    "ce5bd28ae726b65f706e4acfd99efa307d6b816d95a1d970b1083cd6ddc7b826",
    "d070ae1c45571c22ab3305943c05f16315cd7d48fc64bb442a8e6403e22b06d7",
    "d091455a56a178bfc0d249266383d0e11d38e6c48d2023d5f7227e0e5dcbc4d0",
    "d244eb3c42bb66eb62597cbfea92f01727381bc52bb46409107bf1c207ce8de4",
    "dd3e73c2b194cdf0c8d24b49edc18181631f3c58fca4ab819d16b05364452c21",
    "df66c9bbf65e4ff39426969b96869bb89e328939c485dd3f9a8fa60f295269fd",
    "ea0cda6840e68c9b5f7c3376c9a207f915d720d0696ae3455a8b0d353ee4fe81",
    "f05bb272166767ac3db9e19faa8cca078774217a9251cd8a68258cd8c0b3996b",
    "f276c388f1cfb261be9327c8b576213d503931966fc912426eb0241eff8df82c",
    "f4612dc54632ae162d19b7ce53ad15e623782fd739cb8a03c340243881e50ab2",
}
# TEMPORARY: the private names whose plain text could not be found again when the tables moved to HMAC
# (2026-11-12). They stay as plain SHA-256 until each is supplied (and moves to the HMAC set) or dropped; this
# set may only shrink (test_name_key.py pins it), and nothing is ever added to it. Already public in history.
LEGACY_PRIVATE_NAME_SHA256: frozenset[str] = frozenset({
    "18eaed5cd72e74361e9c620a5b1df3f93cc34007f9c288abf27802d52d101506",
    "19f798ce9f70706dd4334369cbf056e51922830d480360b8ace258e818ba7dd4",
    "6faef1df454754cb29df4e7e91bf6e37f64060ba852cb61d9f201f7b2a96b6bc",
    "8653d44dc6fd3b625cba9865fc10f6b8d2d9d871d5da9e3624bc6d87179ed8f1",
    "c379346cda6119421f42651914a8871e340094d8f5b7980335354e8b70028c28",
    "c96267f290f76ac40a777ee9d90bc6d2198bb4a1632a987d8411f112d5e42286",
    "de735c5c8d8ee5f8ba0468a624fd847d37591b8f112579e7fc5216760088221c",
})
# The short ids the work sessions go by are two hex digits ("6e"-like). Alone they are
# ordinary numbers and words, and a list of them, even hashed, would be public (256 values
# are enumerated at once), so this public check holds no id: an id-like token is a finding
# only next to a word that names a review or a session ("レビュー", "review", "指摘",
# "検証", "session" / "セッション" before or after it). A token glued to a digit, letter,
# "#", "_", "-" or "." (a date, a version, a colour, x86_64), or followed by "_" (the start of an
# identifier: `fb_open()`), is not id-like. The contexts
# "<id>の" and "<id>'s" are left to a check that knows the ids: here they matched counts,
# dates and "oracle 10's" far more often than ids.
ID = r"(?<![A-Za-z0-9#_.\-])[0-9a-f]{2}(?![A-Za-z0-9_])"
IDS = r"[(\[]?" + ID + r"(?:\s*/\s*" + ID + r")*[)\]]?"  # "fb", "(6e/fb)"
REVIEW_WORD = r"(?:レビュー|(?i:review)|指摘|検証|セッション|(?i:session))"
# "be" before "review" is the English verb ("cannot be reviewed"), not an id
SESSION_ID_RE = re.compile(r"(?!be\s+(?i:review))" + IDS + r"\s*[、,]?\s*(?:の\s*)?" + REVIEW_WORD
                           + r"|(?<![A-Za-z])(?i:sessions?)\s*[:=]?\s*" + ID + r"|セッション\s*[:=：]?\s*" + ID)
# labels of review findings ("F1", "F2 / F3") named as such
REVIEW_LABEL_RE = re.compile(r"(?<![A-Za-z0-9])F\d+(?:\s*/\s*F\d+)*\s*(?:の\s*)?(?:レビュー|指摘|(?i:review))")


# the row labels of an internal list ("F7", "R2", "C3", "A1", "B4": a letter and 1-2 digits) are
# findings only next to a word that names the list or its row: "canonical", "ledger", "tracker",
# "台帳", "項目", "行目", or "行" standing alone (not inside "行列" or "実行"), with at most 20
# characters between them on one clause. Alone such labels are ordinary (1,167 occurrences in
# the commit messages and files of the 8 release-train repositories: phases, Mooney-Rivlin
# constants, rotations, table rows); next to the closed list of words above only the internal
# label of one commit message was found. The English "row" and
# "item" were left out: their 5 occurrences included a public table's own row ("the C2 row of
# the table in §1.3"). The gate ids G1..G14 are public and the letter class has no G; the float
# widths F16 / F32 / F64 are types, not labels.
LEDGER_LABEL = r"(?<![A-Za-z0-9])(?!F(?:16|32|64)(?![0-9]))[FRCAB]\d{1,2}(?![0-9A-Za-z])"
LEDGER_WORD = (r"(?:(?<![A-Za-z])(?i:canonical|ledger|tracker)(?![A-Za-z])|台帳|項目|行目"
               r"|(?<![一-鿿])行(?![一-鿿]))")
LEDGER_GAP = r"[^。．.;；|\n]{0,20}?"
LEDGER_LABEL_RE = re.compile(LEDGER_LABEL + LEDGER_GAP + LEDGER_WORD + "|" + LEDGER_WORD + LEDGER_GAP + LEDGER_LABEL)


def ledger_labels(line: str) -> bool:
    """a row label of an internal list next to a word that names the list or its row"""
    return bool(LEDGER_LABEL_RE.search(line))


# a commit subject that ends with an id-like token in brackets ("… the oracle (70)") credits the change to
# whoever goes by that id; 1 such subject among the 4,214 of the 8 release-train repositories, and it was
# one ("(#70)", "(v0.7)", "(x2)" are not id-like)
SUBJECT_ID_RE = re.compile(r"\(\s*" + ID + r"\s*\)\s*$")


def session_ids(line: str) -> bool:
    """an id-like token next to a word that names a review or a session"""
    return bool(SESSION_ID_RE.search(line))


# In a commit message (a subject or body is not prose about joint types or plan labels, where
# such tokens are ordinary), the ids the current work sessions go by are findings on their own,
# as whole tokens: HMAC-SHA256 (the name key) of the lower-case id. An id of digits only is a count far more often
# ("53 comparisons"), so it is a finding only in brackets ("(53)", "[d6/53]") or next to a review
# word (above). File text keeps the context rule above only. This check accepts some false
# findings by design; the known forms: a count in brackets ("baseline (34)" in a warning count),
# a joint type in lower case in a list ("hinge, slider, d6"), and a variable named like an id
# ("max(fa, fb)") outside a code span: write the joint as D6 and put code in backticks.
SESSION_TOKEN_HASHES: set[str] = {
    "09aa5328efb099baefa3481196122bc4829a4d1b06982f6b80e1d5683d8ac530",
    "1d3de79938251c7ff9b6c4f91cec6c5c882f6a5e595a5da1fe1bc186eeb55f9f",
    "257b192b0018b58811b02ad7eb89ccefc6fa75dd4e542331f85d1bcc8f8daf45",
    "32cc3a9e3a0d34eaaface796e4d6aedb38e2e900a9b0fcea97ab8683a5711ae9",
    "510c8ed4397ca4174267aeb747785342297468e267037fd47d033fb1796759db",
    "74b394d419035cfedc18e30ebb2b383ebc783e14bd196c9f635ec42004fc652f",
    "7f8019decaf6fca52ad77dc917f96ad2c6656d25278f8084845804f5e1a3a857",
    "8010124e45815412ac48b31ef91789433bf19ef852a0c45d4e8e433e69702d1b",
    "8ea4aa0101bb6f44609a4866e5a3db99a5ccb7433b61faef34a16a2473bcedeb",
    "e1d6806520ae5f24aa5526aea457e729ca17584994d5a13e6b91ad80b14c0076",
}
BRACKETED_IDS_RE = re.compile(r"[(\[]\s*(" + ID + r"(?:\s*/\s*" + ID + r")*)\s*[)\]]")
# a digit-only id after "by", "from", "cc" or "per" ("verified by 53"): 0 such quantities in the
# last 300 commit messages of this repository and the release-train repositories
ATTRIBUTED_ID_RE = re.compile(r"(?<![A-Za-z])(?i:by|from|cc|per)\s+(" + ID + r")(?!%|\.\d)")


# A commit message is written once and cannot be fixed afterwards, so the closed list of agent words is
# also read through a fold that undoes the usual disguises: NFKC (full-width letters), look-alike
# letters and digits ("w0rker", Cyrillic "ѕkill"), separators inside the word ("wor-ker", "w.o.r.k.e.r",
# zero-width characters) and spaced single letters ("w o r k e r"). 0 new hits over the 40,479
# message lines of the 8 release-train repositories when this was added.
FOLDED_AGENT_RE = re.compile(r"(?<![a-z])(?:workers?|subagents?|multiagents?|skills?)(?![a-z])")
CONFUSABLE = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
                            "ј": "j", "ѕ": "s", "к": "k", "ԝ": "w", "ѡ": "w", "ɡ": "g", "ⅼ": "l", "ι": "i",
                            "ο": "o", "κ": "k", "ρ": "p", "ν": "v", "α": "a", "0": "o", "3": "e",
                            "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})


def folded_agent_word(line: str) -> bool:
    """an agent word that appears only after the fold (a word in plain letters is left to kinds())"""
    for a, b in reversed(own_skill_prefixes(line)):
        line = line[:a] + " " + line[b:]
    line = re.sub(r"SKILL(?=\.md)", " ", line)  # the format's file name, which kinds() does not count
    if FOLDED_AGENT_RE.search(line.lower()):
        return False
    base = unicodedata.normalize("NFKC", line).lower()
    for one in ("l", "i"):  # "1" stands for either
        t = base.replace("1", one).translate(CONFUSABLE)
        t = re.sub(r"(?<=[a-z])[-_.·\u200b\u200c\u200d\u2060]+(?=[a-z])", "", t)
        t = re.sub(r"\b(?:[a-z] ){2,}[a-z]\b", lambda m: m.group(0).replace(" ", ""), t)
        if FOLDED_AGENT_RE.search(t):
            return True
    return False


def commit_session_tokens(line: str) -> bool:
    """a current session id as a whole token in a line of a commit message (not a number's
    exponent such as "6e-8", not inside a `code span`)"""
    line = re.sub(r"`[^`]*`", " ", line)
    bracketed = {t.strip() for m in BRACKETED_IDS_RE.finditer(line) for t in m.group(1).split("/")}
    bracketed |= {m.group(1) for m in ATTRIBUTED_ID_RE.finditer(line)}
    for m in re.finditer(ID + r"(?![-+]\d)", line):
        token = m.group(0).lower()
        if name_mac(token) not in SESSION_TOKEN_HASHES:
            continue
        if not token.isdigit() or token in bracketed:
            return True
    return False


TOKEN_RE = re.compile(r"[A-Za-z0-9_\-\u3040-\u30ff\u4e00-\u9fff]+")
# Japanese is written without spaces, so a name is usually glued to a particle or
# another word ("<name>の", "<name>編"); the second tokenisation splits at every
# change of script (Latin / katakana / hiragana / kanji) so such a name is still a token
SCRIPT_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]+|[\u30a0-\u30ff]+|[\u3040-\u309f]+|[\u4e00-\u9fff]+")
# a name glued to another word with "-", "_" or "." ("<name>-notes", "<name>_x") is one
# token above; the third tokenisation splits at those too, so each part is looked up
PART_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u30a0-\u30ff]+|[\u3040-\u309f]+|[\u4e00-\u9fff]+")


def private_names(line: str) -> list[str]:
    """Tokens / phrases of the line whose hash is a private-project name."""
    found: list[str] = []
    for toks in (TOKEN_RE.findall(line), SCRIPT_TOKEN_RE.findall(line), PART_TOKEN_RE.findall(line)):
        for n in (1, 2, 3):
            for i in range(len(toks) - n + 1):
                phrase = " ".join(toks[i:i + n])
                if phrase not in found and \
                        private_name_hit(phrase.lower()):
                    found.append(phrase)
    return found


# A product may name another company's product as a feature (a fixed agent-instruction
# file name, a plugin manifest name, an extension's entry point); development-process
# mentions are never allowed. Each allowed mention is held per repository as
# sha256(<scope> NUL <name as private_names() returns it, lower case>) -> the number of lines
# it allows, where the scope is a repository-relative file path or a directory prefix ending
# in "/" (every file under it), so the entry neither names the product nor allows it outside
# that scope. Build an entry with `--allow-hash <path or dir/> <name>`. In `--tree`, an
# entry of this repository that allows another number of lines than its count fails the
# check (a new mention under a prefix, a removed one, or a mistyped entry).
# keyed by repo_key(<repository name>)
ALLOWED: dict[str, dict[str, int]] = {
    "7218f32b0e056930aa0b50b7188ffcded8e75ba4f201971c26a746a5a0661bba": {
        # the model family the crate implements, named in its public function and their tests (src/llama3.rs only)
        "k:718f3a21158e4056c77fd0bd555d13d1c5084615a9badc3cbd05cc0c5cadb299": 219,
        "k:76d25801587f8527f7dcc0f1cb140cee14559a365a3e6912e14b350d96a80f98": 1,
    },
    "598c241b2b948aea419169192b86735dffc29164943d415426dd18992f2b4192": {
        # the Agent Skills format word in the product's own skill (each file of skills/lol-sdf/, per word,
        # at the lines it is on; the skills/ directory as a path is not the word)
        "512ad23a184edef1a5d78480c4fb4cbd03528ce6196b8df9bedeb361a27f2cfd": 10,
        "ab6f186f271f1c41ea57165beeb7c1daf08cfc40bfcacd5a03344625c57240bf": 1,
        "7e7501a2d6aa84f9cc4b918da457555a20e441cdb0617f9249e5d7dd66168ce4": 1,
        "7d3700f94d6d0dbe6a8edf0ead7cd56c77932220e797c0a9c32b576081bd8be1": 1,
        "7c7a702ffb8ee276b9a766bfbb157b098962dcec6b911ffd11052554f0f2c1e6": 2,
        "27b4d045e26de80668342952060c7d9a66b37840945cbef23f49a7b47627bf36": 1,
        "d2cf3aa96340fe696a4f84c4263ac02d56f9714c6c7fa839569f76a10a6f9c4e": 1,
    },
    "47478baa10ad68e5f952aa5d8bf62257cbd2871ff3fd7cbf1f569f490e75a813": {
        # the getting-started guides name the product's own agent skill and the tools that install it
        # (the heading and the paragraph under it, in English and Japanese): "skill" on 2 lines of each
        # (the skills/implicit-cad/ path there is the directory, not the word)
        "e8ffe091255705a39a10c1476cdadfcf856f4025fd7ee763e37862f5e31c1de6": 2,
        "662e9ad920561c66b0c9dbc9424ef1a918ac25b0bb7961f6c2ffa024c24dece1": 2,
        # the Agent Skills format word "skill" in the product's own skill (each file of
        # skills/implicit-cad/, at the lines it is on)
        "0d1cd546472a1c5eee45984723451b0408d436c6d843b38f26dac9e7eb0a4343": 14,
        "9a8f57102826f398f994cbb920815580d78399b352dc1ea9b38af5844286b729": 1,
        "1ae9c10dce04f534d3a998d5cb41bc0f4c48ce949a8cd0cc97ccf083c94b757f": 2,
        "0964b7a889f85fb027d2847cae7950483969456ac826fd6000b1f37cb76163de": 2,
        "k:0151e41b108417d87c6a4d5cbea54620e87b365846fb8c9a611529783a36392d": 2,
        "k:a57883fcdc88837ab922fed27bea99673f9fbc1f793c0ce3eab160d74376b889": 2,
        "k:7229f2978704f3cd380c22fef6689a4f7c9230a18595bac1aa86c030a820fa50": 6,
        "k:4695ae90390ea7b02d0f8ba4b2ce85016b67b615c51d8ca2ea38e7329ccdb562": 6,
        "k:1543b99320d155cbe7dce287949f11fab0dfb49aad453c3e661159ac55eb48e4": 3,
        "k:17553566f0214ab01f301c9db6143df0b64012fd211db2cd1c72fcbb8093cd73": 3,
        "k:944d11d25177482f48595d6d85d7490073cd7d74318aa8b28ed0b4f74e172f5c": 3,
        "k:b36bee550a90aeeff9d0b97386ef5cd2d45a0741db46514b3eb02894b4c82a8a": 29,
        "ab6f260bbe48e591409dcd53d07c2044e0a4da028ef1d51bc2ae01ff4009ba58": 3,
        "2d5c1dc549cf7601676a0cb7f5452d20be3fa5f1d0f55140305152ca6be22625": 1,
        "e7fc9e6005c1c2d4cad2be2070cced19d2ce2874f93d046f0cabe7194671d81c": 2,
        "k:fa9e5874e334df30b658c7fd6ef6192827d4d110c578266427ac463477a6df18": 1,
        "k:024f722f24f51f8f93e4dff00fe1adee7c877e0c0f855e207fe0c8571e003c00": 1,
        "k:a2a9dc0b69a01120aae7b4ac3ed1a5408d6558e771df48513151264be0d693a0": 1,
        "k:6eb27826dbd94cdc7a2ed5a0d5ca442991df221e6e8dbc6e1007f353e8d22e10": 2,
        "k:14344caa49b5c422f1b72efc48294a8e87c3e6047bc064b47f11b2624999051d": 1,
        "k:c3b69ccb556f1b1e1de0fdd9847a8c555a9bdbe817221db86acec44708a61449": 1,
    },
    "02a148b01541d8673d3b2ada504d8e3cdcf7725fefaac791004ff60620516970": {
        "k:b44fa20aef0eed38572b7ec4ee8de8abf3a5f5450a1914648b8dd55c856d3b36": 2,
        "k:aff770f96273a2df1c41bd79c42102e319d19c2aebe0f6b8088acac558be6180": 2,
        "k:a9d8d282d0e6ce5fcde3417cb738be215d055a7490ad247464635a0df11f726a": 2,
        "k:eb04e5ce41c0aa0bd5edc84c0541c059bff8912fc4ca0b72bfa1c427bac3348c": 6,
        "k:bb54a30fe9bb8e0305133c248bf1d976b986fefdd820461009e70bee7b9343d8": 3,
        "k:51ff7dbb3c4fa1914488b3de32cf907ca2bd2d4d5ada6670daa95ada84872652": 3,
        "k:5f10cbf4e706ee8cb201dcc283c9c420bad9755fd154cac15ee56b7623dc750b": 3,
        "k:1fc3cb55bf1c33e1313bc1738a9d4d61c156a31137e97bf8aa415126acb653c9": 2,
        "k:e9d2f1f9bf83eb4aa6c391b65acd5ed7be0808cd82d64034ba66b39c72054638": 1,
        "1a6aa0d6a24df8ac5f6ff7c0f6b30c35e552c12433f09c88e349ac63c3694c69": 4,
        "k:241b926f01b0a9b5b84ad4acc9d3d33518ff54ee922e2c78434783de43465867": 1,
        "k:c601734080c73d6244ccb6b78cc372337b95712585fb4235fb9d235d252ab2f7": 1,
        # the LLM providers the product lets a user bring a key for: their names in the provider settings, its UI
        # and translations, the stored settings, the provider-compatible backend and its docs, per file
        "k:826fe6e0a889df6ea67eaf22ecf1edb4cd51bc0be1754fee521dd9258bfe378c": 1,
        "k:53c1b87af06a1cb22782da365021045b976953f99b1349668a7e8cfbb07ae302": 1,
        "k:b5a73e1bda8428ece78edb8e4b116583cd43083e76a08e806180bfd095f74fa2": 8,
        "k:24d30d48441af0a9ed38c307fc545a2eaa37a7160c7e9ed6d198b809583a29de": 18,
        "k:a4b599680ffcfca700818f24e0ecd37afc9c3db893f706a23d493eb2e718b85f": 13,
        "k:56e13bcf35a4a1ebdd895f6f9023b10019d523f08759e5710c8d5c09a09504b6": 18,
        "k:33d999dccbd259f8f76ceed66498ef431d280762da2bd04eec009a228753108a": 3,
        "k:0f0ee2557ff7f34209ba9f2b863d6ca850ef187209ed954092fc63c1872817c4": 46,
        "k:c9b4d49a4d1c1757560e9e0ef09bc7fccfa3dc86df4a0f3beccb2275cafc8559": 12,
        "k:62145ecbf10bf1e0573f91203c7fd3705b32052991f8f9d89e314749dd2e487b": 2,
        "k:196506c8fb4762d74fd8918b43d166d5d49cf89f40b914d6d4241a414882554c": 1,
        "k:7f0fc5a6644ecae47ef2413bb13c8d052aee0adbe4fb30cac7b723b5d2352861": 15,
        "k:1884b2512ee64765b5c78dabd1e349d3f34a2b34844917edaa978fb676f33a81": 1,
        "k:8a8b64d7a67b6d0dc5a78b8bee970f47cf5297f03e808c852b25b335f5f557be": 1,
        "k:9759c9b84d8cfcbb7a5e49564ce4979c5bebfef337a266b7df0a2b5deb641715": 1,
        "k:b2cf1841c180ddfef306d96047c71a00684f3346b97a3ff5d16cdb94a916f528": 36,
        "k:3dbda8d24784af6190958509f9ee7f2afb89b2f7afd4b5bc349997689657ebe0": 20,
        "k:cc592c0f053e80484b9b538fc410cf9c4d42a3b6150083f2addc2c8a17e3846a": 2,
        "k:6d8ccca41e0876430cf7b4738974eaf3ecd4901022c6a2d3df64eaf899f7b84d": 1,
    },
    "k:8a2481b6c20ad401a9bfbafea91cad1a8108cbc8a88167562f8bcdd3147a39f3": {
        # the product records LLM coding sessions through a provider hook: the provider's and its
        # coding tool's names in the hook installer, its settings path, the model IDs of the cost
        # table and the docs of that feature, per published file of the mirror's allowlist
        # (mirror-allowlist.txt)
        "k:a9d8d282d0e6ce5fcde3417cb738be215d055a7490ad247464635a0df11f726a": 1,
        "k:907eca665b746a453aae75e4b508c20c61035e2261e9718cdecfcd3c4bc32eb2": 1,
        "k:4ea18d553229d27b4f09875a2f588ffcb33d6d613c8fbb3f9d8a45b6217f65a7": 1,
        "k:444732a28a513fe59f4f4a684c6b2bd8c623d38166a1ea4751f41811c09ed11c": 21,
        "k:f054ac4878117379a9d2472aa39b634fcc396ab62cb2fd4e7857c0fdf872439a": 7,
        "k:9618d4c40b825bc51ae3c414fb625ca8a7d88321282f80a7ef27478559d38450": 3,
        "k:4fcbb884c8e79dfc40340e203503f6064dd2257744888860af4559b7fb3498a0": 6,
        "k:5a5efe4ef1640f186fbcdbb7b5346fd571e9122705d99fd7f53bfe0ae5df6f4a": 3,
        "k:ea1759e6074c16cdf664bee40317a5040535965d28fa006d8f220eeebbef18d4": 7,
        "k:e3dde8c1dcb2e572a0eccf3af6952d4e76dd25f95792c9cc464837878ab8f56d": 1,
        "k:a82da97a13fc32a159a86b17836472d2ebe5b418afd3d60c6f56e656d05d1445": 2,
        "k:8f29747fdcb1833453b7a9573c55b0cced77d2b93952d069f79984e7e83581ed": 1,
        "k:2ef9f26c2b2dc6585e4289e036264b43bb5bf59945f518231e88824d11fba4e4": 8,
        "k:8374cd0f34c29fb612383e0b3e712df8adcde1540a3cb9c0ee90d9dfc0baba74": 4,
        "k:bad63706c1dfccbded548df9145990fbe49b36b85de1e7f1fad86c53946e4169": 3,
        "k:317dd07e511fa9832df85e97f723c1ac94cbf146f5f34c2d13de4331e267d6fd": 1,
        "k:e59d9c12b82ece2d53fcdf374bb9516dc5527d44804a87097a8f4ded1a5ff61d": 4,
        "k:07d7fe8ed9c3a70ed7919cb62b25e831c9d7ad1019522220a3a3e95754a7a4b7": 2,
        "k:4fbdb348c29d4fe860d95433c2fc09b5c0b295d18dc4440eec8f4612fcf29f60": 2,
        "k:be0d5d2baa7c5fac62aa6da564e1f5a303d6caabc2a869d7e3e46ea3b6f03324": 1,
        "k:df83c48578d8c20349c4a16b4861b9163f7d43e61cddc4606b26b1fe090a64cb": 3,
        "k:25efbee27ae22c489784b7112da5f02dd07dc3d8935600e3d622c7e1c127fa60": 1,
        "k:3fc294f30c310e331ce79ff4cb35fec66d629b94c209a64f29e2f6c22a869695": 2,
        "k:e93222db4d872e7183e0c09a7582737b5e552384385d1d9c48fed20faccb4c15": 1,
        "k:156434dad41233041c9bc7173db6c78291ea48ccb0f34c45b9e79f4adabbe276": 2,
        "k:6be78f2b956bfa8790b2de65cb87baf5eb7845bb42e03ccba15274023c5f005c": 1,
        "k:c6643acdd1693771af251d685f637bbbac2208610ef20f429ea116fdcfd573ca": 4,
        "k:15d24f14a8c8ee467ec48bec02837a843b6459b1b982ea8b69e51f0b0f913229": 2,
        "k:d61ef41f38b5069e6ac52e2c02addfe6676dd01ef91760e01e0fb81db90b0933": 2,
        "k:fe18f50ad61e5e6d43da2d049c0dbc61f52bc0454ae8eb0f0be275a7db4b14d8": 1,
        "k:388aa6c75d1818cdc7c8b8c6821fb395237d2d8602ee7d0de56e6230b81cc782": 8,
        "k:29dc5423ed50d449c93d1bbb4bf0c3d22345d693d5c5a9d4653501df7cad8736": 3,
        "k:226c3445a2dccc6172b3b7a740cc95ca133099dff52c3dc981847c5e364685a5": 2,
        "k:926acab6242b10b689a5d67b8b6166343c65cd2cb30ae4d70dc230db138e28a2": 1,
    },
    "3ef7c23054fecceba84393adf2be7da3e61c15adafac87399f90c9ffd92b43ee": {
        # a technical worker / session of the code (a job system's workers, a protocol or training session)
        # the product's own integration: the providers and clients it registers with or calls
        # a protocol's session (the client closing it), or a session directory of the tool
        "81195d09e3aeaac8cb290b152c0e37377b5bcc9b7e966915df3111ea4fdb8591": 1,
        "k:0d70bfa774c30a9062f0e74cd35f6c731f7538c4a0a55739705dc645e4b88383": 1,
        "k:b613c3d165a29bfe763175863121a9da79effadc642d0772556ef19dfd349bbe": 3,
        "k:f2bd17fa34deb288861e4d523497fea6fa1c40e17e47ffb6f88b0e6e85936dff": 1,
        "k:7ff1db0a3c15b720d517c7d0b3d3b28239ba37c25569180e2bd52ccff3c02c5f": 1,
        "d7d94612de7a8b15676b089967d90fc02d65825e651d6133e523fe323ed7901d": 1,
        "k:9f7a00523ff5a151d05271627caf6d942e76bd9a1952c43f73e62dd3560cc85a": 2,
        "559a86e79330f637a9184cb58fc3fdda4a11258335d439d70650c20d0247aa2f": 1,
        "307d20155e0afe1d89c9c4c4c4da266136b13d94dbc310fd8877f0b4ae50053d": 2,
        "18a79de1f8029c9c7dca68e2aca663114defd9e7004767d9231f37cd26037a9b": 1,
        "k:7cec814162b2cbb00f7dea091173055da52b6a2d375788fb8d42753e942e2ae2": 1,
        "k:3916327d4ca8537a2c13d7b42b9b4a5e69c74969b64289cad6ce0e030cb2c769": 1,
    },
    "e42a1f4ae9070e759b04e9b00380f1381c1c8ab9b1e2baaf832b3ff3c6f44059": {
        # a technical worker / session of the code (a job system's workers, a protocol or training session)
        "df9edbabb365aa396c27e7ec628280d7bdbcc921080e847c5ec89f577f438c6e": 1,
    },
    "2368fbbea5ea4043cb84a5ae027905aec833fcd7648a68e5b9473d5cdd8b941a": {
        # a technical worker / session of the code (a job system's workers, a protocol or training session)
        "cd05384f63a4f3cc53317a433ba2c4d01a58e1ae6b71177c6d2573eb395f3209": 2,
    },
    "dc0d9538d358ba3c2f339c142819661915e5955421932efab0d5fb0a581c0578": {
        # the product's own integration: the providers and clients it registers with or calls
        "k:782360d2915f61f354743f3161e9ba359a4d688585666b83178e71b60147b970": 1,
        "k:e98a2d48a8dfd022156ed9a042d56679142f955ddede6430aad86cc042e0c41b": 1,
        "k:a9d8d282d0e6ce5fcde3417cb738be215d055a7490ad247464635a0df11f726a": 2,
        "k:5cc716e34cf8fa0a6f8137b1e89414bd9dfe0a95a918caaafd63ff39e1a0bf90": 2,
        "k:935c4d8e7dc1e72450b055e962356b4248a4999601d76526c610dadd92f13c2c": 1,
        "k:d2706feb3ca4419196773ed58352e756bbf120fb2e51cc9ecf0b2f004996e45d": 1,
        "k:364337a1a0fbfb99d31e0533f6dd6bac7bf60d0a68ecbf10f62c690f6e2635f5": 1,
        "k:003c160bb09011e7eb146fec51ae369b0ca7fbe4bf2725f7bc23fa7263c96fc4": 1,
        "k:ba9910e99599d0d4a3a5b14a1085dac3c3062bc1e0f4a5d2bd50d9990f81bf88": 1,
        "k:d65a3e5f000aec89c9a8ed7d6c1f52d4064b93cfb273ef6814fd132e06efb9ad": 1,
        "k:2384fb8737231feed8c9afa7da907e6178e75c938c33aeaf3d7a58ab17132de6": 1,
        "k:3f399a9f6cea38152fb549a08c0f698667ec9cc4c34a5e51049d3af003070dd0": 1,
        "k:7483f4508ae79c76758174f797d88b19ea0b9b8550ae0c59e486d7911baeabf5": 6,
        "k:08077c2e04bfda515d19d3c383e228321f675045a6dd61ef8f53f20cc2fc44bd": 19,
        "k:fb571b5f418acf5f50029c86c5dbe28c0211c6241f0fd192332c86e0f369741b": 18,
        "k:ac967f1f8665d52d52b1281a26dc4c0cecd99ed988055c2beeecdfea696cce2f": 3,
        "k:e1e380dded9fbc03239fa553969f21dd40db41e64ed4ef0271cea4b84828c762": 2,
        "k:74490597a94e3388deaf7cbb68b63fcaf4182a474935c80ffae030bf94a2f4bd": 2,
        "k:1bf4d26fed468505c5ca4b5fe08235143dbb6ef77060ae4dced4dc07e532f96f": 5,
        "k:8f0d1a6eddaec6d8cc1678f71884a1e0304fd9f0e9adb00a10b8dce609f64fab": 4,
        "k:b87f6920c1fde9dec4c54d7d46bb3787a4930dab45063bd7a84f1734c88c0dc7": 3,
        "k:e421703e839683a881de4484c4374b71dcdc2b387618d15a7e7f4dbe2e932f91": 4,
        "k:4b0f210fe4e48b3ccc4259f458053b77930b72584929ce15ba42f27651579d2e": 7,
        "k:1e30e12537eb456ef1cdd384d42e2ae1f20dc7d8e56ccd2e78ee5164a45c38cb": 1,
        "k:295009d595ba1ec4e9b8bfc5cb5c8d84112408bcfaa7de5613969d08fdf4397c": 1,
        "k:a255ca7925e1ebe19d56a517ba186db6ae781ca4ef3e3cf17a2201058f98bc62": 1,
        "k:4277e7aa03773eba729702bc4fdf517fb144311701d77ee5259bd871985349b0": 1,
        "k:59432581cb90bcd833ea8c4c51189a5222d49b48ddde09aa6e484ae7ed790a6c": 1,
        "k:740af06134d58b4e1e80d6b4cebd28706c988e41371a0f1d48aa1811009067c5": 1,
        "k:99f8117dca0d49c492dee795d8a82e41165dc8461737e12389c42a9379f171c4": 3,
        "k:acff22083955925ddbd15992c2b89744002a32a3d98c6224f355a77ae7dbdfe6": 2,
        "k:8971494e96bb0b64512a987b18f937db4da02e82a034a9681d4398867b1cf47f": 3,
        "k:c4ada37cf8b73bfefba8463139481cf68c0fe1eb8e6c9756b0b95e11b3e0035e": 1,
        "k:84c76ff2fccf308d68cfb2b66e5a2b995151748a9fe287912d3610892049b79a": 1,
        "k:4312d800bf633598a25ee9b3ab488e40aa375b9243e91ac1a24f9155de96ef56": 1,
    },
    "7bc7bbf1c0be20ad56186deeee18e365398c650bb49bc70e7cd01bc3d26467dc": {
        # the browser's own API name: the web-sys feature that names the type, and the one `use` of it
        "4cbacd7be90e144cf90504dfa1d1b10879741a7f1beb16b79009e1a692115c9a": 1,
        "09e1f72353fdc507dc8be51c6cf3cb87bd2210ab3be78d9bdee0f83de264ffbb": 1,
    },
}


def allow_hash(scope: str, phrase: str) -> str:
    return hashlib.sha256((scope + "\0" + " ".join(phrase.split()).lower()).encode("utf-8")).hexdigest()


def allow_mac(scope: str, phrase: str) -> str | None:
    """the keyed form of an allow entry (`k:` and HMAC-SHA256 under the name key), None without the key: the
    entries that allow a private name or a note link, or that belong to a private repository"""
    mac = name_mac(scope + "\0" + " ".join(phrase.split()).lower())
    return "k:" + mac if mac else None


def allow_private(phrase: str) -> bool:
    """an allow entry for this phrase is keyed: a private name, or a note link / note name"""
    low = " ".join(phrase.split()).lower()
    return private_name_hit(low) or bool(re.fullmatch(r"\[\[.*\]\]", low)) or \
        name_mac(low.strip("[]")) in NOTE_BASENAME_HASHES


def scopes(path: str) -> list[str]:
    """the file path and each directory prefix of it ("a/", "a/b/"), whole components only"""
    parts = path.split("/")
    return [path] + ["/".join(parts[:i]) + "/" for i in range(1, len(parts))]


ALLOW_STATE: dict = {"allowed": {}, "used": {}, "repo": None}


# ---------------------------------------------------------------- scan

# "worker" is also the name of a technical part: the Cloudflare Workers crate and its runtime
# files. Only there, by path (whole components, so "crates/worker-notes/" is not one), and in
# a Cargo.lock only on its package lines, is the word not a finding; anywhere else (docs,
# README, CHANGELOG, comments) it still is.
WORKER_DIRS = ("crates/worker/", "migrations/")
WORKER_FILES = ("wrangler.toml",)
CARGO_LOCK_WORKER_RE = re.compile(r'^(?:name = "worker(?:-[a-z0-9_-]+)?"|\s*"worker(?:-[a-z0-9_-]+)?(?: [0-9][^"]*)?",?)\s*$')
TOML_TABLE_RE = re.compile(r"^\s*\[\[[A-Za-z0-9_.\-]+\]\]\s*(?:#.*)?$")
# a home directory path into a private repository (`~/<repo>/`, `/Users/<user>/<repo>/`,
# `/home/<user>/<repo>/`): the repository names are held as HMAC-SHA256 (the name key) of the lower-case name
HOME_PATH_RE = re.compile(r"(?:~|/Users/[^/\s]+|/home/[^/\s]+)/([A-Za-z0-9._\-]+)")
GENERATED_BEGIN = "# >>> " + "generated private repositories >>>"
GENERATED_END = "# <<< " + "generated private repositories <<<"
# >>> generated private repositories >>>
# written by scripts/gen_private_repo_hashes.py from the account's repositories (do not edit; --check finds a stale block)
# The sets are held in this file: a repository's CI fetches this one file by its SHA-256, and a
# list kept beside it would not come along (the check would then compare against nothing).
# An empty set is refused (private_sets_problem).
PRIVATE_REPO_HASHES: frozenset[str] = frozenset({
    "00289dff530adcbd4bcf866282373533717cf25f84225808827c3459032512ac",
    "00c48ce43e387183f43c28586b40623397710b5b494b6e3eed214f245a7425de",
    "00d75ddfd57c20dbe224221299db63f0e52f3130cc0a0cab2e956406a10784b7",
    "02bb1b6790b2e8a2894140249b81a5d21bb7d358f2aab42d9347407f8e7c46e3",
    "02c8bc47815a80822070e565512f3bf7c66a155acf38b2a6548199b09af3ecf8",
    "038d6f4479f92eb383b32fb952bbeaced379b4523c8106a7aa7b8d3e7fa9ef74",
    "04fa5277575b2161d365f3328bdc78d22c1f3692bdab6a5e9368fb4d709d2113",
    "052a4085a2535729e3eb5be8a830f2143426a426edb9028a3194c66fcf550033",
    "05e4de634d54ce8abb8661803c1aa175464a2ae2d03a49e3b7013c4be99de4fc",
    "066f5d50816d7f9fc80957ff387b6291fd95029434cf33c23418539c3a29383b",
    "06bb8b791dc9d3cb15f45a11d70aebcf688d40da8482b611e0ebe5d376a38c34",
    "06dfd0d8b0c976c451cd5b82c5aaf6b56e91aa9a5626e813e7d5b9295715c215",
    "076dc08160e946c2b6b6ad7d99cf86bb824de552c72a0d2641f5b182d0d21efd",
    "08422e835a085d814eedc8f24034c0a0f910343c16b9ea9282f5efe3a3944a0c",
    "09b3fe2a4056d06b60843f177c078415f14d8e16f0d12928b20cd74089b7861b",
    "09fbc966cb6dc3dad3a4e452c732485065be6795d3597d6383c669d1f0f87fac",
    "0ab49bfedfd1c84d86ff7a613601dedffcde374449caa32f8f1ad6f3b3b272eb",
    "0ba2a2388b3ac10ff2287440a2ee90ca21971e27ce29419f5279fb8c7ab8e95a",
    "0cd218bb263ec9f96bc0d0b0ff3d6b6c7bbea9e1f4ed410e8c92306b207fb85c",
    "0cefc824e7c215f50d065581425a64b79a5ef22e81edf37d7e630bcc68a7b107",
    "0e01a079b7a10b0bf6c46ff65cdc0585d0ad3cf0df1741a3a00ca683bbff19ef",
    "0fe86395333ee9dd22b27b92cf06c0f2da1fed529bc02637383d75ce02eb6262",
    "1022b7bc0a4b5477c8b919ba383da750233407a5640cc6ad81f146c5d1e0faf6",
    "106e347c500e327b22593fc744ab3680e946f0b236608a0e255425f1b914435a",
    "10796ea7051f3c4d0b78c928be585f0db44ba3154f2bd46bf576dc2a961e23c2",
    "1244917d04e697a4c3686c1cd0ba63e14a07c91dd318b875cb51d8879e4f4b56",
    "152caac1ccffb5646e45381cdddd230ccd7af62283d2b2faf4e594407d6b6066",
    "174509f31b858f06f973fc7ab9c6b29f3e0f46cc75380026b49afce377ef2d31",
    "1817f85642168b010142e0d2cead30fbfb29a329ab2528a1398f5f85d71571af",
    "1835005652251172c484a924b513d71116cfc4eac5d9007e5c6706b1dc36a68a",
    "183ecf527ad23e450507c82a12250745ba508574b6d0d9ca3800623abaaced1a",
    "19293a7e50f8baf7bca5e25b4a59a04cf0ea45ff5be5f24cff5bc7882bf167d7",
    "1a15fcae19465d9ff067a04669a5d31eb261d72dcded7ef96a44204af714233a",
    "1a192029eec7181485061a394e82da7574240d7fd11ead5df0faaccf2785bfc6",
    "1a6381e0f49a6842f9adf5121a6a909e56a1d9eb6abb048aa7570c1409f86a7c",
    "1b115592d396f0452a4c3646a4883a2dd03426faee3e1245f7aaad0e23dbad24",
    "1b561441e65d4ec0c7c3fc7b29b948e0eacdb8fbb50f6bc68a39049199e23851",
    "1b5b322666656446e68ea80a2f02242c4f068682fac5a515f8f249c368e7c62d",
    "1c27c81e5a31ed14aff80afcbdc7f9229eaf58738f0bb8fe1aa5269a84cf8da8",
    "1d520423f0cbc94897548efe64a8b56928b08fc61a42048f17743284fb0958f1",
    "1d9ee9058911d55a2abd8c708171c0d5e5f81c7c2d952297d8573210a748017e",
    "1dc7f34bd43b2db26fd329fe781bebc4a9f77ad7c492cf661c73d02ae2d6fa81",
    "1e49e25e2b4c21d24ab4ae5945286f234a191295bb9d370810da7fdf993b38fd",
    "1f0e865381f02428875de07532cb3449b2bac95db570c54dea2f265410fa4a4f",
    "1f2cf8a65a9d427b257d34384fca233c0e45fa22c215367559e22db1f3eaa493",
    "1f74322935b4f3d5347b9ae3ee0fe0952917efa8c1cafb31ab9a04294c7934b6",
    "213527f11d70da7314a6dac5ec7ec044a5927f3284e10ad895ef9d2e09c3e7a3",
    "21c3fd925ac5876fca3801fe5afcdd62eec8a268607a31b67ae7048cd96a8f15",
    "222a63416049eca2c936faaa436c33a78e01ab2189f0a612c7eac0668200bbeb",
    "22e38e6441b24f3e9408532b6f8fffca17e4b25605fdfa4f1868c39ae113f80a",
    "23940e08ff6e19fdb25c44dc6124e31a9ef3bdbc6a1b6514abf25a8e9d6cac9c",
    "24fcb9b61bd162a3a61277984d220550114914121d5ab6e32f67b7e98294d897",
    "2617f90d5dc5d1f7127e2b0aa604b67d6cb4f721862242cf08d43b9d43c9c732",
    "270411d0e6d92b09bd5e46872e91480da0b435e76ae5fe1dc9dd06941ba4dfcc",
    "2715b434dfee1bcc28e4b0657637cf8c928d514e68fee640bdd7216cb8e4f6ef",
    "2716ce326d265425673ce3108c5ea31108e41dc8ab35914af5c409a7e40ecfbd",
    "27d208e28354801e08b8481b7215f31b6c21cd70bda34ac58ea0498ce805ded1",
    "29319e3d6205fd2210c82cc30c6de5c6d4a3199858da7c041c20bf9d25032e4b",
    "295c559ba8af7032f9cc3cf19f7f9ea4e5d70ba1ad08e130d77ed530f82f42c0",
    "2974b5053decbbf96f3fe24b96b6c958c801c7b7a83c00216e0effbb4fb223d3",
    "29e62bb8510d79a733ef5d39c175adc24cf8c8415798ca4594c52af63f3f4211",
    "29e8b2581a160a1017ec2b0e68e2ce8cba2b92d1f07cdc038b9f05d6901f2720",
    "2a619521eca246363f5c05738618ecff8c4f51f5afda0d9bbf3a37d87116ae20",
    "2c44b9abfe4016df790e866272e6bef2d2f9bac9408385f5f2eea987427f7cea",
    "2c8a7953168f17461421199d363cad277f1e7f8a45e3c3c5bbfe6d0f4294506e",
    "2cef9724471e0d833261986d289cef40ef96f4403b6e28037af0631ceda9d573",
    "2e1a7e2eacf7bd69c8e0c655dbec11b6a52566085aa3c340e2f6bc65afa35f0a",
    "309e8786fef342a663d7bf68599b009c0ba431c7266a00c6d6ed1445a335ebeb",
    "30f0b20a82ec0bb50fdba914b2ba3d22a1e8c7180a6699cc0a3d8b14d40211b5",
    "31627fe3ca40cfa910a940713265cb9490a9b0eb2ee2d44a93a73370643bdcdd",
    "329698749e05e2809ca41cc2e01477cdacdbb76d9e67bf909466e01bb88cc1ce",
    "32cbdd2e35375b3325a7025c092638342687b11f1018aba279792abdcb19d486",
    "32e47a82ed61cbfce327acc484db7499ae933d94e340a452b62f85a240a0cc00",
    "33929323c2c9c418bab698e8e879e1755dff4cafec38b67945153a3c0d385ec8",
    "34ea14e01ea4cce6502911bae043396bba454f9587c9da863331b4536e26b4b7",
    "34fb10a8b08c07ce8bc4453caa681daa552e60ca7c0ea897ef8f6ecc075b3cff",
    "3501e3da96f0d8e48517f7539c104a5144e17dfd8123d3b35d018f2772ad8e95",
    "35e335465ea450bc1dfb7ab7a64241f454abac0c7d11ed432b0949f16743d669",
    "35e668d2ded61b1a2fb60e443aa5a9cb6c09661984dcf7e390724f98d37e7c69",
    "3776c37bcbd1e6c7f3c1424a74c3a1a0bdf980deb2453e9eeed8466d8394ef0b",
    "38b7afc98bc032fc42ddc6f14faa7d7a25f599ca13b0e9f5606492ef52c3475e",
    "38e0c21a4c5ca706f1bb6dc82991a16251cb041a831aecc0d8230b517385b9db",
    "394f37efa384336201e6a3f4fce2f0572ea4a0fc7ef6f989c8e157a5100fcc74",
    "3a39d9c921edafede122eac9ada44e5fdcb4d394abaee6157c41a3a22e21033f",
    "3d2aad1bcff339869f5664c07a414a47bf4dadb06d2606ee894610166fc14038",
    "3e3fc5c9511a9c0c4bdb25196218a6eb5eae57283c8bb249d18404bc9975acc7",
    "40548fbade4381ca1bd259cbc242bd742884f73e4a252165db7657a371730a46",
    "406e51a98520962d808fb8e5f400e80a7c277fce377a200476f16df270ad694e",
    "41c90f0303518fb3f0184a938b9a9cf433dddb568bde0c5b73fa59d0e4424e80",
    "43244f89b8dbdaf83e01393eee0e191338137ba8ddd088aba4dbbc8fc694eee4",
    "438ddf4e4327da9073f35cebf845803f52ece3d0bfb566d385593363cbf0d610",
    "444786d327af886d16ba3c385fbc848060fb6e8527eb2931d7ca7f5621018141",
    "46749497a6df43c879b0f4f203fb8aaac0aacc3cdd7f36fce77077778067d982",
    "4709045d1bde2517159b1f0098a23d5fd515b3c7eb47562bf8f0b4642e75cd05",
    "4831d4ac146ea00301e8936a52bda6ba7a016dfcc699d82286d1b2673f2dce42",
    "48565fcae5712b9352d85308be8ca9c88dd1f7dca6ae02557cf2b5fca206e1cd",
    "49b2f72df48b75439713dff88cf666075699c20ae2bae7a0f7ea3b32c6fdab94",
    "4a5d3cb37e4b9812676516eec964db22e117bda7b8f1099db76668cfdde1eb2b",
    "4b11c8eec4a557af82fd0fac0c4e2cbb00792a50911f9cd1acc47ad590242a31",
    "4bd24b3602c298f2625a84ff2a9e6fb7241de941b0694872a71882e2e22af068",
    "4be9df784f9b124d4ce7ddad957473d5a17e73592edbaf5dbf10b864ad93dc14",
    "4c25aecedaeb39481ba5f8365c5cbdd47a4ea64990b004a085ee769c8e5af109",
    "4cbeb48ae5ab768b536dd24a01133c329d843f7d73d73b3f66dfff771edb6037",
    "4d5124dfe7be389d176ca2e47a263ec36695c8f58bdac2d6c6de8865e9b25e59",
    "4e12b3020767309fde09105844c3c2205d1f146d71bc627ce7d892ade4edcec9",
    "4ef83035c8e84130286bc65a9323764dd644b7b9da9c62d6514e9cfd94041701",
    "502d534e56760eeaae11a1166f37843553a88578a333b6e94348662618d94497",
    "50764a9f81c5e9d65296d6e36aafba55227d30a4c0fd014cf9c6b1d0c242b1c9",
    "5172e1a15dc42b34cb207a3cf97e01b1e662aec2fe9dab3a261027708119e543",
    "5467f056d0218fed4827072cb357a54e8ea9081a8fc1f40155e4b7db1b4af75c",
    "55281db512abc80bac01d9dd41de1f773085358178b9ae81a6b1fffcb68803e3",
    "5554872ff0168b8cbbfd3cb332d406df610c34f7e941759a846d68ad3b528667",
    "57a0f1d3ad9a421d201ce39bbbc5d6d49a9a91703cef23c1d8606fcf379cdbaf",
    "57d0a0b6f0b0c0db96c9504d6fa9472092a8e90e74ad957877d9d9764b47871f",
    "593b5c447712f3d55edb482e21088d07fcfd27cf1db23e71ad554703eb607f6f",
    "59cc3b019d01832d1f1f447fdb9ec164e6c87fc60d0fcec54f43b8cc324e30dd",
    "5b1890911c561bd18760b39e0c22c48891ac568b52e5b5c8b5a6510adb582a8b",
    "5b35aa244787eb89568c6cb0b2b7fbf82ac2f29481b0bc4f34bd793b7ddfcf99",
    "5bb2f1f7775ecdecd4fea2644225b5612f34547e0e7ba84d631343791599370d",
    "5d5800580a8b288480523b26965a459e78a2d030c16c1609b05efde3214483b4",
    "5dc71dda42f7ae7672b43182343323fd06e44765c56da26a45cbf179e61fd736",
    "5ebd1555b7305a9d6299197406b923f7eac1e845b2c72fab2890056184a51835",
    "610d7e3e6c19b81596bf18a2da53428089d6d73f3c6874401a38b852dc8a239e",
    "6122ee672a8beb29aa615ef3a80cd4d5b19b25efe88bac7a8c3d641c5abb23f5",
    "614205dfb147dc06077a6426e3b19c2c4884b0accf728c403fc3681b463e5364",
    "61c3c4cea963c0e0b6ad5922d20f031625669f1761903f32bfa9ee39bd8c4cf8",
    "636fd8c0985488f9915ecfa3ace16c6707dc1b3e6037754c5549862e13ae1c28",
    "6475a8d1c2feca9ad68bfcb0bff0e24e1d864b44bb7c901c52fb96c9c26ccd95",
    "65cac5ca39f9e859ad7fb63129244ec9d8573a22a96be323c7384cb90f0755ca",
    "666afec98fe2b2b370cb74895d195d4f0d1211c47989508953e4f2934236102d",
    "67f6a83553d431b204a9b28757ef91fb4776e0fe53f1aa2861233ed1140c80a6",
    "6882cdf35d2adacd497f761de02d6327055a3a1284016e84b7593f786f3a3b5f",
    "69140f986f64ce530343f039d1613df5e62080edc19b9395a6e61a061df80d2a",
    "6938d688c7ed838eb4ca6103c1bdcb243a6cb03f0c20ded32e43d8c6ad70d3ca",
    "69608fd44ff07674c93d3a79a2ee6e0ebb364c9524d93eb9a7e451c717723240",
    "6a42696037030098adb19e3848b99e2a0700831cfb1742ae7f2bf9d79bc828d6",
    "6b6e7f769f66854858cd684080f9454bfa24e67c1d1933f5f0f2be69013c6cd6",
    "6bcd8a005e66640a9a345db3c98ace40f21526e8325d738ea2a2b120e5c8b47a",
    "6c6eb642df3b3055c5d5c5107ae57cc447250bea7d8cfc8cc2fb8b1fd4169e98",
    "6cb2f593bca1e9bd1dfedf52046072fb60a45a6b7df0f6b0d229dbace8e3a52f",
    "6d78f9f11fb2759f0feeedc604516cbacd9da9b19658e0d6cc15328d3bb59843",
    "6e07db411a727cd1493b69d79e736d49224119e3ad562b73849cac5af96f163f",
    "6e8affc58564f46227bd2f829ab78c476a6a7a0a303499d621ccffea237e3d70",
    "6fb9ff638e84044cb453d7127cd8b938e4288e4abeaad7690436325d368392d9",
    "73953216b77fe1e98380d9980df48f23615da074bc623480fc5e269b8e6c79ca",
    "73e184faac76332125cf8ec86c046595f8437e831fd3fb1985be890a7e451c14",
    "750eb9a86abac2215cbe47a038c157f94f0a1134416dfa98201d83a3d6b0f630",
    "751efc4229b0de3110841c0f7b5172b0df7d6da35c185081b8f935c77369f515",
    "768612b0eae011b005841416b9a3310213028fc4d1c0fdfc874e11d9ef08832d",
    "776f63d57b851b1f27beacd945bb0069dda8f451bfe943eb29574a9612d53dc6",
    "77dda3a5255b240d5db893310a519031b368dd2325eeb7e96b42f8f0ea36b63c",
    "77e0777f7235c3d72a2a2bd6a694bc00c2c90f18c2e56d46327b61eff3f4e30f",
    "7957cddefa880ed8b7e273558fd38fe1ee5813f0ab2618caf1be6d122e7295bf",
    "7a682f40a3d7d86752821aec3eb506e54aba913bd3727e2c021f189d04499b6c",
    "7b80ec8000acec0b123b8bc5a03b8e17b7947cfedf3a66486a7dd979dfbec26a",
    "7cba08f306d452f704721cd1825f22c7eecb9a1418c609a67ec8a09879c11955",
    "7d99b6a0f0989b47834e2634209af8cb4df955fa156bad62ea2c1a029caac289",
    "7dc1be853cb6601a99ba35b637edc5023220de65cc571044ec0dd5a9e874d13c",
    "7dffbf0c799773d6cfc788fc48aaf7cead9a431a0877382a599d9c2fe2a91b7a",
    "7fa8f1451476e06f1ffdd1f482c2e75d0c80743dfdf321985bd535c97ec38f26",
    "812b5ba8341c8314e80d02a007eb69310964fb732968531550d9ca05d58981fc",
    "81f43b23baad1660fff84754286fd5e1d78262b9de18fb9d46fff2dca2dbabb9",
    "8252f38542797ca71cce10d7cd50d3866c2763efe55c027536fb20c4e8d538bc",
    "827af82d323bd8702074c9aae6d06d1ed302c17a4abd100caf82a1396249a7c6",
    "835eab5fcb84e40c5c3ec4a67323a00d1da827fe0885ad6a5774801f7e043885",
    "836333b30811d58188dfd868951ccac237912388a43473597e5126d4ada56fe9",
    "8380e6ee31839c51e6cfad2c00ee39305853e0b7dfe7f85a2c8039da120d521c",
    "8481df23458febd0dcc158f5a8c0d3758df48648fa927c4042177ff7d9eaf672",
    "85a33a53c94401ffd5bbb0cc6e65e15cf8b01e103f947810be86bc701ba6f66d",
    "8645297af7a345b96970aeebcc55923e92cb14d5deb8cfa21ad95cfe3885931e",
    "8751122d94d6473c8b8136ff138fb3452a713ddd641688b8089076dcb78bc477",
    "896d434bb908d216a6db45e20182780f2606c0c282649e7c3322bb7c869554cd",
    "8ac84c6da95c28a20aeee62e50c1570964d22a1726ef3f95fd0e0309ddcb899e",
    "8c627a50314061fa4329840e13ded6cd4c5e935d460435efdbee71470e7f0877",
    "8d2b7dfa83457d8fe42600d8a02f644e25493cbe54954697ff6910e36e07e30b",
    "8d3ee35de93e4ff474057930d533c0d7a64ae5bc6305566ee9792459eccbbad1",
    "8d88c4be76c7f9364736978a4aa961fdd6f4c6b7ae1e35e40eccc2f8b6091d11",
    "8f3b0bb706dc09c27f66d647518838b1f2b7ebdb6a751f1934da88ec429a603d",
    "8f6e630294f99837480d70b6a86d307740230fb4b802bbd684b565dca7d9f966",
    "91638ccd4dbf4e66edab1b675eb3b567b22fcdb54cb968246111747663378309",
    "91ceb04ffd83965390e28e0bd2aa3a12efaf7b29b1a43ec505b7cd136b3fa8dc",
    "91e168d347a0aad7fd83433a95cb23dad1c943ad835af20ac85c18a9c2b737da",
    "91e8a34ff4ebf6da98da4e672eb90bd1ed6ccea2b69d28d91cc6923ceb18fdf9",
    "923b8017dd9ea82911d7dd93675bcf620fdaa32aae76c2b6bc5767c1e1382297",
    "93121ce685a228c9b0ba62edf0ad69a67b97d2b558f959c58755aeaf72cc08be",
    "93db1572714599b03c107b06daf8fa9cba99e815fb2d0b7d749ee17d2ff94a03",
    "941c8e03240a0384b1a55d28e6be07a95ed5c0b9835f508f376412a2d804f615",
    "97a044e7fb319814847a53fa7df2c3f286618b0ed03f6b3552cfc450050731a9",
    "97a58e92f8fa693ae427caf01a41c9350c592de4889c2479ed6cacc1a6784df5",
    "980422c4226dae23072bc3bd16b0f74a9b28c08f059d9ff19e9a8c4a229c480d",
    "9b80085371f803a75028995a791abdb483994e3b7eab806218eed5f7fb49652a",
    "9cbbb4fd0ebf7bdf3fe83cba3aaac75e3f9779fa67bc382e861f7a9042e2a61f",
    "9e03080d592c35b5213dc07a16e67a0253e1ecb0461a5bb32bb9b7a19622e6de",
    "9e4eded01cf25cd0530a036e8a0b8799142a6ab9e85cc2deb72886dc8337d80e",
    "9f204551eb20d33d89f7cb73ce39d6afded9d7a9c53a734fd3a525757e8b234d",
    "9f4fe6461e5cff9fe937893c87bb63c0f12f3aafa5b1048d6c8e3c6cef3dbb48",
    "9fce30134187d9564c8075895b31dc4c76bf805ffa0fbdbe1774e73413732fe8",
    "a060df77fd6a4381fb387bf6a7542f5dadcbc1aaf503af1822841bd6e78e8724",
    "a1cee57a9bd0e7251ee3158c3fcfe68ff502166fe65a93c7c126ff072a692214",
    "a3b71516b9d9ff32a9a2263b7a9789f58015519aa304efcf03d57a984883be22",
    "a3bcec54a97687c9c41bad6d02894980e6eb853b9a39ac59c5183e4e0b811fb4",
    "a4a14e8519be80bf35fd984084fc0024e1394dfd7bd59fec6494e336b982fb09",
    "a4d33c46e3ad376ac18b40fff2ac882d4b6b0940453f24f1419127a65e9889bf",
    "a63a0c1e51805ad49eaf163203aefacf42a455eb977d12099ce494c3aa82fdc5",
    "a71605bca07b522291922baf21583ab038eda523b45a0843106bdee1b5ace7ba",
    "a791a863ec3599e39b889d8561d6288c44fc7a8c86403f7508d0df4e2f796dfb",
    "a963b3169ef85270d91f72715c64342ebf5a90b3b8da1241298ef0f794dd4d3c",
    "a9eabe6a9ffa43198328edf564f3cbf1c0db3296c26790dc8bec04473db9d10f",
    "aa02a966ff93f631ce6dde6e42a3e498d074e5e968b543c37aaa50cc98fd1485",
    "aa9928e41ecd5d000c5411379995bc8d41b8e111b44574048798a31efe651169",
    "aabfe68c24aff2de59a298a439f2468e817713a6da2df4bc2b50d4b11dbd184d",
    "ab9ddd6bf85e4856455edb096bf1e2e9a45deb32f67b27be04753606f0088850",
    "adad24b31f1d61e96a5a2069afb6cb4263d5b3fa81a9af5c75abfcdf2979f1a4",
    "ae6e6e2c34c21ac52706252136dab83fdbcf2a939d8d62a6772e75135f23cfff",
    "af78e2bc8e82dacc40b88be45575fb8554008a962ad632c70bcaa33a9483a42e",
    "b06af9fdba71a6b7e9e5ea6f42661e5d766c91fe277488c357d28c4014d96cfa",
    "b0e19c767384973713afd89d556229cd6f273750ccbc8c8a083886759f03832b",
    "b26afcd2097c5b83ef3d4287afc49a4508beb035223e9b14e81b24f2299bf4c2",
    "b2b7a9c8a6ae91c7d68a7ef0d4ff46781732de9d54617a338a435b661c9e417d",
    "b388b0476279be82689a2f2575e745614b55678bed6165b1c476cd499f76cfcb",
    "b38cf9a499172cead0ed99b71f72a622a95cb49c74ec992cc18c56cc90dc8ace",
    "b4cfa6b6d992f6d052926360cf9a79fd8918faee3344b7c657a5f846cc5cb32b",
    "b513a56f95aa9f126c67af181c9fcb732d6d2d01633159b2a4dff057daf724e1",
    "b5773cd0c804e6718fe35f5f7e7b299ff27913632ee8c07f2dc507f201b1b48c",
    "b58febf2aa6ad2a424abafed61575776006c5a0df7482633f44bfe74e735b513",
    "b6f55d3d66529d5290ec1e2e69ef010e9518fbf839c75efc5958120698661ae9",
    "b888d2aad1f1cff79846c43223530a7a10ed062eb99d0983af2e093e071778e2",
    "b9081c22d26ba3cea0b5ad30fca5fd6b0cb0add8039d936afadb3210daf2cc71",
    "b91f75cbdc8a17e6accee43e95ab8c91a5e77c975311607aec15537f5a4350a3",
    "ba8832d2cbbd0093f7d9616a7aad1b08beccd7d98ecd64a17a6055f10c3be317",
    "ba9f05797d80767af41a33fdbf3947574f74372bc6389995076f3a42f418fe46",
    "bb4c2ecb4f4635b14eac61cc55649cfbe2f8bf4687c1d961e40f0836c74fc735",
    "bb75e72e07e2db847480568ba8b60541924de74ef27d738acec89d3b700196cf",
    "bba9557ed27457268b82b65aaa1e944cf85a249be30a4db879c80bbd7d65c944",
    "bbbf46444f1d424ee1afdbb4d866842c91c0af91c4e2c6e0d5c47b846cdb4100",
    "bc5bbacfdebf5ba5b83657c98dabe49cbdb601add72cb6e8ea1937713cc8dea9",
    "bccb3fa8f00509ce7bc76d6426eb5563e84857a91ae3130dc139ad741aa5da61",
    "bd11243bc6135330d6fe9f980a0d985ab22ad4faaa75fba4bafe646e088442f4",
    "bf2ef287f0e8b1a443babc3bffdd33f71cdc23c5f2d72304d500a19b575f13cd",
    "bfe46447bcba605554de34271a1876fe3b22c4d8cb2071285ad37a5beee7e3d2",
    "c4bb0303ce12b3bd2d2f2669b99de163480a9014a9537de2d513d1b7e42985b5",
    "c4c3133c627fe572dcc437c2b49632d0e36b99d6632f6434d1d9efd57dacdefc",
    "c5081bace52ba0319e6f3acd66d976f3588137fab3782b4cd487df6322fa357c",
    "c55fd05dbc6babe9ed911f007373744c23a795c3230b5cc6424acaca817e4a3d",
    "c7e3281e6516a7271cb8d18fc08f59ec2895af691de961ad3133de83aa5548d8",
    "c7f22c60ab1485760585a8c4feb3d6290f72ff1696f5b60f1ed31b44a1b0bbf5",
    "c94b09ae3bc5b3b9d6d5b171278d85ef7393bcab13c24eafa3f591bf6b1cb6c0",
    "cb5606fb79f2ab7b7f026dc6789fde7ed906d0135a8a60cc68ab655ef68a0da1",
    "cc5053fb13bff2688906d8778ce3b0a8f36de65529901b84d4b0960f454ee534",
    "cc5183c91e973e426f5e3e8e9a29a22b9d0b61b12ddf59a51b01c85fe30606db",
    "cc931cfd40e3f21bf5b46612891f0ce53c116904d089e94f332105074d84d333",
    "cd965a34db68c00dcebc122390fcb147df0f8f7370d6ac475d76549c8b496ed6",
    "cdfc65b588de8a8ad225a751f79aab5cb9c235999ea02db56403e5f3e516886a",
    "ce945ef60a90d37d22e4c355a36c1b072e6c064c98822f6d04c10fca8899922a",
    "cfde2b148569eff4711f8087c921381f946948885e563be9aff6b1441c2e03e3",
    "d012aa11cfa709cf1438b00dd69510f1b75856a5f49a27d539d662b937dcafe0",
    "d025894b4c536391ed613084d725fdc84fee63b1ccac9c33617c74276bcd87d7",
    "d06e4985d2117d5846b20365f973d62a228457a3bcffc6e0a4a05eb1a11841b7",
    "d11113b3a1fbd04756fa57f1b9e74f867774a5f54c8593757a8676a05a793bd9",
    "d122a0e9dd54cdf2a985fc5a294b4dc2fff5a3997b6af1f73f3db3490db23fba",
    "d1334e6399ce17c5e5dedd6c6f2b97b8f512a71fdbc329b6f058fdcf381b79aa",
    "d30c9f9102a513445d8722fe4082bc689fbdb9db5670e1b9b4b571f7c112c830",
    "d4048c93b728132f88d98902f1054a0be13e3e6d34222d12f88acb178b0247eb",
    "d49b918cddf1a2b830eefff30fe55362b46943195604a1be62023321ac038048",
    "d64c5b11eef50ecf03bfc48668a97856d53c645fe4e8afae061d2a593c30f8fc",
    "d74ed219ec70e9abe7fa1c65359bbf2a6b6c4795f4ba80e2b15a88c22ad00921",
    "d7caa56385154bcab9217ff075a67dedc178eab3846be5fcc25d5dca573c77ab",
    "d84062a738afee6d41d6631395c86fccdf6c98c0384a89f60285497007a34fb8",
    "d8c200bd131633a20bdb6e5ed89bb036d4cec7c7368df645a85d150acc4b8bf9",
    "d9113c84c04f6a71857a11152634f650b83daf116b1153f3595760d97911a2a9",
    "d912f9d6d878039fc4f71b93c3b84bbd3d2d2f9a39f3f8849cfd7f5a079c083f",
    "d91a989968f5333ea22b5b3ae228909ab84a1a2806694fbbd52fa32fc66ff24b",
    "d924a297cf250e1cf3260dfc669cc78396686b52ae14554c74067f579d9461f4",
    "d98b166596c609d808e7477f22522eb96316722ea5f3e28c83d6ef29e4218e30",
    "d9ebc0196c8ba2a1f1a08ea89380e2c310e72f3ef8b9a8c7665647e58dab4027",
    "da28e029267a60702cd374af53086e2e76659b14af85cd07fd2d90d54f47291f",
    "db7406feab870c4cae2d7731df44d29df67f15d75fd5fed2dcb4a4a9b323879d",
    "dbf1269fd0386acb5167e89203feeb73cd4817e1deac61c970efdf9b1e1fad7a",
    "dca43f6e7179365df4c2de07821ea77bfb6ef15d28966c9e056f60c5c9d8fb8b",
    "dcdbb65e25c7dc02b3c3b418513d010ed2eb6eb196bd4142e028b41e34a571b2",
    "ddd36e8bf4ccd02050f7ab8474fab325e3e6fecaaf43731917a1152f556956ad",
    "ddeed44798ed09960a09f78fd76f2fc21b1bab32b9ff19ae7b47cfc011e50b38",
    "de4ab97a5a77278d86530216c76977a09f5e99d19d6ad87a2edd1cce855cdaf2",
    "de7b9e7199dcdbaf619c98996625f05f7e3c80a000a6d042e72824ff7f0aefed",
    "ded0fe34795a416601b8e00c28b7bfd50a3b16cb9a4b7ba51966553cf2b340d3",
    "dfeb2123b8281b68ef218ba6de3a7d6ae859f7f87649e44c697d45cd098c28a6",
    "e05a490da4320628a5579ec2e563275f684ff0f3363c9183c6ae5f6d49b8e3ba",
    "e0deef60a4d00b1bcc6c8447b6dfb3767a1cc555f0fc9c06c333a32549be880c",
    "e16c7da1ca8c231deb209891988788d0e89c83a0adb1564a6d4cdc438d591ba7",
    "e2e1b63ffca15779f32ed03f981b8ffaafa7554ec57b5f5509e6274c80b48c70",
    "e3b48ad67e39536cd11fcb64a32f6c49a2a8e7048f1bc6a41b7e2143c476c0b9",
    "e3c987939b11d7d667c268d65456c2c796aee2b153c2564c89e07bc7d2889cc7",
    "e3ef7dd04ebbfd6bc10b6ba57ac5beca99b96ade058f55469e3fdfb9ac965af2",
    "e5b0eb8a313154a27e28068d05cbb1ce6ba1941c03408bf17b17f3793d48c7e0",
    "e6247da579eb1decabfd83c999f4b04bfda911fde426ccd8d1a6737d4e0b925d",
    "e859e6b08ca9cbc52eb4aa29b9b57de93b14f46ff1b97a2df7b42965193626ae",
    "e8857948b7f4ba3736b52d813dcd0f36f0aca7aefabad6993335fb71c26cee24",
    "e89197a925414536737c2b4cd6d8365b8215202294475c12c90951efdbb7f04a",
    "e954cfa14af9c0c7cc5c0f42b470a9f214e5ef744a9d3710f73accf439c11ff8",
    "ea7b5ae83f79e796c525ffb49ef0071bd983388621b4502be76dccf57e9b00db",
    "ebbf3e474cef6276680d73dfb1afa206762010228300f3b9fd806767357aaff9",
    "ec180b2b2af76907b4db7ff8425bc774c1e6ff21b4ccb8e9c351e4f1a161122b",
    "ed3450b0461ea42f7dbc3e3ed5834f8454392a27c34873dd2d6fb1ba4239597e",
    "ee5a20ff540603c0da72a262102d419e3252ddd916072fef409a8f3ab75b5d26",
    "ee6e6f2d78f536a470be494dc06b1ef36f1f221906abc7112bf4d37a78c282c2",
    "eecf5ea8579d00598a063ab185d9e99c01dd950b7fc90fef2bbacec6d529b187",
    "ef93dbeebf2730563a3b6c31c6ecc81a4d5e1f6b21b9cdbc6f865b5413c9c8a1",
    "f008878fb8aadf66c4217aecfb0541359fad8b36459ad69029c08d1fe39107d5",
    "f07196d7406a176ae451d35e6302530bcf28e59003e3a6331f1af06c8b4032d7",
    "f09b3c27737975add97b6de459f2d6a6027a111386ed702755978235173d0fb6",
    "f0d4025eea4497db2977f6f4f47911bf3ff3cfe9890ef4e9e0da7abefd40bf37",
    "f276c388f1cfb261be9327c8b576213d503931966fc912426eb0241eff8df82c",
    "f2ffe75e0543714fd2c16e82ce0db5032c365d82885792534e4913f55d7ec1a5",
    "f38cfed8ed2c554d4fa036e7f84597b18ccbc72866ac06fb3d8ae78d59e22160",
    "f3a1ea1bafcb8f73ee684a1eeace056c45e847bfe01760932f4cce5f398c417a",
    "f4ecdc3337511547b93c0d0f1cdfaa0050a17c7c30273e3154b5b756b65afe19",
    "f5907f7a4372af44beeb469eec9814b63d2226212271c66600e6393e5fd8fd3a",
    "f5fce92fda0d4edba9745b460d3edc238fd6a5c99e5fe8af0c5187e40a8a9b3f",
    "f7059808d770d6ef322376ec25ed181b539d86b34252f519181e2af66072ef5a",
    "f93ec4db878c507529577a7862367b1d8d10e104c9893c80e8ab93e776911bcd",
    "f9d5821f6adf8863b498c41ba1b74942b98e21bfe3933799edf45da41b2dbdfa",
    "fa5420a3d034a9bbc9204769815e187910f63922328fe5314b4c5868a98267a7",
    "fab25d8a21d7ee4312732a80598968931127f49dd0459ce070086047560f7e71",
    "fb8bf52f34a615f26faf80e47ad378ee265af95a6fdc1e7deaef0109de94c126",
    "fcc1c0d7e285a7048514fbb81e4b089768bd6f038f241532d649ce35161e6970",
    "fdce07f9575ec1804352f8f098abf6f68682b64b535f7cfd0d3fdf78d888c6b9",
    "fe43e1b735751e158b30ac6672415e6d2adba5d69a2338af0cc23f18a16747f7",
    "ff08346a8744a5949ab6bd18f96d2bc4766301af621ff85d5b0d1ff5cea6b0b0",
    "ff35ab8c3c52c595a248238ad0c3cbfc314bab38ff42e0de2e0d5e4694489ec6",
})
# the private ALICE-* repositories among them: the name as a whole token in any text is a finding
PRIVATE_ALICE_REPO_HASHES: frozenset[str] = frozenset({
    "00289dff530adcbd4bcf866282373533717cf25f84225808827c3459032512ac",
    "02bb1b6790b2e8a2894140249b81a5d21bb7d358f2aab42d9347407f8e7c46e3",
    "05e4de634d54ce8abb8661803c1aa175464a2ae2d03a49e3b7013c4be99de4fc",
    "066f5d50816d7f9fc80957ff387b6291fd95029434cf33c23418539c3a29383b",
    "06dfd0d8b0c976c451cd5b82c5aaf6b56e91aa9a5626e813e7d5b9295715c215",
    "08422e835a085d814eedc8f24034c0a0f910343c16b9ea9282f5efe3a3944a0c",
    "09fbc966cb6dc3dad3a4e452c732485065be6795d3597d6383c669d1f0f87fac",
    "0ab49bfedfd1c84d86ff7a613601dedffcde374449caa32f8f1ad6f3b3b272eb",
    "0ba2a2388b3ac10ff2287440a2ee90ca21971e27ce29419f5279fb8c7ab8e95a",
    "0cd218bb263ec9f96bc0d0b0ff3d6b6c7bbea9e1f4ed410e8c92306b207fb85c",
    "1022b7bc0a4b5477c8b919ba383da750233407a5640cc6ad81f146c5d1e0faf6",
    "1244917d04e697a4c3686c1cd0ba63e14a07c91dd318b875cb51d8879e4f4b56",
    "183ecf527ad23e450507c82a12250745ba508574b6d0d9ca3800623abaaced1a",
    "1a192029eec7181485061a394e82da7574240d7fd11ead5df0faaccf2785bfc6",
    "1d520423f0cbc94897548efe64a8b56928b08fc61a42048f17743284fb0958f1",
    "1dc7f34bd43b2db26fd329fe781bebc4a9f77ad7c492cf661c73d02ae2d6fa81",
    "1f0e865381f02428875de07532cb3449b2bac95db570c54dea2f265410fa4a4f",
    "1f74322935b4f3d5347b9ae3ee0fe0952917efa8c1cafb31ab9a04294c7934b6",
    "22e38e6441b24f3e9408532b6f8fffca17e4b25605fdfa4f1868c39ae113f80a",
    "23940e08ff6e19fdb25c44dc6124e31a9ef3bdbc6a1b6514abf25a8e9d6cac9c",
    "2617f90d5dc5d1f7127e2b0aa604b67d6cb4f721862242cf08d43b9d43c9c732",
    "2715b434dfee1bcc28e4b0657637cf8c928d514e68fee640bdd7216cb8e4f6ef",
    "2716ce326d265425673ce3108c5ea31108e41dc8ab35914af5c409a7e40ecfbd",
    "295c559ba8af7032f9cc3cf19f7f9ea4e5d70ba1ad08e130d77ed530f82f42c0",
    "29e62bb8510d79a733ef5d39c175adc24cf8c8415798ca4594c52af63f3f4211",
    "29e8b2581a160a1017ec2b0e68e2ce8cba2b92d1f07cdc038b9f05d6901f2720",
    "2c8a7953168f17461421199d363cad277f1e7f8a45e3c3c5bbfe6d0f4294506e",
    "309e8786fef342a663d7bf68599b009c0ba431c7266a00c6d6ed1445a335ebeb",
    "31627fe3ca40cfa910a940713265cb9490a9b0eb2ee2d44a93a73370643bdcdd",
    "329698749e05e2809ca41cc2e01477cdacdbb76d9e67bf909466e01bb88cc1ce",
    "32cbdd2e35375b3325a7025c092638342687b11f1018aba279792abdcb19d486",
    "34ea14e01ea4cce6502911bae043396bba454f9587c9da863331b4536e26b4b7",
    "3501e3da96f0d8e48517f7539c104a5144e17dfd8123d3b35d018f2772ad8e95",
    "38b7afc98bc032fc42ddc6f14faa7d7a25f599ca13b0e9f5606492ef52c3475e",
    "3d2aad1bcff339869f5664c07a414a47bf4dadb06d2606ee894610166fc14038",
    "3e3fc5c9511a9c0c4bdb25196218a6eb5eae57283c8bb249d18404bc9975acc7",
    "406e51a98520962d808fb8e5f400e80a7c277fce377a200476f16df270ad694e",
    "41c90f0303518fb3f0184a938b9a9cf433dddb568bde0c5b73fa59d0e4424e80",
    "438ddf4e4327da9073f35cebf845803f52ece3d0bfb566d385593363cbf0d610",
    "4709045d1bde2517159b1f0098a23d5fd515b3c7eb47562bf8f0b4642e75cd05",
    "49b2f72df48b75439713dff88cf666075699c20ae2bae7a0f7ea3b32c6fdab94",
    "4bd24b3602c298f2625a84ff2a9e6fb7241de941b0694872a71882e2e22af068",
    "502d534e56760eeaae11a1166f37843553a88578a333b6e94348662618d94497",
    "50764a9f81c5e9d65296d6e36aafba55227d30a4c0fd014cf9c6b1d0c242b1c9",
    "57a0f1d3ad9a421d201ce39bbbc5d6d49a9a91703cef23c1d8606fcf379cdbaf",
    "59cc3b019d01832d1f1f447fdb9ec164e6c87fc60d0fcec54f43b8cc324e30dd",
    "5b35aa244787eb89568c6cb0b2b7fbf82ac2f29481b0bc4f34bd793b7ddfcf99",
    "5d5800580a8b288480523b26965a459e78a2d030c16c1609b05efde3214483b4",
    "5ebd1555b7305a9d6299197406b923f7eac1e845b2c72fab2890056184a51835",
    "610d7e3e6c19b81596bf18a2da53428089d6d73f3c6874401a38b852dc8a239e",
    "61c3c4cea963c0e0b6ad5922d20f031625669f1761903f32bfa9ee39bd8c4cf8",
    "636fd8c0985488f9915ecfa3ace16c6707dc1b3e6037754c5549862e13ae1c28",
    "65cac5ca39f9e859ad7fb63129244ec9d8573a22a96be323c7384cb90f0755ca",
    "666afec98fe2b2b370cb74895d195d4f0d1211c47989508953e4f2934236102d",
    "67f6a83553d431b204a9b28757ef91fb4776e0fe53f1aa2861233ed1140c80a6",
    "6882cdf35d2adacd497f761de02d6327055a3a1284016e84b7593f786f3a3b5f",
    "69140f986f64ce530343f039d1613df5e62080edc19b9395a6e61a061df80d2a",
    "6a42696037030098adb19e3848b99e2a0700831cfb1742ae7f2bf9d79bc828d6",
    "6b6e7f769f66854858cd684080f9454bfa24e67c1d1933f5f0f2be69013c6cd6",
    "6e07db411a727cd1493b69d79e736d49224119e3ad562b73849cac5af96f163f",
    "73e184faac76332125cf8ec86c046595f8437e831fd3fb1985be890a7e451c14",
    "751efc4229b0de3110841c0f7b5172b0df7d6da35c185081b8f935c77369f515",
    "77dda3a5255b240d5db893310a519031b368dd2325eeb7e96b42f8f0ea36b63c",
    "7cba08f306d452f704721cd1825f22c7eecb9a1418c609a67ec8a09879c11955",
    "7d99b6a0f0989b47834e2634209af8cb4df955fa156bad62ea2c1a029caac289",
    "7dc1be853cb6601a99ba35b637edc5023220de65cc571044ec0dd5a9e874d13c",
    "7dffbf0c799773d6cfc788fc48aaf7cead9a431a0877382a599d9c2fe2a91b7a",
    "7fa8f1451476e06f1ffdd1f482c2e75d0c80743dfdf321985bd535c97ec38f26",
    "81f43b23baad1660fff84754286fd5e1d78262b9de18fb9d46fff2dca2dbabb9",
    "827af82d323bd8702074c9aae6d06d1ed302c17a4abd100caf82a1396249a7c6",
    "8481df23458febd0dcc158f5a8c0d3758df48648fa927c4042177ff7d9eaf672",
    "8751122d94d6473c8b8136ff138fb3452a713ddd641688b8089076dcb78bc477",
    "8ac84c6da95c28a20aeee62e50c1570964d22a1726ef3f95fd0e0309ddcb899e",
    "8d3ee35de93e4ff474057930d533c0d7a64ae5bc6305566ee9792459eccbbad1",
    "91ceb04ffd83965390e28e0bd2aa3a12efaf7b29b1a43ec505b7cd136b3fa8dc",
    "91e168d347a0aad7fd83433a95cb23dad1c943ad835af20ac85c18a9c2b737da",
    "93db1572714599b03c107b06daf8fa9cba99e815fb2d0b7d749ee17d2ff94a03",
    "97a044e7fb319814847a53fa7df2c3f286618b0ed03f6b3552cfc450050731a9",
    "980422c4226dae23072bc3bd16b0f74a9b28c08f059d9ff19e9a8c4a229c480d",
    "9cbbb4fd0ebf7bdf3fe83cba3aaac75e3f9779fa67bc382e861f7a9042e2a61f",
    "9e03080d592c35b5213dc07a16e67a0253e1ecb0461a5bb32bb9b7a19622e6de",
    "9f204551eb20d33d89f7cb73ce39d6afded9d7a9c53a734fd3a525757e8b234d",
    "9f4fe6461e5cff9fe937893c87bb63c0f12f3aafa5b1048d6c8e3c6cef3dbb48",
    "a060df77fd6a4381fb387bf6a7542f5dadcbc1aaf503af1822841bd6e78e8724",
    "a3b71516b9d9ff32a9a2263b7a9789f58015519aa304efcf03d57a984883be22",
    "a3bcec54a97687c9c41bad6d02894980e6eb853b9a39ac59c5183e4e0b811fb4",
    "a4a14e8519be80bf35fd984084fc0024e1394dfd7bd59fec6494e336b982fb09",
    "a63a0c1e51805ad49eaf163203aefacf42a455eb977d12099ce494c3aa82fdc5",
    "a791a863ec3599e39b889d8561d6288c44fc7a8c86403f7508d0df4e2f796dfb",
    "aa02a966ff93f631ce6dde6e42a3e498d074e5e968b543c37aaa50cc98fd1485",
    "aabfe68c24aff2de59a298a439f2468e817713a6da2df4bc2b50d4b11dbd184d",
    "b0e19c767384973713afd89d556229cd6f273750ccbc8c8a083886759f03832b",
    "b2b7a9c8a6ae91c7d68a7ef0d4ff46781732de9d54617a338a435b661c9e417d",
    "b388b0476279be82689a2f2575e745614b55678bed6165b1c476cd499f76cfcb",
    "b4cfa6b6d992f6d052926360cf9a79fd8918faee3344b7c657a5f846cc5cb32b",
    "b888d2aad1f1cff79846c43223530a7a10ed062eb99d0983af2e093e071778e2",
    "ba8832d2cbbd0093f7d9616a7aad1b08beccd7d98ecd64a17a6055f10c3be317",
    "ba9f05797d80767af41a33fdbf3947574f74372bc6389995076f3a42f418fe46",
    "bb4c2ecb4f4635b14eac61cc55649cfbe2f8bf4687c1d961e40f0836c74fc735",
    "bba9557ed27457268b82b65aaa1e944cf85a249be30a4db879c80bbd7d65c944",
    "bccb3fa8f00509ce7bc76d6426eb5563e84857a91ae3130dc139ad741aa5da61",
    "bfe46447bcba605554de34271a1876fe3b22c4d8cb2071285ad37a5beee7e3d2",
    "c7e3281e6516a7271cb8d18fc08f59ec2895af691de961ad3133de83aa5548d8",
    "c7f22c60ab1485760585a8c4feb3d6290f72ff1696f5b60f1ed31b44a1b0bbf5",
    "cb5606fb79f2ab7b7f026dc6789fde7ed906d0135a8a60cc68ab655ef68a0da1",
    "cc5053fb13bff2688906d8778ce3b0a8f36de65529901b84d4b0960f454ee534",
    "cc5183c91e973e426f5e3e8e9a29a22b9d0b61b12ddf59a51b01c85fe30606db",
    "cc931cfd40e3f21bf5b46612891f0ce53c116904d089e94f332105074d84d333",
    "cd965a34db68c00dcebc122390fcb147df0f8f7370d6ac475d76549c8b496ed6",
    "ce945ef60a90d37d22e4c355a36c1b072e6c064c98822f6d04c10fca8899922a",
    "cfde2b148569eff4711f8087c921381f946948885e563be9aff6b1441c2e03e3",
    "d012aa11cfa709cf1438b00dd69510f1b75856a5f49a27d539d662b937dcafe0",
    "d025894b4c536391ed613084d725fdc84fee63b1ccac9c33617c74276bcd87d7",
    "d06e4985d2117d5846b20365f973d62a228457a3bcffc6e0a4a05eb1a11841b7",
    "d122a0e9dd54cdf2a985fc5a294b4dc2fff5a3997b6af1f73f3db3490db23fba",
    "d30c9f9102a513445d8722fe4082bc689fbdb9db5670e1b9b4b571f7c112c830",
    "d49b918cddf1a2b830eefff30fe55362b46943195604a1be62023321ac038048",
    "d64c5b11eef50ecf03bfc48668a97856d53c645fe4e8afae061d2a593c30f8fc",
    "d84062a738afee6d41d6631395c86fccdf6c98c0384a89f60285497007a34fb8",
    "d9113c84c04f6a71857a11152634f650b83daf116b1153f3595760d97911a2a9",
    "d924a297cf250e1cf3260dfc669cc78396686b52ae14554c74067f579d9461f4",
    "da28e029267a60702cd374af53086e2e76659b14af85cd07fd2d90d54f47291f",
    "dca43f6e7179365df4c2de07821ea77bfb6ef15d28966c9e056f60c5c9d8fb8b",
    "dcdbb65e25c7dc02b3c3b418513d010ed2eb6eb196bd4142e028b41e34a571b2",
    "ddd36e8bf4ccd02050f7ab8474fab325e3e6fecaaf43731917a1152f556956ad",
    "de4ab97a5a77278d86530216c76977a09f5e99d19d6ad87a2edd1cce855cdaf2",
    "de7b9e7199dcdbaf619c98996625f05f7e3c80a000a6d042e72824ff7f0aefed",
    "ded0fe34795a416601b8e00c28b7bfd50a3b16cb9a4b7ba51966553cf2b340d3",
    "dfeb2123b8281b68ef218ba6de3a7d6ae859f7f87649e44c697d45cd098c28a6",
    "e0deef60a4d00b1bcc6c8447b6dfb3767a1cc555f0fc9c06c333a32549be880c",
    "e16c7da1ca8c231deb209891988788d0e89c83a0adb1564a6d4cdc438d591ba7",
    "e3b48ad67e39536cd11fcb64a32f6c49a2a8e7048f1bc6a41b7e2143c476c0b9",
    "e3ef7dd04ebbfd6bc10b6ba57ac5beca99b96ade058f55469e3fdfb9ac965af2",
    "e5b0eb8a313154a27e28068d05cbb1ce6ba1941c03408bf17b17f3793d48c7e0",
    "e6247da579eb1decabfd83c999f4b04bfda911fde426ccd8d1a6737d4e0b925d",
    "e859e6b08ca9cbc52eb4aa29b9b57de93b14f46ff1b97a2df7b42965193626ae",
    "e89197a925414536737c2b4cd6d8365b8215202294475c12c90951efdbb7f04a",
    "e954cfa14af9c0c7cc5c0f42b470a9f214e5ef744a9d3710f73accf439c11ff8",
    "ebbf3e474cef6276680d73dfb1afa206762010228300f3b9fd806767357aaff9",
    "ed3450b0461ea42f7dbc3e3ed5834f8454392a27c34873dd2d6fb1ba4239597e",
    "ef93dbeebf2730563a3b6c31c6ecc81a4d5e1f6b21b9cdbc6f865b5413c9c8a1",
    "f008878fb8aadf66c4217aecfb0541359fad8b36459ad69029c08d1fe39107d5",
    "f09b3c27737975add97b6de459f2d6a6027a111386ed702755978235173d0fb6",
    "f38cfed8ed2c554d4fa036e7f84597b18ccbc72866ac06fb3d8ae78d59e22160",
    "f3a1ea1bafcb8f73ee684a1eeace056c45e847bfe01760932f4cce5f398c417a",
    "f5fce92fda0d4edba9745b460d3edc238fd6a5c99e5fe8af0c5187e40a8a9b3f",
    "f7059808d770d6ef322376ec25ed181b539d86b34252f519181e2af66072ef5a",
    "f9d5821f6adf8863b498c41ba1b74942b98e21bfe3933799edf45da41b2dbdfa",
    "fa5420a3d034a9bbc9204769815e187910f63922328fe5314b4c5868a98267a7",
    "fe43e1b735751e158b30ac6672415e6d2adba5d69a2338af0cc23f18a16747f7",
    "ff35ab8c3c52c595a248238ad0c3cbfc314bab38ff42e0de2e0d5e4694489ec6",
})
# <<< generated private repositories <<<
# the committed set (the tests swap PRIVATE_ALICE_REPO_HASHES)
PRIVATE_ALICE_REPO_HASHES_COMMITTED = PRIVATE_ALICE_REPO_HASHES

# The private repository names a public repository's text already holds, as a ratchet: per repository
# (sha256 of its lower-case name), sha256(<repository> NUL <path> NUL <name>) -> the number of lines. A
# tree with more lines than its entry is a finding (a new mention). One with fewer is stale: printed as a
# note (`stale: <key> <lines> <entry>`), since only ALICE-Law can lower the entry; Conformance (live) makes a
# stale entry a finding after its grace and gen_private_repo_hashes.py --baseline live --check fails on it,
# so the count only goes down. Written by scripts/gen_private_repo_hashes.py --baseline.
BASELINE_BEGIN = "# >>> " + "generated private repository name baseline >>>"
BASELINE_END = "# <<< " + "generated private repository name baseline <<<"
# >>> generated private repository name baseline >>>
PRIVATE_REPO_NAME_BASELINE: dict[str, dict[str, int]] = {
    "02a148b01541d8673d3b2ada504d8e3cdcf7725fefaac791004ff60620516970": {
        "039c868536717ce1806ea8ebe24b2b5800106e1ff6c90dc140ffeea8367ffef6": 1,
        "060a693ee68b1ebf3e5f3b70cbb5eee990b26ee755f5ebfc6268a924b6f5643b": 2,
        "2092cecc80d2f3894406895e710b05e477afc3542fa6f8ab9ce080c8d86c1633": 1,
        "262257dfbe122d3bfce69dea7184fe68613fc4cb7e16e636a41e8de85534b871": 1,
        "27a55539ce71bad2d8e87b5e49dcc9755cb03e3bbac007afdb4fb7b2bfb2edaf": 3,
        "30a9fe344e738343637a6f5d5fd9fdb2992cf9b6cb042228ac7f85d06cb929f0": 2,
        "354d76ac9151c2622d032bbc7fa1215c8f8a47b3e4fead6c5fc0574ed824f874": 15,
        "360886e3824af8fda73f01a620ae166cbd16294762d7bd7826bb21445b20fe3f": 5,
        "3a695a671527dff4c358fa6cb120ae4198adc024f37dbf6901c6e0c0f462f00d": 5,
        "3c155798391a71fe4bf41c0c293d7a493a7c5dd3f96d366ab8b416c404e2f72d": 4,
        "3f6099da731834958fc099e1445dc648125bd68b0c17a00a87029dfc130bb316": 1,
        "50c135154398ac5d4ad90cee1ec8aaa49ef6118850b7a9394c9564cdc154bcbf": 5,
        "594f0faf9a9202ff1d655f4444071c41fa13e3d40264bee0e78ce578c338fd62": 3,
        "6d5a3846922386bea9056d3c2a40f1c6b8649dddd1b47264a5eb808540a1f27b": 1,
        "78abf593ac510ccb283488a44898a85dad094741b39fc391190cbcd745bf3489": 1,
        "802e32b53768f899afd3ae0f2fe95ce51aece5009f6bd35ee35d80559bf5ec09": 2,
        "90c579c73b277f2bfd985bbe6a3ad0bfa471a85dec8e12470e915adea7d72aa0": 2,
        "b770f5deace8d2940d7ead36260acbb9ca68150907dd1780856875b4c3505b52": 2,
        "c5ba5662d0c565c74df86fbfddf187178675dbc6a8d46f367624c4b9cc517c34": 1,
        "c81a169b02e830dc09b8e213a9d1e19557a0d28db494a333385a2a037cbf5e38": 1,
        "cb0a406ee68847330921a9079f8bbc08fb666f9aaf35bd2124923f2d9e0b97ea": 7,
        "cbfa97f14e809aca38da3cd0a2303f8990f0c613a2f52feb7fd0ea769b0e5eab": 7,
        "d2a86d3cd681927713c1666a3ad7bfe84b97217aec7ae33cb077a4af3344917b": 1,
        "d3c4b99d9566f9fb5c1f47536a14c554885b76b73a35d0772c89eb99ff378190": 1,
        "f62cc9dc4dbd72ddbad21d43a664dcb29416ae68aa675413c0cae0c022bafd40": 2,
        "fccfb9e3b68bbd16185e443777d3b7bae3e25eccafad48d2c09f01649e0484fe": 2,
    },
    "1a82b9a08acf92623279000cb4e3b49a969edcf8aee1c50fea043cba46ca444f": {
        "0944e0678917e60ba506b521919607c407b4c24addd1c7ec0028757eb3d25345": 1,
        "0da810d3969be8e122e4bc294f20c5d2d89009c3759d725ad431a5ac96a427b8": 2,
        "203a3046e2288ce2028c91ca83795d4866d1cd09766431335f76fcdae5e4a20f": 3,
        "2a81884cfd6725a5db84d5fd15a22ea43f357fb2e00a1ecb193ad60fa53a01fa": 1,
        "38dfbf937f963dcdd616f94c6587b725fade8b696e27ee64ad25b26cac11364f": 1,
        "5a2cb4325faedfed0a20921253aa2dc424f39ae66eeb23f14f7bb3137846496c": 1,
        "5c32c63bd80a621deee89bbef61a30ef552b6eb9d53e35724465e00e9485ef9c": 2,
        "5d241eb65097f633e79e711c6ea27ed9ebd71d3574af6925db133221b904899b": 1,
        "65bfb065a49b32209a3bda94726ef626c91e1368806a93f6647752ad112ef122": 3,
        "6fdcd0a1189217c5ae7924ec0cd5c98b95ef2690b251b57c63ba8181e3af5911": 1,
        "7432ef101b5ca968d9d28c53326d7df35f0f159425cbe07f69d3d41ca91659da": 1,
        "7ef681213ee1d94f0f32320a1f05739b1c5c7685a2aadb8fdc012dd14b2570a6": 1,
        "80d945d51d719d012ae7165abbceb7730c7185d03063a25cb62220217a93526d": 2,
        "84007b7f157b6df274010286ac5d911630ee0a145ce004a536c186d62961d8ac": 1,
        "85956768d0bb5c8e52e1b310818790fe8f7ee6a91592f1b41301de9bc504cd1f": 2,
        "b44db151fdcd0d1d14cc3e9a085d89026a64a6e494719f84f093b7619c903fff": 1,
        "db3dfe543f8812c14b66c6ce9ee00d1b5924e264939c5ecef8bd47705003fd41": 1,
        "e6af9de8aedb19dc90f768ed7954d858e2bff57f531205a992b6c959f438eef1": 1,
        "ee0bbcdb63da7f941590dad6d92c00a3bed647f62caae9cbca1bb1e13bf2b109": 1,
    },
    "247d30e091213346d42fc7d93467a962fa9f5f7672208e3b34d9f30eacd591d7": {
        "3e509374040a674a9aaebabaf31e1ad9a698a35d3a99fbf39ad0f9e7021158d3": 1,
        "d95006ff7f5c24bf0e09d629b597a94a5795d88a4acab033430c6f3b6e9aea00": 1,
        "ef3451babeb5367b1207aff4470b20584f8bdde109b483b46b79f7246ce24f3d": 1,
        "f5d14440ee8adc19c96670797280e666ebed633753beb843e42bd8f6ffe905df": 1,
    },
    "27a738e130f2f267232766dd944827fcd2e880450050ada00d8448b8240b6f80": {
        "3790b666ecbec8e959b88eaae5ff4e0282570dbd7fcc2d2a76d419ce37db14fe": 1,
        "58a17879edb0a51d652ee623d3f54d9f57114db8cd990d6797a991fdec73aa0d": 1,
        "a2800a8ff9c2eb46c9bed294c2ac40d6e1bef679bb5e7be5f890d8fc2b18a198": 1,
        "f78a2ba8c2fc41ffa52a26b566820bdf39097825e0e69af5ffadcc0b8772f24f": 2,
    },
    "2d25a5fa6a252cf735a1e65d348bde05d7e3118cf9c3bf0671aadde5295591b5": {
        "14a773ce29b52ac9e1a73289f54ea92c74f17a52777f77b151a4efe03c4fb5e1": 1,
        "473b08b9fd399da233c783908d683ac3b058d3e1bc4d46d6f5e3eb49802dc4e3": 2,
        "74d18720d903d90a88246e8fa521bf62620f598f8a8bdb43e7d8e4c94e614881": 1,
    },
    "2dcd0e15a1ff982c2aad268374e16c68d1bf0290b4981eab6de043b0b5f85874": {
        "006a5b3ea656dbf8f3a8ad01cdc392144da206df2484069f474847f954e1bfb3": 2,
        "09d8fcbb2a9bf21cf7156571668a2c1fb88c32fe7db11e8c92bafff0df027093": 1,
        "1411e3b9b70ea5111be77d062316a83b65cdffb921e68f45df18f739e875bf09": 1,
        "1a7be309f3d0d23792f8f720b262936105d2c1b8f636d522495dc583f33aab51": 2,
        "1cfe56ea087bfbdd617e655eae95247eea8c31d99f2b84f146d305e2e84c4d42": 2,
        "1d8c17d454a097ed9b0136c731d9205d4c596eaea86180f28fba255a9feb3652": 1,
        "1dfbb0dc8994c309f41cc85f4f38e8bc622491ab77671be6de59adf042aee056": 2,
        "1e3095635fadbe551a7a3e5494899d571932004832d98d76655f2c5d7358a28b": 1,
        "1ff1e00421aae30448a01b7595dbb0a57fbe55c1e2f81f1fd00f1a3d04e61c8e": 2,
        "205742a0872ba1e5deb396761a42705c6dbf08c7d65a73d09a57015c90c07113": 1,
        "285a3fec8f5dc920950d8978bf2e9781176acb6ab2c77ad1cb45afa6f9085d8e": 1,
        "2922e7f3a5fe62dc1084ec06873fdbad8d3dd412bddad11737baa78d6a713ab0": 2,
        "297f883f6d39eb72919173feb70df342ba525fa28efcccb0085e06ca18d8ee68": 1,
        "2c637ce1c0c12464939797e7b7c9f4f222e90ac1f9246ab0831ea1650a48e781": 1,
        "323639a18174f991489dbeb3a03f47c50dee1a596c97ec73dfe10a8561683e86": 1,
        "3257ab52de044c391b6446dc9e4d46b22c5df95ebb66d0002509ac6b0d964dc6": 2,
        "32a6506cba35ca7560e5c7682cc40be2f36cc4934d7dee6d8c66a208b73a9ec9": 2,
        "346f7afb077c3bcb74c7524e452b997e37e9932c6ad7074c68dc9417608fe869": 1,
        "34b6c23b8e32d3289fa381ded234a55864e0e09b70c34d79f08b933128bfd698": 2,
        "37c5059fb7fd3f015c5899cd22685a802783fd6902ee5e3e3427fd10eb6f6f83": 1,
        "394d3518f346ea0fe79341b0139730abb074e7833f7344087a08a0b87f882e6b": 1,
        "3b1aaaffc4b213db345d029d4037c54047ee9a3f2ac663c3009c6102e1448894": 1,
        "3d2a5f1efa060e8bc8775aea2cafea6fc0da883e7e1fa34cc7de32c3971ccaaa": 2,
        "3f2e30372474be1361108f3fe909b1ac13f9366cf554b34d52d89f1a37b34191": 2,
        "408e408122f639166c63f645a2134362827c8528cf90d4c52250e63d8accde41": 2,
        "4201356e40276f4c29d5fcfff4d189e5e2cad231eda101352f3d135490f2a65a": 1,
        "44a1ecc44888561c27060867aa1d0bab050784d1fc12b3dcadc18bd503fd8b1c": 2,
        "463b42d3e32b119c47a07164971c9c811a4f06d884de65057be5f8d917004485": 2,
        "46bb20b2e59525895b43907daff4907ac3ce206cf5719399998f5b9c718e5122": 1,
        "4c731a1d0a79e9634901aed2a364e61548b641b546936b8103b4b02afae38f9b": 2,
        "4c7e29274436f32501143ef257f844f934f18811cef9a652e1c645e5cd30b0c5": 2,
        "4e4e7bb1d7ce653855ffa936ac62e96910f87aa213e05292ed97df38ddc69f54": 2,
        "4fdb99be4bdafb96f6006a3ef463c388d9f6e5b221767c9a43e8f37007e8c3c3": 2,
        "52aab4dc609c0ff5a6495edd0cf14e077d9aa3ae7c9d4b1e17ae87c03392f1f7": 1,
        "52dc09068af878a8d945b29551ca3e694b01c4183046aa2aab6aa72b71489af0": 1,
        "56556fce5979140862f41feddefd2f2db8ed76a013e48f1190b7ebf3a0f6f27d": 1,
        "575a2976ac5f7b4db271923c1a97e2ddc6736f19587f33abdb84e43bf3735b98": 1,
        "57a5e225ce39d3df1ad4b74653c1ca231fdee14ed9359dbfd16550b2c01a010e": 2,
        "58440252a21c6f48e957690d2d800239ba4222767ca9b9eba9f6d145a591f312": 2,
        "5ab16d0ea4655429b1b861fb1133b8046b154102006d758743ad59b923fe7e3c": 1,
        "5c473c1d75ff4406bd12f4955e7f5895e91e809cb1f385afe4cf26f9bd9b9a4b": 2,
        "63c9c805de021b8c6a1d882fdaf1c7fd48a25a5daa7bba2c2a81db54fd0b83c9": 1,
        "63e6ee347726ec760d3b3771c2d2db1bb837d84b055a847354bccd8688b0dfb2": 1,
        "659b75857cd2af35b2d91f25543d06b69a386c38f71817eb9bcffd10d71ac3e0": 2,
        "6670480a07f605fbd1cc14f37ebd3aa27145ba6a2ee880b500c60b9107c8dfc2": 2,
        "676242a96c1d189b2b906ec2bf7faf6844bbdc46b135ad5e35c9126851528c30": 1,
        "684cc0e8c6b2ea9e5253ca563c52acddcf7e41651b4e953b50c5f2dfdca66406": 1,
        "6cdab514282c465042954d686520f8f28ee0da4f7698110c9fa65f26eaa982ac": 2,
        "6eb90482e5d9548dbd72b9a8b62e4dc72f6cdd95f82930bab87aedaf17371b25": 2,
        "71bad19b4eab5780b74cd76aec930c32c65734cafadbfc174f710339e1566c35": 1,
        "73a114879b8fcecf1b339f851507fe0df87882a4669615b6f949880f7779cfa3": 2,
        "741261ad53581d70674ab5b28221cba16885cd72419c45b43b7e7407b61f6ea6": 1,
        "77f33fb597ba0688ce55a276aa85c2bd77bf0158df244ed90d09e5ec04598eb3": 1,
        "7845ae5a13696d05b2747491824ef23ecc4f1a5ca6edbc270285f2a2ca529a77": 1,
        "7912de41cadcf4133817a68938a791fbb63d8b87740b68894ad1015d91d42da4": 1,
        "7b06b2d70e7ed0761c03a434e45bc0d723f186aef5660686b7951900b7961ccf": 1,
        "7ea199b53d128e559d766fddec179775d826a684b87f61caa64288ad8040330f": 1,
        "7ea5a9a91f0c315098d420f2ae2b155ff1c523fb7d3b195a3890d876f969ec30": 1,
        "84a8aee4647e2af789b747769e418663f587bcac1365cf654b6642ce5d8591a3": 1,
        "85cf9fa6dbd53376b2fefc0e8520f3fb6117f9931e431c56577da3408f003680": 1,
        "868f17c2d369de2665bc369073704669a3c61a132b139377addef2dd9f37ea30": 1,
        "876acef790795a19aeccf5d6609d0bd89b325e067a93c70bd5eb2828ddf2ad66": 1,
        "8a20a1d80a97ae68bc00ee11e827771550088de66cecae892389ecea68fa1fe4": 2,
        "8abff6c023c9f9184bbba870cdaf867e7a10baf514e71db62e02599605f90568": 2,
        "8bbc8c9fde95e158be5e7b5080a3426a948c7cd71784051ccf2562b8e83f7998": 1,
        "8c8054c9b77feadb6598545c8afd085686cde3910403af6b2da2ef4c679b56a1": 2,
        "8fa4f38fb67df5b499a053b90236495aad2f2fbc62e111cf3b11ff213206e0f9": 1,
        "907b570b414cdd55be6c87cf0e48b57fe4af41736dae9deafdacd55a2c542de3": 2,
        "91e8bd4f17ed7d710bc30ed05de102160700a12e431be1e37a69e9b9fd6579cb": 1,
        "99521b48e22db96edca0bb4164d2abcf0ee9f3e1e238b9c70a8869774b44f0bd": 1,
        "9a37ea8c0a2da254aa55aca671ce498f4fcb356e884e3fb6f3f38f5a8702da85": 1,
        "9f5866ebce7d3b96677924d0695707d11f46c7e265966a01f9a5193da10e4358": 2,
        "a23ba1b1f99dd6d513e022be9390ca432e925673b3fdd0acb47ffcb48691c504": 1,
        "a2c517196af3ccebd98e23ebe4b6a478d4660898ab32ec20c986d0cda90a56de": 2,
        "a3fface95721ab943303fb6724f540a1b3a22530933b3cdeec1ab6c0fe3b3b91": 1,
        "a525e1428113fd59ce93c254094f180afe7e00088259e4ec710d7426702f07f4": 2,
        "a5ebb0969118fa69f0c516d72761059c977283e3aaf4edbc750f9ab65738eec1": 1,
        "a60516baee07d727364f77ed9346eac4204fe39137dff9305835e2ba782259f3": 1,
        "ab2082491c2b6a7777d5c58f43dca19e02f7a373a9e58fa0a06ef6be38f76a12": 1,
        "ae823b5a33224c3fc2b0f99831989045395f1ed97454318f5e6d2e12f92238ac": 1,
        "ae9057a5bcf16d8c33f85e5f03898e8bdfe066628df53bb9ed4790042cfe0b90": 1,
        "af600ca71774c40ef37f98d85ddb003af67d6fea22ea38e1a4cac3cdb8a59305": 2,
        "b0ded4100251d86bf29cf7ec65ce4532ceb6403c4fad4c83e832c4ad506e7f5f": 1,
        "b1919b94986eb7bf0afd3306ef3624156edf03e04e7d0f433a0645f17082fcb1": 2,
        "b6f74b27332803afc1bc7fd4c9bd5bba28b7063f14e0fdf1a8e72aced74e5f75": 1,
        "b8128892d298a5bed3ddc579240f8b50c3037e8aa5452e6ca24b5a5429c027ff": 2,
        "b8937c9f136ef495943dfcf48088d9c93a5beae7a7e94084ff67e50d858c4af6": 1,
        "b8c60239abb61f9b811030090c9dc76147b77a1a54e6a24537689bec43d5fe7f": 1,
        "badec74e4dd2caf5e7abdd2b47969b88fb7f1dcf9755669a78bbe16976a81899": 1,
        "bc612d1645694532de2a1594b97e7958f8ac2bb93dea4b3e9b28a9b4129dad89": 1,
        "c00f9a05060a367b18796616e01675f0476f8adc0a1e68c240768344434244a5": 1,
        "c1c1b261ff7d5423bb109aeea98766de55dbe4b750b69ca1b614ec73fa65b097": 2,
        "c589e19e2d9ef69d6e4034b4976855d89c73346982548a295cbc96efb35e3632": 1,
        "c5f7087ec5ba3da88474eccd6a072e68e34afed5a850c677c4d063e93a85dc45": 1,
        "c5fa3f03f6587e93086bca6a2e60b02f722fd9fd5405043a7552193f3190638f": 1,
        "c7b3265e0ffa2237530f9903ea7d4a4a12e5606efa67260b415a27adcb9319b0": 1,
        "c91e02c67f66bfb13d84466759a7b2d59d33233d3ef1db69c3aed0bccad2c7ef": 2,
        "cfb77947bc36ca046709f7cae5bde57b0ce4411380effa3ac506a7519972b179": 1,
        "d4f258160e26fc5ce99f6d68d446e4a42b838a7925d7bf1828a1067d8774c7ae": 2,
        "d5c35c8d4273b27c93d975374a5e28195f73ffd9f65a8ff0a9423f667a00112a": 2,
        "d6bdfc102b060480d810ea7523add2ff8f5c591c69856c8b8dc068999f68400b": 2,
        "da5c84791ffeb0ae1163ce581e9eb5451b8b30489bbe725c9983cf95c8f8d084": 1,
        "e2f14394df102d61ea5b35a758d760c3cac556109c8df2cbf643205c76ddbcca": 1,
        "ebc55d6b052463dd9064eed1f1471fe6e03c3ebf7b4990decd4c53d30776aabb": 2,
        "f0bed286f29c76985e10f0cedc7ac611345c11fa9e97676b8510d45ed8c4b17c": 1,
        "f150c8c5ef51550f85f777d02be56614ba2d67a6f609c2cc05855900aa6e46f0": 2,
        "f5c6c9b5a3d15f0d8f21b7f501927e25220ff4849ad7c95c10aaa26e99f0c961": 1,
        "f68981a68ebbe61b2dcbc79b6fd78ebf89d8266742c1733bed568855d13c736e": 2,
        "f7c04bca9defa18c90214261cecd2a1be8d3175f92fc5c554a2b2edfc40d50a2": 2,
        "fac0bed3513d3fa01ea709df752276fcc72a39658e7cff22fc43fa2b3bd67140": 2,
        "fc15b5c1d7249592e01af00be6797c532289a05b2a2d5ef091db77b044a332c9": 2,
        "fc8091a9b9b7317b4a0aac6bbad91655db886113c6924a58e14378d6acaa1760": 1,
        "fd9c069fe10196c665eb46e0fe21d5cbe6f3bc80d3bfb3352dbc1f2c456315a9": 2,
    },
    "36d79aabc7e233cb2bfff33359f1e6ac1cf08d2bad9823c7c3ac5f65d9189821": {
        "1d4e9689d6b766b211d960f2d4623b548e3b3316871812a506541551cdbc77ae": 1,
        "41617d17e4b080ebb583ec6787dd6baabbb24a059f7be0b9b57c90e4b723829c": 1,
    },
    "384aa0f3c3fa3d96183aab4ad09e258d46b74184a1e53c698ad105705951000e": {
        "2565c71661a42b939395022566443163c39e4b0d77cb935aa9571b6e652f90c1": 2,
        "27af424583c32f1eb02d4a270676635e96b7157a6ef5c5e2a5620d7f8c5138b7": 2,
    },
    "428e3fa081ee2a407e7516e6bc0357d4c01809ba241f1df76a563076d478b2c9": {
        "55eea1e8c89e04bb6fe8550459e04caedd6056f7c8df2bc9725317f166bdebe7": 1,
        "cf97d7d33b79dd9802519a8a4a83a09df1c17e966420e72a55d4facd1ca384cc": 1,
    },
    "448ee9e3ad0e33e0e635d4a88583bacc8a0649cb6a3b8e0a82008b108d9eb746": {
        "051ee2190f91e5baf64a467131dab91d1aa8b784450f792724f86bc87a904021": 1,
        "0a51db8e6064f004c75f0703dac70ece3520cc7282d2c89a150eaf7ffaa7b506": 3,
        "0efe10da3416ee1f3d81e56ce969a0a5e4d8c555642db520207315bef60d6d86": 1,
        "30c97d2fbec9493d077300fbac8cd07182fb8b2f5646fac24758606ea4529525": 1,
        "43f41f81c2b520a0922fc56adf863868257a1c3c8a3f37d5e5d5413b397fe230": 1,
        "523301ad04bd6e69d710db9e4e7b3a4df3d1b05cedeadeba5e41ebcc04623433": 2,
        "543f2916e4da78da9f7a629faac0145a0250e25418b660316603a0a453a727f8": 1,
        "669daa3c5d6d6f5398a5ed3ed0cb0513d023848bbb8236bf0b476c9e80ff5b05": 1,
        "6e8a33c781a189c5514b5ff263ac846408b53e029690613b904e9d764a8d393c": 2,
        "9e48f24f6cada616564d7a7c89f726d5cf972a3ad7de8c947cc4a9070b0c7d74": 1,
        "b7b04eb166889051bc46797bfb05a0a8b5c5dc55000263de2164419f67dbdb4a": 1,
        "baf54c3771bc1aa77f6b2616231d7be55b1747ae58f4179bfc2a6e9123f00687": 3,
        "cd7b1588f2021a4ebb55a3298c4575de0bbad549d5e0a210285b34cfbcf7393d": 1,
    },
    "4ca21ca72a8759be3a0fc58023322511efbcfe5ece4afbd4fcdd2f84efcbfac1": {
        "aa9e51bcc11d41272289dd5347dc2b6d833dc7b78d1eb4cee9fe4e4a07190014": 1,
    },
    "68f838c9a6c910b359a31d0db5c0100a689066406397a03d2a33823ef1a58210": {
        "8c8d3376517e68ec03052752a9dbb5edf96c2c1b00a2aeaec3abf4863201375d": 2,
    },
    "7d128cb8fb3d29d456bebb86adcc9944ec00909a9c4164cf54c75fbc8909a521": {
        "bd2db41d17fac9e58bfb1cd1a794f8619c2bbc7dbb41de0f41d889b6b513354c": 3,
        "ff420f97255ff4a68ed2c8cb9cf67b21ede1666aaa199f83d01541e3deb59555": 1,
    },
    "84a67b619d09a4c7c1b7c87d0f83d3f40350be0131a8db355676a7fad09b116d": {
        "021444210b046333d6738b67511a0e2ce3241173d8481f8f11230f73e66b5d6a": 1,
        "0c80b9254c31841bd5cbfb3a8f01904b220a8b31acb49b131b0c75833d07d8a3": 1,
        "17ab1be4b4ba65a5f63538296fb72be0ae15042e9dadd70152469b1e11c49f3f": 1,
        "1966d23898215e7a11170e94e1d4c42d14f82c18a454ee3f5973b6da98bbf8e4": 2,
        "2831fc6b1acc656a7e5026040f269da8d5f760e42ab60693a3199694a1ebd400": 1,
        "3e2a51e69550026639bf84a801dd6739353d4b6efa084f3821bf0bfe8d76e553": 2,
        "6f151fa036208f943b15a1869574fdad80c31495c4ca6752a852b6e92fe7fdf2": 2,
        "71c309a1156334331d7b7247a2c5aa895d2b2c3e13c87fe96d5aeccf0627b000": 1,
        "8288b6f1b7abbb925f45ee469ecdd84e63c57aeff1941b2b552635e9b4801574": 2,
        "c82044ad9a07f5597c193f306387bfe31799d3460d9715e33a70ea6aaad7fcce": 1,
        "cc0a0f96f1910ee2c804071d4bc340949fe043caf79c80dbc1fd7a1f0a013104": 1,
        "d254a6598bfec94856d49dc5ff41bac50b491c82b1adb25a460cd2e77614f32d": 3,
        "dfbd99453b8dc0648e66a06a2f26018134a7db8d4505a43b3a3a64f2a1e392fe": 2,
    },
    "880066fe18fc0c44b8c0439d1ecb9285221e18f53b2b3a0bff3f831ebd067b32": {
        "42bf67495ac38d68bcd9568a45ba0e2d2fb9e1f7a66e9c2922694aac9ab8312f": 2,
        "5f43ceb22cf860eace9fa7b4356459aac79b0ad5ee57ba156f918201696177a5": 11,
        "c86a486f24fda3d31c3afcd485f476b4e68555a0cc8b8aff79d161768949db3b": 13,
        "d073a7d2cd2b322e68a93e975286e7406d8caac647d0dad9764cd9ccbff126ed": 1,
        "d5c4026f1214b20be875a1c72faa22cd6e0bf7292d513b90b4071ea649a13629": 3,
        "f9cdfbb9f072988acf356e16d61d5b39efee39072021ef13fac287532f6009cd": 3,
    },
    "89f6aeb1c7c305c9ed233d1d36e35e03117136a4cd33c644cfe2cc16e73fc850": {
        "3c035107f97b2149d7f436df0e9cec4d3a47343ff3fde06ef76f1d9b67a35c05": 2,
    },
    "d07a3e57ba911194ca5c448de2168ed0202752d625749b29c8ee83c707f68ce3": {
        "a7a67e76f930d3e8c561866ee6689330b4579b2a34a9f542bf78d00e69b6205a": 1,
        "c7557909c689f998d949787552758127a60bbd37617722c0ca7a4e9ff9204383": 1,
    },
    "d73e7b1a3e023a8114261a96eed03a6efcd608487b557503136762f85526152b": {
        "43c1d73cc369a3d26a4959c7922ab9a8742d1c15b220255ca4a38e46a587f675": 1,
        "7ac94532d0f4916fa1f7b2e84a46e20c61f833c4dd89e8c0d6774208cf7b97a3": 1,
        "c25815094686d2085ef2ea36d8343f95ba7112962ff96b1238ec76de8703274b": 1,
        "fe92e42f8b5d3f7c46eca9128722060f5843d09de67d94b5fbf4f1296c85de5f": 3,
    },
    "e95fc0407ba0bf3e6d17f5080be1e8d290590c12496012c95ca86759ad7ea6ca": {
        "b901115aa15bdfe4cc654b7925d23cf5600f1b315547c252b48196c07c8fe015": 1,
    },
    "eb1be37e716a03011b3c0905d2f5c9158b75b037d5c82de4856090f434122f36": {
        "4d109d3aedd46a1b1ca69e692c09a1680c39bfed7fffe96ef0d8304e84c9b2f0": 2,
        "5e3b32ef0d7490534fd580a1ef4666cbc3384248c08eb44af5db3e51deace277": 1,
        "7fd58d8882a5983cdda391d65bfafb626f77cbe40cabc5203f64daf58bf5fced": 1,
    },
    "f079bfdf6b47444ea00b1b511af924def022bbb36e76c00dc84ef9585b8e8183": {
        "1f1bc706ca88767d1026d41066007f154ab596fd2f249b5ce6a29317c6fbf60b": 1,
        "2b166ceefcaeabf19dfb4fe01247c54bad08d7e814c239299628e30c36e7dc19": 17,
    },
    "f7021da024892f4af1c263e226c500a49764150d7406d449768c507a5fa131e5": {
        "14c20afc15fd1c98e4755f75f09d944e670314c5b5f87c09e68cecd88c24e5a2": 7,
        "509db8f37ba7745d0dd1280b2eeb4a6b63fc65bf385562e1bca358f26da5ab54": 7,
        "564bb811b6a5c025ca69001b4017905b22a13a14518fd52ea0029c69da54df53": 1,
        "d2680250925b10d4c70673bfa0405cf0c3060a9d3908da8700d60a7e4800444a": 2,
        "f37af37e99fd9e5764e2b93362e0b12294be76bcef8f156cd8c5f4d448064f35": 1,
    },
    "fbbf580ef93fbec786b704025eb79b3786506ce9680493045f563646fb6b4790": {
        "13f4a1fc46036ab8c2d9aec04affdf54cbf51944ab7502c688c223686f0ce31d": 1,
        "6f9a0144b9a4678b25394b48e7eadc05d1e95f2c7fc5fcbe74f82508e3fc28c5": 1,
        "c77a9cd06467e94c155c4389a60712d039cc6064ab6067f199b23de9ebe6636b": 1,
    },
}
PRIVATE_REPO_NAME_BASELINE_DATE = "2026-11-12"
# <<< generated private repository name baseline <<<
# (path, name) -> lines, while a tree is read for the baseline (None: every line naming one is a finding)
REPO_NAME_COUNTS: dict[tuple[str, str], int] | None = None


def repo_name_key(repo: str, path: str, name: str) -> str:
    return name_mac(f"{repo.lower()}\0{path}\0{name.lower()}") or ""


def repo_name_counts(repo: str) -> dict[str, int]:
    """the counts of the tree just read, keyed as the baseline is"""
    return {repo_name_key(repo, path, name): n for (path, name), n in sorted((REPO_NAME_COUNTS or {}).items())}


def baseline_findings(repo: str) -> list[str]:
    """the tree's private repository names above its baseline entry (no name is printed): a new mention.
    Fewer lines than the entry is not the tree's finding (only ALICE-Law can lower the entry): see
    baseline_stale"""
    base = PRIVATE_REPO_NAME_BASELINE.get(hashlib.sha256(repo.lower().encode("utf-8")).hexdigest(), {})
    out = []
    for (path, name), n in sorted((REPO_NAME_COUNTS or {}).items()):
        want = base.get(repo_name_key(repo, path, name), 0)
        if n > want:
            out.append(f"{path}: private repository name ({n} lines, the baseline allows {want})")
    return out


def baseline_stale(repo: str) -> list[tuple[str, int, int]]:
    """(entry key, lines in the tree, the baseline's count) of each entry the tree is below, an entry whose
    lines are gone included (0). Printed as a note by --tree: the repository cannot lower ALICE-Law's
    baseline, so it is not a finding there; Conformance (live) turns an entry that stays stale past its grace
    into a finding (public_text_live.py, which reads when it went stale from the repository's history), and
    gen_private_repo_hashes.py --baseline live --check fails on it, so the slack it leaves is closed by lowering
    the entry"""
    base = PRIVATE_REPO_NAME_BASELINE.get(hashlib.sha256(repo.lower().encode("utf-8")).hexdigest(), {})
    got = {repo_name_key(repo, path, name): n for (path, name), n in (REPO_NAME_COUNTS or {}).items()}
    return [(k, got.get(k, 0), want) for k, want in sorted(base.items()) if got.get(k, 0) < want]


def private_sets_problem() -> str | None:
    """the reason the private name and path checks would compare nothing, or None"""
    empty = [n for n, v in (("private-repository", PRIVATE_REPO_HASHES),
                            ("private-ALICE-repository", PRIVATE_ALICE_REPO_HASHES),
                            ("private-name", PRIVATE_NAME_HASHES), ("note-name", NOTE_BASENAME_HASHES)) if not v]
    if empty:
        return (f"the {' and '.join(empty)} set of this check is empty: the private path and name checks "
                f"would compare nothing")
    return None


# A repository's own settings, in `.public-text.toml` at the scanned commit:
#   [public-text]
#   upstream-fork = true          # a fork of another project's repository
#   upstream-fork-why = "..."     # required with upstream-fork
#   upstream-base = "<sha>"       # required with upstream-fork: the upstream commit it was forked at
#   technical-paths = ["dir/", "file.ext"]   # more places where the agent word is the technical part
#   [[public-text.allow]]          # a kind of finding allowed in a scope, at a pinned count
#   path = "scripts/lint.py"       # a file, or a directory ending in "/"
#   class = "device"               # one of CLASS_ALLOWABLE
#   count = 3                      # the number of lines it allows (in --tree, another number fails)
#   why = "the linter's own word list"
# In a fork, the commits up to the fork point are the upstream project's: `--range` reads
# every commit after it (whoever wrote it) and `--tree` reads the files added or changed
# after it (the settings file is one, so a fork always reads at least that). The
# base must be an ancestor of the scanned commit. In `--range`, a commit that changes this file
# is a finding ("policy config changed", with the keys that changed), so every change of an
# exemption is in front of the reviewer, unless it only tightens the settings (an allow row removed or
# its count lowered, a technical path removed, compared on both parsed versions: see config_tightens),
# which is named in the output and is not a finding.
REPO_CONFIG = ".public-text.toml"
CONFIG_KEYS = {"upstream-fork", "upstream-fork-why", "upstream-base", "technical-paths", "allow"}
CONFIG: dict = {"technical-paths": [], "upstream-fork": False, "upstream-base": None, "allow": []}
# a kind of finding a repository may allow for a scope, at a pinned number of lines, with a
# reason (a vocabulary linter's own word list and fixtures); a private name or a private path
# is never allowed by class (only by its hash in ALLOWED, or not at all)
# these name a person, a device or a network: allowed only for a single file (a linter's own
# word list and its tests), never for a directory
# a binary file's build paths (counted in paths) and a file that could not be read (a container or
# a Git LFS pointer): allowed only for a single file (a deliberate fixture)
FILE_ONLY_CLASSES = {"session name", "session id", "device", "private address", "binary home path",
                     "unscanned container", "unscanned LFS object"}
CLASS_ALLOWABLE = {"agent process", "work session", "session name", "session id", "internal tracker",
                   "note link", "internal note", "instruction source", "device", "private address",
                   "binary home path", "unscanned container", "unscanned LFS object"}
SHA_RE = re.compile(r"[0-9a-f]{40}")


def read_config(repo: str, rev: str) -> dict | None:
    """the [public-text] table of the settings file at rev, or None when there is no file"""
    r = subprocess.run(["git", "-C", repo, "show", f"{rev}:{REPO_CONFIG}"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None
    import tomllib
    try:
        return tomllib.loads(r.stdout).get("public-text", {})
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{REPO_CONFIG} at {rev}: {e}") from e


def load_config(repo: str, rev: str) -> dict:
    data = read_config(repo, rev)
    if data is None:
        return {"technical-paths": [], "upstream-fork": False, "upstream-base": None, "allow": []}
    unknown = set(data) - CONFIG_KEYS
    if unknown:
        raise ValueError(f"{REPO_CONFIG}: unknown keys {sorted(unknown)}")
    fork = data.get("upstream-fork", False)
    paths = data.get("technical-paths", [])
    base = data.get("upstream-base")
    if not isinstance(fork, bool):
        raise ValueError(f"{REPO_CONFIG}: upstream-fork must be true or false")
    if fork:
        if not str(data.get("upstream-fork-why", "")).strip():
            raise ValueError(f"{REPO_CONFIG}: upstream-fork needs upstream-fork-why")
        if not isinstance(base, str) or not SHA_RE.fullmatch(base):
            raise ValueError(f"{REPO_CONFIG}: upstream-fork needs upstream-base, the full SHA of the fork point")
        r = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", base, rev], capture_output=True)
        if r.returncode != 0:
            raise ValueError(f"{REPO_CONFIG}: upstream-base {base[:12]} is not an ancestor of {rev}")
    elif base is not None or "upstream-fork-why" in data:
        raise ValueError(f"{REPO_CONFIG}: upstream-base / upstream-fork-why without upstream-fork = true")
    if not isinstance(paths, list) or not all(isinstance(x, str) and x.strip() for x in paths):
        raise ValueError(f"{REPO_CONFIG}: technical-paths must be a list of paths")
    allow = data.get("allow", [])
    if not isinstance(allow, list):
        raise ValueError(f"{REPO_CONFIG}: allow must be a list of [[public-text.allow]] tables")
    seen = set()
    for e in allow:
        if (not isinstance(e, dict) or set(e) != {"path", "class", "count", "why"}
                or not isinstance(e["path"], str) or not e["path"].strip() or e["path"].startswith("/")
                or e["class"] not in CLASS_ALLOWABLE
                or not isinstance(e["count"], int) or isinstance(e["count"], bool) or e["count"] < 1
                or not isinstance(e["why"], str) or not e["why"].strip()):
            raise ValueError(f"{REPO_CONFIG}: allow entry {e!r} is not {{path (a file, or a directory ending in /), "
                             f"class (one of {sorted(CLASS_ALLOWABLE)}), count (1 or more lines), why}}")
        if e["class"] in FILE_ONLY_CLASSES and e["path"].endswith("/"):
            raise ValueError(f"{REPO_CONFIG}: allow entry {e['path']} / {e['class']}: this class is allowed only "
                             f"for a file (the linter that defines it), not for a directory")
        if (e["path"], e["class"]) in seen:
            raise ValueError(f"{REPO_CONFIG}: allow entry for {e['path']} / {e['class']} is listed twice")
        seen.add((e["path"], e["class"]))
    return {"technical-paths": paths, "upstream-fork": fork, "upstream-base": base if fork else None,
            "allow": allow}


CLASS_USED: dict = {}
# --range: the distinct (path, line) pairs each base-config entry allowed among the added lines
RANGE_ALLOWED: dict = {}


def range_base(repo: str, rng: str) -> str | None:
    """the base of "a..b" (a) or "a...b" (their merge base); None when the range has no base"""
    if "..." in rng:
        a, b = rng.split("...", 1)
        r = subprocess.run(["git", "-C", repo, "merge-base", a or "HEAD", b or "HEAD"], capture_output=True, text=True, encoding="utf-8")
        return r.stdout.strip() or None if r.returncode == 0 else None
    if ".." in rng:
        return rng.split("..", 1)[0] or "HEAD"
    return None


def class_allowed(path: str | None, label: str, n: int = 1) -> bool:
    """the repository's settings allow this kind of finding in the path (counted: n lines or runs)"""
    if path is None:
        return False
    for e in CONFIG["allow"]:
        if e["class"] == label and e["path"] in scopes(path):
            key = (e["path"], e["class"])
            CLASS_USED[key] = CLASS_USED.get(key, 0) + n
            return True
    return False


def upstream_commit(repo: str, commit: str) -> bool:
    """the commit is the fork point or before it (the upstream project's)"""
    base = CONFIG["upstream-base"]
    return bool(CONFIG["upstream-fork"] and base) and subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", commit, base], capture_output=True).returncode == 0


# A change of the settings file is reviewed when a later commit records its hash in the repository's
# pin file (`config-sha256 <sha256 of .public-text.toml>`): the approval is a separate commit, never
# the one that changes the settings, and any change after it needs a new approval.
PIN_FILE = ".github/public-text-pin"
PIN_CONFIG_RE = re.compile(r"^config-sha256\s+([0-9a-f]{64})\s*$", re.M)


def pin_config_hash(repo: str, rev: str) -> str | None:
    r = subprocess.run(["git", "-C", repo, "show", f"{rev}:{PIN_FILE}"], capture_output=True, text=True, encoding="utf-8")
    m = PIN_CONFIG_RE.search(r.stdout) if r.returncode == 0 else None
    return m.group(1) if m else None


def config_hash(repo: str, rev: str) -> str | None:
    r = subprocess.run(["git", "-C", repo, "show", f"{rev}:{REPO_CONFIG}"], capture_output=True)
    return hashlib.sha256(r.stdout).hexdigest() if r.returncode == 0 else None


def first_parent(repo: str, commit: str) -> str | None:
    parents = git(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:2]
    return parents[0] if parents else None


def approval_changed(repo: str, commit: str) -> bool:
    """the commit changes the pin file's config-sha256"""
    parent = first_parent(repo, commit)
    return pin_config_hash(repo, commit) != (pin_config_hash(repo, parent) if parent else None)


def config_approver(repo: str, tip: str) -> str | None:
    """the commit that last set the pin's config-sha256, when it equals the hash of the settings at
    tip, or None (a commit that changes the settings and their approval together is a finding of its
    own in scan_range, so it never makes its own change reviewed)"""
    want = pin_config_hash(repo, tip)
    if want is None or want != config_hash(repo, tip):
        return None
    for commit in git(repo, "log", "--format=%H", tip, "--", PIN_FILE).split():
        if approval_changed(repo, commit):
            return commit
    return None


def config_changes(repo: str, commit: str) -> list[str]:
    """the keys of the settings file this commit adds, removes or changes (sorted)"""
    parent = git(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:2]
    try:
        before = read_config(repo, parent[0]) if parent else None
    except ValueError:
        before = {"(unreadable)": True}
    try:
        after = read_config(repo, commit)
    except ValueError:
        after = {"(unreadable)": True}
    before, after = before or {}, after or {}
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def config_tightens(repo: str, commit: str) -> tuple[bool, int]:
    """(the commit's change of the settings file only tightens them, rows compared), from both parsed
    versions (not the diff text). Tightening: an allow row removed or its count lowered (path, class and
    why unchanged), a technical path removed, nothing else. Every other change (a row or path added, a
    count raised, a why reworded, any other key, a file added or unreadable on either side) is not, nor is
    a change that compared no row: that change stays a finding until a later commit approves it"""
    parent = first_parent(repo, commit)
    if parent is None:
        return False, 0
    try:
        before, after = read_config(repo, parent), read_config(repo, commit)
    except ValueError:
        return False, 0
    if before is None or after is None:
        return False, 0
    if any(before.get(k) != after.get(k) for k in (set(before) | set(after)) - {"allow", "technical-paths"}):
        return False, 0
    old_paths, new_paths = before.get("technical-paths", []), after.get("technical-paths", [])
    old_allow, new_allow = before.get("allow", []), after.get("allow", [])
    if not all(isinstance(x, list) for x in (old_paths, new_paths, old_allow, new_allow)):
        return False, 0
    compared = len(set(map(str, old_paths)) | set(map(str, new_paths)))
    if not set(map(str, new_paths)) <= set(map(str, old_paths)):
        return False, compared
    if not all(isinstance(e, dict) for e in old_allow + new_allow):
        return False, compared
    old = {(e.get("path"), e.get("class")): e for e in old_allow}
    compared += len(set(old) | {(e.get("path"), e.get("class")) for e in new_allow})
    for e in new_allow:
        o = old.get((e.get("path"), e.get("class")))
        if (o is None or e.get("why") != o.get("why") or set(e) != set(o)
                or not isinstance(e.get("count"), int) or not isinstance(o.get("count"), int)
                or e["count"] > o["count"]):
            return False, compared
    return compared > 0, compared


def worker_technical(path: str | None, line: str) -> bool:
    if path is None:
        return False
    base = path.rsplit("/", 1)[-1]
    extra = CONFIG["technical-paths"]
    if path.startswith(WORKER_DIRS + tuple(p for p in extra if p.endswith("/"))) \
            or base in WORKER_FILES or path in [p for p in extra if not p.endswith("/")]:
        return True
    return base == "Cargo.lock" and bool(CARGO_LOCK_WORKER_RE.match(line))


# host names of private machines: HMAC-SHA256 (the name key) of the lower-case name (a whole token, hyphens kept)
HOST_NAME_HASHES: set[str] = {
    "8f5fd397b5bfd3a224187693126c7b3afa89547491381f83a642e89f1372594b",
}
# a release string ("R36.4.3") is a device detail on a line that names a device or next to one
RELEASE_RE = re.compile(r"(?<![A-Za-z0-9.])R\d{2}\.\d+(?:\.\d+)?(?![0-9.])")
DEVICE_RE = dict(FORBIDDEN)["device"]


def host_names(line: str) -> bool:
    return any(name_mac(t.lower()) in HOST_NAME_HASHES
               for t in TOKEN_RE.findall(line))


# Private repository names are read where the text is prose, not where it is structure: a manifest's
# key-value line (`path = "../ALICE-X"`, `git = …`, `name = "alice-x"`) names a dependency, and a
# repository that lists the members of the conformance programme does so as data
TOML_KEY_VALUE_RE = re.compile(r'^\s*"?([A-Za-z0-9_.\-]+)"?\s*=')
YAML_KEY_VALUE_RE = re.compile(r'^\s*(?:-\s+)?"?([A-Za-z0-9_.\-]+)"?\s*:(?:\s|$)')
# a TOML array element on its own line (`"crates/x",`) and a YAML sequence item of one token (`- x`)
TOML_ARRAY_ITEM_RE = re.compile(r'^\s*(?:"[^"]*"|\'[^\']*\')(?:\s*,\s*(?:"[^"]*"|\'[^\']*\'))*\s*,?\s*(?:#.*)?$')
YAML_SEQ_SCALAR_RE = re.compile(r'^\s*-\s+(?:"[^"\s]*"|\'[^\'\s]*\'|[^\s#"\']+)\s*(?:#.*)?$')
# keys whose value is prose in a manifest (the last segment of a dotted key): their values are read
MANIFEST_PROSE_KEYS = frozenset({"description", "summary", "title", "help", "about", "keywords"})


def yaml_comment(line: str) -> str:
    """the comment of a YAML line: from a `#` at the start or after a space, outside quotes"""
    quote = None
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[i:]
    return ""


def manifest_parts(line: str, low: str, parts: list[str]) -> list[str] | None:
    """the parts of a manifest line read for private repository names, or None for a line that is not
    structure (read as the file's style reads it)"""
    is_yaml = low.endswith((".yaml", ".yml"))
    m = (YAML_KEY_VALUE_RE if is_yaml else TOML_KEY_VALUE_RE).match(line)
    comment = (lambda: [yaml_comment(line)]) if is_yaml else (lambda: [t for t in parts if t.lstrip().startswith("#")])
    if m:
        if m.group(1).rsplit(".", 1)[-1].lower() in MANIFEST_PROSE_KEYS:
            return [line] if is_yaml else parts
        return comment()
    if (YAML_SEQ_SCALAR_RE if is_yaml else TOML_ARRAY_ITEM_RE).match(line):
        return comment()
    if low == "cargo.lock":
        return comment()
    return None
# (repository, path prefix, why): files whose private repository names are data, not text
REPO_NAME_DATA_FILES = (
    ("alice-law", "conformance/", "the conformance roster and its state: one row per member repository, by name"),
)


def repo_name_text(line: str, path: str | None, spans: list[tuple[int, int]] | None) -> list[str]:
    """the parts of a line read for private repository names: the whole line of a prose file or of a
    file without a code style (shell), the comments and strings of a code file (prose_spans), and of a
    TOML / YAML / Cargo.lock manifest's structure (a key-value line, an array element, a one-token
    sequence item, any Cargo.lock line) only its comment; the value of a prose key is read"""
    if path is not None:
        own = (ALLOW_STATE.get("repo") or "").lower()
        if any(own == repo and path.startswith(prefix) for repo, prefix, _ in REPO_NAME_DATA_FILES):
            return []
    style = comment_style(path)
    low = (path or "").rsplit("/", 1)[-1].lower()
    if low == "cargo.lock":
        # TOML without the extension: read with TOML's comments (the scan gives it no code style)
        style, spans = "hash", None
    if low.endswith((".yaml", ".yml")):
        got = manifest_parts(line, low, [line])
        return [line] if got is None else got
    if style is None:
        return [line]
    if spans is None:
        spans = prose_spans(line, style, lexer_state(path))
    parts = [line[a:b] for a, b in spans]
    if low.endswith(".toml") or low == "cargo.lock":
        got = manifest_parts(line, low, parts)
        if got is not None:
            return got
    return parts


# File names that ALICE-Law itself defines as public (the conformance manifest every member carries,
# MANIFEST in scripts/conformance_roster_check.py): a private repository's name inside one of them, as the
# whole file name, is that file and not a reference to the repository. Any other `<name>.<ext>` is still read.
PUBLIC_FILE_NAMES = frozenset({"alice-law-conformance.toml"})
PUBLIC_FILE_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?:" + "|".join(re.escape(n) for n in sorted(PUBLIC_FILE_NAMES)) + r")(?![A-Za-z0-9_\-])",
    re.IGNORECASE)


def private_repo_names(line: str, path: str | None = None, spans: list[tuple[int, int]] | None = None) -> list[str]:
    """names of private ALICE-* repositories in the prose of the line (whole tokens, "-" included, any
    case), other than the scanned repository's own name"""
    own = (ALLOW_STATE.get("repo") or "").lower()
    found: list[str] = []
    text = PUBLIC_FILE_NAME_RE.sub(" ", " ".join(repo_name_text(line, path, spans)))
    for toks in (TOKEN_RE.findall(text), SCRIPT_TOKEN_RE.findall(text)):
        for t in toks:
            low = t.lower()
            if low != own and low not in found and \
                    name_mac(low) in PRIVATE_ALICE_REPO_HASHES:
                found.append(low)
    return found


def private_paths(line: str) -> bool:
    return any(name_mac(m.group(1).lower()) in PRIVATE_REPO_HASHES
               for m in HOME_PATH_RE.finditer(line))


NOTE_NAME_RE = re.compile(r"(?<![A-Za-z0-9_])(?:feedback|discipline|success|handoff)_[a-z0-9_]{4,}")
# File names of private notes that the patterns above do not cover (a hyphen or an underscore in each),
# held only as the SHA-256 of the lower-case name: found as a whole token, with or without `.md`, in prose
# and in a code file's comments and strings (note_names)
NOTE_BASENAME_HASHES: frozenset[str] = frozenset({
    "011c2e092cc7a30187cadae7e11254edd575b6a36d53af0d5333a2e07c81eb51",
    "016a9f853faf445dca093fc2349d9d4f3701a2cbb460749f2db46a79e451b02d",
    "030423971dc227ee122940c272b46ad739cb24e3c3075b562de3c8d56e0e10cf",
    "032b69f7dc2e0cff76ca61de1e202753aad29fd9b0af6e1f83f4b1a381778646",
    "03c9de7c19702860e03b69a69d5081805f8ce735fbc7ae14d47a8367a006c501",
    "0863e621e8bc3e3091f2bc5ec69fd38d933c3874f9562bfbbb9350bfbbcdc0f7",
    "08c32fc4b915591ab79a75d4ba4bd4ed0b510a641d766ce36592d8d4290278e0",
    "092099ba202ca34fcbc736f6007b132c09385e75db71fd2d25903a3ad66442d2",
    "0bae04e95e5a73126b5b6b3428904c52d333953e4f081ef9e595679d385b17a7",
    "0c37c3b58a47d2ead1cf2058d05f88b161049916d32cb87a73e114687461fc70",
    "0d3ddc9f8a4b6d87fd01c7056a1d8ec4bf5fcfe9d835d908dd92b1eff6200229",
    "0f42474485efd6f1fd1606c3a6d584c5eec1b5235daef01f16d00600b93b441d",
    "13294d1dbc52db18d37668b8f0ffcc0a44b446f89495f937cfd436c864202fd1",
    "13a152ea5496617cbfbb854a1624ad11a4820df9455e7041a9cbc0d94ac39801",
    "144f898ff5f6667c244dc47efe2f1257f876d0d2d13f241bb6d4f56d69f95568",
    "1480aed0bc3ed278372aa9b4d0f495e1e4b8554425e71243f9666461b7c79122",
    "154b4a5a5487055a51b01070f2e28bad107be65a6dda427a96acaa28ae4b0bb8",
    "16c6cb5adacd71f65db69dc3cb0fcd5eec40880202c716989f84b8e3c9a3dfad",
    "17cb92f529e2010c2616670b5414865be2450ffe20cad671e7df4e520cb460bb",
    "17fa0a8c12ef2688e1d32fe3602c9920153bf758083a7bbd005447bca86c914a",
    "1832ba3dbf942949d8821f79fa0d11805f5189dcdd9d6d3481d22b25200c6bc5",
    "188150b929e3ca6eb02206216c92007656503d8b3f20cb60229d571ab246a6d5",
    "1a095b606e5f9c69d710cf0119ff3f9d17d5df949f75ff9dee660b8ad4b12fbe",
    "1a4052fa1335d97c386d0cb2b9dc76313e229b0740266856ad310e1b68a7d1ba",
    "1b115592d396f0452a4c3646a4883a2dd03426faee3e1245f7aaad0e23dbad24",
    "204a4480c8c3100281c2f9d70378dcd1ef747a2851d3b5bd64592bc35c093b29",
    "27319a1802bdb654fb71b9d30eb51c755a02fdf1baa617108dc1fdff5fcdf4f9",
    "2a797721b565df461e67361d58d1a44c28ab90a234332c8e96481bec0b2690ed",
    "2a9521be8ef47cbe7e0a05969a3ed83758de52d92fbeb2800be3d11b1b870085",
    "2ef04fa902e74167db54b07f740abdf87104b269b8b583d0220fa9b1993a54fd",
    "35cbc177735805a9ff18e9c935594e81f4b261a635070836fba0f617feaa330a",
    "37cd20c1e6cfdb6420b16142fccc66b796d3c6ab15fcdf878376af2bfdda8b32",
    "392e1caf53bd91e29a0fbc613290b70b936dc7ad1565efe01fcc87c19d5198bc",
    "392eae2df0dcdde50e8c8c9a749d852d0cac66ce6eb36709b654486a78cc80ce",
    "3b66e459c56ff597a4e58ba6a0b685c07f3bfabd019db849031eb6ab8c5a3e41",
    "3d952c614f0d420f25902536d9f77dc1095f189ebc5b550f26ef4b661bdf4447",
    "3e81e733a92928dfcfa5cb53122f1c3ee23d4dfa5e372547dc76a710502b3755",
    "3efbb8cafd1452a69a3ae60b04a2271fb969c887dd612058cb1264cb467778c4",
    "3fc2c8ce50fce95c1ba4f10e58b6888db5756a97f9d855dacb52069f63bd7daa",
    "42ad40749b6f5c7185aeecaad85b2fe84f05dcdccdce4018587bae0bc74cdf33",
    "44cae39dd35f3a29962fed47f6cd7a02e1547009d1baa849636acecb02d68b26",
    "4788134b0d4794bb17c1a7dfe8716469b408a93cdb4382b37452d326ea004e75",
    "480a32234544cd1c23b7e2c3d42ae9043a0dfb7ca818b3f364e69c58280b2e2d",
    "490ff9488190ecabe8e9d0437fbe8b631bfca7705bdaa9b524c7d4dca1a0216f",
    "4974855c5c5af2b7979b2b82833590f48cdd623807e27ea09f67911e3c2b3e0a",
    "49d7be519be17c71d603d73cca49dcede8f3c07253d61e471cc3dcd8241dd2cf",
    "4a3a5fe097dda7df52831654439f8ed89bfd094382df6b61d7856b50080decec",
    "4c25e69d3cabb91306432d93401dbb4bf2fd3ae47cbc0737abc4a3acf289ba60",
    "4d4d08803fe1d2a35bb30e3f56c8c92fbc4c17a7c51233913368361b8d702e4e",
    "4e178055c143ffc0308809b629d55eb777753da690e18a3b8ee68eb9aca04a87",
    "4ef51adca3f484655539a1b895759554b4a6cd7aa1a22d7f7e054d684160ee8c",
    "5062897c544581ff95d38a07373ca2759d0199c3a62b1e10c84cc34ab5de20c9",
    "518271387db817c62a85dc64d1b4507748f94e24cbf1648ed5b751c137a86dee",
    "53d8c7765c9ca05f5d69947d6de22c7fae0df4973cb424194e7874ac7f7b63d1",
    "588376602585ee505f0b1ffe08a1a5b539bc20bd157f8810da0b0e72086693c1",
    "59f68d66ca5e878970b3ebeef1f2a2dc07f6b53319f9e698445e456a0943387a",
    "59fdedd7d25788b4b9c1b79b231ff1b1ae44fc58074ce94e893196f7ee317da3",
    "5b2652362f9b0add04e5d3958df865297840aeaafe4d7959b26c0cae4557cf46",
    "5b30dc72ad1ace5c59741963479528991df17f9d94f7e1c8f1a2f7ee1efc0d0e",
    "5e688646fae7ec795ff7d2d6566bfe0ec178edda926762e629bb41549ee72c77",
    "5e7cc48111966efd43c9badbd851abf6a2131ad790b50dd794e523b295592863",
    "602464e7bbe4afa6ce4ce5c0b77e58e8f25c68c095f9c0d03328ee79625f43e1",
    "60fca3354b6eeb488d88b0eff0237e3d6e65ef6e49c6e473467ee69d200faa0f",
    "6248da3590bcaf44f3fcae6859a22572017b5cb687115ab40fb332357a9ee7c7",
    "64420dc8d41162e4b966213d57ca948ed231538bd8fc75c1ec9b3f8b5e276622",
    "659455db1153bab917303dca758f38b5723d639277f747983ad1df074fce2a59",
    "66d4d78060ec461e6747330ab995231d8b192e940e56d0c50f53cb2970b22734",
    "67bed9c4700454f333a63b2e3d838e3e823644452edcaeeeecf5b89b6d4a332a",
    "688a416d20be9619c38f69a19237045aaed6856114f4d2f5c63c27aa1bd1627a",
    "6a661da90bc8924fa61c4bf104ef3d86a1729b77e64f2ef2f66634f3dd2806ac",
    "6bdebf5e13bc18e9f465b611650f73c19dcf4736b5068b9c505c24b07a495acb",
    "70db4a5bff3013fe2b7b73cd87cd138c02ee836455f02e85cdb05ddb91048009",
    "71588a21beb725aa588b9f50830112036ea937844aaee2b2cb21771ff6fb40e0",
    "7250c52c451d0d40c75f273554b6e61b1910c4217d74a114df6be2f7f791b0f0",
    "72c33f79bf4e23aeb66f24c6880d97c6ff6b2b2970f24f3da1cd54a27efef2bc",
    "73023bd3d0ad1762fdb658c71a6ad369d5686a0fa97573f23157ca96d58bddf4",
    "73143475710027acb8101f905fdc645f12cc1533ed7b19caeb05e26d437df24d",
    "74f7218211633b03863a723a7a8427e8a4c85aaff1dbc69133ff33229a0ab2b5",
    "75ea2ec881602c8ec5674d2768ee5c5367555cb89cf663472818d4092486127a",
    "77762e7ce03a987db1daa9c86566378687e656c98c77137f0d49ded28dd8ba7c",
    "7b34721d0aebcb6537344ddbd143175deeeec91ce6305346c7b456463c1f44ad",
    "7b900de264ef6cf86aac2e2280104762bcbab1f531dd9490dbb3915c8066c92e",
    "7bd1391dc09d9c4ae5896f9b5b04ad2970f691d94c8f74b3f3ff4877a87e07d9",
    "7d1f2bbeaa2c8108bda82c485097d71cc7ec2b72ed63a19e3e0ec9363a7467c2",
    "7d6e924dcd97cd6eb53fc4d64c9820703275eb421efdbaa72ce5d58160be29fc",
    "7e4045d54720ab7edc0efef8ae2911fb078cc69fd0819ca28b61e9f09225d770",
    "7fc2910a97555bcdba74873af88f2311ba500f0297e4775299a98334a206da43",
    "825f3893af0dcbb8e81e0b7df5773e4748109998bc3dcd68e66e8c7a14de493f",
    "832c7fbcc50e61ad997c5edc2a3fc51d24a771e5bdf5ab1fbfb280572cffd645",
    "84a47cc9058cde23d6f503a3794c92a6a243f797133e3c7fa99a2773df4817f4",
    "872d341fb8eb2d5138cb3008361514f654507dd0f2b74c916879a02843ce0ca3",
    "878cfc09e08f10a9574644b7f18f89a044aed666e29c92085e9d477dc4541a08",
    "87bb022886e3dc8149d80362c8670c21fd72123f76452ba54eb30706a24fcb0e",
    "8a877adf148b6060a95abe8db3d75c4619c47641f6a832185e250720f45b7c72",
    "8c627a50314061fa4329840e13ded6cd4c5e935d460435efdbee71470e7f0877",
    "9030472b9d7e46f8033f3e3c9c1bdc9b8f1350447edd45ea1e100b9921967da1",
    "95adb263fc67f261521e19732ee85a8f55cb6911fd65eea160d33488f0f12f9c",
    "9754673780338721464e47fa58d9b0b92b48e6d9e12a8916f2c68e63bbacdc32",
    "a0fc291f26b4a7b3caa98139730380ac2cf37ea9d16e27665ff3c718e54a29e6",
    "a2747a5718ab95b11ae3f9b6f0396fbac1556a2221a9892f392e12299dc8a9c7",
    "a31c44a6d2ef0b955aa7505bb3f25eb6ce8501ad9816f87ac83f5af01227cd91",
    "a40c76008995136fa6b0bd9c1b4cb3c95613fbc1a84c50beec3598ea29c727de",
    "a5b394e43812fe4210b4cbb984519d448679b94e13f139646436aaf0af8ca653",
    "a5cec4f05e63c1b3f495a6736df6ffe23b41ba712a7d5029d2c8410b4467bab3",
    "a6fb7ef8e5fcca46e31ed60d6b2474e7ae149521598ebb1986f33841c946854d",
    "a7a6b86c8d4d9a379189e36488c9c9326e2d33a5ec119c3548a6f5f338cbd483",
    "a7d409e3fff1498e54fac23d304de5285660915993de90284e71f1cbdda5ab6a",
    "a84672b7ede2e40d80768b623ccfa745fe6af1e55942343d0e60ef88f554ce99",
    "a8abd766e631b0d3a73f19b4b27ad15fd79d1abb62c9d2ca33b4d5cfed63f330",
    "afe625409b10f8f9a9f688213a7770967f3264290a9a7962aa75fcc90e843bb3",
    "b109f5492f40bb181b55d226ffa13d4aed55fe90287b89bcec0fdcc3a0f9e4d3",
    "b46f41f8cc3a16f6d3caf6b6defce9c95e3419b227d62a88eb5bf90a940b2d87",
    "b47734761e1b3960ad00dc1f7825db04169f83cd50853b26e16b8fed273425a1",
    "b637384466e2cdd8318b1682aed864f1ad91dd7cd3aadefb1d9502f64d592181",
    "b81a7189ea095244c226cf7b8aea3267a285189c6ab1e7ad74a446d81178157e",
    "b8e7ba2dbeb5e3ab4bf354e8caf03a1d93fa9a4d1ca24cddb7d1fce2a55ba024",
    "ba27a7e276b50d7b26a67b368cf2c20a497138250ca0e59ba572107a0bffa025",
    "bc2cf43918c791738a4384440905c6e71d5c2f4217904ddba5d867546ddc52e5",
    "bc641b794d3e2c06ad9376a55a206d92749c8c1ea47128313a788d75b52bc519",
    "bd542bfe2714c9451eae2f231b39e7a7486ff6c3db78efd46a629079b588efe6",
    "c19b73e52e1d25f0bfaefd25469165654bb90853f08098fd1f07ff2aaffeb21b",
    "c4187d45faf1f62f80338cf0e42ad59cb2a195a326606e375a3d376f6045b6fd",
    "c4afd940dccb71c509e289233294ebb496a6e494acc3fa5a3946b96d405867fe",
    "c6950cccb03c9ed9992d044576a4996711c5a1d74ea487a04df4c71e92b067bd",
    "c695d92575521e2ef75d82da07cefba75d982773e8a7597b840ae1c1656b64db",
    "c7ba85d51dd4d442e5233c060713ccf62a9679f99236446f67ea5e1da744079c",
    "cd4d0e6a719eb0f70a197d61b5eeb62fc63bffa294ed32968bfef3834257b371",
    "cdc0b4feb90ebd3a261d509c5b5f6a8381ff752c06e2831a45c58f934ca414ec",
    "ce4e7c23ca94f49eebf61935611da0cdf29d2ea4260f2ddbb38cff5c04d06e26",
    "cfa77bfcf5e2802aca537019a760666940c078ea897b4699c2ca587534aa39f6",
    "d25f368ba74cdda90d9907511e4973ce7e1f8ed860bde3b77a3652f75146aef7",
    "d3756ec7b0cfe0b542f36ff0147a0803df3e9c67808937754b1acdbf65a70530",
    "d7fe4d17bb5b976f7337b88b8e35d2bd4e091aca032115b2cf5e2418847f3af1",
    "d92796869c1919c8bb3570100ab507be0ea3df6baec0a9365919328ba2f31e21",
    "d9a3cc5fea86201689136a9b5cf0b6354c6ab0044214a3a84c2f5de63b99e513",
    "da0917635fa957c61916489db05fa8e445edc7e4a74285d909ff66a47cf5b599",
    "dbf6b4d1bef248327e584da7f0f469ad868f2df0032f4bed2515e2190298714b",
    "dd16986b99281915c4ab331109fc3e82b56350fbaddb024dfd8b676208b4b124",
    "e18ec87a5553a5b3e000ceb9c234f013ef7d92cd3d9465f64dcecf05c3b8adcf",
    "e269a8c670f783127dfb12c012c5405719c2ce0cabd9c195041185959253da8c",
    "e3553469f4646b1e61b6cf879f5267909298a83e5a8009e595a5f9e1e400e665",
    "e859e6b08ca9cbc52eb4aa29b9b57de93b14f46ff1b97a2df7b42965193626ae",
    "e9c436050121275e00ccaf41006a89918ed413982c0b80323f54135cd2c42649",
    "eba382717996e468b8373b818fe0a20cb66d5a88818b94861d955814059dc3e8",
    "ebbebeb06b493f979d5fd2b01ce3786b322d71d29f5f5e083148932c56f90404",
    "ec178a75018ab8a1f324407b254f1406c5143ea56bf1425d985567601666e995",
    "ec20f71652a46096c92e8dcc103a5bd22a275ebb6cf72a9939c3d4772c5bc4df",
    "ec9af1c387f2995d2ae366d6fc35c0be63b836692361fc71291dab0822699776",
    "ef5fb30d0b3c673ecc4b6577e0178b05887811d395de151df1f554b361687c21",
    "f051bbe082a03686339914d1af0e8106e49dcba348155e4d64e21c7dd8bff832",
    "f189dd76424a8382f558455a84784b65f9aa1846d5b0ee051a0f68b34772477c",
    "f193a8f324404ec430c585f8665dfa993acd20e1fb72b05b2c84f3cadb880ed8",
    "f2f759467c4963612caf458d689d7a956bdf32fe0975ddc422a5a8b9647d413f",
    "f3ee3c174f6872b53061d4c725a60cacb8a3dc0ff256574b522fc88c8d3f1597",
    "f4ead957dfa7a966af26bf1dc4c1e18c0c225e73fb93490ac027633fbe9812b2",
    "f72f958da4d3573cb79ef1580c962ff3fa35384a10a8d3f4ac15e27c0f52decb",
    "f764df93f8d2cb14ab0601e17b7e99a0f917d8392d05ac4b6604af4bc3718892",
    "f8ab23870caeca85fb8387b2c13e61836c1ff724d17bfcb6a589178d2925f5d3",
    "f9f37d0258f8f3c2e5622ee0146a7bd8c6364a6a386198df6bfa4c649472b931",
    "fab25d8a21d7ee4312732a80598968931127f49dd0459ce070086047560f7e71",
    "ffc6ef8d714fafa5b9a10bfd21be37a1eab01eac929ed30bd9e061595dd0619b",
    "ffd5cfadbb04203133c7c0f17f1e44bc5bd8992e518c7ee9843e6e4a7b895374",
})
# a token is a maximal run (finditer takes the longest run from its first character)
NOTE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*[A-Za-z0-9](?![A-Za-z0-9_\-])")


def note_reference(line: str, m: re.Match) -> bool:
    """whether a token is written as a reference to a note: `[[name]]`, `name.md` or a path segment
    after `memory/` A bare word is not: many note names are ordinary identifiers too (an environment
    variable, a field), so a bare match would find them in any code that uses the same word"""
    before = line[:m.start()]
    # the path the token ends: the run of non-space characters before it (`memory/sub/name`, `memory\\name`)
    path = re.split(r"[\s\"'`(\[]", before)[-1].lower()
    return (m.group(0).lower().endswith(".md") or before.rstrip().endswith("[[")
            or "memory/" in path or "memory\\" in path)


def note_tokens(line: str) -> list[re.Match]:
    """the tokens of a line that refer to a hashed note's file name (see note_reference)"""
    out = []
    for m in NOTE_TOKEN_RE.finditer(line):
        t = m.group(0).lower()
        t = t[:-3] if t.endswith(".md") else t
        if (("-" in t or "_" in t) and note_reference(line, m)
                and name_mac(t) in NOTE_BASENAME_HASHES):
            out.append(m)
    return out
# code files whose comments and strings are read apart from their code tokens, by comment style
# (JSX / TSX are not here: their element text is unquoted prose with apostrophes in it; nor
# Groovy / Gradle, whose single-quoted, triple-quoted and slashy strings are not modelled)
SLASH_COMMENT_EXTS = (".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cxx", ".m", ".mm", ".js", ".mjs", ".cjs", ".ts",
                      ".go", ".java", ".kt", ".kts", ".swift", ".cs", ".dart", ".scala", ".zig",
                      ".wgsl", ".glsl", ".hlsl", ".metal", ".proto")
# slash-style languages where '…' is a string (elsewhere it is a char literal or a lifetime)
SINGLE_QUOTE_STRING_EXTS = (".js", ".mjs", ".cjs", ".ts", ".dart")
# Only languages whose text sits in quotes are here  Shell, Makefile, Dockerfile, PowerShell, YAML,
# INI, Ruby / Perl / Nix / Terraform (heredocs) and the like pass words unquoted, so they are prose
HASH_COMMENT_EXTS = (".py", ".pyi", ".toml")
CHAR_LITERAL_RE = re.compile(r"'(?:\\(?:u\{[0-9A-Fa-f]+\}|x[0-9A-Fa-f]{2}|.)|[^'\\\n])'")


def comment_style(path: str | None) -> str | None:
    """"slash" / "slash'" (where '…' is a string) / "hash" for a code file whose comments and
    strings are read apart from its code; None for prose and every other file (the whole line)"""
    if path is None:
        return None
    low = path.rsplit("/", 1)[-1].lower()
    if low.endswith(SINGLE_QUOTE_STRING_EXTS):
        return "slash'"
    if low.endswith(SLASH_COMMENT_EXTS):
        return "slash"
    if low.endswith(HASH_COMMENT_EXTS):
        return "hash"
    return None


# raw and verbatim string openers of the slash-style languages, each with how it closes:
# Rust r"…" / r#"…"# / br#"…"#, Swift #"…"#, C++ R"d(…)d", C# @"…" / $@"…" / @$"…" (where ""
# is the escape), and the triple-quote forms ("""…""" of C# 11, Java, Kotlin and Swift)
RAW_OPEN_RE = {
    ".rs": re.compile(r'(?<![A-Za-z0-9_])b?r(#*)"'),
    ".swift": re.compile(r'(#+)"'),
    ".cs": re.compile(r'(?<![A-Za-z0-9_])(\$*@\$*)"'),
}
CPP_EXTS = (".cc", ".cpp", ".cxx", ".hpp", ".hh", ".h", ".mm")
CPP_RAW_OPEN_RE = re.compile(r'(?<![A-Za-z0-9_])(?:u8|u|U|L)?R"([^()\\\s"]{0,16})\(')
# languages whose block comments nest (/* a /* b */ c */ is one comment)
NESTED_COMMENT_EXTS = (".rs", ".swift", ".scala", ".kt", ".kts", ".dart")
# C-family files where `#if 0` ... `#endif` is text the compiler never reads
PREPROCESSOR_EXTS = (".c", ".h", ".cc", ".cpp", ".hpp", ".hh", ".cxx", ".m", ".mm", ".cs")
PP_IF_RE = re.compile(r"\s*#\s*if(?:n?def)?\b")
PP_IF0_RE = re.compile(r"\s*#\s*if\s*(?:\(\s*)?(?:0|false)\b")
PP_END_RE = re.compile(r"\s*#\s*endif\b")
PP_ELSE_RE = re.compile(r"\s*#\s*(?:else|elif)\b")
# languages with a """…""" string (C# 11 raw, Java / Kotlin text blocks, Swift, Scala, Dart)
TRIPLE_QUOTE_EXTS = (".cs", ".java", ".kt", ".kts", ".swift", ".scala", ".dart")
QUOTE_RUN_RE = re.compile(r'"{3,}')
QUOTE_CHARS = "\"'`"


def lexer_state(path: str | None) -> dict:
    """the state prose_spans starts a file with: its extension, which picks the string forms"""
    name = (path or "").rsplit("/", 1)[-1].lower()
    return {"ext": name[name.rfind("."):] if "." in name else ""}


def prose_spans(line: str, style: str, state: dict) -> list[tuple[int, int]]:
    """the comment and string parts of a code line as [start, end) spans; `state` carries what is
    still open at the end of the line (a block comment, a raw or triple-quoted string, a template
    literal or an unclosed string) on to the next line"""
    spans: list[tuple[int, int]] = []
    slash = style.startswith("slash")
    quotes = "\"`'" if style == "slash'" else ("\"`" if slash else "\"'")
    i, n = 0, len(line)
    ext = state.get("ext", "")
    if slash and ext in PREPROCESSOR_EXTS and not state.get("block") and not state.get("close"):
        depth = state.get("if0", 0)
        if depth:
            if PP_IF_RE.match(line):
                depth += 1
            elif PP_END_RE.match(line) or (depth == 1 and PP_ELSE_RE.match(line)):
                depth -= 1
            state["if0"] = depth
            return [(0, n)]
        if PP_IF0_RE.match(line):
            state["if0"] = 1
            return [(0, n)]
    if state.pop("continued", False):
        # the line comment above ended in a backslash, so this whole line is part of it
        state["continued"] = line.endswith("\\")
        return [(0, n)]
    while i < n:
        if state.get("block"):
            nest = ext in NESTED_COMMENT_EXTS
            j = i
            while j < n and state["block"]:
                if line.startswith("*/", j):
                    state["block"] -= 1
                    j += 2
                elif nest and line.startswith("/*", j):
                    state["block"] += 1
                    j += 2
                else:
                    j += 1
            spans.append((i, j))
            i = j
            continue
        if state.get("close"):
            # a raw string closes only on its own closing text, with no escapes
            end = line.find(state["close"], i)
            stop = n if end < 0 else end + len(state["close"])
            spans.append((i, stop))
            state["close"] = state["close"] if end < 0 else None
            i = stop
            continue
        if state.get("verbatim"):
            # C# @"…": "" is a quote inside, and a backslash is an ordinary character
            j = i
            while j < n and not (line[j] == '"' and not line.startswith('""', j)):
                j += 2 if line.startswith('""', j) else 1
            spans.append((i, min(j + 1, n)))
            state["verbatim"] = j >= n
            i = j + 1
            continue
        if state.get("quote"):
            q, raw = state["quote"], state.get("literal")
            j = i
            while j < n and line[j] != q:
                j += 2 if (line[j] == "\\" and not raw) else 1
            spans.append((i, min(j + 1, n)))
            state["quote"] = q if j >= n else None
            i = j + 1
            continue
        c = line[i]
        if slash and line.startswith("//", i):
            spans.append((i, n))
            state["continued"] = line.endswith("\\")
            break
        if slash and line.startswith("/*", i):
            state["block"] = 1
            spans.append((i, i + 2))
            i += 2
            continue
        if style == "hash" and c == "#":
            spans.append((i, n))
            break
        if slash and ext in RAW_OPEN_RE and (m := RAW_OPEN_RE[ext].match(line, i)):
            spans.append((i, m.end()))
            if ext == ".cs":
                state["verbatim"] = True
            else:
                state["close"] = '"' + m.group(1)
            i = m.end()
            continue
        if slash and ext in CPP_EXTS and (m := CPP_RAW_OPEN_RE.match(line, i)):
            spans.append((i, m.end()))
            state["close"] = ")" + m.group(1) + '"'
            i = m.end()
            continue
        if slash and ext in TRIPLE_QUOTE_EXTS:
            m = QUOTE_RUN_RE.match(line, i)
            if m:
                state["close"] = m.group(0)
                spans.append((i, m.end()))
                i = m.end()
                continue
        if style == "hash" and (line.startswith('"' * 3, i) or line.startswith("'" * 3, i)):
            state["close"] = line[i:i + 3]
            spans.append((i, i + 3))
            i += 3
            continue
        if style == "slash" and c == "'":
            # a char literal ('x', '\'', '"') is skipped whole; any other ' is a lifetime or a label
            m = CHAR_LITERAL_RE.match(line, i)
            i = m.end() if m else i + 1
            continue
        if c in quotes:
            state["quote"] = c
            # a TOML '…' is a literal string: a backslash in it is an ordinary character
            state["literal"] = ext == ".toml" and c == "'"
            spans.append((i, i + 1))
            i += 1
            continue
        i += 1
    return spans


def beside_quote(line: str, start: int, end: int) -> bool:
    """whether the nearest character before or after [start, end), past spaces, is a quote: in
    code an identifier does not stand next to one, so the reading of the quotes is in doubt"""
    before = line[:start].rstrip()
    after = line[end:].lstrip()
    return (before[-1:] in tuple(QUOTE_CHARS)) or (after[:1] in tuple(QUOTE_CHARS))


def note_names(line: str, path: str | None, spans: list[tuple[int, int]] | None = None) -> list[str]:
    """bare note names (`feedback_…`): every one in prose; in a code file those inside a comment
    or a string (`spans` from prose_spans, else the line is read on its own), and, failing safe,
    any one whose reading as code is in doubt (it stands next to a quote)"""
    hits = list(NOTE_NAME_RE.finditer(line)) + note_tokens(line)
    if not hits:
        return []
    style = comment_style(path)
    if style is None:
        return [m.group(0) for m in hits]
    if spans is None:
        spans = prose_spans(line, style, lexer_state(path))
    return [m.group(0) for m in hits
            if any(a <= m.start() < b for a, b in spans) or beside_quote(line, m.start(), m.end())]


# the kinds an entry of ALLOWED can allow, each by its token in one file (or under one prefix) of one
# repository at a pinned count: a product's own integration names, a technical "worker" / "session", a
# protocol's session ("work session"), memory as RAM ("internal tracker"); a device's model name is never
# allowed by entry (only a linter's own word list, by class for one file)
ALLOWABLE = ("private name", "agent process", "note link", "work session", "internal tracker")
# GPU / OS "shared memory" is the hardware term, not the internal tracker (a whole word before it)
SHARED_MEMORY_RE = re.compile(r"(?<![A-Za-z])(?i:shared)\s+$")


def allowed_entry(path: str, token: str) -> str | None:
    """the allow-list entry of this repository that allows the token in the path, if any"""
    for sc in scopes(path):
        for h in (allow_hash(sc, token), allow_mac(sc, token)):
            if h is not None and h in ALLOW_STATE["allowed"]:
                return h
    return None


def kinds(line: str, path: str | None = None, spans: list[tuple[int, int]] | None = None) -> list[str]:
    """the kinds of internal word in a line (labels, not the words); a private name, an agent
    word or a note link that this repository's allow-list allows in `path` is not a finding"""
    tokens: dict[str, list[str]] = {}
    for label, pattern in FORBIDDEN:
        own = own_skill_prefixes(line) if label == "agent process" else []
        hits = [m.group(0) for m in pattern.finditer(line) if not any(a <= m.start() < b for a, b in own)
                and not (label == "internal tracker" and SHARED_MEMORY_RE.search(line[:m.start()]))]
        if not hits:
            continue
        if label == "agent process" and all(h.lower() in ("worker", "workers") for h in hits) \
                and worker_technical(path, line):
            continue
        if label == "note link" and path is not None and (path.endswith(".toml") or path.rsplit("/", 1)[-1] == "Cargo.lock") \
                and TOML_TABLE_RE.match(line):
            continue  # a TOML array-of-tables header ("[[bin]]", "[[kv_namespaces]]")
        tokens.setdefault(label, []).extend(hits)
    notes = note_names(line, path, spans)
    if notes:
        tokens.setdefault("internal note", []).extend(notes)
    names = private_names(line)
    if names:
        tokens["private name"] = names
    if path is not None and ALLOW_STATE["allowed"]:
        for label in ALLOWABLE:
            if label in tokens:
                entries = [allowed_entry(path, t) for t in tokens[label]]
                if all(entries):
                    for h in set(entries):
                        ALLOW_STATE["used"][h] = ALLOW_STATE["used"].get(h, 0) + 1
                    del tokens[label]
    found = list(tokens)
    if ledger_labels(line) and "internal tracker" not in found:
        found.append("internal tracker")
    if private_paths(line):
        found.append("private path")
    repo_names = private_repo_names(line, path, spans)
    if repo_names:
        if REPO_NAME_COUNTS is None:
            found.append("private repository name")
        else:
            for n in repo_names:
                REPO_NAME_COUNTS[(path or "", n)] = REPO_NAME_COUNTS.get((path or "", n), 0) + 1
    if HOST_NAME_HASHES and host_names(line):
        found.append("host name")
    if session_ids(line) or REVIEW_LABEL_RE.search(line):
        found.append("session id")
    return found


# "next session" split over two comment lines ("/// Phase A2 (next" + "/// session) work"):
# each line is also read joined with the next one, the comment marker of the second removed
NEXT_SESSION_RE = re.compile(r"(?<![A-Za-z])(?i:next)\s+(?i:sessions?)(?![A-Za-z])")
COMMENT_LEAD_RE = re.compile(r"^\s*(?://[/!]?|#+|\*+|--|;+|>+)?\s*")


def scan_text(where: str, text: str, path: str | None = None) -> list[str]:
    out = []
    lines = text.splitlines()
    style, state = comment_style(path), lexer_state(path)
    for n, line in enumerate(lines, 1):
        found = kinds(line, path, prose_spans(line, style, state) if style else None)
        if RELEASE_RE.search(line) and any(DEVICE_RE.search(lines[k]) for k in range(max(0, n - 2), min(len(lines), n + 1))):
            found.append("device detail")
        if NEXT_SESSION_RE.search(line):
            found.append("work session")
        elif n < len(lines) and NEXT_SESSION_RE.search(line + " " + COMMENT_LEAD_RE.sub("", lines[n], count=1)) \
                and not NEXT_SESSION_RE.search(lines[n]):
            found.append("work session")
        for k in dict.fromkeys(found):
            if not class_allowed(path, k):
                out.append(f"{where}:{n}: {k}")
    return out


def git(repo: str, *args: str) -> str:
    # paths as they are (no C-style quoting of non-ASCII names)
    return subprocess.run(["git", "-c", "core.quotePath=false", "-C", repo, *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def paths_z(out: str) -> list[str]:
    """the paths of a `-z` listing (NUL-separated: no quoting, spaces and tabs kept)"""
    return [p for p in out.split("\0") if p]


PRINTABLE_RUN_RE = re.compile(rb"[\x20-\x7e]{6,}")
UTF16LE_RUN_RE = re.compile(rb"(?:[\x20-\x7e]\x00){6,}")
UTF16BE_RUN_RE = re.compile(rb"(?:\x00[\x20-\x7e]){6,}")


# a build path compiled into a binary file (a compiler records source paths, such as the
# dependency sources under ~/.cargo/registry/src/, in debug info and panic messages): the bytes are
# read as they are (one byte a character) and as UTF-16 (both byte orders, both alignments); a
# path has either separator, may be glued to the text before it (strings are stored without a
# boundary) and its user name may hold any character but a separator or a control character; a
# home directory but a placeholder's or the CI runner's counts, and so does cargo's registry or git
# checkout under any home but the runner's or under /root; a path inside a URL does not count
_SEP = r"[/\\]"
_NAME = r"[^/\\\x00-\x1f\x7f]{1,64}"
_PLACEHOLDER = r"(?:runner|user|USER|username|you|me|name|Shared)"
BUILD_PATH_RE = re.compile(
    rf"{_SEP}(?:Users|home){_SEP}(?!runner{_SEP}){_NAME}{_SEP}\.cargo{_SEP}(?:registry|git){_SEP}"
    rf"|{_SEP}root{_SEP}\.cargo{_SEP}(?:registry|git){_SEP}"
    rf"|{_SEP}(?:Users|home){_SEP}(?!{_PLACEHOLDER}{_SEP}){_NAME}{_SEP}")
URL_STOP_RE = re.compile(r"[\s\x00-\x1f\"'<>]")
# containers and compressed streams: their members are read (up to a depth and a size); one that
# cannot be read is reported as unscanned, never passed
CONTAINER_MAGIC = ((b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"), (b"\x1f\x8b", "gzip"), (b"BZh", "bzip2"),
                   (b"\xfd7zXZ\x00", "xz"), (b"\x28\xb5\x2f\xfd", "zstd"), (b"7z\xbc\xaf\x27\x1c", "7z"),
                   (b"Rar!\x1a\x07", "rar"))
CONTAINER_EXTS = (".zip", ".apk", ".aar", ".jar", ".war", ".whl", ".nupkg", ".crate", ".gz", ".tgz", ".bz2",
                  ".xz", ".zst", ".7z", ".rar", ".tar", ".ipa", ".xcframework.zip")
CONTAINER_DEPTH = 4
CONTAINER_BUDGET = 512 << 20  # bytes read out of one tracked file
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1\n"


def _views(data: bytes):
    """(text, byte offset of character 0, bytes per character): the bytes as they are, and as
    UTF-16 code units in both byte orders at both alignments"""
    import array
    yield data.decode("latin-1"), 0, 1
    for k in (0, 1):
        b = data[k:len(data) - (len(data) - k) % 2]
        for order in ("little", "big"):
            units = array.array("H", b)
            if order != sys.byteorder:
                units.byteswap()
            yield "".join(map(chr, units)), k, 2


def in_url(text: str, start: int) -> bool:
    """the path at `start` is part of a URL (`scheme://host/...` with no break before it), not a
    local file URL ending right before it"""
    lo = max(0, start - 4096)
    stops = [m.end() for m in URL_STOP_RE.finditer(text, lo, start)]
    seg = text[stops[-1] if stops else lo:start]
    return "://" in seg and not seg.endswith("file://")


# the rest of a path after its home directory: up to a control character, a space, a quote or `<>|`
PATH_REST_RE = re.compile(r"[^\x00-\x20\"'<>|]{0,512}")


def raw_build_paths(data: bytes) -> set[str]:
    """the build paths in bytes, each whole (to the end of the path) with `\\` read as `/`: a path
    read in several views, or stored both as it is and compressed, is one"""
    found = set()
    for text, _, _ in _views(data):
        for m in BUILD_PATH_RE.finditer(text):
            if not in_url(text, m.start()):
                rest = PATH_REST_RE.match(text, m.end())
                found.add(text[m.start():rest.end()].replace("\\", "/"))
    return found


def container_kind(data: bytes, path: str = "") -> str | None:
    for magic, kind in CONTAINER_MAGIC:
        if data.startswith(magic):
            return kind
    if len(data) > 262 and data[257:262] == b"ustar":
        return "tar"
    if path.lower().endswith(CONTAINER_EXTS) and data:
        return "unknown container"
    return None


def _members(data: bytes, kind: str) -> list[tuple[str, bytes]]:
    import bz2, gzip, io, lzma, tarfile, zipfile
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return [(i.filename, z.read(i)) for i in z.infolist() if not i.is_dir()]
    if kind == "tar":
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            return [(m.name, t.extractfile(m).read()) for m in t.getmembers() if m.isfile()]
    if kind == "gzip":
        return [("", gzip.decompress(data))]
    if kind == "bzip2":
        return [("", bz2.decompress(data))]
    if kind == "xz":
        return [("", lzma.decompress(data))]
    if kind == "zstd":
        if not zstd_available():
            raise LookupError("zstd unavailable before Python 3.14")
        from compression import zstd
        return [("", zstd.decompress(data))]
    raise ValueError(kind)


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


STREAM_CHUNK = 1 << 16


def _inflate_at(view: memoryview, i: int, make, error, budget: list) -> tuple[bytes, int | None, bool]:
    """(the bytes a stream starting at `i` inflates to, the offset after it when it ends well (None
    otherwise), over the budget) fed in chunks, so a stream that breaks off keeps what it gave"""
    d, out, size, pos = make(), [], 0, i
    while pos < len(view):
        chunk = view[pos:pos + STREAM_CHUNK]
        saved = d.copy() if hasattr(d, "copy") else None
        try:
            got = d.decompress(chunk)
        except error:
            if saved is not None:
                # the chunk again a byte at a time, to keep what it inflated before the error
                for k in range(len(chunk)):
                    try:
                        out.append(saved.decompress(chunk[k:k + 1]))
                    except error:
                        break
            return b"".join(out)[:max(budget[0], 0)], None, False
        out.append(got)
        size += len(got)
        if size > budget[0]:
            return b"", None, True
        if d.eof:
            return b"".join(out), pos + len(chunk) - len(d.unused_data), False
        pos += len(chunk)
    return b"".join(out), None, False


def embedded_streams(data: bytes, budget: list) -> tuple[list[bytes], list[str]]:
    """what the zlib streams (a valid header), gzip members and zstd frames found at any offset of
    the bytes inflate to, within the budget; a stream that breaks off gives what it inflated before
    (read as well: a damaged stream does not hide a path); (streams, what could not be read)"""
    import zlib
    view, out, unscanned = memoryview(data), [], []
    kinds = [("zlib", b"\x78", lambda: zlib.decompressobj(), zlib.error,
              lambda i: i + 1 < len(data) and (0x78 << 8 | data[i + 1]) % 31 == 0 and not data[i + 1] & 0x20),
             ("gzip", b"\x1f\x8b\x08", lambda: zlib.decompressobj(31), zlib.error, lambda i: True)]
    if zstd_available():
        from compression import zstd
        kinds.append(("zstd", ZSTD_MAGIC, zstd.ZstdDecompressor, zstd.ZstdError, lambda i: True))
    elif ZSTD_MAGIC in data:
        unscanned.append("zstd stream inside, zstd unavailable before Python 3.14")
    for kind, magic, make, error, valid in kinds:
        i = data.find(magic)
        while i != -1:
            nxt = i + 1
            if valid(i):
                inner, end, over = _inflate_at(view, i, make, error, budget)
                if over:
                    return out, unscanned + [f"{kind} stream, over {CONTAINER_BUDGET >> 20} MiB"]
                if inner:
                    budget[0] -= len(inner)
                    out.append(inner)
                if end is not None:
                    nxt = end
            i = data.find(magic, nxt)
    return out, unscanned


def build_paths(data: bytes, path: str = "", depth: int = 0, budget: list | None = None) -> tuple[set[str], list[str]]:
    """(the distinct build paths, kinds of the containers that could not be read) in a file, its
    members read out of containers and compressed streams"""
    budget = [CONTAINER_BUDGET] if budget is None else budget
    kind = container_kind(data, path)
    if kind is None:
        n = raw_build_paths(data)
        if not is_binary(data) or depth >= CONTAINER_DEPTH:
            return n, []
        # zlib and zstd streams anywhere in a binary file (a whole compressed file, or a compressed
        # section such as an ELF debug section, where the build paths of DWARF are)
        streams, unscanned = embedded_streams(data, budget)
        for inner in streams:
            got, more = build_paths(inner, "", depth + 1, budget)
            n |= got
            unscanned += more
        return n, unscanned
    else:
        if depth >= CONTAINER_DEPTH:
            return set(), [f"{kind}, nested too deep"]
        try:
            members = _members(data, kind)
        except LookupError as e:  # a reader this Python does not have
            return set(), [f"{kind}, {e.args[0]}"]
        except Exception:  # an unknown, damaged, encrypted or unsupported container: reported, never passed
            return set(), [kind]
    n, unscanned = set(), []
    for name, inner in members:
        budget[0] -= len(inner)
        if budget[0] < 0:
            return n, unscanned + [f"{kind}, over {CONTAINER_BUDGET >> 20} MiB"]
        got, more = build_paths(inner, name, depth + 1, budget)
        n |= got
        unscanned += more
    return n, unscanned


def zstd_available() -> bool:
    """the standard library reads zstd (Python 3.14 or later); before that a zstd stream is reported
    as an unscanned container"""
    try:
        import compression.zstd  # noqa: F401
    except ImportError:
        return False
    return True


def is_lfs_pointer(data: bytes) -> bool:
    return data.startswith(LFS_POINTER) and b"\noid sha256:" in data[:200]


def file_findings(where: str, data: bytes, path: str | None) -> list[str]:
    """a file's build paths (binary files, containers and compressed streams), as the path and a
    count (never the text), and what could not be read: a container, or a Git LFS pointer (the
    object is not in the repository)"""
    if is_lfs_pointer(data):
        return [] if class_allowed(path, "unscanned LFS object") else [f"{where}: Git LFS pointer: unscanned LFS object"]
    if not is_binary(data) and container_kind(data, path or "") is None:
        return []
    out = []
    paths, unscanned = build_paths(data, path or "")
    n = len(paths)
    if n and not class_allowed(path, "binary home path", n):
        out.append(f"{where}: {n} build paths: binary home path")
    for kind in unscanned:
        if not class_allowed(path, "unscanned container"):
            out.append(f"{where}: {kind}: unscanned container")
    return out


def blob_bytes(repo: str, rev: str, path: str) -> bytes:
    return subprocess.run(["git", "-c", "core.quotePath=false", "-C", repo, "show", f"{rev}:{path}"],
                          check=True, capture_output=True).stdout


def binary_runs(data: bytes) -> list[str]:
    """a binary file's printable ASCII runs of 6 or more characters, and the same runs in UTF-16
    (little and big endian)"""
    runs = [m.group(0).decode("ascii") for m in PRINTABLE_RUN_RE.finditer(data)]
    le = list(UTF16LE_RUN_RE.finditer(data))
    runs += [m.group(0).decode("utf-16-le") for m in le]
    # a little-endian run read one byte off is the same text as a big-endian run: a big-endian
    # run that overlaps a little-endian one is not counted twice
    spans = [(m.start(), m.end()) for m in le]
    runs += [m.group(0).decode("utf-16-be") for m in UTF16BE_RUN_RE.finditer(data)
             if not any(m.start() < b and a < m.end() for a, b in spans)]
    return runs


def binary_text(data: bytes) -> str:
    # tokens of fewer than 4 characters are dropped: in image or compiled bytes a short
    # printable run is usually noise that happens to spell a short word
    return "\n".join(" ".join(t for t in TOKEN_RE.findall(r) if len(t) >= 4) for r in binary_runs(data))


def blob_text(repo: str, rev: str, path: str) -> str:
    """the file's text; for a binary file (a NUL byte), its printable ASCII runs of 6 or more
    characters, one per line, and the same runs in UTF-16 (little and big endian), so a word
    stored in a binary file is still read; within a run only tokens of 4 or more characters are
    kept (compressed text, such as a PNG zTXt chunk or a PDF stream, is not decompressed)"""
    return text_of(blob_bytes(repo, rev, path))


def is_binary(data: bytes) -> bool:
    return b"\0" in data


def text_of(data: bytes) -> str:
    return binary_text(data) if is_binary(data) else data.decode("utf-8", errors="replace")


def tree_entries(repo: str, ref: str) -> dict[str, tuple[str, int]]:
    """path: (mode, size) of every entry at ref (a submodule has no size)"""
    out = {}
    for entry in paths_z(git(repo, "ls-tree", "-r", "-z", "-l", ref)):
        meta, _, p = entry.partition("\t")
        mode, _, _, size = meta.split()
        out[p] = (mode, int(size) if size.isdigit() else 0)
    return out


def git_binaries(repo: str, ref: str, paths: list[str]) -> set[str]:
    """the regular files git itself takes as binary at ref (not empty, and no line `git grep -I`
    reads); a symbolic link (its blob is the target's name) and a submodule are not files here"""
    entries = tree_entries(repo, ref)
    sizes = {p: size for p, (mode, size) in entries.items() if mode in ("100644", "100755")}
    r = subprocess.run(["git", "-c", "core.quotePath=false", "-C", repo, "grep", "-I", "-z", "--name-only",
                        "-e", "", ref], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode not in (0, 1):
        raise subprocess.CalledProcessError(r.returncode, "git grep", stderr=r.stderr)
    text = {p.split(":", 1)[1] for p in paths_z(r.stdout) if ":" in p}
    return {p for p in paths if sizes.get(p, 0) > 0 and p not in text}


SKIPPED: list[str] = []
APPROVALS: list[str] = []  # reviewed settings changes and the commit that approved each


def scan_tree(repo: str, ref: str) -> tuple[list[str], int]:
    findings, compared = [], 0
    want = pin_config_hash(repo, ref)
    if want is not None and want != config_hash(repo, ref):
        findings.append(f"{PIN_FILE}: config-sha256 does not match {REPO_CONFIG} (the settings are not approved)")
    paths = paths_z(git(repo, "ls-tree", "-r", "-z", "--name-only", ref))
    if CONFIG["upstream-fork"]:
        # a fork: the files the fork added or changed after the fork point (the others are the
        # upstream project's text as it was forked)
        ours = set(paths_z(git(repo, "diff", "-z", "--name-only", "--no-renames", f"{CONFIG['upstream-base']}..{ref}")))
        paths = [p for p in paths if p in ours]
    scanned_binaries = set()
    modes = {p: mode for p, (mode, _) in tree_entries(repo, ref).items()}
    for path in paths:
        if path in EXEMPT:
            continue
        if modes.get(path) == "160000":
            # a submodule: another repository's commit, not a file of this one (its name is read)
            SKIPPED.append(f"{path} (submodule)")
            findings += [f"path {f}" for f in scan_text(path, path, path)]
            continue
        compared += 1
        findings += [f"path {f}" for f in scan_text(path, path, path)]
        data = blob_bytes(repo, ref, path)
        findings += scan_text(path, text_of(data), path)
        if modes.get(path) == "120000":
            continue  # a symbolic link: its blob is the target's name, read above as text
        if is_binary(data) or container_kind(data, path):
            scanned_binaries.add(path)
        findings += file_findings(path, data, path)
    # every file git takes as binary went through the binary scan (a repository with no binary
    # file compares nothing here, and that is not a failure)
    missed = sorted(git_binaries(repo, ref, [p for p in paths if p not in EXEMPT]) - scanned_binaries)
    if missed:
        findings.append(f"{len(missed)} binary files were not read as binary (first: {missed[0]})")
    return findings, compared


def added_lines(repo: str, commit: str, path: str) -> list[tuple[int, str]]:
    """(line number, text) of the lines the commit added to path"""
    out, n = [], 0
    diff = git(repo, "show", "--format=", "--no-color", "--no-renames", "--no-ext-diff", "-U0", commit, "--", ":(literal)" + path)
    for line in diff.splitlines():
        if line.startswith("@@ "):
            m = HUNK_RE.match(line)
            n = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++ "):
            out.append((n, line[1:]))
            n += 1
    return out


def scan_range(repo: str, rng: str) -> tuple[list[str], int, int]:
    findings, compared, files = [], 0, 0
    # the tip of "a..b" or "a...b"
    approver = config_approver(repo, rng.split("..")[-1].lstrip(".") or "HEAD")
    for commit in git(repo, "rev-list", "--no-merges", rng).splitlines():
        if upstream_commit(repo, commit):
            continue  # the upstream project's commit in a fork (up to the fork point)
        compared += 1
        short = commit[:12]
        message = git(repo, "log", "-1", "--format=%B", commit)
        findings += scan_text(f"commit {short} message", message)
        findings += [f"commit {short} message:{n}: session id" for n, line in enumerate(message.splitlines(), 1)
                     if commit_session_tokens(line) and not session_ids(line)]
        subject = message.split("\n", 1)[0]
        if SUBJECT_ID_RE.search(subject) and not session_ids(subject) and not commit_session_tokens(subject):
            findings.append(f"commit {short} message:1: session id")
        findings += [f"commit {short} message:{n}: agent process (disguised)" for n, line in enumerate(message.splitlines(), 1)
                     if folded_agent_word(line) and "agent process" not in kinds(line)]
        # the paths the commit adds or changes, from a NUL-separated listing (not the diff headers,
        # which quote non-ASCII names and end a name with a space in a tab)
        changed = paths_z(git(repo, "diff-tree", "--root", "--no-commit-id", "-r", "-z", "--name-only",
                              "--no-renames", "--diff-filter=d", commit))
        if REPO_CONFIG in changed:
            keys = config_changes(repo, commit)
            tightens, rows = config_tightens(repo, commit)
            if tightens and not approval_changed(repo, commit):
                # removing allowances can not let a finding through: named in the output, not a finding
                APPROVALS.append(f"config change {short} ({', '.join(keys) or 'file'}) only tightens "
                                 f"({rows} row(s) compared)")
            elif approval_changed(repo, commit):
                findings.append(f"commit {short} {REPO_CONFIG}: policy config changed and approved in the same "
                                f"commit ({', '.join(keys) or 'file'})")
            elif approver and approver != commit and subprocess.run(
                    ["git", "-C", repo, "merge-base", "--is-ancestor", commit, approver],
                    capture_output=True).returncode == 0:
                # reviewed: named in the run's output, so a reader sees which commit approved which change
                APPROVALS.append(f"config change {short} ({', '.join(keys) or 'file'}) approved by {approver[:12]}")
            else:
                findings.append(f"commit {short} {REPO_CONFIG}: policy config changed ({', '.join(keys) or 'file'})")
        gitlinks = {l.split("\t", 1)[1] for l in git(repo, "diff-tree", "--root", "--no-commit-id", "-r",
                                                      "--no-renames", "--diff-filter=d", commit).splitlines()
                    if l.split(" ", 2)[1:2] == ["160000"]}
        for path in changed:
            if path in EXEMPT:
                continue
            if path in gitlinks:
                findings += [f"commit {short} path {f}" for f in scan_text(path, path, path)]
                continue  # a submodule's commit, not a file of this repository
            files += 1
            findings += [f"commit {short} path {f}" for f in scan_text(path, path, path)]
            added = added_lines(repo, commit, path)
            if not added and git(repo, "diff-tree", "--root", "--no-commit-id", "--numstat", "-r", commit,
                                 "--", ":(literal)" + path).startswith("-\t-\t"):
                # a binary file: its printable runs at this commit, and the build paths in them
                data = blob_bytes(repo, commit, path)
                findings += [f"commit {short} {f}" for f in scan_text(path, text_of(data), path)]
                findings += [f"commit {short} {f}" for f in file_findings(path, data, path)]
            elif added and added[0][0] == 1 and added[0][1] + "\n" == LFS_POINTER.decode():
                findings += [f"commit {short} {f}" for f in file_findings(path, blob_bytes(repo, commit, path), path)]
            for n, text in added:
                for k in kinds(text, path):
                    entry = next((e for e in CONFIG["allow"] if e["class"] == k and e["path"] in scopes(path)), None)
                    if entry is None:
                        findings.append(f"commit {short} {path}:{n}: {k}")
                    else:
                        RANGE_ALLOWED.setdefault((entry["path"], k), set()).add((path, text))
    # an entry allows at most its count of distinct added lines, and at most its count of lines in all
    # at the tip (lines already there before the range count too)
    tip = rng.split("..")[-1].lstrip(".") or "HEAD"
    tip_paths = None
    for e in CONFIG["allow"]:
        got = len(RANGE_ALLOWED.get((e["path"], e["class"]), ()))
        if got > e["count"]:
            findings.append(f"{REPO_CONFIG}: allow {e['path']} / {e['class']} allows {e['count']} lines, "
                            f"{rng} adds {got}")
        elif got:
            if tip_paths is None:
                tip_paths = paths_z(git(repo, "ls-tree", "-r", "-z", "--name-only", tip))
            total = 0
            for f in tip_paths:
                if e["path"] in scopes(f) and f not in EXEMPT:
                    text = text_of(blob_bytes(repo, tip, f))
                    total += sum(1 for line in text.splitlines() if e["class"] in kinds(line, f))
            if total > e["count"]:
                findings.append(f"{REPO_CONFIG}: allow {e['path']} / {e['class']} allows {e['count']} lines, "
                                f"{total} at {tip[:12]}")
    return findings, compared, files


def suggest_allow(repo: str, ref: str, scope: str) -> int:
    """print the allow entries (TOML) for the classes found in the scope at ref, with counts"""
    CONFIG.update(load_config(repo, ref))
    CONFIG["allow"] = []
    counts: dict[str, int] = {}
    files = 0
    entries = tree_entries(repo, ref)
    for path in paths_z(git(repo, "ls-tree", "-r", "-z", "--name-only", ref)):
        if path in EXEMPT or scope not in scopes(path) or entries.get(path, ("",))[0] == "160000":
            continue
        files += 1
        data = blob_bytes(repo, ref, path)
        for f in scan_text(path, path, path) + scan_text(path, text_of(data), path):
            label = f.rsplit(": ", 1)[-1]
            counts[label] = counts.get(label, 0) + 1
        if is_binary(data) or container_kind(data, path) or is_lfs_pointer(data):
            paths, unscanned = build_paths(data, path) if not is_lfs_pointer(data) else (set(), ["LFS"])
            n = len(paths)
            label = "unscanned LFS object" if is_lfs_pointer(data) else "unscanned container"
            if n:
                counts["binary home path"] = counts.get("binary home path", 0) + n
            if unscanned:
                counts[label] = counts.get(label, 0) + len(unscanned)
    if files == 0:
        print(f"error: no file under {scope} at {ref}", file=sys.stderr)
        return 2
    for label in sorted(counts):
        if label in CLASS_ALLOWABLE:
            print(f'[[public-text.allow]]\npath = "{scope}"\nclass = "{label}"\ncount = {counts[label]}\n'
                  f'why = ""  # fill in\n')
        else:
            print(f"# {counts[label]} lines of {label}: not allowed by class (a hash in ALLOWED, or fix the text)")
    print(f"# {files} files under {scope} at {ref}")
    return 0


def allowed_for(name: str | None, key: bytes | None) -> dict[str, int]:
    """this repository's allow entries: the plain ones (a public repository's row, a public word in a public
    scope), and with the name key the keyed ones (`k:`; a private repository's row is keyed by its name too).
    Without the key the keyed entries are left out: they allow only private names and note links, whose
    checks do not run without the key, so their counts are not checked either"""
    if not name:
        return {}
    out = dict(ALLOWED.get(repo_key(name), {}))
    if key is not None:
        out.update(ALLOWED.get("k:" + (name_mac(name) or ""), {}))
    else:
        out = {h: n for h, n in out.items() if not h.startswith("k:")}
    return out


def repo_key(name: str) -> str:
    """the allow table's key for a repository: sha256 of its name"""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def repo_name(repo: str) -> str | None:
    """the repository's name from its origin URL (".../<name>.git"), or None"""
    r = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"], capture_output=True, text=True, encoding="utf-8")
    url = r.stdout.strip().rstrip("/")
    if r.returncode != 0 or not url:
        return None
    name = url.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--repo-name", help="the repository the allow-list is read for (default: from the origin URL)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tree", nargs="?", const="HEAD", metavar="REF")
    g.add_argument("--range", metavar="BASE..HEAD")
    g.add_argument("--suggest-allow", nargs=2, metavar=("REF", "SCOPE"),
                   help="print the [[public-text.allow]] entries that would allow, by class, what is found "
                        "in SCOPE (a file, or a directory ending in /) at REF, with their counts")
    ap.add_argument("--repo-name-counts", action="store_true",
                    help="with --tree: print the tree's private repository name counts as the baseline keys them "
                         "(JSON), for scripts/gen_private_repo_hashes.py --baseline")
    g.add_argument("--name-mac", metavar="NAME",
                   help="print the table entry for NAME (HMAC-SHA256 under the name key of the lower-case, "
                        "single-spaced name), to add a name to a table without writing the name")
    ap.add_argument("--require-private-names", action="store_true",
                    help="fail (exit 2) when the name key is not set, instead of skipping the private-name checks "
                         "with a notice (ALICE-Law's own jobs pass it)")
    g.add_argument("--allow-hash", nargs=2, metavar=("SCOPE", "NAME"),
                   help="print the allow-list entry for NAME in SCOPE (a repository-relative file, or a "
                        "directory prefix ending in /)")
    a = ap.parse_args(argv)
    try:
        key = load_name_key()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if key is None and (a.require_private_names or a.repo_name_counts or a.allow_hash is not None
                        or a.name_mac is not None):
        print(f"error: the private name key is not set ({NAME_KEY_ENV}, or the file {NAME_KEY_FILE}): the "
              f"private repository, project, session and host name checks cannot run", file=sys.stderr)
        return 2
    if a.name_mac is not None:
        print(name_mac(" ".join(a.name_mac.split()).lower()))
        return 0
    if a.allow_hash is not None:
        path, name = a.allow_hash
        allowable = (private_name_hit(" ".join(name.split()).lower())
                     or any(p.fullmatch(name) for label, p in FORBIDDEN if label in ALLOWABLE))
        if not allowable:
            print("error: NAME is not a name, agent word, note link, work session or tracker word this check finds (give it as one word, the "
                  "words of the phrase, or the [[link]])", file=sys.stderr)
            return 2
        print(allow_mac(path, name) if allow_private(name) else allow_hash(path, name))
        return 0
    problem = private_sets_problem()
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    CLASS_USED.clear()
    RANGE_ALLOWED.clear()
    SKIPPED.clear()
    APPROVALS.clear()
    if a.suggest_allow is not None:
        return suggest_allow(a.repo, a.suggest_allow[0], a.suggest_allow[1])
    name = a.repo_name or repo_name(a.repo)
    ALLOW_STATE["allowed"] = allowed_for(name, key)
    ALLOW_STATE["repo"] = name
    ALLOW_STATE["used"] = {}
    try:
        CONFIG.update(load_config(a.repo, a.tree if a.tree is not None else a.range.split("..")[-1] or "HEAD"))
        stale: list[tuple[str, int, int]] = []
        if a.tree is not None:
            global REPO_NAME_COUNTS
            REPO_NAME_COUNTS = {}
            try:
                findings, compared = scan_tree(a.repo, a.tree)
                if a.repo_name_counts:
                    import json
                    print(json.dumps(repo_name_counts(name or "")))
                    return 0 if compared else 2
                if key is not None:
                    findings += baseline_findings(name or "")
                    stale = baseline_stale(name or "")
            finally:
                REPO_NAME_COUNTS = None
            what = f"{compared} files at {a.tree}"
            for e in CONFIG["allow"]:
                got = CLASS_USED.get((e["path"], e["class"]), 0)
                if got != e["count"]:
                    findings.append(f"{REPO_CONFIG}: allow {e['path']} / {e['class']} allows {got} lines at {a.tree}, "
                                    f"its count is {e['count']}")
            for h, want in sorted(ALLOW_STATE["allowed"].items()):
                got = ALLOW_STATE["used"].get(h, 0)
                if not isinstance(want, int) or isinstance(want, bool) or want < 1:
                    findings.append(f"allow-list entry {h[:12]} of {name} has count {want!r}; an entry allows 1 line or more")
                elif got != want:
                    findings.append(f"allow-list entry {h[:12]} of {name} allows {got} lines at {a.tree}, "
                                    f"its count is {want}" + (" (it allows nothing)" if got == 0 else ""))
        else:
            # the range is read with the allow entries of its base: a commit that changes the settings is
            # a finding of its own, and the entries it adds do not apply to the range that adds them
            base = range_base(a.repo, a.range)
            CONFIG["allow"] = load_config(a.repo, base)["allow"] if base else []
            findings, compared, files = scan_range(a.repo, a.range)
            what = f"{compared} commits ({files} files) in {a.range}"
    except subprocess.CalledProcessError as e:
        print(f"error: git: {(e.stderr or '').strip() if isinstance(e.stderr, str) else e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for f in findings:
        print(f"found: {f}")
    if compared == 0:
        print(f"error: nothing was compared ({what})", file=sys.stderr)
        return 2
    allowed = sum(ALLOW_STATE["used"].values())
    settings = [f"technical-paths {CONFIG['technical-paths']}"] if CONFIG["technical-paths"] else []
    if CONFIG["upstream-fork"]:
        settings.append(f"upstream fork at {CONFIG['upstream-base'][:12]}")
    if settings:
        print(f"public text: {REPO_CONFIG}: " + "; ".join(settings))
    for a_line in APPROVALS:
        print(f"public text: {a_line}")
    # an entry the tree is below: not a finding here (ALICE-Law lowers it); one machine-readable line each
    for k, n, want in stale:
        print(f"stale: {k} {n} {want}")
    if stale:
        print(f"public text: {len(stale)} private repository name baseline entr{'y' if len(stale) == 1 else 'ies'} "
              f"above this tree (ALICE-Law lowers {'it' if len(stale) == 1 else 'them'}: not a finding here)")
    if SKIPPED:
        print(f"public text: not read: {len(SKIPPED)} entries (first: {SKIPPED[0]})")
    if key is None:
        print(f"public text: private repository, project, session and host names not checked: the name key is "
              f"not set here ({NAME_KEY_ENV}); ALICE-Law checks them over every public repository daily")
    if not zstd_available():
        print(f"public text: Python {sys.version_info.major}.{sys.version_info.minor} has no zstd reader: "
              f"a zstd stream is an unscanned container (Python 3.14 reads it)")
    print(f"public text: {len(findings)} findings in {what}" + (f" ({allowed} allowed mentions)" if allowed else ""))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
