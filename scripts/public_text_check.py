#!/usr/bin/env python3
"""Check the text this public repository publishes for internal vocabulary.

The vocabulary is the one of ALICE-Physics `scripts/docs_lint.py` (copied; keep the
two lists the same): development-process words, internal tracker and note names,
instruction sources, device names, private network addresses, and private names
held only as SHA-256 hashes of lower-case 1-, 2- and 3-word phrases; id-like tokens
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
import re
import subprocess
import sys
import unicodedata

EXEMPT = {"scripts/public_text_check.py", "scripts/test_public_text_check.py"}
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
# SHA-256 of a lower-case word, or of a two- or three-word phrase joined by one
# space; documents are tokenised and every 1-, 2- and 3-gram is hashed and looked up.
# Owned equipment and private network names (generic products stay in the
# `device` pattern above); hashed like the private names below and merged into them.
DEVICE_NAME_HASHES = {
    "1034f65141df5f4ca8ef4b136e1a4d67f70ef8f7bfd16f91f6d94d980c8061c3",
    "af1c54d629fb3db8ecaa658f846b5b3f8ef314e6b01d1494bafac6af8a9183f1",
    "699074f67aea6aa9733bfbf42ff376f92b7762049c5ef5136256908c298b7514",
    "795b104abe3e4134960ca245ded0e617f162c751209347b7f003bc35e062f43e",
    "791dc82c39c048682e2c8bcc953dba44838997987e5678c161f257d6fc27bf4a",
}
PRIVATE_NAME_HASHES = DEVICE_NAME_HASHES | {
    "76ed632eacc9561fddba93918e12fa408df40a2f9e8e038c7803346d9e2b9ab3",
    "8c2dda2f006c7c4f0e8875c47387901590ff92de46261dbf15c325dc5b3ff8aa",
    "2ef7d5809068e897d756dd958a81b31e3811673a716eb80b96680436550e096b",
    "db526bfe062629b1fd8b8bf3cd70bd6cdbd37e1dd1e81d080fdd29de2a6873cd",
    "80d387ccb3289295ecace879442a42cc8cdc1ff10a0047b9b2b9b11f569142c1",
    "d79d1f8b76c9a4f5546474bf8cfea05cf1a9d3ef73698de05d5b6ae502861708",
    "4a3eb33262fe03814a0fdc3ca9bb5ed5cdff99d8e28fc5788ebe767c0b2158fa",
    "c7b00200a27d82bed0ee6fcaa71a29c535f41e839248aee0e3526c81396fd64d",
    "e1eeef0077e8f80d6f71c92be9758592699ed9171c22e9be78978616ea8e5686",
    "a3c1038a8770ed804b6544ad1a0100415f7ef7ae012f89253d0cd09675c59107",
    "3925ad1a1f1767a30bfb7b606c8bd43bed9123e245bc46fd11950ffb7f0a9745",
    "1de7dcbdf455feccbb694bf1fba9a65830d84ab96b16603624ea33f8cb417802",
    "28bb90e451eea46b2ef4000ce1872e29f7e5a8aecbf94aaf184f5536ebcdb7b5",
    "b4a959fb606739afc8c9f69bc1a3fca53807bdefd44ea46e352c6c84b45c05e0",
    "e78a14d392a171bb45a5bcb8d6d4e3a685893ea6e1a04b811066c34873e0c311",
    "33bdb6df44ce6cd93063bc0ca38b52808f72825c837bac136a3b45720022ef56",
    "9c118c2d1b0d10df8c67942b4cb330fbcaeee47dabb27d9729a706825036ab74",
    # assistant / tooling names: no form of them belongs anywhere in the tree
    "c857d09db23e6822e3600bc06ad8d58f92ed62bc8efd81c753f77048662cb97d",
    "b5cf43ae07a7364e0c0ca9e838f01f278fc6a71c207a6f8c3de8d908608b2db1",
    "28e174396028f226b3bead259d19749205378d9204ce12fd9b1918ab6032a15d",
    "c70eca6b0f88f44d81a41311647e50fda1ac454ec04ffd442b0eb4743a993131",
    "60965168ce762e949600281ba6d01fee136e5b6e8257b1f216f9025ed324474c",
    "ea198c98bbb82c6d3163194fac6132c3f41a95e1be9235cab986980f5ba04253",
    "7d3194f79e645c42e4396dda38be04766810ec6a00d00aced3ffc2a0a1f1a9ef",
    "f3ed9ee201ce50b5961e91b689cad0460a7c48848f7954f6dc96be2d1e435816",
    "ceaf6109c3cc4fea4dfda2b5ba0665b95cda7bc99efbf0b5c4e5826fce812bf0",
    "c547687a37cc0ecabffbad2699540f419a47e0a86b77fd9c0140b88bb32bc80c",
    "6f7ac1823da81d2e52d1a1549ee69c85bbf8bb56d06682849e7c09da2785ce3b",
    "6faef1df454754cb29df4e7e91bf6e37f64060ba852cb61d9f201f7b2a96b6bc",
    # titles and settings of a work that are not to appear in public text
    "19f798ce9f70706dd4334369cbf056e51922830d480360b8ace258e818ba7dd4",
    "f20f5b0dfb30b315a2adacb9fd240ce53301a01a4cbc4c16b26c1c64c815eaa8",
    "e3638dd52d300c1393f35ef855ed00ae35e66b48acc8a8fbfc6b5dfba335593b",
    "1f770aff55b29cfc7d0e86b0d15f0ca06b523420221794dbb9a4f028a698203a",
    "cb49d686203bf58098b7db4cc309f826ee749366c217d7f108cad562507feccd",
    "f59367a3ffb393334570507ae7d5d0f7b1b92b0b7dd8a9a229c0d3f1df416388",
    "96ac2ef53544a3e2fbe76274f9124be1e78c878f03bdf2ae6ed9aa7864dddecd",
    "8653d44dc6fd3b625cba9865fc10f6b8d2d9d871d5da9e3624bc6d87179ed8f1",
    # internal review / rule names
    "d44eb131a9fc729aae3aed377733d242c0796290775259142e0bd8d26e1e3132",
    "9330fafbe2c0a50b50ab63cbc545e79f87b76394b7efa9b6f1dc90f4ce76bfb1",
    "3834bde4d50c16031f0b0c9ff9371cd8aaa10155dbbf964f199a5b965e369925",
    # unpublished sibling repository and crate names (one word each)
    "0357cdb2e625fd39d77f689fb7f66857751043f1989408b4f7ee4782269f45e8",
    "0fde6f882c8c655fc05d2d7beedc92995c9338a5c85e616234161783f7138b4e",
    "18eaed5cd72e74361e9c620a5b1df3f93cc34007f9c288abf27802d52d101506",
    "48d644e12fa5826c48ff2a8cef316a8b3a52dbc4d418d77f0698eb84abdda0da",
    "7564f199eba942a04c0cb9e4c2b89ce7a3c1ad43899e715f96218ed83eae5c75",
    "8e2774e9aafc847145a806227a80c6a51d91abade45a6220e0519c39f3f567bd",
    "c379346cda6119421f42651914a8871e340094d8f5b7980335354e8b70028c28",
    "c750b7d2a5ab98430cc013a51fa56a60cbe6640840fadc1bd764575cf216618d",
    "c96267f290f76ac40a777ee9d90bc6d2198bb4a1632a987d8411f112d5e42286",
    "dca96b6d3e181f2c0ab4b1c3ba764e847a49ec988aada9d18a11160d68ed549c",
    "de735c5c8d8ee5f8ba0468a624fd847d37591b8f112579e7fc5216760088221c",
    "f082e4292686ac18af2580b58bc82a7e858174c173316f540158216ed1c445cb",
}
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
# as whole tokens: SHA-256 of the lower-case id. An id of digits only is a count far more often
# ("53 comparisons"), so it is a finding only in brackets ("(53)", "[d6/53]") or next to a review
# word (above). File text keeps the context rule above only. This check accepts some false
# findings by design; the known forms: a count in brackets ("baseline (34)" in a warning count),
# a joint type in lower case in a list ("hinge, slider, d6"), and a variable named like an id
# ("max(fa, fb)") outside a code span: write the joint as D6 and put code in backticks.
SESSION_TOKEN_HASHES: set[str] = {
    "2858dcd1057d3eae7f7d5f782167e24b61153c01551450a628cee722509f6529",
    "3e1e967e9b793e908f8eae83c74dba9bcccce6a5535b4b462bd9994537bfe15c",
    "5f3731b52ad0fb2f820b91495eb9961aebcbbc79d5881e5a26a382e694b9ccc0",
    "76a8277347f52530e1cf979175a178980b3a180d176165c985d85f7e142f1eed",
    "79b98f273c175489f40b682a0e43f0b22aa3cc9cf2a578e163f291f13fc9f912",
    "86e50149658661312a9e0b35558d84f6c6d3da797f552a9657fe0558ca40cdef",
    "a88a7902cb4ef697ba0b6759c50e8c10297ff58f942243de19b984841bfe1f73",
    "b896847e673d4c22a7c4e2a2b06f2559eaacc530874a7ad3215fbe746a3277b5",
    "cd70bea023f752a0564abb6ed08d42c1440f2e33e29914e55e0be1595e24f45a",
    "f4df435df41a53e0ed8d0e16397d0287705803e94328316ef5b5ca97a9820e67",
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
        if hashlib.sha256(token.encode("utf-8")).hexdigest() not in SESSION_TOKEN_HASHES:
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
                        hashlib.sha256(phrase.lower().encode("utf-8")).hexdigest() in PRIVATE_NAME_HASHES:
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
        "659c5bd1c20fad2d50a9b52724b150d9ff941865275154a9c463afd40d66e7c3": 2,
        "8b580e67b172f24cf9b96fae3fa28b61c797d845d7b6a361c99f910307261a69": 2,
        "2315a2794fe699f753409a368e78d5cb110e6bca06075307e9450adff7a35475": 6,
        "4b5762de09b9db6f9d43cf54cbc6fd08d0c6d330eeae0540fb0c2865a0d7268d": 6,
        "865a3e24f0f7791cde0e953082115370e4da270336ce5511874e9b9a8d5aa982": 3,
        "a4622a60be38f8a45ab31d0020cd2a3c10fe791a8e8d24a3460ab354243e3e46": 3,
        "7491e84ba07ab24d4e2aa02dc0683f34f12a8aa3d7bdab4a6b31bcee5af66447": 3,
        "e01f408722226af8e97c873dd04fdaa90bc7a16190d976791cbe1bd1660454f7": 29,
        "ab6f260bbe48e591409dcd53d07c2044e0a4da028ef1d51bc2ae01ff4009ba58": 3,
        "2d5c1dc549cf7601676a0cb7f5452d20be3fa5f1d0f55140305152ca6be22625": 1,
        "e7fc9e6005c1c2d4cad2be2070cced19d2ce2874f93d046f0cabe7194671d81c": 2,
        "98a8fa0d30c68d35ec24833ea7b968723d7efbc42fb19d14c3be04d25ed93777": 1,
        "c5d357f33c7665256b4fc1fbed5c0895022dcdc4a5779ea479cbf431329fbc7f": 1,
        "ba925428c28bf52c69e1f5374eb2145146dfd46130aead6eff97ed4b6f25959d": 1,
        "365665ccc6fa70ffab77990d7139c7959ba98e79e52ea8f6c1e307ed2454bc3c": 2,
        "e9d63e3dffc1b27f1523ec59287c6d9417323600d122e307e7e3664fdd8fc8c2": 1,
        "2df2972e8f255f8eecf23fcb9b7cab87e2880fdaefc3ede80ff403f8c499583c": 1,
    },
    "02a148b01541d8673d3b2ada504d8e3cdcf7725fefaac791004ff60620516970": {
        "8e3dd0d012a2b0488d6e314c823c6db1fc86881986cf3597f9cd9f3365108bb7": 2,
        "938fe3b8cdc685f71f04da5f9a7e225edfddd6e89137a73eee378842cfaf329b": 2,
        "2fce1206037fe05fb65ee69462a31f0021e4d0f4cfe89f2325b6c00a72beed1f": 2,
        "5e9759a32eeb52bfadaed64f1f2475cdef4c47bffa624a5e694593308c66d062": 6,
        "c4e9802423d3ef13964b175c670bad3a11daa8bd6874c18d4d1c4edc048d1393": 3,
        "d47bbfaf39f76243602a5e88dfc65279df2b05cfa4c562ff4dba04ac0f15e73a": 3,
        "971a9ffc87554fbe3293e6ca79ca2ec3be9f6e9fecc4138fece5c35a75900ef7": 3,
        "a5048c9fd6603806b175feb184239bfada1ace37eebf811c75fc400b14b58a05": 2,
        "40f8027e044e8aa3e2783d55bd9628c9b073f4e82458878f6bbdc8fcb3f791ed": 1,
        "1a6aa0d6a24df8ac5f6ff7c0f6b30c35e552c12433f09c88e349ac63c3694c69": 4,
        "ffb612c1d576098613b338fc531a3e5defa9fa781e646cbe11d0c55536a753aa": 1,
        "e0ad96a94c13edafde51753681e9e1e936ec647f6df219c5ee6eb70c6da67bea": 1,
        # the LLM providers the product lets a user bring a key for: their names in the provider settings, its UI
        # and translations, the stored settings, the provider-compatible backend and its docs, per file
        "8021da4dbe6367445e3ca97c486434c78f2c7ae7b258524797ec59ef183e018c": 1,
        "ea28c38ec77891d4e409682e7e434e6360069fa4cf24ac61c6bd009438064125": 1,
        "afe61520ef0ecf5dd16ade1a230b71827e5214295b26aed29cf557a30ece6155": 8,
        "86a76ba62e3d660c274cde8bafc3210f51afb4125ca7a328f5e0e6e30068883a": 18,
        "c2c1ac3fa6dab5d67c4f9777f00145e89dbd0fe076079ce3cae1e82e458138aa": 13,
        "28eca770a90071d71ae07ff742a0900e377856eea8d56fc1eaabda841e9dd6b7": 18,
        "4fe81809e9fec87a90b7a44b32cb0d8e3b1ab4e6a99ea355ad864a5e7903c428": 3,
        "ee3d8a4176e0070cd2815dc1e3ff4aeea8f076cfd5b1dca67577d6fdd5eab96f": 46,
        "c3985f2448fb6ab6f77561c037c8e074c97fbd3a249cb7d19f11cadde9ae1cda": 12,
        "d10132183d11da9f825a6c5f0d7678755a9e1c47eec40fdcc595a0ef87c67128": 2,
        "608ba465bb014ebae8edab6a8bb5fe8ea6a087f08b2a141736789f76eae4e3bb": 1,
        "c9a533d65436349de2f1306acb3b6049b134d76f88b542f5cf0d3f4cdb661a0d": 15,
        "bb4cfb53dbc95feb5e905e13a1beebe85e48c5ed28ca51274aaf2e02ef84ca5b": 1,
        "ff1e80a526336e71aea2f1dcabd55e58fd3320f49a03acad32e34ad6037f749d": 1,
        "78630b4c6ab9d76bc908691e4bd0acac2cf598d7c98194bc879ff0a2fe2d8cb4": 1,
        "b87c00b29f8da89b179ed5a0cd0ae3a18ea3f9b78b96f7ec75236f17173aeb57": 36,
        "4d8ea2179bb1f4fcbecc6379dcac19e7754647dc81d6e709d427ac3576158657": 20,
        "ad61fec51d697a9ec8318b673b0ea83a18fb38eda98428ea77215fdda33381da": 2,
        "5a626491677c01fc686a01e0bfa150bb599109893c11c9223b6662a735f5aec1": 1,
    },
    "3a983693b3386aed1c03b948f8160c17895986e69a4ba48966a24ea98ebc3b6d": {
        # the product records LLM coding sessions through a provider hook: the provider's and its
        # coding tool's names in the hook installer, its settings path, the model IDs of the cost
        # table and the docs of that feature, per published file of the mirror's allowlist
        # (mirror-allowlist.txt)
        "2fce1206037fe05fb65ee69462a31f0021e4d0f4cfe89f2325b6c00a72beed1f": 1,
        "62215a0753f5c74d061bf0491dec31ce4a1be008f022586a1d2efc30ee9d9296": 1,
        "8ea2b5ec7bfd6d9691e1dadd07d2303337534ace39cea01363bd2bd1629037d8": 1,
        "81be11b1e9bab124d13dff3a6551c0c7ff5b472fdeb568ecb507117ba41f4d81": 21,
        "0716e9c347ea2d15d9d40b50bd503097ff1b69d8e8db62680fcb9d0ba7a41122": 7,
        "a820351e9073f15b32b0fa6d24301cf49c91e4ccd231facf2f17393c3d850b0b": 3,
        "ed2e3b37e15dc5da0c65e7148a01128ba5d56bb6746a88730b6aeceeae432ce2": 6,
        "6b09b2e2912376384754dadd4cdb27d1c603d7cd6951d986e9053923339ceeb9": 3,
        "5ef4302507f010334f5b2483deec72731550d0151c59b96d1d96fe79c2472178": 7,
        "f1b0304a8cfffc826a34b6ed531cd914d9b9952ec25b96e084f40d1def0f332d": 1,
        "6837022b98e15ee7c16ce7ff55294a86bdcb9dffcaf523a5c97eab6ed894cbe9": 2,
        "2139fc0a5be025ac57f2769bce15862b48c07382a80a0fe472f818871bb096a3": 1,
        "c475e86aae4a825c58b94e9e911b0083739d6bdaf956c9d710f20f9a20953b45": 8,
        "e9c79d248b00b0db8b31ef32d751fb7031867a4c3cde8ca30f505717743c6dee": 4,
        "b6fe398ea3c90bae6dce9c4ce8d9b276b259a09b498cbd759b0523d50c58cdba": 3,
        "ee3f438631e41b5ce4caaeb0fa5fc7bee0b4cab86262250851fcebc9388dd453": 1,
        "d57104fbed7dc4474093161ae6a6e900007e573dfc89fd5e8782caebbc1d32b2": 4,
        "5db93016a46b37e75a75f4bb187769f97ba0c3429958af290639bef04fcdb700": 2,
        "3bf67c57afa024fe91d028c195ce7cb5015efa0685a16b220c4608ff759f68a9": 2,
        "08cbade40c0887bd7683334588ab256a1a64e807b568cc435f42a88f3a90671a": 1,
        "9889d523d669762624ecd367df7ed39705617ebdbfdfdb51ca9fd4afd4772a45": 3,
        "b496f23c7352fb943f3d3cc7af12994594ef33ea016be20ebd0dce07ba6522bd": 1,
        "f8df6c985c10601b245102bde55b56883d700dbcdebe0e6b6dc8bd66ea98f621": 2,
        "7ddbbd692e03f91d41e8b63092c967592b4a0fcc672caff9dc2dbf5f3293ad1f": 1,
        "a70fecdfb362e03259afc997c2da844a30338b91645cd5acdc61b1b7981c1b13": 2,
        "7a1fefc96c87c42045df732c6be82e88b855ff7c1c70a39fa3449d595c489201": 1,
        "a8a4f632a3288a9d5262c4efc5419b8e24c432994d71c9baa0264fb20221541d": 4,
        "045d3bdde8d78ba4c7135cfe93a560dd2e6f3e72b78b7b166054979537fec602": 2,
        "dd59d69d562b26080740c3f0c5a3e8f038ea9196c44b979c250d215b2d972b51": 2,
        "3e58dbab2109a4e331c241eaeddd8f023483f09a76319204e7ab1a55f8da5305": 1,
        "47177c1104c61dfb27767bc23a2bc1a0edc463eb138110e4bae6ac5f6c6c95bb": 8,
        "64bd9786b73d18d9b73e5eb177d296bee9b5a6b56d3e6a0268fcf7187f7bc830": 3,
        "8da6c361946741a699522f3e1c8818e06df9526873c3fc3314d2a7361981c336": 2,
        "4332255961133c06c86d1c42c2ebecc694d3f4467789356fdd88050f8fe34dba": 1,
    },
    "38e19ab81bc34c0b638d4f0fa92e95d030e127bb8897517fe70ba91c5227e5b5": {
        "b1b065b5f49f4b5fa7b6c07d114bc8398be43a8635a8753ecd4155c2f3af524b": 4,
    },
    "3ef7c23054fecceba84393adf2be7da3e61c15adafac87399f90c9ffd92b43ee": {
        # a technical worker / session of the code (a job system's workers, a protocol or training session)
        # the product's own integration: the providers and clients it registers with or calls
        # a protocol's session (the client closing it), or a session directory of the tool
        "81195d09e3aeaac8cb290b152c0e37377b5bcc9b7e966915df3111ea4fdb8591": 1,
        "1209f54b283cf7865dc0434e97cb522ad893f30cee011e38815cc3c263aa6983": 1,
        "72e0017523ef42bf45e930d6eb6fcc2d709d50a0a975ad57054033e9eed77916": 3,
        "1bb151f42e42a7fc080b78fd66dcbb9cba7110368148a493a2e598a131f0e96b": 1,
        "3437b42afa7ea96db3ef6cf324d967a25ec9d88cee5cc3663eb57caf010e71e0": 1,
        "d7d94612de7a8b15676b089967d90fc02d65825e651d6133e523fe323ed7901d": 1,
        "e7286202f212188286f43f8fe9ce6b0863a93f47e1c8d0ec31956eee02e5e7c4": 2,
        "559a86e79330f637a9184cb58fc3fdda4a11258335d439d70650c20d0247aa2f": 1,
        "307d20155e0afe1d89c9c4c4c4da266136b13d94dbc310fd8877f0b4ae50053d": 2,
        "18a79de1f8029c9c7dca68e2aca663114defd9e7004767d9231f37cd26037a9b": 1,
        "60c485082a968c73fcad4a35d464edaa60d2f93824c0811737e8c89802b8254d": 1,
        "cbdb15d22f7b8b1ae7248e9155db19674412e650206a3dc59fb2fac273d235b8": 1,
    },
    "e42a1f4ae9070e759b04e9b00380f1381c1c8ab9b1e2baaf832b3ff3c6f44059": {
        # a technical worker / session of the code (a job system's workers, a protocol or training session)
        "df9edbabb365aa396c27e7ec628280d7bdbcc921080e847c5ec89f577f438c6e": 1,
    },
    "2368fbbea5ea4043cb84a5ae027905aec833fcd7648a68e5b9473d5cdd8b941a": {
        # a technical worker / session of the code (a job system's workers, a protocol or training session)
        "cd05384f63a4f3cc53317a433ba2c4d01a58e1ae6b71177c6d2573eb395f3209": 2,
    },
    "853a08873479add5fda50b6b6fe19b02fbd35e2200af2fb0df9caeb08311f234": {
        # a technical worker / session of the code (a job system's workers, a protocol or training session)
        "5d453f19b84b8d17468a349ebdb12b9dac5977e334745cd59c9c2a5e6597f44b": 1,
    },
    "dc0d9538d358ba3c2f339c142819661915e5955421932efab0d5fb0a581c0578": {
        # the product's own integration: the providers and clients it registers with or calls
        "3c108d1b33a7ac149e10cc6d12f3d2bf54fb1057e547f3ae819a20b11448f66e": 1,
        "deea0cf4cf88ed22035db13059ebd37553dbb1f3cda4d490826842c99d4977f2": 1,
        "2fce1206037fe05fb65ee69462a31f0021e4d0f4cfe89f2325b6c00a72beed1f": 2,
        "b91840586b886414f281238e19d5f84d7f127a79c1fc7231103ffb852df031a3": 2,
        "69bcf23ec5928add282908210b03b8dcd5dca636ce57651adc2b7fa30b5e4d44": 1,
        "a2a8c7eff9a125280931810eddd7c6d1c7a16727e87529e530a77429298df9b8": 1,
        "042782826eae3095aa3af0890722def668f55e28f6e23c2f83e81ca551a39126": 1,
        "6151707c2b3fce7c9b3ff173e5494c2f85093981fbe8a2f315f6baabeb4804eb": 1,
        "256e26e6196873b15e702d2b68880f1efd7bb749232c518b4c6bdba9ad60eff3": 1,
        "bf99bdf7af40a946554990931eec4443de2de8978ca9b0234c0530afdd7c0485": 1,
        "06bae21f32fd852067d1fc6221347b50a9ec6d77d17ecee64bf5f8ebb2be4292": 1,
        "533451835fb9cfa1dcaf2b0b1513ca23dd0f7db666b873ddaa6d3ee804b43a74": 1,
        "294c838d007148e809aa410ec07deee7afb4c8ea7ed45da1486d1f9d5e3f753b": 6,
        "342790822f9d44d6f545e36ca63527a502bc523ffe8a132380f572af0138d369": 19,
        "d3c41527598983e3fb38e9c120300d151c1f3725affd4578c3f3727b43b20ea4": 18,
        "1f698f05b874dd71686adf6b6be72fc0bdb2f21fc7bfdab830a648507dac9da4": 3,
        "3f3fc6bccda2b98aef65326e31044019ab2e8bae983ba3c17f8b5d9ee10f9e38": 2,
        "8043a29bc049d947f76080926d90323a0b1f013b03fa363cefdf96c63c6adc96": 2,
        "1bef052afed94239401e3cec93a1337e5cf0bb4be63631d910250212c6be4b6e": 5,
        "33a00d2d2af03742dadccb73cdf17ff54bb17f8532aed8d93de5138ac9fc6c58": 4,
        "d97a31bad1267428946b4f560b7659591705f14e919cfaa7cf26e346fab212ad": 3,
        "60a565e037a1dc2e8de9c30d5476210ee7db3214442f51ae7a702327154869d0": 4,
        "85bef8ada3e5aab3d93223a39911861678a9fd0c2f22d1a54ae4aacb7d4f4595": 7,
        "de4d90d4d870dd2ed098f5f93622cb65163b752960926d355f794d4637249beb": 1,
        "04eb5140940006822621d5c58dd722d4c7f180bd18dc1597fe022cedf6994ce8": 1,
        "a71e2ae2d9dab1f34b42a14ae3cabfb8c803dbd10938857522ba9660f3637e94": 1,
        "2b441703deafaac08fbb11e72e6a273346875ed6af66d758cd010026410d2b86": 1,
        "dc6a02a60ad3efac02a89ed90a6a9045597abbc52218bc24a4ff1fa43087827e": 1,
        "f0c6bc045a5d29080092877b245e12edd92aa7924f56d7e5c29fac81aaa250cf": 1,
        "28e5c3f099e63f337d86de45c731d9b6c9ebdc7efac4e1c3aba4ab0ca80c6f8c": 3,
        "417e2abd4f766a7e6093563120b8c80f62f8d0a16537582533dcea5b52f42261": 2,
        "b8d1a2bd261548edf21e52f30652ab789fe2e89064314b4f2aec2b3c45ea142c": 3,
        "88078db19fed9afc6d5c40b1a5ad051c70abc837cb21964084f96d0090c78b3a": 1,
        "994c4b6281c79930d57e193ae4d07e9f0f0d9ef405bdaf8e2d9b930c69567c45": 1,
        "c98a6bd295e3e40e635718aef307281f056819f944b4662afe8efd3b1a9da44a": 1,
    },
}


def allow_hash(scope: str, phrase: str) -> str:
    return hashlib.sha256((scope + "\0" + " ".join(phrase.split()).lower()).encode("utf-8")).hexdigest()


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
# `/home/<user>/<repo>/`): the repository names are held as SHA-256 of the lower-case name
HOME_PATH_RE = re.compile(r"(?:~|/Users/[^/\s]+|/home/[^/\s]+)/([A-Za-z0-9._\-]+)")
GENERATED_BEGIN = "# >>> " + "generated private repositories >>>"
GENERATED_END = "# <<< " + "generated private repositories <<<"
# >>> generated private repositories >>>
# written by scripts/gen_private_repo_hashes.py from the account's repositories (do not edit; --check finds a stale block)
# The sets are held in this file: a repository's CI fetches this one file by its SHA-256, and a
# list kept beside it would not come along (the check would then compare against nothing).
# An empty set is refused (private_sets_problem).
PRIVATE_REPO_HASHES: frozenset[str] = frozenset({
    "009efce69e63884cc51a1d652aba09f2592ae5619f0ef655bf489d37d6441d76",
    "017a920b4b49ba2c2029a50c6488faa018b2272c7f53cf7cc26e5fb02a02270c",
    "01f7f2f6c02fece7a339a816cb3ee8cc5272c6808489f355154fadeb18600da9",
    "0286860f04dbfec1818c5c03817fbfe874b0e69852cfe536409d9ffaf7e9eb0a",
    "0426a72064cc73a3ed7238a6e481c72d69228253b0f8a5b29948c45d3eb78662",
    "07d6c19a51a14a9a50c16760291bdb09d87fa1cc8e661c26dcb266a50be02482",
    "08f57882f4eff6214397d8cc58407f0966e564627491d8ea1d25b084d0c29a6b",
    "0934833ab0843495ba31d0d195c0221685d9da7b783a833c51165bca60e72bf7",
    "0ab7255f88e6e3f5a8795cbf061b6998e30e4c935a158e7befd6f05a83312d0f",
    "0b4943843128adbeb763db923caa28cbb79a234e51ccf76c07a8220bcc815c02",
    "0c781eb5d2aae803d3e374ef6a8416e1fa3e6214cb6a56f349cca85ce537d5c7",
    "0de43b7985884053a36ff02c9614256621c7df726c2c0d593dd2dd677da49fa5",
    "0e0ef1537cda3efbe9a06c6ae9d1aead42664dc8dcc0ef52844bc75a561272cf",
    "0e9562684217083eae9a05ec5553a570adbca78ebefaac0e5dd10c7237ca8370",
    "0eb323dc73e26d3ba37272a27cf1546a0ddc5aa4cfa24b8b7c62c6bc2f4e1b22",
    "103a021e00d532a3e66390b66d6027aae957f3a73b8e20e9498d741b3decae80",
    "10471b1fe18d3ae67e66d105dbee46b8e8d70bf40d2c634020d32c0485f48a93",
    "10f4a361c39410bd7d8ee2ace5fa000e27dc84c7cb356b4f94676efeef6030b5",
    "116135381ee6e8b39231909fa412b6f973b55d293408cb568c34d9db339f75c8",
    "11698ce781a0f93f4ebdac92f987f68edd0bcf63390139e27a83e0477bc634ee",
    "133e304730325df85c4325bc5cadab9e5cc44ec915c05bdecfb8a964573b3981",
    "145b8ad12fa74d3e516d6b25b9bbc3658c8d7a749c34ac19d85015881de52bef",
    "145c52a854b72716135dda9fb0e2f25ecfadffa33321019c076c3c3177311b60",
    "14eb67957164432af13b6f3fd55374db77d11f2d9cff8a69f6d0845bc25fbd6b",
    "1589d73b53aad6afd0c45465d13d709c0231c25b8bd1c9cbdba19e28da0f5c14",
    "16bf87285d8ee599797ebf52f7e8ceee2805ec9565b9b1ac1739a040422308c7",
    "16d189ebb51c08241c10ccde7144ed2671fcb0618db0e18951b6fa45168aa053",
    "17ccd2ddb9f66b5f3530631ecc5db5ed431e27d6ff7f7d4aac1c1468dddb6113",
    "18fb2f9b792e5dc71d34a20029e20796d2482f033d81d19afb9f9201fc6b8ac1",
    "1c005b4f06c7804528aff33fbbe064a3aed0e8e7c40e0d6a7cfa5a4fe3d8fc9d",
    "1cadf6c60c9b78f8abc843777f58f1e1e48214fe9299cda39e52c8f2ad6d27ec",
    "1ce9a161cfbdb00a4d7bae3c3eb32d3a513d1880d4342221f399125772ae48ad",
    "1fadcfd5c68dcce455bd8e0c180a061d732f9a2dbd7745408b387eb2d405bbbb",
    "20aa94b8cc08fee620e4a982c322af7629743940c135df496f02f0d5ce64e4d3",
    "20f4dccc6619f73f4149625ac83e2fd5ec467b0c7064639b94d92b8cf982347f",
    "233328bc0ec649ccf7a54dca441ffa3998ebf12d9de39f8c23feb0739a5afa95",
    "248cf5c41d64e689cfe8e98f39bfb1db7a26d1c9fca965dc4539bf5af5a628ae",
    "251333d415713756b93a4bc6aba7e01b171e4dd86697f22003a8144a12308d4c",
    "2537f80df45b85ed8028a5c53b10df32279e037fd1230e05a1d706c5d8a1ccc0",
    "258437fa38eddcbbbae3b8fe9280929d0904a3674a36bac26b384981bc827392",
    "265af92560195d72769fba41ebfc5ae498a4e4e3f2cba9d2a01addabffeb17a0",
    "268fcdeb74e27ca8fe906582e6191833c1eac1d249411e262a420e4313bf18f5",
    "27d628f4c5ef9d622abfb867fdef124431262940a7a7075064282740010b7c56",
    "285b39b17dd071b1535a2d613ec5170c80af76c02c5b53b6838a1a45d89818fe",
    "286e8582101e9d02fac9fc1261571b07eae654603ae5b3ae226815c6cb36fdf9",
    "28952501290a718c501109c2f219f787b9ac7d40e5802a726f38ef2acf61f3c3",
    "290fc5d255df120a441f2a9e016904cff54c82eed72eae19cd90adccd5c5cdfc",
    "292b3088c52a6c88be783208633fb18291176e1db5ae2af54202209025f11bdf",
    "2a620fcdb0127bd419530f2b13dbe0fc127a586e8632d35b5ed6b538a6fe93df",
    "2a6543e9bddf7b2964df567ec74358a0dbb342fb4d2c39f7e3dd6b0b313e7b60",
    "2a927c40032e158d389c0e9a68f6ec77018e19896ec669b7a32870593690ea24",
    "2d07308e6454ed0245841e4fe8dca5106acce519117287e72eec1a8ecba838a0",
    "2da171d77d56d2d15de3fae38c10fd66c1013ce39d9098612ebcfb0c01d353b4",
    "2ef7d5809068e897d756dd958a81b31e3811673a716eb80b96680436550e096b",
    "2f30bd4607f94dc21d4c15465784df792b65bb4a786436b3fac48996790733d4",
    "30a8054cec310312b5efb4a9b7918bc9017a9ba7791be810fe620717c1baf887",
    "311bcfca397e2b2fc945896c65e1f77c65b65eef1566facafd2efa5422af9a87",
    "320e6f462b90746e702949a0855de5e98643ab8247a2790324fd096d8a5bc05c",
    "32c63e28472aad0a11b02e5aeb4b8929f7e8696308e1913f61801830e92cb1a3",
    "32e17ceb806576ebd8e32d4b41f66bcf608a15bd0b210aa15d77796d4ee83c9d",
    "3352d284f6e40cbb13f64bce8402f27120729e1580891b16f5ebad98a0b71c5e",
    "337678d219d0bf7820808a5601253b763a134c5d7eb54611646a9e31d7c76da9",
    "375d804d7224f67d68777237772a7ff4700ef206c4d37e12f7e6c29a5b4909dd",
    "378f5e12256d4d847ec2e5a750b2e5118b4749cd75352d1ab26cfd3fca936f06",
    "38192530b3fc969c96e69d2639f27d69827b72f9768dc230fee963a1c0e30503",
    "385b651256d0dead00913354a63b739928be37bc6ed690290b893cba5cfa612b",
    "3925ad1a1f1767a30bfb7b606c8bd43bed9123e245bc46fd11950ffb7f0a9745",
    "3a612daf447da2b013c9447275dc050be6d2bdd24a9a7b9f6eb79733c7adbe06",
    "3a91fc2e11460b1261b1ecd2602ba0a90136013a217ce57193e0942271092d78",
    "3b20f38da3ec025f9e8d09269c58ac844a3961fcf5edbd2ee838128defc1ff5b",
    "3c64b96a3abc6fc189cb1f34ad49ba6c5fb5e5e20a0ea3e92cc2449382208292",
    "40f58fd668b5dcefc0961f23df6603facf4af34e3f41a587fd7db14d7b6ae28e",
    "41018deb1c25226dbddbfc15aad2449b445fb3a7658ab2ea4a9b37379b424b0c",
    "416fd38610f42d9ef8f75105241b3283a3ad8bd81be1160567bc471ea2c72c3f",
    "418e7bd49cf82fa4db223d459972dcde5537101b190decdd977a5ca5c40026f5",
    "421d01dad959cdd52ca554778069bea1fbcf998b4c381d47667a154cce00c042",
    "42f53c2b2eeed16663a43b5762d27c3bb7f1f1788de309495467ceb0a73de661",
    "43f90f5bcce5a0c127dbde2073348fd998ad3c4f60c435309847843e09200d71",
    "44680ca71d4526f2215d696fe1d21bca6811763b2961c525395d2df051900fb5",
    "44cecdd7550e09f2fe461809b841d77032c43b289c62d7bf2aab1a009d2c42d5",
    "4515a425ba543f326f4adb314b2b8cef4ff250125cbed14840c4bdab272601f2",
    "452b85d8be4a52abe64546391ae3055eb6145b689520b29313a79a1a0d1f209d",
    "47d8bd8487117f1f9f05fd56fd011946858084a01cbaefd5deed52e9a05eeb2b",
    "489d19f8ae1354df5db2148c49eade4a15c0641c69818e313a0fdd9eabad8995",
    "493e7f615eecc4d2dc0d96ee7a52b44404fed8580293d78a030b4e1fbfc90193",
    "4a91c00b1aa24eba6da1c5e7a994fded7998d79ed07501ea069691162ae12a58",
    "4c5b7243205d1a5ee684ceae229e61aa71c0fdffc38f9ec241c85d5a0c7b4ac2",
    "4d0240bf1ad4711a36ebf580b0dabc11217f3ab8b344def19788a30ad3ec605e",
    "4d75e96515c0f818c45cb393d11c5df86cc88c538dbe5cbfde75bb8f396a84b5",
    "4dc76b03c4ff5a28dc980949cf5ec00e573f5787494b7b051b5ac9a6fbb5a245",
    "4e3cf84fe0e0ec8b9fc0e4524ee65aadde6c31c8bbe58e6401e61d4257b15ce1",
    "4e4124114ef92a501dea4343982f196c4f45ca4455ff8950c8d9c929406d8ade",
    "4e891dfbdccb53b3e2097cf6e88925a7ebd745379c785b8a7d80cf1ad720676e",
    "4f111f00d2926d8f01d9b1a976dcb0bf3711217d077710ca24991751bed08194",
    "4f5f22fc612c952547aa473dbf415bde20ad1be13bf79030d7b3d984596af75f",
    "5142c2ad3fdad29994df7ca3511b29c1dd583de25b60a0cfb86a148896478b79",
    "52c1efa3d3f6bfe678a920f6aa3fa449818aa5a0f977b09425ee433b23efdfe9",
    "532bcf740ba61401178cc282b9563a4c6f453242c950b19c7e7df793f8c4a212",
    "538bd7a7556633f3b68c8e7c4d903c032c30b3ea5bb9c70cb821613979c88c70",
    "5539828070720d76f113d895fcda29cfaf894d04efae18528c69439872f77bfa",
    "55c3fe57a8f9075727da742f1e2599984ca4413439f67a16d4e515e550c25557",
    "55d9814224fb31792e4c5ac20af7bcad95d862048c2a77c8d6d1ccc59a0e575a",
    "57c454ec8e57d79da1d186e5f60148fb852f1622fad96d582520c5388a270aeb",
    "595d98cc7840412aa30b75c29bfe6660b738ae5121181575a85b392bfb783ad9",
    "59ef3d51a2b43966044b4cb6f8d2a1c242325ca2cbec1774ea20fec1f0af4a19",
    "5af45c8c470d38ceffaf6f8c84c683d7387374854c595dc3a203fd686e3dd61a",
    "5afbaadaa5a027c53cd0a1049f0e208d7aace102ee92c3555b4eda3c26510627",
    "5c364e239f609d5c3b342aa05a5f73dad3327a99608c1e6d42b5f78c9858e447",
    "5cd7b76f6a07ce9e2faaff3a736a186baf83fdc5d498a86bdc8be35aaf0ae818",
    "5da07be6b5ec3a33241a9c835b061a7c858424e5385e9f7c143dd7bd4932e872",
    "62da62583976549ba701c3519663e71d68163c434b3e33d1904d195f703494b8",
    "630410822d19d58cdf8702f21d08c7baa5be4daef76505267ff65772808abfc4",
    "631ef570c1d8d2c40df3c0a6bd0abf52cd4ecfc201a97eed1188be1578ff13ea",
    "64ade7fd31ccbb04f726d1e8763e832a70d54d5510694a1d24df504237863d4e",
    "64e068d5647dd38fccd666a8f931bc5520b9f6fca82f3f096682a9c919d95623",
    "65d95807b006c46c24afcb80e129537a4cb9a23a3a40948de868df8693984751",
    "6630abffcbed28044cfd761789a398ed9ed34a02913194a579bbbfb38c5e0d88",
    "6698d6d64410cf58500b1809d37a22bb8c4ad106d3097a540c4d764f2d22158e",
    "67546e46be64c0ddfcade023c4e1b2bdec32c3549916a9beca10697700d06f75",
    "69a7c4b8bc2973b74e139cf10826fdf9ceb4dbd4cdaeb4d4155c6b1ccbea1f22",
    "69f9f73e1a3beda2febb0b96b8b66185c9e678d2f6ab0e3e9fc1c489ba862ee7",
    "6a39dd1ee27c5c26877e10f12738625c0387602ec88ac34b72838a9355d63cc2",
    "6c0e44a644361a281e09217c379869be298a564eb3a9f660f122ca20a8bb02be",
    "6c18d6cf515fa467214b9e1e70f23f81872fd3d94674183f455866c4a191ba51",
    "6c8f01238b1b01b82b4f658a1db74a042582877474c023559cbbcacfb907e410",
    "6d7bb4494182fb91ba5bd784dc5cb607acdface719115704d589e964a04c0b59",
    "6d928e2e337e93aa7ddcaba0c1d3042319a2604fac64633a1343c702155daed0",
    "6ebebc18481392d69825fcd8132fdd482b7a57e059fc717ecbff4f250fd5b56e",
    "6f22f8173a786389c223efbfbfe51d31148c0e495c498a4db5951254645a3162",
    "6f46cbd2042de3560ba457b53655841d17601cb874edcc5941364db7f24fbe67",
    "705d073c3643dfc416eabdc438e92ca47c6c2ee07c4c86566f4352064d671798",
    "7103aef424c3fc6b2bb79418fe8f82647ffa013ada8ec15a099d5031cd4f2fd5",
    "730b19e39f798ee16abe94a38bcc72196c1e0947f50cd76888e2b789c5637232",
    "73409c66566e6139bfddd59d71edc3e7eb7a8d801ce9bf03ace83ec55ec26d7d",
    "73c451af4c2ba0dc90b232db9aa0bd8fff583193aa47a57edbce059dbe6e99c8",
    "745c7ab2002b2e83b21e40cdf3bc2c1754a726fcbb225bd4199ca67acfb8aad1",
    "74d0a78112aa0deb02e301f1e0d81b34afd4cf551cb4d75c8d3a0663282d7fd2",
    "75ad4f65d9cc65ab2364c448235dfea3b93f4afd4f6dd29b493fbd9c241dc9d3",
    "75dbed5caca7f66d64756a0d664bed9917e221caddcf84a8c707133ea6e1a776",
    "76aca80954ac0af715f12df3eadd2bda4a694e55b93fd3da7f74452aa80a1daa",
    "76ed632eacc9561fddba93918e12fa408df40a2f9e8e038c7803346d9e2b9ab3",
    "77438d05743dc9b4e8dae359521432b16a1821a524682dc850d2229520e47d3f",
    "77f4589a1f3270da4ab351949397f82a8260be8a50dbdfa3062e757d102d3e0a",
    "78b90c284508c45b77c81595273f185649d8f05219f9e9ef20dc3b0d2962504f",
    "78f76acebb580f9a847d3d122c4f3541a3234ec542fc56545864ea496987d507",
    "79d094bedfa8c4c0e506b20448a809af829478468641034c304eb362e4b29c7b",
    "7a35d09ee7c42faca162552e6e4d5b41bd5e2fa7124e5edbe5ab492c8b3f31a3",
    "7a3b52246576901c52057c9bab44a450c1cbb9de9da40fe6eca607f48464e82a",
    "7a451cb4095eb1dd8c03ad540e3b456838f4b671795b7d5e93395ae2cc8a6a51",
    "7a4c90bd221c15fe7280238b23ece62fb53d416743fc55de113b519c0bcf191b",
    "7b666f70280d7779774293f9ae08960860d93fdfb7dd6cb1757ff79369acae93",
    "7bdd15fb429acbce68befa9092d6ef535e8a6fc7097c97187429802a6ccf12a5",
    "7c3cc78603ff184c006c1d7a0e3efb24d03edf278702d9359b742b227a6fc731",
    "7f153bf814075079965592c6d8fa0dd3067c501094c89978cadfa30ac552c7d4",
    "7f691a62b3f87eb1780e566702142a0eb87b9e046e1073666d1767e676543e40",
    "7fdfbb9da814aef2d816dd01488f6a1d14ecc13c9cfd7e0116dba0b8fd114074",
    "8006286e3c8f463f0a656cc211fc069febe3c83d11f67ccdcd06efc6756f75e0",
    "8092b14a6a36b96f70e4da690e477ae61ec9f066d96b016f583f9f935a827172",
    "80f8b828a90fd544aa4fc204007f7cd7581205f55a92b06a304acf151e9124ac",
    "81bef40a508da0cd631ead6a5bf97afa0f9ca803375401130e45e0c51c4b964c",
    "8304d1fd418667ec4e9295a6cb8b998e22b02e08c31f8dad9fa39dbe97f78477",
    "8377fb40924e2a3f0d6483a97c14d4787cc24f1ee9742a5837b5f981fbbcd5ad",
    "84e9c3f0807f3055cd2a4f952c86b9f7f4a402140ce1ef3abd874b8f1fca3498",
    "850bf3c8b261354aedca1fdf00e774604ec64b6c5e9255b4bc92eb77ede0eb66",
    "85923e9d01f9d8fa2f4089e9526d5fbd97053d13fae0d59d44b515f13f6776f7",
    "86361d287d47aeed6a4f7e388508b985abf8a18fd403a4c537e049b13b3b937f",
    "8651b52666ea62d6c798477a0df8ffb1a4b3731a19cdd6cc9c635a5be0b6de2e",
    "86e828d51a937d3c1f2e7f0f9514f66b8055546d70b87aaff0aeaea3a8723793",
    "8731bc8ccd3bb46c23308b15b584b6addebd92a87886a2991e981d624abc0910",
    "879c9e25c5944d176282ac67bfe6608ad4c355cc04b280e8a5c015084d8cc9c4",
    "87ea4048cd72e725b5b9bf68f4073e692d37864236483844a39967fe838dab91",
    "88c2790b2605c2a500fa22a11d61d893a665e4a546b1f1edc4e9b456272559fb",
    "89cb403a972b4b6dc8e6d39d8f893c49441c7b06c0b63b5c9a6393a2ea74415c",
    "89efe1212ba81b8a18f90987fda8b4b7519dfe50e9e30c72195fd52123eaaee2",
    "8a4f6da49c5faf5c95f60d0c7326975abc037f18849a2a21b6fc66c6f9846113",
    "8b0ea402454b03e9f5bf9c5ef2909096a05438931d0dd2603c47bbfcf10c0149",
    "8c1dd7b48f95e2e419e9edce90063264ba73b77b99ee5196eba1928bfb2eba2a",
    "8c42d390ced393ee687a7a780b22c358f951264cbdf2a67f85fde4332c946981",
    "8c675a406ce0997c0137ca65539751a3cdac84d0bbe5dfe3804bee72f923a9a6",
    "8cbba5a304020d0957bc060796fb0e460db4252e3047cb9e1c7b6b0d009d3987",
    "8dc13ae17cfa3c5af71517e85daf13da1a806ade525e0e57aa88158344c8b776",
    "8f28bb67eef7c9fb4bd06e33947c53b2f291e69e27048f3428c7a95d1f2e81ba",
    "8fcc320593ced1656a06ff093f796ef8db02c92e21be43eb1d0cb462a6d8b567",
    "910ae9d126a6d21d8cde131b291a653f7980f3c0f9c4cd5d52d13f892a0c1a5e",
    "926f727e395505b584a084ade1282bfce2eee14eb0b7e0b675cc41e8fec30ea3",
    "928e555fa245e5364ea0d8e7194cc03e0ac686e3cab9853b5c0b645f90f3bea9",
    "9317b6ef5752e1e33ae9d392f13c0ab056e93e7bd0da275f4cb119ad8e0f0e06",
    "93403aa8d77f8ea9be3e34e5a860d7402ec4b1356e140a2622c09c793ac5e687",
    "94f632c5d2c8d56281cff4f285bafc9a6548e0dad3d8f18d2500e5fcb72c2ffb",
    "9516c7bcb62d774748d85ecaa69fe9a98c8e4803beddb917b2bb490c6f1819c2",
    "95e4c8c2d26c39ceaf3292f450349ecdc0056baaeac5e7d70643502b244a78dc",
    "95e77281b965392607ba64b63314296893d0d6c360a864a79af766ee612b3759",
    "99545df060fde3055cf7536c72d513d02cfa4daf1c91d04f0658fd73fb2b5536",
    "9a5edfaf1854212f89377944bb478b5823aa687b08d88c5a829874afee23b2b1",
    "9c08caed71f4dfd1ccdc6fd151ef630e5d1f3267c8d53e10f7590584e35127a7",
    "9c48b71659eb658e8a4147c9180837675522ce55c3a778375785f91237c4695e",
    "9d0b4b7d6375a4dee340c3498ca6869c7448ab4a091d982578bde4daf5c6d8e3",
    "9f46f3b6d488c4dd50e9551707d8ce2bd7f905bbbb37a56c772548113c8271c1",
    "a00361277bc9ee9a643c9afc46ab4286aa42855d57920c9241919b08676bc885",
    "a030cfb0ded6a976179d5335fe8f2c41a5e219658f5e1ad30425aebedddbbf0d",
    "a08d6f8ba3de029daa8b9ea304b4f7b17081a44bece4d2972bc7b5b05a3026d4",
    "a1300159e91ef43ba29863ac66ee05f91ba168553e179dfaafd84af36e5d7f07",
    "a231f5599ab0a77580bb76d6f160b65dd543d0150d05a6c82e62056c5573998d",
    "a29451eb83325135424d99e05445267a4ec638149111a590a651c072fd059869",
    "a34c37d0f9491daa560bafd1d2bd489301f92e0b66f4d9c41bb18b30024836e4",
    "a3e72d68de04b3e4d09f0d51d8af9c923e7fd43430b77e7f000ba4010517a20a",
    "a46e25618775d2cbb770ea02aeb5f6e034bd3eabb916940d01f3d0e002193d28",
    "a49b2f4bcb1d25296705a4402ee1d7cef970d7a3338f9c92cbbf0ddb00bdd10e",
    "a4ad2546137fede21b43dd48cffdbcaef94a43c6e57e8a40c98aa4f18bce70d1",
    "a6aeef8e56fc6a6180bbaae11255f0c84a6e74f5467905f8aac0a64b5281a099",
    "a71ed99e8f7918ae366ec05d25a7e7ffce6540a8aacc8c059d4bdc1ce3ee42b0",
    "a739ce8a02bcd9a48743d5b022cd616e36fe880cf6537c9d895102d017b81938",
    "a850ddb4e67ed306ff06ed10b34f48ccf7bc5548c4dd4f0a0d4329d28c66f9c9",
    "a8a7053e65fd36aaab0953cc99ac7213f7e26dd9f474ad054516fea1b813746e",
    "a904626d6fa78e9f7e732836701d26b950350013fceb09f6effae57c39f6db74",
    "a99ff3868a4aadfcd751ad706212c08f1d685e68487c3b18f2f2b4eebce56f63",
    "abb0bc3b974ccf53fe05ced798bf1863581565418f4a87ff2f9c6024f2a402c9",
    "ac27b32e10b4e20cfa3965fde322c4246f4866c9b6cab168719c970742815428",
    "ada852d3805edddec22f82c0cc90e038a07d675de086557d56b4e808aaa0f64a",
    "ae4687b8c7aa2fa2b44d16b7fc07b29e99af59216d3c34649907ffaa852597bd",
    "aea57bf4fd857fbc2f55d02a036b0d6e66922420753369ae7564c3b327696e4f",
    "af0cc2b2bc389c87bf5a2c3092fff272e19bdc9c8ffa56497bf1c8fe7981b040",
    "afff081ae425ec6953d7694e1585b0ef08d9e0ead952b817722afc5f8a12f5c9",
    "b0e601419e114f3b1cc6399a772749ef29e6f228dd0e394fb6c15f37d991a9f4",
    "b252b3931553cf1fcf2fd162ba75b63172255d6495997a8c7837f2b17587387b",
    "b29a05ff95a8fecc22ea23fc7f9a04bd1e37f74132ebe59394f230bc31670a73",
    "b34d83a8ed3733580c56980339abba991163f5766c10d328b2eab8ba71332b4e",
    "b3e842749ab2ef2a3f28c98252cc41ecada7a1f034a649d936f0c28280a3d4e3",
    "b70b70f41c760a810be924f945c80563546f2f737730d773cac192b849f87711",
    "b72d5acfafb363ebe3ec0f3d2c5224dd4c74bf8a7aecd9203dba28b65aed6165",
    "b77a96f7551d1ab88dc9a24f3c3f9a4c713ab62b5ec13be0c22e83ce5d4e8c41",
    "bb00c4e0802f0c00b335a60e94ddaf860b6629749e8c572cefc630afbefafa2e",
    "bc47e7ecebde45b8e2bc225bdfc2f1d9527e2f8a3bb10ff798f1c63533d1519d",
    "bc6ec641a9809051629b7a8f4dc06f6148d32af213cc312d5d324a303333bb52",
    "bcd2d29b7d1b7d79b722d8b606923749327fb9988b87094798dcc62fc7eac4f6",
    "bd335cddeff9586f9c703fa604fcdb0bb2f771f07bd1477eb6026e231d39c6c2",
    "bd980e822e1313eee662dcbe48edb763510cb90fc6bfed796f34cdb55e3ea0a8",
    "bde64f0765d46174f2679c29ec5014ec01dfe7896dfbb72a99145bea0cb35cdf",
    "bdfe8c52e628966eaec4a8537303f0c40f9f41c4af99b35653a6d946bc7cd77b",
    "be5363a941fdc11bfa4ed7907f502e006635a83952f22ea930d9bc1e84ab6c39",
    "c0b20d787621c62060f5bdca5cd787738188f2c597387810ef9ac8f452b22ab5",
    "c1b8e471d81a82a55ef5664efe87bb25bdeaa42e9871260d70522e0bef177510",
    "c38a90dbd8cfb4d6c31c63f7fad8847e16bba119b1dad3db0e96cba494028635",
    "c468759924d327f72c6de262f60a3dd3c1389fe0301d6971b3ffb3c37cdace50",
    "c4dbdafb952fde3cd504eabe6bb2c6c215d80c02f0db3362fdfbef1f1a1c70db",
    "c4deaf6f6be3f59179988da312dca9de46e0212b141494492506c2473991e585",
    "c4f93d246588900767866dca0813e07f49a9c6d11f05dbb189600f403823d9f5",
    "c57d7e92019708b614c90fa3685cd644f543a60153fb99ec9b67c381a245fb2a",
    "c5adb2c6fc0e9bf4daa9c6c738739dc09bf4096c7b1a09322a12aafff07b8161",
    "c645232f2e2648e3015bb92db076a1e721cfe13387e9e7eac49b8b6873ca9e97",
    "c7b9be0165f908307f9930376001fc146dbb5cc5cd40095b3cea17dc62053096",
    "c831515a37c307b0cffdc628082a09ee335cc790e2b7dae97f3333dc69c355f6",
    "c850087d0588d18fa21860802bb3b9b2664d43c583eb0d2f556463ce5e5ed2f6",
    "c890cdf182f3beea6bd3836f3c38574d73c7a37f0bb3af2b9e5790d4450013e2",
    "c9324ec51697783d5886899c50cbd15283ce40bcdca416726f156d2a3df70dd2",
    "ca23c946bf7502648892ccff27d0c367f898c2fb39e83aa2daf3e2b0003dcf7d",
    "ca75049b36d7e80c1b2f22f5732a1b52f4edeb08fd6eb4a6760146267b0b2cb9",
    "cb388061bf03c498eb34d195fd9ba19d584b4219e1aaebcd86f80d2a9181c774",
    "cd5292bd63ae826035d428e65f685fa7ffb59ce418a94b3e0ff67e8601b88112",
    "ce138cb4818c0bd447afd91f06bdac8421168a29d01c4d76696f91a7b18aada5",
    "ce93d49452541f34c3921af1112c46f5a8bdde3cb9302053df9b3b27f94719a0",
    "ced97bdf2fc86a7d46933423481cd5eeef745851a42ea5f1f1754d4950ab4dea",
    "cf12abd6c179c2396371b50ad6e66e4b9344b31f50908918e9e2642cb42aea0b",
    "cfc1815aacfa36fb6ac142f933a36a1035417553553dacc9cf38cfd95405fdd5",
    "cff3abd71a45330188d9a5cb91852190a59edfb7a7454a1b29a898587752ccb0",
    "d040228987c40dbcbed9c6bf38fc83c5c3f61ed5554140e71ce61236b68edb8a",
    "d2138dd99e0f6b89ba4678504d858861e939d4be672d79e30cfa1043a9b49fd9",
    "d2240b0776c2b730066c04f213491026714003710c6df3fe69df8a653c344efe",
    "d25a19fc772ed4c865c6c27e55ebf2e28df094accbb33a7bc98ae2e5b64b140d",
    "d32848617829c36485e47c10782ab0a39a99cb3bcb4320c27cb8938dbe395f06",
    "d4dfd09e73510e839d5465979f883b064585e7e1d35cb67199ec6c07fb42b1f5",
    "d650941feab2653cdee2932e0bad657bb9987f11807b7dd0896e1f82108c3bbd",
    "d701717167b2a37556ec36749129354505bae89e3d00877b5fb243bff08fc5fb",
    "d79d1f8b76c9a4f5546474bf8cfea05cf1a9d3ef73698de05d5b6ae502861708",
    "dab6d5fcaa9dba360b6ad99fd4476b059ad505d8062aefa98145935dbbeabc56",
    "dbe52bcfe123f334e33e37a22144c659033c89772764006eaf68424bfe00ab74",
    "dd711957bbab2686824b1127ef9f8b62a6921a667c14a5cc82b1da44b99c8341",
    "deb795abad2a89ac0030f2d527a6bebc9eb24962d9093f3c18a8e944af91b623",
    "df18e37142548b817f4100c6a0fb3056db4df82e327f5e5383ffd569d98c0afe",
    "dfa9599435dd9f5ef1ca43e3152e6d8a4e4e772d4501acdd468bee6a153074b1",
    "dfabd4043f20512a835356151db8e398244d204361d5039684d5a3a96720d24b",
    "e120049c0f9bffe62c94e71f720f6b699423608541b7ac1bcecdef7cb5202466",
    "e1eeef0077e8f80d6f71c92be9758592699ed9171c22e9be78978616ea8e5686",
    "e22f3130f5995764c7c314c41b1dcee12423100d634ac713b6f84c285675f396",
    "e2ae04aa4f977f8cfc1daffaada0a4ba468b45c32376ae090ac39205bcf9dfae",
    "e304bb2319b8523486e55ae279228acd62ef70e6692fdf161b686d463a13f83d",
    "e41c6d8c290101b72381d468e596e4b5c314ce10bde59f6b12c5c824940ab117",
    "e5f129cae1f2b6f26e51184994cab94bcd125f02e09ac6ba71dfa1de87175db3",
    "e61e7bf0dffd5529f5d1504ad67305079fecdf1b6d509aa78f2db91c30e990a8",
    "e61ec6e6c2e03637cef858db99289a6ae5d636086f186a22b631da7410b63cc0",
    "e78a14d392a171bb45a5bcb8d6d4e3a685893ea6e1a04b811066c34873e0c311",
    "e8351a772546e180d86e5e64cad4e860698c6d08f44878de4a6af3bb44d0a4f5",
    "e86d5ce28625ff8165c8fcacce6858c25f8606ecabfc7db10e5b39a52ec07add",
    "e8cb8e656300ac5650c1d1be41861c8d21bbe8f5aa7fc4455b3bd8ffd0887bae",
    "e971ee2e9562376e8fbe0fc009f31698088eebdf667ab7fbc7ec109ab7191400",
    "ea59fe8732be2e09be2841ac3282c6c1f14afc9a0854aeb998a4fc66a4c708b2",
    "eab23c49572e4a0c28c2b31622c90bcf73cb015ce66b1f19a75e9ac9d0127f0e",
    "eac419fa3d3fbf476c91c589dee60b91d5bb08db55bf2e381cb3898b67f4035a",
    "eb65fce58301a9a60758264fb3641e309d67350a560b5ebe24becd162bed7242",
    "ebb083174dfdb988ba06697d35855e42073e1f271aca5f68b4e595fd91c81cda",
    "ebe7a04f1273e2083e09cbbc815e048e6232f6d7f08aba25574728af48f6fd8f",
    "ebfc11638abb9118641202e8f1999776b71ec49f8289af8e08e398fb5a8019be",
    "ec4857907238a8bc8fe7ed19e7b65000460ca2a67711c35db476f6ab392b2ede",
    "edafdad01528e506da1582a17cbce0b0010a730f53c45627ec066e0aa39d81d1",
    "edbfa3fd90f56ac31d8967735e84731085f6c7940ffe0ff7433b02b47f701dbc",
    "eeae5aba605538054807fef1b9b7f4d134d0e00487cee53302a9a9c4c74a7c4b",
    "f12cafddad73f302ccecf7380f75462c0e2a7022accee6ff172695cb72d07126",
    "f1acc1d502c676843cf4ab61b0807a011d85beca5df281562afa38b540804e67",
    "f2c483240d89d1cd4f7614f7c7fa0936321d258ee08baff8bce0b638e6211121",
    "f39af3ca6c2b50af0a48bcd370e76da82013900a7711f712a3e52958bfcf8d17",
    "f3d654a81e7cc04655149a491d4f9896e773581f1174a42f440711cf787a839e",
    "f400cc273fd87328862b8b226c06700fb699d056efd894cfff830911e91ec451",
    "f422a792b2ac6daec8d415858909923c2d44d25ff1d43564b3e35f50268b01fe",
    "f69df6f1f22a5ef238166236b7fa8819cbdf235a07b7b11d57133745bcddbac1",
    "f8295084053ebab9959d8c36b786df0615b6183107617f6dc257cca63c456915",
    "f86ed485e02dc93af75fbd167d1cae80246b95dc82bdf577dfc33e39699e4a3b",
    "f96da0861d98aa87a1151a7e3a606dcb6a39ba271658762bf7f5d99b449d40af",
    "fa9305783447d18185b2969241f7f7790f11f72ee556ab06641cc871b657b510",
    "fb56fa31593939dcca348c3fd796489546472babafea912250a8a7cf7ca8c688",
    "fba407f6d616b0bb6d0720dda9c1b5385499d71e0dd1a400d754a6401213dda4",
    "fbed3d391ab0c1a705f52b5035b353fade77f04a803c5c4fad7b3adc9ff85824",
    "fc4539e13aa4e61112ac42ae16890725c4ae31cab2a3e8fc7874dbb2f6bd1a82",
    "feb7d54f164d3fc98209bfd03e0c43af424a47690669399b9e3e4b497f2d0dc0",
})
# the private ALICE-* repositories among them: the name as a whole token in any text is a finding
PRIVATE_ALICE_REPO_HASHES: frozenset[str] = frozenset({
    "009efce69e63884cc51a1d652aba09f2592ae5619f0ef655bf489d37d6441d76",
    "017a920b4b49ba2c2029a50c6488faa018b2272c7f53cf7cc26e5fb02a02270c",
    "0ab7255f88e6e3f5a8795cbf061b6998e30e4c935a158e7befd6f05a83312d0f",
    "0e0ef1537cda3efbe9a06c6ae9d1aead42664dc8dcc0ef52844bc75a561272cf",
    "0e9562684217083eae9a05ec5553a570adbca78ebefaac0e5dd10c7237ca8370",
    "10471b1fe18d3ae67e66d105dbee46b8e8d70bf40d2c634020d32c0485f48a93",
    "145c52a854b72716135dda9fb0e2f25ecfadffa33321019c076c3c3177311b60",
    "14eb67957164432af13b6f3fd55374db77d11f2d9cff8a69f6d0845bc25fbd6b",
    "1589d73b53aad6afd0c45465d13d709c0231c25b8bd1c9cbdba19e28da0f5c14",
    "16d189ebb51c08241c10ccde7144ed2671fcb0618db0e18951b6fa45168aa053",
    "17ccd2ddb9f66b5f3530631ecc5db5ed431e27d6ff7f7d4aac1c1468dddb6113",
    "1ce9a161cfbdb00a4d7bae3c3eb32d3a513d1880d4342221f399125772ae48ad",
    "1fadcfd5c68dcce455bd8e0c180a061d732f9a2dbd7745408b387eb2d405bbbb",
    "233328bc0ec649ccf7a54dca441ffa3998ebf12d9de39f8c23feb0739a5afa95",
    "248cf5c41d64e689cfe8e98f39bfb1db7a26d1c9fca965dc4539bf5af5a628ae",
    "265af92560195d72769fba41ebfc5ae498a4e4e3f2cba9d2a01addabffeb17a0",
    "268fcdeb74e27ca8fe906582e6191833c1eac1d249411e262a420e4313bf18f5",
    "27d628f4c5ef9d622abfb867fdef124431262940a7a7075064282740010b7c56",
    "292b3088c52a6c88be783208633fb18291176e1db5ae2af54202209025f11bdf",
    "2a6543e9bddf7b2964df567ec74358a0dbb342fb4d2c39f7e3dd6b0b313e7b60",
    "2a927c40032e158d389c0e9a68f6ec77018e19896ec669b7a32870593690ea24",
    "311bcfca397e2b2fc945896c65e1f77c65b65eef1566facafd2efa5422af9a87",
    "32c63e28472aad0a11b02e5aeb4b8929f7e8696308e1913f61801830e92cb1a3",
    "3352d284f6e40cbb13f64bce8402f27120729e1580891b16f5ebad98a0b71c5e",
    "378f5e12256d4d847ec2e5a750b2e5118b4749cd75352d1ab26cfd3fca936f06",
    "385b651256d0dead00913354a63b739928be37bc6ed690290b893cba5cfa612b",
    "3925ad1a1f1767a30bfb7b606c8bd43bed9123e245bc46fd11950ffb7f0a9745",
    "3a612daf447da2b013c9447275dc050be6d2bdd24a9a7b9f6eb79733c7adbe06",
    "3a91fc2e11460b1261b1ecd2602ba0a90136013a217ce57193e0942271092d78",
    "40f58fd668b5dcefc0961f23df6603facf4af34e3f41a587fd7db14d7b6ae28e",
    "41018deb1c25226dbddbfc15aad2449b445fb3a7658ab2ea4a9b37379b424b0c",
    "42f53c2b2eeed16663a43b5762d27c3bb7f1f1788de309495467ceb0a73de661",
    "4515a425ba543f326f4adb314b2b8cef4ff250125cbed14840c4bdab272601f2",
    "452b85d8be4a52abe64546391ae3055eb6145b689520b29313a79a1a0d1f209d",
    "47d8bd8487117f1f9f05fd56fd011946858084a01cbaefd5deed52e9a05eeb2b",
    "4d75e96515c0f818c45cb393d11c5df86cc88c538dbe5cbfde75bb8f396a84b5",
    "4dc76b03c4ff5a28dc980949cf5ec00e573f5787494b7b051b5ac9a6fbb5a245",
    "4e3cf84fe0e0ec8b9fc0e4524ee65aadde6c31c8bbe58e6401e61d4257b15ce1",
    "4e4124114ef92a501dea4343982f196c4f45ca4455ff8950c8d9c929406d8ade",
    "4e891dfbdccb53b3e2097cf6e88925a7ebd745379c785b8a7d80cf1ad720676e",
    "52c1efa3d3f6bfe678a920f6aa3fa449818aa5a0f977b09425ee433b23efdfe9",
    "55c3fe57a8f9075727da742f1e2599984ca4413439f67a16d4e515e550c25557",
    "55d9814224fb31792e4c5ac20af7bcad95d862048c2a77c8d6d1ccc59a0e575a",
    "5af45c8c470d38ceffaf6f8c84c683d7387374854c595dc3a203fd686e3dd61a",
    "5cd7b76f6a07ce9e2faaff3a736a186baf83fdc5d498a86bdc8be35aaf0ae818",
    "631ef570c1d8d2c40df3c0a6bd0abf52cd4ecfc201a97eed1188be1578ff13ea",
    "64ade7fd31ccbb04f726d1e8763e832a70d54d5510694a1d24df504237863d4e",
    "64e068d5647dd38fccd666a8f931bc5520b9f6fca82f3f096682a9c919d95623",
    "6630abffcbed28044cfd761789a398ed9ed34a02913194a579bbbfb38c5e0d88",
    "6c8f01238b1b01b82b4f658a1db74a042582877474c023559cbbcacfb907e410",
    "6f46cbd2042de3560ba457b53655841d17601cb874edcc5941364db7f24fbe67",
    "705d073c3643dfc416eabdc438e92ca47c6c2ee07c4c86566f4352064d671798",
    "7103aef424c3fc6b2bb79418fe8f82647ffa013ada8ec15a099d5031cd4f2fd5",
    "73c451af4c2ba0dc90b232db9aa0bd8fff583193aa47a57edbce059dbe6e99c8",
    "75ad4f65d9cc65ab2364c448235dfea3b93f4afd4f6dd29b493fbd9c241dc9d3",
    "76aca80954ac0af715f12df3eadd2bda4a694e55b93fd3da7f74452aa80a1daa",
    "76ed632eacc9561fddba93918e12fa408df40a2f9e8e038c7803346d9e2b9ab3",
    "78f76acebb580f9a847d3d122c4f3541a3234ec542fc56545864ea496987d507",
    "7a3b52246576901c52057c9bab44a450c1cbb9de9da40fe6eca607f48464e82a",
    "7a451cb4095eb1dd8c03ad540e3b456838f4b671795b7d5e93395ae2cc8a6a51",
    "7a4c90bd221c15fe7280238b23ece62fb53d416743fc55de113b519c0bcf191b",
    "7bdd15fb429acbce68befa9092d6ef535e8a6fc7097c97187429802a6ccf12a5",
    "7fdfbb9da814aef2d816dd01488f6a1d14ecc13c9cfd7e0116dba0b8fd114074",
    "8006286e3c8f463f0a656cc211fc069febe3c83d11f67ccdcd06efc6756f75e0",
    "81bef40a508da0cd631ead6a5bf97afa0f9ca803375401130e45e0c51c4b964c",
    "8304d1fd418667ec4e9295a6cb8b998e22b02e08c31f8dad9fa39dbe97f78477",
    "8377fb40924e2a3f0d6483a97c14d4787cc24f1ee9742a5837b5f981fbbcd5ad",
    "84e9c3f0807f3055cd2a4f952c86b9f7f4a402140ce1ef3abd874b8f1fca3498",
    "850bf3c8b261354aedca1fdf00e774604ec64b6c5e9255b4bc92eb77ede0eb66",
    "8651b52666ea62d6c798477a0df8ffb1a4b3731a19cdd6cc9c635a5be0b6de2e",
    "86e828d51a937d3c1f2e7f0f9514f66b8055546d70b87aaff0aeaea3a8723793",
    "8731bc8ccd3bb46c23308b15b584b6addebd92a87886a2991e981d624abc0910",
    "87ea4048cd72e725b5b9bf68f4073e692d37864236483844a39967fe838dab91",
    "89cb403a972b4b6dc8e6d39d8f893c49441c7b06c0b63b5c9a6393a2ea74415c",
    "8c1dd7b48f95e2e419e9edce90063264ba73b77b99ee5196eba1928bfb2eba2a",
    "8c675a406ce0997c0137ca65539751a3cdac84d0bbe5dfe3804bee72f923a9a6",
    "8fcc320593ced1656a06ff093f796ef8db02c92e21be43eb1d0cb462a6d8b567",
    "926f727e395505b584a084ade1282bfce2eee14eb0b7e0b675cc41e8fec30ea3",
    "928e555fa245e5364ea0d8e7194cc03e0ac686e3cab9853b5c0b645f90f3bea9",
    "93403aa8d77f8ea9be3e34e5a860d7402ec4b1356e140a2622c09c793ac5e687",
    "9516c7bcb62d774748d85ecaa69fe9a98c8e4803beddb917b2bb490c6f1819c2",
    "95e4c8c2d26c39ceaf3292f450349ecdc0056baaeac5e7d70643502b244a78dc",
    "9c08caed71f4dfd1ccdc6fd151ef630e5d1f3267c8d53e10f7590584e35127a7",
    "9c48b71659eb658e8a4147c9180837675522ce55c3a778375785f91237c4695e",
    "9d0b4b7d6375a4dee340c3498ca6869c7448ab4a091d982578bde4daf5c6d8e3",
    "a00361277bc9ee9a643c9afc46ab4286aa42855d57920c9241919b08676bc885",
    "a1300159e91ef43ba29863ac66ee05f91ba168553e179dfaafd84af36e5d7f07",
    "a231f5599ab0a77580bb76d6f160b65dd543d0150d05a6c82e62056c5573998d",
    "a29451eb83325135424d99e05445267a4ec638149111a590a651c072fd059869",
    "a3e72d68de04b3e4d09f0d51d8af9c923e7fd43430b77e7f000ba4010517a20a",
    "a46e25618775d2cbb770ea02aeb5f6e034bd3eabb916940d01f3d0e002193d28",
    "a49b2f4bcb1d25296705a4402ee1d7cef970d7a3338f9c92cbbf0ddb00bdd10e",
    "a4ad2546137fede21b43dd48cffdbcaef94a43c6e57e8a40c98aa4f18bce70d1",
    "a6aeef8e56fc6a6180bbaae11255f0c84a6e74f5467905f8aac0a64b5281a099",
    "a8a7053e65fd36aaab0953cc99ac7213f7e26dd9f474ad054516fea1b813746e",
    "ac27b32e10b4e20cfa3965fde322c4246f4866c9b6cab168719c970742815428",
    "afff081ae425ec6953d7694e1585b0ef08d9e0ead952b817722afc5f8a12f5c9",
    "b0e601419e114f3b1cc6399a772749ef29e6f228dd0e394fb6c15f37d991a9f4",
    "b77a96f7551d1ab88dc9a24f3c3f9a4c713ab62b5ec13be0c22e83ce5d4e8c41",
    "bb00c4e0802f0c00b335a60e94ddaf860b6629749e8c572cefc630afbefafa2e",
    "bc47e7ecebde45b8e2bc225bdfc2f1d9527e2f8a3bb10ff798f1c63533d1519d",
    "bc6ec641a9809051629b7a8f4dc06f6148d32af213cc312d5d324a303333bb52",
    "bde64f0765d46174f2679c29ec5014ec01dfe7896dfbb72a99145bea0cb35cdf",
    "c0b20d787621c62060f5bdca5cd787738188f2c597387810ef9ac8f452b22ab5",
    "c38a90dbd8cfb4d6c31c63f7fad8847e16bba119b1dad3db0e96cba494028635",
    "c468759924d327f72c6de262f60a3dd3c1389fe0301d6971b3ffb3c37cdace50",
    "c4dbdafb952fde3cd504eabe6bb2c6c215d80c02f0db3362fdfbef1f1a1c70db",
    "c4deaf6f6be3f59179988da312dca9de46e0212b141494492506c2473991e585",
    "c645232f2e2648e3015bb92db076a1e721cfe13387e9e7eac49b8b6873ca9e97",
    "c850087d0588d18fa21860802bb3b9b2664d43c583eb0d2f556463ce5e5ed2f6",
    "c890cdf182f3beea6bd3836f3c38574d73c7a37f0bb3af2b9e5790d4450013e2",
    "c9324ec51697783d5886899c50cbd15283ce40bcdca416726f156d2a3df70dd2",
    "ca75049b36d7e80c1b2f22f5732a1b52f4edeb08fd6eb4a6760146267b0b2cb9",
    "cd5292bd63ae826035d428e65f685fa7ffb59ce418a94b3e0ff67e8601b88112",
    "ce138cb4818c0bd447afd91f06bdac8421168a29d01c4d76696f91a7b18aada5",
    "cf12abd6c179c2396371b50ad6e66e4b9344b31f50908918e9e2642cb42aea0b",
    "cfc1815aacfa36fb6ac142f933a36a1035417553553dacc9cf38cfd95405fdd5",
    "d25a19fc772ed4c865c6c27e55ebf2e28df094accbb33a7bc98ae2e5b64b140d",
    "d32848617829c36485e47c10782ab0a39a99cb3bcb4320c27cb8938dbe395f06",
    "d4dfd09e73510e839d5465979f883b064585e7e1d35cb67199ec6c07fb42b1f5",
    "d650941feab2653cdee2932e0bad657bb9987f11807b7dd0896e1f82108c3bbd",
    "dd711957bbab2686824b1127ef9f8b62a6921a667c14a5cc82b1da44b99c8341",
    "deb795abad2a89ac0030f2d527a6bebc9eb24962d9093f3c18a8e944af91b623",
    "dfabd4043f20512a835356151db8e398244d204361d5039684d5a3a96720d24b",
    "e1eeef0077e8f80d6f71c92be9758592699ed9171c22e9be78978616ea8e5686",
    "e2ae04aa4f977f8cfc1daffaada0a4ba468b45c32376ae090ac39205bcf9dfae",
    "e304bb2319b8523486e55ae279228acd62ef70e6692fdf161b686d463a13f83d",
    "e41c6d8c290101b72381d468e596e4b5c314ce10bde59f6b12c5c824940ab117",
    "e61ec6e6c2e03637cef858db99289a6ae5d636086f186a22b631da7410b63cc0",
    "e8351a772546e180d86e5e64cad4e860698c6d08f44878de4a6af3bb44d0a4f5",
    "e86d5ce28625ff8165c8fcacce6858c25f8606ecabfc7db10e5b39a52ec07add",
    "e8cb8e656300ac5650c1d1be41861c8d21bbe8f5aa7fc4455b3bd8ffd0887bae",
    "e971ee2e9562376e8fbe0fc009f31698088eebdf667ab7fbc7ec109ab7191400",
    "ea59fe8732be2e09be2841ac3282c6c1f14afc9a0854aeb998a4fc66a4c708b2",
    "ebfc11638abb9118641202e8f1999776b71ec49f8289af8e08e398fb5a8019be",
    "ec4857907238a8bc8fe7ed19e7b65000460ca2a67711c35db476f6ab392b2ede",
    "edafdad01528e506da1582a17cbce0b0010a730f53c45627ec066e0aa39d81d1",
    "f12cafddad73f302ccecf7380f75462c0e2a7022accee6ff172695cb72d07126",
    "f1acc1d502c676843cf4ab61b0807a011d85beca5df281562afa38b540804e67",
    "f2c483240d89d1cd4f7614f7c7fa0936321d258ee08baff8bce0b638e6211121",
    "f400cc273fd87328862b8b226c06700fb699d056efd894cfff830911e91ec451",
    "f422a792b2ac6daec8d415858909923c2d44d25ff1d43564b3e35f50268b01fe",
    "f8295084053ebab9959d8c36b786df0615b6183107617f6dc257cca63c456915",
    "fb56fa31593939dcca348c3fd796489546472babafea912250a8a7cf7ca8c688",
    "fba407f6d616b0bb6d0720dda9c1b5385499d71e0dd1a400d754a6401213dda4",
    "fc4539e13aa4e61112ac42ae16890725c4ae31cab2a3e8fc7874dbb2f6bd1a82",
})
# <<< generated private repositories <<<
# the committed set (the tests swap PRIVATE_ALICE_REPO_HASHES)
PRIVATE_ALICE_REPO_HASHES_COMMITTED = PRIVATE_ALICE_REPO_HASHES

# The private repository names a public repository's text already holds, as a ratchet: per repository
# (sha256 of its lower-case name), sha256(<repository> NUL <path> NUL <name>) -> the number of lines. A
# tree with more lines than its entry is a finding (a new mention), and so is one with fewer (the entry is
# stale: lower it), so the count only goes down. Written by scripts/gen_private_repo_hashes.py --baseline.
BASELINE_BEGIN = "# >>> " + "generated private repository name baseline >>>"
BASELINE_END = "# <<< " + "generated private repository name baseline <<<"
# >>> generated private repository name baseline >>>
PRIVATE_REPO_NAME_BASELINE: dict[str, dict[str, int]] = {
    "02a148b01541d8673d3b2ada504d8e3cdcf7725fefaac791004ff60620516970": {
        "07af59c27628a56caec065b6a595f9138c57ae945916deb06e2f74125532c769": 1,
        "16fbff26d0f2284290ac879d8b1ef9f9648fda32c936c3900662e2bdac670f59": 1,
        "22f28386597ae6385bded720d0a32ee721c3dc9b769213e3807354e64efefe06": 1,
        "27c216c2cfaf4894c57e6b0db2aabf7091e9557b2af0a92b2886840e94e0ef55": 1,
        "2d7fe233313f16b827e1db5cd16baa79ef92928b6fbc48039c1cd259b52f87d3": 3,
        "35632ae1f4734240ed4652246457e31180d5cf571b524b327564246a2aacb73b": 1,
        "375eadcb4e6c4538d49223453e46828e37ac543c5f541570b44c8e5cf643ea36": 5,
        "3fcea33c0c3eb2317623e977c6cb56a051f1be5317fdf9711482438a4397a4c6": 3,
        "4d553b2c0839bd8a34831c3c4bbd37c27bbc36f678a2a2f964c023624c36a8fe": 5,
        "554713f9bf9d637614598e6a5d0431acea19a43d998fdf8a48cda99b6028c73b": 2,
        "5884ef74bbdeb31ac191d21d550d4fbe1fb8d76809eb4978bb69c547784170e3": 1,
        "645b73e33bf996613784cd087bb52069c6698ed1cc397b99f93bdde0c612a0db": 8,
        "686860826bd72a264cafdb46708ddb7d272f1cbca0a7437e77ef603b7985541b": 2,
        "6ceb52b07f205969e9aba0baa2a81e2aa7ac8086937a3096ccf71c74ac7407d9": 1,
        "8012120363d943f64493843f0f9dc3a40c4cfdd8321a923a206fc8ccb12069bd": 4,
        "85aaea0938d16b3d8c4c055e914a5be2bad671aac3d4b72b61aad7b951407b65": 1,
        "8630456a30906bb36d43e42d4dd8a2e88fecfc3db94400aeb6dc96378ac4a042": 2,
        "87f3c1368cb21495acf6ffed2919e14f0a579ccd40581a81b11b040617d2a7ee": 8,
        "9033c73506ab719847987af5d45cfc44039b33f143475aa92cea7f2f15cb8cd9": 1,
        "985f5c56084a2ee94e0c8d10c6cbb521f471518388198dfbc9e8211c4afcfbc8": 1,
        "ac702087b3fe5fd949cf144fed0185d87ba1e89086c5efad6f37ab5459952e18": 1,
        "ae090f435808d178390a4bf8d4996233a4f381c339909026f2470bdfcdc02d19": 1,
        "bc6f04412aa0278aca3d4937f4fd1ff9e51592b2d979ad8a1315eda3054dc75e": 5,
        "c37099404847349985f66ed311147664213448b21c59224e09f4bd96471ae52f": 1,
        "cfecb33870fa458b038d3ed706b9bf5a4e21254b0477a495d71e36079a5b48b6": 1,
        "d5befc0f10a4b89062991a22a23aa8aaa91a6711063324aea03b3065d9f55e71": 2,
        "dde9e5d7e3ca100507f8d2db4d3ab8feefe13135526b9f543bf93ef8844a89e7": 1,
        "e69bb42598639662e56bd8814142cb254baf916ef33ad147fc3dd33c56da1256": 2,
        "f098ecc8fed8fcb5a108e949db8e59d3ccbfd3eb0dc09f7a943a82d0fd0ad952": 2,
        "f2b0570656a0c9d292101afe0d38be4405f37c6c5dec7ab61163b51b665a49b1": 15,
        "fbfa79dc7ab97ba46801acef4d6472b4ad3637af27811ac1cd39a9a8d2b75adb": 2,
    },
    "1a82b9a08acf92623279000cb4e3b49a969edcf8aee1c50fea043cba46ca444f": {
        "0bb1b482a3daabff2f1da5d69e72ee9556502b11827d27a2388f2a9b8d4a2db5": 1,
        "15c24f67fd7572f56973bee45b4c2dccf5714397f728c5703ddb16c28bbcc55c": 2,
        "175582e3f9aa3d429b1ebc68f9f3bb60e4fc9660af49851786cce185f2a94b48": 3,
        "21eff03324f9fad66e90f9416de2139ff1dc711bef737e353fcb3c9af026bb3c": 1,
        "28a8850c2439db41bfb160f3eaf9bc49be8f5bdb210c53e451e70425e220d661": 1,
        "37c7679d4b318f09fcd9fb1319536fef2543308efdf61d952b5e9af042fb42e3": 1,
        "3a841ad893bc7ab25501a80522fa64f5cd524bdf17fe1f3d4461cc443f9d2842": 1,
        "44db0f647052de174e519abbe28c9a2f86c50ef18e99c1978b046735ebe8e0b7": 2,
        "48af430f240a40452fec27315ce7e4f2f1fd1ba1b965a9ef56f2de0afe4d2bb0": 3,
        "61aea8dde8f50302c71ebc762843a05d6451a3618ee237deee1cf4efb2aab06c": 1,
        "8f0a424357787a04203a7fd429303d3196c018ba03b56b78aed3cb9da5c7e43a": 2,
        "920e8807213aa3004fcedca6aae3a5200760bd9d860c5d9df99cd108a8ab2fe6": 2,
        "bf442bcc6af42b0418e6a86fd6d608149ef2735fe4674642fde5a4112a267b6e": 1,
        "c6eca52d198dfc3c1ed95cac54482901d85f4420257d1d1ed058d6041dba8cff": 1,
        "c943a7e9e057533ebcc42e47ac671b9d032e6e4dfd3a161332383293c1cb7f46": 1,
        "e011eeddf55634dc2af083b0a818a88aeb089d949c13c8c646ee558563790825": 1,
        "e56689a899d62566e76e6a2665479c7be8b8c81f6d3a4490c28d4f57851a016d": 1,
        "ed630b7e529a488a1042373063a9ab8be6b2accbc85e57b7af020b99745669ea": 1,
        "f467334ed59ff45b2aaf76d7a30850ec39a842969721c5582292c0a1ec05a3b5": 1,
    },
    "247d30e091213346d42fc7d93467a962fa9f5f7672208e3b34d9f30eacd591d7": {
        "29ea44d6cf1411c8630bd759768547f546f6855d1ac99ef746d37840387a8f42": 1,
        "7e0e84547d9d47b85fadbee41abae852b864d5deac7ae8ea78704d76626d019c": 1,
        "f4715231d8578968bab109eaf54bba3585d1b087551cb1afb29ea1af20299928": 1,
        "f84fe7c38cbc247d40c5c1774f054fe6f32069328d8f9bd8bebd76d66b976a85": 1,
    },
    "27a738e130f2f267232766dd944827fcd2e880450050ada00d8448b8240b6f80": {
        "3df22261d512de77cb990e1ace42e21d66368142a7f2c96d17b128319d085f08": 1,
        "72ab433c4258dfc45508518b2b2b328e8ba4f2015b4a6904b9efc544871f05b1": 2,
        "c324f36ab4c4981aed8209a81fa35e01fbef47ed465b9b84051d7ba8ae2186f6": 1,
        "dc285f3960a51b8d44bbae5c84ec93db153795d31db8e36632d9d92627cc0946": 1,
    },
    "2d25a5fa6a252cf735a1e65d348bde05d7e3118cf9c3bf0671aadde5295591b5": {
        "076a6441488c055aeeac7f405025117983e3abc2c609f88323870e7ee71aa68a": 1,
        "319079a8e8d4a35dea092de29866b4d6c31d814a417dc5a620b3e3783dc1bfa0": 2,
        "e49376fcaa2b497c79398f61488b8a0bba18148197c417e02a719297d140f1e7": 1,
    },
    "2dcd0e15a1ff982c2aad268374e16c68d1bf0290b4981eab6de043b0b5f85874": {
        "01222e7f082b86f46ad8e46928ef5799cf729aafcb9473972260a8eec21557d9": 2,
        "03f34c97aa0accabce5ca2efacc8327f6d509557ce95953148db52d9396c082b": 1,
        "043607917aeb78beafbbbeaddf78630906c6daf8703f4a63d4ebd90ab3caeab9": 2,
        "0443e3e6bf0ec02db68ce1939a2211a8e617c12eaa6ee72f138aad23afc17602": 1,
        "05cad0d6afeedab66709e67224bda0486ff1c2c45013b51acbcb07f93b10bda6": 1,
        "094029d89591cd8e7c210945d77c6325680a64a2b6b26c59712784e78cc3d3f5": 1,
        "0a90dc621c50299bc8202cb7ed77449da78723995ce78832b17e22f90423a970": 2,
        "0befc6f366774915cde949765142315855b9260c7e891bee2b86cbcde3f94e38": 2,
        "0c15f189c70bee4afd9f4aa86dba25dbad6cab75ddfb8de252db0eb709d2dd9e": 1,
        "11c67059f43ed8d3584815341dcf84fa7cbabebdf97bb56fe73c6a796dee7bc3": 1,
        "13616dff791c044d19ddbd0f4bb27b45e6069665c323cd8f82a90773c02f1895": 2,
        "16233467af44a6556553ece7bfd5eea3141aeefff6054daf61e463b9619889f7": 1,
        "16f2d98e280fd7b0a8503e2081224d1761cc152f6e7c71eb5cc53f4520667399": 1,
        "18449e5430f021ce78f2963750d1490f85abfffbc454f6ee4e4e06be2e55c990": 1,
        "203c927173e16d653044adc53c2acbbce70609c1e472f73a21d0b7bc12ec331d": 2,
        "20f3b1958531e7806104be0e90fd7b57a7864ae97b1edeba640f05dfe8486b15": 2,
        "21c4e3e2e855529acd6f2dc303b6045104576b256f6c1ec8a656309a224132af": 2,
        "22d22799c66078d61aebdffffbe0e0fc1644bdb31885eb23f24ce74574750496": 2,
        "24210e423fc262252a1b90f035dd701a230162a43c9110bb8fa316b9a380d05e": 1,
        "2510b3438e1c63b994e6457e00cfee0f27f7e5bc005eff52a0fa05c8512d0cc1": 1,
        "262887af81f0bb05d9aee46602c105fe30f8c174587ff72cac190cc855ddc522": 1,
        "26ed8babef34d2f24924bc05d7c815049672ad9a561a4860fee46fb9c62ef049": 2,
        "29675faaa5bc89cf51539c895b37044ea578080b8aeb9a5826c9affccfbd6cc0": 1,
        "29afb1c823bbdb821aa38c3ded7df97b0ebc451f5f0016c35c070c83bceec611": 1,
        "2a0eba4dd76a007d88a0392b24b47b22d3e286776d5aeeb81e842e187ecc8ae2": 1,
        "2b46ccd23e4f50f001b9df66d21a1cb32a2981c83f1d107cd7ca114bc0e73ff0": 2,
        "2def2621a2a9c8b8a64e01f0a5b831c080346698ea012b6e0fb209391192ef5f": 1,
        "2eefa5961c8d9e75b8bc41985e7f30ca79f270c52c394bce3c83e82838d46087": 1,
        "3230ef451a5aef8f124c93c2502fd7c02a10ba7a706009ca8049f0c433db6342": 1,
        "3322c56c9ced8bb17550b253f4ffa654332bf0e88110a67df2c413e3d8806009": 1,
        "33734efa71c475e3505e82871d4eada1a59980157b1a781fcadce368cd8a71ec": 1,
        "34367fcb3b389f94752d5ef8c1a6f567dd63693206329fd795b5ca3a10ee7985": 1,
        "35a4c1efec1232a3a7d9dee1c21aea6672d0606e744f4af5cb134356b33c7c3a": 2,
        "36b3994b22f3b0429e9b7397bb86f380d6888c94649dfbb56271196385783d34": 2,
        "3a25ef2dbc700e5070fd7d6940baddc7059fcfc3a233828ad76611ccd5784b0b": 1,
        "3e8c0c95054e5a1fe76943b7f9b7e0db328c7c778bdaf336bb3580bbacae56ae": 2,
        "3fd49f4dbeb9c944607af65df27d86481905121146b37da1ba0ad9b9ca8668a6": 2,
        "4142443f280b8c911d3e8699c76f2fc2bb4ba7acc46f84fea78527abcddd6b2b": 2,
        "4179f6ba59f6c7d288e97a4035dd4cd461963d50704b55dd92376ebe36b9970e": 1,
        "43b70c668c6bddc763e5840c987e65ee529f18561cb5e7670d42747e0f5fc8b9": 1,
        "46e8c5ba68d5ecc531e6895bfa843f75992100a2cebd19060ccafe006c20b163": 1,
        "4811fd48c39ba57b397e87a5f1122beba7e8b6d6201907103503b51488f702ce": 2,
        "48f40307755e2ec77ee6939072db593260898317f702ab2d7e8e8fb914e92a07": 2,
        "4fcfa899ac2ed82c121c340d8c968aeec4fd0deed481652be9d807afd3cb5b75": 2,
        "567a9be7debd464386d3ef3b0c8cc20931d506e805fe12b657168524be1a0360": 1,
        "56d13bcdda4273fff609d099bd4b7f718941ac78ee3d711cc43fe055c2167411": 2,
        "57033901b3ef66bae7b308df4d0b5bd033fc49a9ec7d2c85b4a7d5ea4c4f8a3f": 2,
        "5763b0f8ff6a1cc8ca144cadf24990b073a99c41a7300fa1614a4f28e1fe0bd8": 1,
        "58028c87e53b7731e572e55ec6b3fdc8a36493af930639ea80b959dea5cbe298": 1,
        "58a72d50747d89ddd7555b03be69cf2e46fe0dfafdbab964ce0bf50ca7edac4c": 2,
        "58d84dad49d1e8adbd1b84f93f92ed43da9923e7f55c11a32781e19a072341c4": 2,
        "5bc42e42f88c737442d622fb20ee12128d73117d2a3b24751d5e3846b8224e09": 2,
        "5fb533780ac09bd7da27cd92d3556d041a8193aa6ca1c76b9422fd34610e82e9": 2,
        "60f0e5f73f9237f45d7c97fe00849704f42d0f45774d3cb015b7cc3024b1a29c": 1,
        "684f4f967dc0df64708cf2204ae0638e5d5ac2c3251c0b317baa6285b820a0ed": 1,
        "6b62ecf818227a2ee2bbf2f9604fbb5da4de90d1aaeaa34b13412507ecfb807c": 2,
        "6d934a4a823b4b122d000fd37325f9eb7e9a809e20796003ec92e98d3147c43d": 1,
        "70fe6ddb3b786af3829e168c58081e8f66bc2aba7945a5689a48b6e356674558": 1,
        "732dd9f29d8fea165d0aa6360a1e6764674cb73c961b996c2b2c5024f1000ed3": 2,
        "76e374c586d5731d693e712fc97a03a58e8d9a70690af7fd2be252a92a16605f": 1,
        "7dd3950b098587a6f7f74210ac5270133f3102d3bd21be268a2648aa7d9d4fe9": 1,
        "7e03d6845e7fcb303bb84465285503deb9b636e592f64980f64086860b822564": 2,
        "83a820d42a9ed4ebc71586ce4d3a98e4e4e2cbce0125019a4aeb3529ede80c78": 1,
        "8467b865cf5a6db64573c8507315ddaefa00f4f63f623e078930c18ea83225da": 2,
        "84744ad8dbe23e3eeb7c75f65c932bea76fc82f7623a4bf985ced9a0016811c0": 1,
        "888c93b28a15b4b5b4907afab8a1dcf9aa3aec3be8c1c73487a499dc79805315": 1,
        "89f2055c4147b47f70c7c664df2370d30d930609bfaa8e61220983da353d2e64": 1,
        "8b7aec2a0c6bc872211e7e59a068cc8a0a4cbe91ac6d4eb924be49c95d395ffc": 1,
        "8ebcc4b4050af4c7b3dfcab411a8801f1248e6f9371b0183180caf771a8dab62": 1,
        "8f18e29b02369e50a9bdd8ef9ed215d36bcb88453e365d9c28cbc0033eb17c6d": 1,
        "8f2cb2db2cc52ca77d9e2a690e0065cfe656f4728f7f8231824a57342f3df80b": 1,
        "90c6983f3364e03611bc7cf5163e83cda84f5cf7681f75a89a9bb125f69b8d32": 1,
        "92082bf856342b919bd3af706b5b6d96807455d1df5efd39ecae6f48f05cbaea": 2,
        "92139407a805ba8d83fe5cdd03a0810db63854b624911830827d1cd2cadde538": 1,
        "9314d9ba7078230cf13999aff4a84a215ed5371ddb82673ae109196699f7f721": 2,
        "98463c2de3131e76f6e096ba5165bdfa94d502c127db2e45a641ec50ce8fcbcb": 2,
        "9af47f4fac5fc33ea32db68c025c822a5a2184f8c4565eda3f0c03ecb3fe5416": 1,
        "9d8244fab9675e9ad3330d3c87ebe23d1117b580e02397149b163b84b1c209fe": 1,
        "9e1bfffe6ba7e4f6a4df8c4ee56b9ee820362de09085ba0835439a2bbf0bd360": 2,
        "9ef1bb5ed6e16792193c8daaeab94c4bafd98e338b3b962fb3d03e819d065ece": 1,
        "a6c0180e3e0a17fee3156088400480f8a7170da0becebd8f377affb37ed07245": 1,
        "a70a635107f8380d0af47271f1081c36f67535e117817f3c897d27069f8f0e9b": 2,
        "aa3b43a3c1b2e762458f4f9b618fe1177f0d72569c21ff5853ab6062bc9a6ce9": 2,
        "b046cccd09fbb778fbcc19239447602d4ab8bd43b2d4dcb1f73eb2d21cadba9c": 1,
        "b110de964aab4ae2d5270b353435892465204e9d791b0f02b1ad2a6c26191885": 2,
        "b310b5e366aa14ede57ae2bd6aca1a1a074aa43c282b6f82c208f8b4f5795ff1": 2,
        "b4f3fcdeeee1ca7383434f74c4e989c92c644e837efcc873d655b8bcbf921d38": 1,
        "b677c4325c003501ff2feb0ed4a670dbf4905e502f88e378ef9cbde23eb850cc": 2,
        "c5524155611b6355a308e20f6358688e780f1606a4a583cd26380d2594347fc1": 1,
        "c5cb6a3f4db1baad3e02c45ac09b31aa44c92156f186ae4e8d17c1be25ac10a3": 1,
        "c6fba7c68b909ef80d4219510be11cfc8c3bf49805e9bcdb7a5495ad0cff43d6": 1,
        "c78f690da335cdce669de6392b606346d6a8f6f7d7e3c7720594d1736ef41d6b": 1,
        "c83b8b660dbbccea1f18c5a36db9ca01b1a4bdb36a2cac044c96a01ca5931d68": 1,
        "cc11fa311fb21efa4aeb8a5e9157cb9a91d4a88dfdf14c2683e795d7e6196c20": 2,
        "d1470d20bf64bae511154465150d73c6935e5ba36ce473ccd9299ddf30bd5e18": 1,
        "d2da06c2c93fb0fc9f25088d01d8d9e7f7f90f42ddd5122b08278768dead2778": 2,
        "d52ac6476756a1a6371a948115d1a536fbd252fcebb20db47ef7c0b94106ed76": 1,
        "d5b17869fefb7e42cf11c5a6b118edec3656cdf6c41cb8fba4b3f58fced2cf24": 1,
        "d779ae912e9c28ffa79710d2fa6fc64a4321985b91fbf3c0f6644afe9bbc04f4": 2,
        "d850589a90970b2a19b57175b4e69280582cb70182a2082dd21ef634d2130f11": 1,
        "d8e8a93ff7fae19b7d49fee0776eb3c1fb01312f420f27ffe423c53f0f705b60": 2,
        "db557e036c82a1e570fbaaa848bc59e6c5e1cc2caa0d316c81e19a828bd36194": 1,
        "def91b86f8540adcf641b693d727bc2fa03735d5d7c9318870106a7ec100b602": 2,
        "e444d9542b119a31a5315e8171aaded8104994eabb7c17417a1733269589c78a": 1,
        "e45326962db94feffabe57b841dc6a1cf8d54c9d0dd673877b77f5ab8c309738": 2,
        "e9e2445209b5d1dd884697259d5f35c4d0cbb2841a3847396b50cf150e46b689": 2,
        "eb6b4d7be224caab7c08e5f6482951b023b69ae998075b82af08c7b5e55732b5": 1,
        "edab1b9ade95da3566f4852200649a6133513015d40d1ddca3c7eec8d7c1b8aa": 1,
        "f02d3aeefcf2ccdd654184a4d8cd1a0d92be86ac184e243454b0f77e224370b4": 2,
        "f4585d786e612b2532cf4d2cf0bf480c335a6aa5c04061064b361cf36743ca92": 1,
        "f8d90d0f821a2f3aeaedb6db6cee3b0eb176358dee0585f228c6fbe6445df31c": 2,
        "f9009828e2fdc4b428066227877f1b55dedc2e39d016150e5c54cc809a95d62b": 2,
        "fa651530296c97a065b8fdead4ecceaec89f1c9dab902b801a57576576322591": 1,
    },
    "36d79aabc7e233cb2bfff33359f1e6ac1cf08d2bad9823c7c3ac5f65d9189821": {
        "84359192cb9577b453bc340fc6f2670866b6333b93f480607e8c0f579116d5ca": 1,
        "ac47ad2c41b66df5a98628b42fd2120cb6b1948761ad0a5c81c0fe5e257e141a": 1,
    },
    "384aa0f3c3fa3d96183aab4ad09e258d46b74184a1e53c698ad105705951000e": {
        "175e01a0d67de4756847f99fb9fb163b7c8276e48b5970b5c7e6a86d844a1944": 2,
        "eb2177193cb31bdc27b19eeb66da51aba0c990bfe12612a407a3bcd413658503": 2,
    },
    "428e3fa081ee2a407e7516e6bc0357d4c01809ba241f1df76a563076d478b2c9": {
        "130cb50af13c2bcd37da6c3648154b343cfb31fd48a1a048077dda7cd8b9bacf": 1,
        "33d02b3123164ecfc0e080d2e1bf0026cc03aba57d54df55f4fd2f0467ce06af": 1,
    },
    "448ee9e3ad0e33e0e635d4a88583bacc8a0649cb6a3b8e0a82008b108d9eb746": {
        "0ef105f77d24a433ad3a6173c8d016a4f80f9278b1d0e2aa8924f21c10301709": 1,
        "2d2fc4f515436bfeeea1d4b8168f11d193e43a6d0ddaaa114f2d3cfd46679362": 1,
        "2f8e6598d415050762cdfbf4cd3bc60020d0554f9dca1cabb33a497eada5bd23": 2,
        "5c9718e82d38c9bbba2b3e24ae668b856ead1fdc4767acab6010cb77e6e51e30": 2,
        "6f7af94bd56a4141b22e14d664530f0a492d8eb78cd098da05f0f57423c7870e": 1,
        "8e415a202b0b26fe1ddddf7d5e78f540ee8dc8a5f508919e0c6ea3b20597f214": 1,
        "9ca84dfbdee414e4a18e310c1b48a50cc2448f4c4308d095e30860c948830cc0": 3,
        "a0ee1d35adabb8f742138cd533a290d1e750621c061839e5b64c0e3ec945ab98": 1,
        "abf569a8f75e8319b26e2a7103d6f781f2f6b28d9de6ffc9db1e00015a306d0a": 1,
        "b765823a8135a4b6742c3ce9d6482c2423abc2edf180c892bc90081cb9aa254d": 1,
        "c4ccb47657334ded9c7c0c9dd66197e71ce036465d4c050b8e60c71ca421220f": 3,
        "c714d8ae6a23bb8344aae8b90c5e7c13a3df48cd0d2215e56230140e18b42824": 1,
        "f7d8cb60c1b714971de98a196b5b5dd1122279814e6a76ce5706e51bb768f04b": 1,
    },
    "4ca21ca72a8759be3a0fc58023322511efbcfe5ece4afbd4fcdd2f84efcbfac1": {
        "cb1944b3a190e700c3b9d70084bd8d51052342e240cf867a323d68c34d3d4946": 1,
    },
    "68f838c9a6c910b359a31d0db5c0100a689066406397a03d2a33823ef1a58210": {
        "61971ed5bc5bcd25e4a4215f95d66978cb61972bb260dee6f191c209c2e148df": 2,
    },
    "7d128cb8fb3d29d456bebb86adcc9944ec00909a9c4164cf54c75fbc8909a521": {
        "30ba12ad7fd0d883e0115b467ae14fd4c84158abbe2dc9999d371a4d69cf9dd4": 1,
    },
    "84a67b619d09a4c7c1b7c87d0f83d3f40350be0131a8db355676a7fad09b116d": {
        "119f2af4e1ba2a4140153f0dfbc73f40cee26c3a17c1eb9e6192584c82cbbf15": 1,
        "1eaf203b5549431507faf470eef74e0eda58a95a6cfee453c16d964a7d2f8d08": 1,
        "4f157724a7d36ec685382db3858ed7acf878fa0e1241f44768612a7831d99aff": 2,
        "56de1f6a73f030b3f08f057c87a85273ad1fd1edc76b8a49bd21b45a2d268d0b": 1,
        "6c6a03ee394045475b3bc73d29e111edf8329ab10f04493695d75b4ec0e1a58c": 2,
        "77310a8e3bf1a24b043a2a69f5cca0c377a1c81d2e8047d2f219b2f28fe65ece": 1,
        "8e9fba1153f4941ef096063c844f4b8344ecfcbf56c017f25e83c73b59a8c830": 1,
        "8f7e4c4f643c3ab5fbd1bf7c3ef0e5cf9a2fcc5ec44858ca732725337d46a7aa": 2,
        "918606a54a8757420776b87e8c65fa3ff98134ab44082ff586c4b8a7f3508339": 1,
        "a176720bce93e086a6e35ee630602f8fc8fd54fc1f81738990e21097ef2d7364": 2,
        "d53b12a032d6320fdd3325095ac7d7b6666497b3e01aff16856852716d8ee103": 2,
        "df503de92e1755abe0fb999f235c72016fa7f833d643afe44d98d5ca2488148b": 3,
        "fcbcc09506eecda806df87fa8af357188bce3ee848ab9658c11d9e431da88dff": 1,
    },
    "880066fe18fc0c44b8c0439d1ecb9285221e18f53b2b3a0bff3f831ebd067b32": {
        "3a774b6ead47e958c8a9442402e2a5ca1ebcc0b38f9d18bf561396f0466ddb72": 3,
        "6974adeb1d5c206223ee3c95b6f6fdb5eaf75b3997cc4adcaebba8f073e5a2b1": 11,
        "881f6f54575573a08fa19788e2546a56708669465ad6ce6f03aaa18029cdccb0": 2,
        "c30b50c65a417730de1cfc772175d544d60e6e04d4f68e9ca85249e2432a63c0": 1,
        "dcad5d8a792ac145ff75537c796a31d57944aade357abf6787f5c353d3ff5104": 3,
        "e1aeb47f6b969d4b0083c46d0a1cd3277ee2d64e95c2209c284ebbd1b8800335": 13,
    },
    "89f6aeb1c7c305c9ed233d1d36e35e03117136a4cd33c644cfe2cc16e73fc850": {
        "64427f468cad347d1583a5a35ed4b075081c0508727580fc4b977724ee1ab485": 2,
    },
    "d07a3e57ba911194ca5c448de2168ed0202752d625749b29c8ee83c707f68ce3": {
        "244ee56c03b4566577d80c5a238551fcc8588d1596a64d5d715dfde2e2e91523": 1,
        "3a6f95abb9aecfaba6d8424715dcf81dfa98fe2450987352d34fc1627789f99f": 1,
        "8887c928084b63b093150eecdf06c1e5fd262905c4d5d5e2a84a71a4839b9e34": 1,
    },
    "d73e7b1a3e023a8114261a96eed03a6efcd608487b557503136762f85526152b": {
        "4111650155c10b6f9a9a0ceafbd9641577a5aedf4ed7bf31740d77776fff5fc3": 3,
        "462576a253234dad1b095c67ade9d4555e241bf06d63dbd5dcef98f7b6abaab2": 1,
        "5978319394b8359e443ee656b831b57ab7ae0404686972af1aa4b388b31d5bd1": 1,
        "a0064ce92ab11dc26df1243a03634d3d9e26023e709f3d07205032db6bfd2248": 1,
    },
    "e95fc0407ba0bf3e6d17f5080be1e8d290590c12496012c95ca86759ad7ea6ca": {
        "def6da1a07ed48f89541c2ebd5911eac38a1e7e73c0f68c85ffb1850dd40dcc0": 1,
    },
    "eb1be37e716a03011b3c0905d2f5c9158b75b037d5c82de4856090f434122f36": {
        "92586c041687fc61226e6bfaf3990b23d38a46928b14f945ecb09885a03e3dec": 2,
        "947e5c56c91dc37129262b68585558a22efcf02fa0b69a814a951524205630eb": 1,
        "be30771451ab951cafcf818842fb80f0a115cc56288d4b6cb18d2cd92f2732be": 1,
    },
    "f079bfdf6b47444ea00b1b511af924def022bbb36e76c00dc84ef9585b8e8183": {
        "004b73ea0feba66cdc804e10b521b0a6284789f8bbd0b63e10a9049b7cd5dc82": 17,
        "ee4daa1165074162bc9a58c4b0825f5939e44bafab9b33fe87d52996f28c95dd": 1,
    },
    "f7021da024892f4af1c263e226c500a49764150d7406d449768c507a5fa131e5": {
        "19a10fec92cef4281ca99714be02e17a44596d37438f4688b4990118f6f2e6ba": 7,
        "42d7b6973dd0fba0a561cfbf15693b54cc6b9fbc51dd477010226c67dbc1f6a2": 1,
        "8690d9618344e2eb40626faacf8bd17158552d533da593158e8996c8442f4ba3": 1,
        "9e8f6912ba2646c4a70e3ec524382c240df3b011784e11e14679b8e7d9a54ef7": 2,
        "cc99abe6c76d18a7c00486908773be6cf897c55bee3fd50130236137700b5eef": 7,
    },
    "fbbf580ef93fbec786b704025eb79b3786506ce9680493045f563646fb6b4790": {
        "043c93f7b3c371c12f7a3cebbf18c5bd3fc11bfaf14638f3ef37cfe22bf98975": 1,
        "c1ef510919005589865e41fe54fd27650dd24881d4f8aa7e416019f5bc351cb8": 1,
        "fe908d31383f47f5bd69568b0c93a31337d4b41511d69e8458eac6ed7f003ed8": 1,
    },
}
# <<< generated private repository name baseline <<<
# (path, name) -> lines, while a tree is read for the baseline (None: every line naming one is a finding)
REPO_NAME_COUNTS: dict[tuple[str, str], int] | None = None


def repo_name_key(repo: str, path: str, name: str) -> str:
    return hashlib.sha256(f"{repo.lower()}\0{path}\0{name.lower()}".encode("utf-8")).hexdigest()


def repo_name_counts(repo: str) -> dict[str, int]:
    """the counts of the tree just read, keyed as the baseline is"""
    return {repo_name_key(repo, path, name): n for (path, name), n in sorted((REPO_NAME_COUNTS or {}).items())}


def baseline_findings(repo: str) -> list[str]:
    """the tree's private repository names against its baseline entry (no name is printed)"""
    base = PRIVATE_REPO_NAME_BASELINE.get(hashlib.sha256(repo.lower().encode("utf-8")).hexdigest(), {})
    out, seen = [], set()
    for (path, name), n in sorted((REPO_NAME_COUNTS or {}).items()):
        key = repo_name_key(repo, path, name)
        seen.add(key)
        want = base.get(key, 0)
        if n > want:
            out.append(f"{path}: private repository name ({n} lines, the baseline allows {want})")
        elif n < want:
            out.append(f"{path}: private repository name baseline is stale ({n} lines, the baseline says {want}: lower it)")
    gone = [k for k in base if k not in seen]
    if gone:
        out.append(f"private repository name baseline: {len(gone)} entries name lines the tree no longer has "
                   "(lower the baseline)")
    return out


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
# exemption is in front of the reviewer.
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


def worker_technical(path: str | None, line: str) -> bool:
    if path is None:
        return False
    base = path.rsplit("/", 1)[-1]
    extra = CONFIG["technical-paths"]
    if path.startswith(WORKER_DIRS + tuple(p for p in extra if p.endswith("/"))) \
            or base in WORKER_FILES or path in [p for p in extra if not p.endswith("/")]:
        return True
    return base == "Cargo.lock" and bool(CARGO_LOCK_WORKER_RE.match(line))


# host names of private machines: SHA-256 of the lower-case name (a whole token, hyphens kept)
HOST_NAME_HASHES: set[str] = {
    "24efa8189fd0d344dada7815521623595f13469fd03709a9919cf961f963fb24",
}
# a release string ("R36.4.3") is a device detail on a line that names a device or next to one
RELEASE_RE = re.compile(r"(?<![A-Za-z0-9.])R\d{2}\.\d+(?:\.\d+)?(?![0-9.])")
DEVICE_RE = dict(FORBIDDEN)["device"]


def host_names(line: str) -> bool:
    return any(hashlib.sha256(t.lower().encode("utf-8")).hexdigest() in HOST_NAME_HASHES
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
                    hashlib.sha256(low.encode("utf-8")).hexdigest() in PRIVATE_ALICE_REPO_HASHES:
                found.append(low)
    return found


def private_paths(line: str) -> bool:
    return any(hashlib.sha256(m.group(1).lower().encode("utf-8")).hexdigest() in PRIVATE_REPO_HASHES
               for m in HOME_PATH_RE.finditer(line))


NOTE_NAME_RE = re.compile(r"(?<![A-Za-z0-9_])(?:feedback|discipline|success|handoff)_[a-z0-9_]{4,}")
# File names of private notes that the patterns above do not cover (a hyphen or an underscore in each),
# held only as the SHA-256 of the lower-case name: found as a whole token, with or without `.md`, in prose
# and in a code file's comments and strings (note_names)
NOTE_BASENAME_HASHES: frozenset[str] = frozenset({
    "03ed73eaec019fad68ca8b564b3c05006cd4bb93075f5535180f8fbd88f140a9",
    "04357b34faa5d8c1d3972be6e8d26495d5f78bbd8074643a4f04b8daee9e4f10",
    "06cff9b78f0793cbf273e7b76d1e6beedfb4c34a698cdfe097701e904379aeff",
    "08b93e9dc7d462dbc137b971dc2eb7cc1d58f3760ab14c2cfc27a49b6e205cf1",
    "094dae1b1a96b5af8be30ba3c2d006368dfa6f7771c506076286b2fa93d0eb84",
    "09996bbee6ccaf5bc2779e1d5a410245dc203b39bd2ed0a7757beeded6551201",
    "0ba8e9e0666f20ef5cbbc3f991662bf7d43e83ef4b7d36f5ff77e2db4fdc92f3",
    "0c4bd2b9bc7bd6be0e06a0f24675ce3b1ee822d7926fb89e51b8c6d37015ba96",
    "0d8fecf81f60dfda9210125dd6468c05d399124310c3e9f0050b86c901b97d0a",
    "15dddbf79671b3178b730110824545c4589f2435417c0bab90632247607a5c1b",
    "168c684cf749f09c07bb8b992f8e05dceb835cc65e1923f85d8ae3b3d5e8098f",
    "180d4bac396e3fedce4715fae0c23dc165a2765eafca37a8acab1172f3ad05d6",
    "1bacedc39f69ad4d885a54b2cd123f609d456fd481d61eeb59dc4ea3e21e99b1",
    "1c374f40bd3a33f8d25ed869f0065e4d740631393586927382f9313c1b4e6baf",
    "1d4c90f96acde6cc7813ca9a561b62c192779a7ad15f65f33469530c7f854762",
    "1d60eafc224db59d27915a75932c07080e3cef22c8a21476b94153acf947a5a1",
    "1ec102d901ca4fe6fd54c726fa72a8f039ea9047abea4df8194b4d946aceaee2",
    "1f0b932e9acb7c4066f5b5d7e50695bca987207d1fcadc865e723f1b42d1d06a",
    "1fe439238c4af003dab2708093520536207a680ac79890e31d6e874aba49ff08",
    "2414322e6e14696be95d7406145b1774a464b7340dab99de878f9fbe5e5070f4",
    "24f4f1e59e52f34743cf393d9aae54aa817a57d4ccefe51a3ac68038889ab5eb",
    "276954481b3c2d0f4d27b69849409c853f6f192cbc1e0b3b23c0581d73af8bd8",
    "293a458dc1116349c7bb066c4769aea6cce78f76f5860928ceeb619637e702f1",
    "29afbc4e02f2fa9ec272cbfacf1c6c4da4036f3034375383f3a4c96716c656b4",
    "29cf0235b4fc2ba68c5d1419041504ef2133964bddfe8adab12ace3e6d5c97f2",
    "2a21909babc4c32fd464a7156516dee008669892e22c00f2b47fd73b9c372080",
    "2a927c40032e158d389c0e9a68f6ec77018e19896ec669b7a32870593690ea24",
    "2af5ff09f1b1d793bf5f82fcfa95e2c2f7d1436c7bb5e9d219b1a85df577559c",
    "2bf141fab9490562ada8c28ae53f1897bdd7df799e71fa411aa95623aac22662",
    "2e1ba39d42b27af4c48420f7e22b476631b965b7bde5291af5f347993589ee3a",
    "377b26c47973045c561d975540ff405f8d82f5b2ffdf18cf7dcb139930cd2b70",
    "389059c2042363cb59e538f7b56414f5621d08e22cfe41d56494cc09ff92ca1c",
    "38c0576f44f0f4042aa2da16e1d33cac7f5a9e7fbbeac5ce4b79eb53ef28672c",
    "3b80c168e5e5d1675431c206e191240fc0cfcea876216b67f657ac55afdc397b",
    "3b8ab684dac84af658fa59c940fef37f0ff3ac2efcef26221375e8f780083d44",
    "40828934c9e644d8784e1ff979a5f89b86089ba8418660c2cd7775423fe89908",
    "41ca51947144d40b13d770befbe6f50874b626fafc0dcf7d7219e26d4d17b90f",
    "4267ede79df1788824dada44ee6fde3480aa8f76ab0e438b41efdb21140e1157",
    "452af9b5a4b97429917ea789f3ced0fef21ccdc60547f1c2fc96cc7c751100f2",
    "461cd6e58583f95367e6f427fddd4d0d27c55059f23e874403e5d6252fa82a9d",
    "4635eda35cf9f9203d29e8f9b5f897ca05a8cfaba1a638d44dba66bc221c1455",
    "48a270ee3909557c3fa41d39323556117e280809994c96fc776c8d887abc0cf0",
    "48b31ddce05ad8fba91703a472ac6877be4090c92e6f2e3a16b3170848b361c7",
    "4a029230bb577ab5609c8943a67a814512e012f75626f7396cb5672ec5188967",
    "4a6d58ca4f37617d5ba4b7dd88963b72b440e23710e07953ef476c5e24575131",
    "4b56318e7335d77e611ba0136bc21350a6fb9de0bda64083f006dd56c2d90df2",
    "4be99a709e0d5e9b5d4d42d7623f6542bcab4216a5750671fa550a6cac6ab1eb",
    "4bf5cfea4a195a0050bdb2ff7027f716eaad39a3a27f9afc01f9edfc42551a6c",
    "4f0025d357419ee68db2e1c5ff574d0e8630f02b5379ccf35cf5ca35940abdcb",
    "4f9ac51c5c0fa235b59ed0b42e8ba7b57af67c46922e6cf541c77ffb065517d9",
    "50043e5aa13f5a31c0db2c55787ea09406eea8fb8a66598c0b3b567d34cdb621",
    "5041b7889197065bdbff16b792620fe062abc82b7ccae8a96d49c368511fc16b",
    "537c32d27734187175c6ba95e13a4b61885cef153c2ea6c11eeb4722b1961070",
    "566e2a618fae0f696d5428b83e7bc8e59d520a2e8e5954618a5405f07f334465",
    "59d8813bca1157c957d6d6ad04f211201cf033884ff89c1764992ee67a9e980b",
    "5a170c63099f1eeb9378e4e030c609968061a5c7a839741459f87ca662ffe15e",
    "5df7b3dbffe033841a535ef281afb4871a457b590bb57d88f71e77e87d3c8941",
    "61b5b0af7ffc27f1e3daa6b219459a6cc5c486da1186ad6145c90d06a1a4a2eb",
    "61f51d6e6d1b77f62e008618de5bddbafe9c3a99152db2006affea9451843ad4",
    "6270a2e5b8893d8d6f840b3e68555892463af87b2b9b2730ce05bd677f8a7801",
    "64d703fe6772d61439406e15c74c43b4fa5d77ae83300af5b0c0d3af047bab17",
    "68b44c5885a89938702f222440aa6bf151ce54518a99d284657a4131c9d14b61",
    "69756c7002da4f691d7889dbadc4a8ddf4235da82ad4b676f69e2e4d24a39c73",
    "6a92ca1773ef0045c0b18cb8325edfc4c5a0ebb9a3b9338d68f74cb4b369e9be",
    "6a9fb1da34e23771ecfa595ffc8139fcdf6040bb7b18ff59c067f677b5f2df3f",
    "6aa66349e7335891f5d6ccd5fdaa6d870fe4c249d28d3a5d20eeb3f0d5785122",
    "6b078454dbf234b9c1f208d4c12a99e6ada4cd6162fd58dffad69e2cfdf17e9b",
    "6c525c42682cdab672c7c1ca9e067bb3e24a412342067ee45065e45a6d43f05f",
    "6c63e52b04de3f74c5a43ff19eb34fcc778ea4412456158c2cc22daf28e0dbcd",
    "6f91772e1271b17907704ffa896c37b1036d4696a4ce46b9eb881a083672d8db",
    "70996b2d55766f8ded3114d2d7cb93eea6dec8cc25fe9619198df89fd643056a",
    "72827cd9c97c6eb115d00147c896cc9f43a2f95c9f5d6198eabbf8d7e35b837a",
    "736180092843ae775bc43672ddef2dfa92d9564c08d6ffde905a61ab1ae65306",
    "740be14e8ad1ba535bb5c6cf4c785d4af2bdd694e5a88951fa7cfae7c91c4bf9",
    "74f7d668c9d2104cfdeca13d8d4a14dccc7161afc3dcb14073321922ce42d0c4",
    "75aff2c780fb29777438a17a8656129ce7121bba298113e63014f3de6ad1b43f",
    "79e18c785bb80e0cb3d52c3290eece99fb756b6af28cf4d7a16bc5289a5ca810",
    "7a26620d964a29ddfbe7a502fba6d22a04e1f3dc39bf2591fef6f43c368c342b",
    "7e5b28d050e7624856dab48ec5cd60511fbfe8777bed156285be9bf9c0e3ac45",
    "7e5ebea285990399a66c7b16748b41da8388b08191ce584bcc0142a326ce7569",
    "7fa9c6a8772d4e7813ddab2726c9e3f4ab0f17e416dcf1c58e22a9a7bb2ab741",
    "81a236427efeaa1d4122572e422d0d99f4338f0e7deddeca01e7d58c8813cc7b",
    "83fc07c31b677b179d29164b69422237d1cde5f571f0fd4d05b261808e1e80cf",
    "87120c83fcb0bd408f335df32bcf3aace4ccb263c87522f4bb6d2df40e2a7c0a",
    "87b351250e035b63e4c5412884d41bbd4d4e8f55c003ffb90df9272ecd31aa83",
    "8d8923fbf606002e01e2628c2721ff7e0f4f590158f1771b6737194152e4f2c2",
    "8db9f5c2e1bdc775fc5e6b1e604e3424da61edd00531cf7ac9e596d7725bef0d",
    "8dfe5cd31b516956e2cbe3dad0d034408ceeff3de527c498a549a3ec52639a3b",
    "8e80d2771240b33054f59f4bf73f51c33ffdea336033b42b0cc359a7936249ba",
    "8f4983fd803bda8eb6c02ba974e0ba90b842d02744853018a14be1520cf42920",
    "905ee644eae2078e50da1be9a9c2d5203ade8b83bbf720f452f05ba74e32724a",
    "91414c1485172068b807c923d6c1d45065cb8446014bc0735b8c55278723e89a",
    "91707941a9a7619da1faa3e0b2654ce93c43b5523310b6091a22609f66240167",
    "917450939cb564acf3b84881c9d01d3b4e0754e0cb91f70decb93ba8ded6720b",
    "93de610af7fac3d54125ef3002baf099e0c59ce408b4ac02fbac0a3854938d68",
    "93f4938ffd54e3796b246c3fedb1578a58084f03310a884367a894c2bde97ba4",
    "957cfeb7770ecb3eaa72a582b74ff9cfd04a4d9e8536c75f63ba1b3ec014d131",
    "9731ad946d4a8aa88d81ae42462036b8ecee5893c289a592cc025e5dca63ce8d",
    "97ae3dac2f50ea2bccac0fafdd8bf92f23be7858a92edee80ddeca5e15257efb",
    "98f9d063d8f8d153584769299b0b896f580e44b4dd7c0f84e499a35b93a05a5a",
    "9a3158a1671eb5a5b4a2e68da25ce82c100b761325aacc1449542fbe188082d6",
    "9b644d340b60a5a49d360bb6e81423ab67cbfa9ebfdad1a3fdc43e891397229d",
    "9dd9e9f05fa284bc0a9d29b93edde7f37392926a83c150d5c9bd3799fdaa7051",
    "a0de051ef95cb50d82762d3764f530c686bf11a54c7ccb846c9a3a54ca38fb81",
    "a17e2c656ee2152c03e8710f639bebb8743ec396b82b16a172da261e57484977",
    "a2c02020f20e52d051340032d61260bcc3c2b6328b8c54a4e2682a68a74397a0",
    "a34c37d0f9491daa560bafd1d2bd489301f92e0b66f4d9c41bb18b30024836e4",
    "a5b50e87ccb90f3e546cc0bd84c77005eac178fae077749da9429493ba41cbd8",
    "a9e849d840c3a7b85e3fdb1a7f2da87a31b3e9953401ae4238072750dd970d78",
    "adc082c013bd46ab5d86ff4fc34a4a5eeb600943917640b210e3814a3c58a969",
    "b0526210f5f3bf6a26fbcc5cdd8a6b9e941eaef65d14de3810a113ad14d258c4",
    "b16a3b60331dad50437dbe7783da11618d87535e726b63e9dc6c346cc83661c6",
    "b2494df336e7d468842a3a8bd3a50ad349929a4cbc21184901789236b09895a0",
    "b34baa30fc1a14f703609c69e7c439156151aa30a85910015e307bb36150fae9",
    "b34d83a8ed3733580c56980339abba991163f5766c10d328b2eab8ba71332b4e",
    "b3bf2c54d32daac3abd93c5020120110e9bbac59d652c1491605e17f3302d3ae",
    "b66a906641f3ab1ba88193e8a7763614b4b170a23c6ef0ff3475d469b67854ec",
    "b8f250f8365ae1108c1b6a73b005586bf4efc42c0c212e371a2132e54aa1ac45",
    "b90e554fb10f286e75cf1f0db2f7203f753721a27a727dc96108bc1f377ff7fd",
    "ba338cf90042145efc2f8d0bb7e2a13d846aab5ed900f65a766b96ee6077e601",
    "bbf9a9d0a9492e7b9f2f5fc7a2fe0fe6494ee31ada309b1e0482d0a6978eab3e",
    "be1a3fbca0a69c5d9092c88c30baa9b54619039a0fcbd480a6ecbff6939532b0",
    "c116a4eba5b8ad2930fb96a7dfb48f96c18982c4468df5458c2d1278e4097f67",
    "cac1fec9c5647da5854459c6f32bd7bbd74ca1324a3559caf6d45973d77e6d36",
    "cbd03f4980ae4bab8933bf3dbcff472a4379dbe289b4a2a8fdeb3e0faf861a52",
    "cd45edc9787ddbfbc0961b610951abd4877495adcfef7b214571659f7ebb88f6",
    "ceec0647171691acb10e47809950cc01631d9862bd0a78fe7366e56e51e76a24",
    "d0c8deb450d066c95ae7963c8ab4d66b4c2c0e06ae77df7fddd27ad12e4ec921",
    "d37185e90a71decba67442b6e6b25b79e0b13948dc4c95e132932206474535c5",
    "d3a3d328ceed5e93504ccc0bffddc2dc91b4c01faf2229b31d2e39336ae3c04b",
    "d403361c3cc74a01ef09f4ff938dec416e3343758e6cb5eef494f00bc0e79cfa",
    "d5ac4177b92d4a567e4138ee33cf4a012d729f7b48b0705d80e6408f46f80055",
    "d85d6e9dc4eb5ffc56b68e00c5f6f05ddc8f7b11283f9dab6ff281b92b554a19",
    "d8cbd26b6836c0d5af7e2a7794325c0e9873fc70d3e8025dee63531ccff48b34",
    "d9a2fb633e00b875d3760ac04650036b66a676cfadaef30c973b842e07587413",
    "e25e1fa2b2fff22ae6431655587eaf644162e23e88f1bca79275c6b57a5045e3",
    "e292c110070995f3e40a8e830aff94f71a0c43eca5f097643b2c86531a01c4a5",
    "e36c9d0fd855922c577d273d147f0f713186851559ad127be47db205b4210cdf",
    "e50787438bfce8c0837bc349a8f59decab00cda17b775cdc4eba70767d218d47",
    "e573fe8aa34bf348063f18a3f1ff37e24a6aa3eed3b4f953baaa325643c67bc1",
    "e5a43e7a709970891cb46aea1f66c3cc819508cc24add984e3d5933fc10990cd",
    "e762954aaa749ebc3a72462376ce88b50ff181714ac761c6d94920c4d17e4fd1",
    "e83a0f2e333cbe36522225a51ae76c858be2606efe836f6ea3933434223a8c12",
    "e9023223c649fd144acdeb7cb79b2a333ffb1e6184252584de88a1118f7dab95",
    "eabc76508abe7c0d82c149a6f63b9b8054b268e747d38dd8428e9f33f09419e4",
    "eb65fce58301a9a60758264fb3641e309d67350a560b5ebe24becd162bed7242",
    "ec156e3716ec160813f9f353971adf01407092282a64e4c5fd32072236820887",
    "ee48b7508696fc40f52c466a32e3bb99a8c66cafc9c6ded57f3dc741a391792a",
    "eedbf35d3cd402260d3b7cf937b2ff768ab23b09e6f93105b52827f71fdf7e4a",
    "ef750052f2ce58c0ec7837dffd7289f27b998010614201c3b22c72cfb28838f4",
    "f0d1796c4f0282431df3227a73d4ddb4cbeae20efe77cb718d483ddab9ba5f91",
    "f3578efb6c9d922ff41d74ec9067f0bd626b5f0958f813f53841b189593610e7",
    "f3dcec69dc9c739a9b4375d66c1e69ff7fce4d5a5bd4e8b3db8552364aac0896",
    "fa10113a8951a108c3f486bdd5a2201131c0d9883a86f8671f7e8a40a3f7d0b5",
    "fa927f1b67055313829331f59c78e62dd8e4bf735b2be2fd993505cf3a506575",
    "fa93d54455fad23b136b7c17d6c2875fbd499076f06486165af129b2c35a4be2",
    "fb38e8cb5913499244348a6816784d686d966e6b71b8079c19f97b3d4150cd7f",
    "fbc8a3799e2d66e17d06eddb31933b0ac3cdef690e8803c79eafa75223b47a58",
    "fc4b42270ff769242c06e0abae0002fb8342b9ea3f0588f0c14ac8869aebaecb",
    "fda861a731936ca7039ce5d65cb7c781ef60da279f84484381754cffa139beba",
    "fe5c59c850068b24270d7d154b124869db681543eb2f4e196e68cd6d3280abf5",
    "fe94f65d9d84daae245ad993b4f51682ac2fd7c7f0e3637226adb33cf29a6994",
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
                and hashlib.sha256(t.encode("utf-8")).hexdigest() in NOTE_BASENAME_HASHES):
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
    return next((h for h in (allow_hash(sc, token) for sc in scopes(path)) if h in ALLOW_STATE["allowed"]), None)


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
            if approval_changed(repo, commit):
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
    g.add_argument("--allow-hash", nargs=2, metavar=("SCOPE", "NAME"),
                   help="print the allow-list entry for NAME in SCOPE (a repository-relative file, or a "
                        "directory prefix ending in /)")
    a = ap.parse_args(argv)
    if a.allow_hash is not None:
        path, name = a.allow_hash
        allowable = (hashlib.sha256(" ".join(name.split()).lower().encode("utf-8")).hexdigest() in PRIVATE_NAME_HASHES
                     or any(p.fullmatch(name) for label, p in FORBIDDEN if label in ALLOWABLE))
        if not allowable:
            print("error: NAME is not a name, agent word, note link, work session or tracker word this check finds (give it as one word, the "
                  "words of the phrase, or the [[link]])", file=sys.stderr)
            return 2
        print(allow_hash(path, name))
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
    ALLOW_STATE["allowed"] = dict(ALLOWED.get(repo_key(name), {})) if name else {}
    ALLOW_STATE["repo"] = name
    ALLOW_STATE["used"] = {}
    try:
        CONFIG.update(load_config(a.repo, a.tree if a.tree is not None else a.range.split("..")[-1] or "HEAD"))
        if a.tree is not None:
            global REPO_NAME_COUNTS
            REPO_NAME_COUNTS = {}
            try:
                findings, compared = scan_tree(a.repo, a.tree)
                if a.repo_name_counts:
                    import json
                    print(json.dumps(repo_name_counts(name or "")))
                    return 0 if compared else 2
                findings += baseline_findings(name or "")
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
    if SKIPPED:
        print(f"public text: not read: {len(SKIPPED)} entries (first: {SKIPPED[0]})")
    if not zstd_available():
        print(f"public text: Python {sys.version_info.major}.{sys.version_info.minor} has no zstd reader: "
              f"a zstd stream is an unscanned container (Python 3.14 reads it)")
    print(f"public text: {len(findings)} findings in {what}" + (f" ({allowed} allowed mentions)" if allowed else ""))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
