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
                                 r"(?!\s+(?i:threads?)(?![A-Za-z]))|"
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
    ("internal tracker", re.compile(words(r"Backlog") + r"|memory\s*(?:側|化|に|へ|file)|(?<![A-Za-z0-9_])memory/")),
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
# "#", "_", "-" or "." (a date, a version, a colour, x86_64) is not id-like. The contexts
# "<id>の" and "<id>'s" are left to a check that knows the ids: here they matched counts,
# dates and "oracle 10's" far more often than ids.
ID = r"(?<![A-Za-z0-9#_.\-])[0-9a-f]{2}(?![A-Za-z0-9])"
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
ALLOWED: dict[str, dict[str, int]] = {
    "ALICE-LOL": {
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
    "ALICE-SDF": {
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
        "2315a2794fe699f753409a368e78d5cb110e6bca06075307e9450adff7a35475": 7,
        "4b5762de09b9db6f9d43cf54cbc6fd08d0c6d330eeae0540fb0c2865a0d7268d": 7,
        "865a3e24f0f7791cde0e953082115370e4da270336ce5511874e9b9a8d5aa982": 3,
        "a4622a60be38f8a45ab31d0020cd2a3c10fe791a8e8d24a3460ab354243e3e46": 3,
        "7491e84ba07ab24d4e2aa02dc0683f34f12a8aa3d7bdab4a6b31bcee5af66447": 3,
        "e01f408722226af8e97c873dd04fdaa90bc7a16190d976791cbe1bd1660454f7": 29,
        "c9bcfb4989c30783679258347fd0b88a3671780d1d3ef2e045042d3352a59d3a": 1,
        "a2ca05f4d55ca8dde59e831dcdf400e2785a129dbac2f89ec54dc28ee4bf9e47": 1,
        "04f5e1ffb8f5f13d28b764f887e3e99bfacbf07c0e425796a46711b3ac09fdd9": 1,
        "ab6f260bbe48e591409dcd53d07c2044e0a4da028ef1d51bc2ae01ff4009ba58": 3,
        "2d5c1dc549cf7601676a0cb7f5452d20be3fa5f1d0f55140305152ca6be22625": 1,
        "e7fc9e6005c1c2d4cad2be2070cced19d2ce2874f93d046f0cabe7194671d81c": 2,
    },
    "text-to-print": {
        "8e3dd0d012a2b0488d6e314c823c6db1fc86881986cf3597f9cd9f3365108bb7": 2,
        "525b28df28e719b319178d0fcd956e3932174b4719f6c659081fa554415d633a": 2,
        "938fe3b8cdc685f71f04da5f9a7e225edfddd6e89137a73eee378842cfaf329b": 2,
        "2fce1206037fe05fb65ee69462a31f0021e4d0f4cfe89f2325b6c00a72beed1f": 2,
        "607e6bc8421c315268ff7b882358b1b0554070af4c59f9850e6f70cdc710d30e": 1,
        "5e9759a32eeb52bfadaed64f1f2475cdef4c47bffa624a5e694593308c66d062": 6,
        "c4e9802423d3ef13964b175c670bad3a11daa8bd6874c18d4d1c4edc048d1393": 3,
        "d47bbfaf39f76243602a5e88dfc65279df2b05cfa4c562ff4dba04ac0f15e73a": 3,
        "971a9ffc87554fbe3293e6ca79ca2ec3be9f6e9fecc4138fece5c35a75900ef7": 3,
        "a5048c9fd6603806b175feb184239bfada1ace37eebf811c75fc400b14b58a05": 2,
        "40f8027e044e8aa3e2783d55bd9628c9b073f4e82458878f6bbdc8fcb3f791ed": 1,
        "2929782d0900b7181a937f0dbd61ed44120d917cc882969b9453bdaaff0a4f95": 3,
        "1a6aa0d6a24df8ac5f6ff7c0f6b30c35e552c12433f09c88e349ac63c3694c69": 4,
        "ffb612c1d576098613b338fc531a3e5defa9fa781e646cbe11d0c55536a753aa": 1,
        "e0ad96a94c13edafde51753681e9e1e936ec647f6df219c5ee6eb70c6da67bea": 1,
    },
    "ALICE-CodeTracker": {
        # the product records LLM coding sessions through a provider hook: the provider's and its
        # coding tool's names in the hook installer, its settings path, the model IDs of the cost
        # table and the docs of that feature, per published file of the mirror's allowlist
        # (mirror-allowlist.txt; private documents and crates are not published)
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
    "voicebox": {
        "b1b065b5f49f4b5fa7b6c07d114bc8398be43a8635a8753ecd4155c2f3af524b": 4,
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
# ROADMAP, CHANGELOG, comments) it still is.
WORKER_DIRS = ("crates/worker/", "migrations/")
WORKER_FILES = ("wrangler.toml",)
CARGO_LOCK_WORKER_RE = re.compile(r'^(?:name = "worker(?:-[a-z0-9_-]+)?"|\s*"worker(?:-[a-z0-9_-]+)?(?: [0-9][^"]*)?",?)\s*$')
TOML_TABLE_RE = re.compile(r"^\s*\[\[[A-Za-z0-9_.\-]+\]\]\s*(?:#.*)?$")
# a home directory path into a private repository (`~/<repo>/`, `/Users/<user>/<repo>/`,
# `/home/<user>/<repo>/`): the repository names are held as SHA-256 of the lower-case name
HOME_PATH_RE = re.compile(r"(?:~|/Users/[^/\s]+|/home/[^/\s]+)/([A-Za-z0-9._\-]+)")
# The set is held in this file: a repository's CI fetches this one file by its SHA-256, and a
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
    "24297f063a6f242b240bfc45ef705ef6ee91c2e46d190fad7844de93a73f3934",
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
    "595d98cc7840412aa30b75c29bfe6660b738ae5121181575a85b392bfb783ad9",
    "59ef3d51a2b43966044b4cb6f8d2a1c242325ca2cbec1774ea20fec1f0af4a19",
    "5af45c8c470d38ceffaf6f8c84c683d7387374854c595dc3a203fd686e3dd61a",
    "5afbaadaa5a027c53cd0a1049f0e208d7aace102ee92c3555b4eda3c26510627",
    "5c364e239f609d5c3b342aa05a5f73dad3327a99608c1e6d42b5f78c9858e447",
    "5cd7b76f6a07ce9e2faaff3a736a186baf83fdc5d498a86bdc8be35aaf0ae818",
    "5da07be6b5ec3a33241a9c835b061a7c858424e5385e9f7c143dd7bd4932e872",
    "62da62583976549ba701c3519663e71d68163c434b3e33d1904d195f703494b8",
    "630410822d19d58cdf8702f21d08c7baa5be4daef76505267ff65772808abfc4",
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
    "6d4bd47342a98881b7f40c3bb68874153a7e30682e866f5221c67388f405b4c8",
    "6d7bb4494182fb91ba5bd784dc5cb607acdface719115704d589e964a04c0b59",
    "6d928e2e337e93aa7ddcaba0c1d3042319a2604fac64633a1343c702155daed0",
    "6ebebc18481392d69825fcd8132fdd482b7a57e059fc717ecbff4f250fd5b56e",
    "6f22f8173a786389c223efbfbfe51d31148c0e495c498a4db5951254645a3162",
    "6f46cbd2042de3560ba457b53655841d17601cb874edcc5941364db7f24fbe67",
    "7103aef424c3fc6b2bb79418fe8f82647ffa013ada8ec15a099d5031cd4f2fd5",
    "730b19e39f798ee16abe94a38bcc72196c1e0947f50cd76888e2b789c5637232",
    "73409c66566e6139bfddd59d71edc3e7eb7a8d801ce9bf03ace83ec55ec26d7d",
    "73c451af4c2ba0dc90b232db9aa0bd8fff583193aa47a57edbce059dbe6e99c8",
    "7417be3ed739a1dc362888c193765d56972203dbd8a771c9bf1b52b9a287c3bd",
    "745c7ab2002b2e83b21e40cdf3bc2c1754a726fcbb225bd4199ca67acfb8aad1",
    "74d0a78112aa0deb02e301f1e0d81b34afd4cf551cb4d75c8d3a0663282d7fd2",
    "75ad4f65d9cc65ab2364c448235dfea3b93f4afd4f6dd29b493fbd9c241dc9d3",
    "75dbed5caca7f66d64756a0d664bed9917e221caddcf84a8c707133ea6e1a776",
    "76ed632eacc9561fddba93918e12fa408df40a2f9e8e038c7803346d9e2b9ab3",
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
    "9c08caed71f4dfd1ccdc6fd151ef630e5d1f3267c8d53e10f7590584e35127a7",
    "9f46f3b6d488c4dd50e9551707d8ce2bd7f905bbbb37a56c772548113c8271c1",
    "a00361277bc9ee9a643c9afc46ab4286aa42855d57920c9241919b08676bc885",
    "a030cfb0ded6a976179d5335fe8f2c41a5e219658f5e1ad30425aebedddbbf0d",
    "a08d6f8ba3de029daa8b9ea304b4f7b17081a44bece4d2972bc7b5b05a3026d4",
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
    "bde64f0765d46174f2679c29ec5014ec01dfe7896dfbb72a99145bea0cb35cdf",
    "bdfe8c52e628966eaec4a8537303f0c40f9f41c4af99b35653a6d946bc7cd77b",
    "be48a873ca6325305646bdac66e758b270985da9be74e1cf381c5c7e893e4ffe",
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
    "d25a19fc772ed4c865c6c27e55ebf2e28df094accbb33a7bc98ae2e5b64b140d",
    "d32848617829c36485e47c10782ab0a39a99cb3bcb4320c27cb8938dbe395f06",
    "d4dfd09e73510e839d5465979f883b064585e7e1d35cb67199ec6c07fb42b1f5",
    "d650941feab2653cdee2932e0bad657bb9987f11807b7dd0896e1f82108c3bbd",
    "d701717167b2a37556ec36749129354505bae89e3d00877b5fb243bff08fc5fb",
    "d79d1f8b76c9a4f5546474bf8cfea05cf1a9d3ef73698de05d5b6ae502861708",
    "dab6d5fcaa9dba360b6ad99fd4476b059ad505d8062aefa98145935dbbeabc56",
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
    "e86d5ce28625ff8165c8fcacce6858c25f8606ecabfc7db10e5b39a52ec07add",
    "e8cb8e656300ac5650c1d1be41861c8d21bbe8f5aa7fc4455b3bd8ffd0887bae",
    "e971ee2e9562376e8fbe0fc009f31698088eebdf667ab7fbc7ec109ab7191400",
    "ea59fe8732be2e09be2841ac3282c6c1f14afc9a0854aeb998a4fc66a4c708b2",
    "eab23c49572e4a0c28c2b31622c90bcf73cb015ce66b1f19a75e9ac9d0127f0e",
    "eac419fa3d3fbf476c91c589dee60b91d5bb08db55bf2e381cb3898b67f4035a",
    "eb1be37e716a03011b3c0905d2f5c9158b75b037d5c82de4856090f434122f36",
    "eb65fce58301a9a60758264fb3641e309d67350a560b5ebe24becd162bed7242",
    "ebb083174dfdb988ba06697d35855e42073e1f271aca5f68b4e595fd91c81cda",
    "ebe7a04f1273e2083e09cbbc815e048e6232f6d7f08aba25574728af48f6fd8f",
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
    "f86ed485e02dc93af75fbd167d1cae80246b95dc82bdf577dfc33e39699e4a3b",
    "f96da0861d98aa87a1151a7e3a606dcb6a39ba271658762bf7f5d99b449d40af",
    "fa9305783447d18185b2969241f7f7790f11f72ee556ab06641cc871b657b510",
    "fb56fa31593939dcca348c3fd796489546472babafea912250a8a7cf7ca8c688",
    "fba407f6d616b0bb6d0720dda9c1b5385499d71e0dd1a400d754a6401213dda4",
    "fbed3d391ab0c1a705f52b5035b353fade77f04a803c5c4fad7b3adc9ff85824",
    "fc4539e13aa4e61112ac42ae16890725c4ae31cab2a3e8fc7874dbb2f6bd1a82",
    "feb7d54f164d3fc98209bfd03e0c43af424a47690669399b9e3e4b497f2d0dc0",
})


def private_sets_problem() -> str | None:
    """the reason the private name and path checks would compare nothing, or None"""
    empty = [n for n, v in (("private-repository", PRIVATE_REPO_HASHES), ("private-name", PRIVATE_NAME_HASHES))
             if not v]
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
        r = subprocess.run(["git", "-C", repo, "merge-base", a or "HEAD", b or "HEAD"], capture_output=True, text=True)
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
    r = subprocess.run(["git", "-C", repo, "show", f"{rev}:{PIN_FILE}"], capture_output=True, text=True)
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


def private_paths(line: str) -> bool:
    return any(hashlib.sha256(m.group(1).lower().encode("utf-8")).hexdigest() in PRIVATE_REPO_HASHES
               for m in HOME_PATH_RE.finditer(line))


NOTE_NAME_RE = re.compile(r"(?<![A-Za-z0-9_])(?:feedback|discipline|success|handoff)_[a-z0-9_]{4,}")
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
    hits = list(NOTE_NAME_RE.finditer(line))
    if not hits:
        return []
    style = comment_style(path)
    if style is None:
        return [m.group(0) for m in hits]
    if spans is None:
        spans = prose_spans(line, style, lexer_state(path))
    return [m.group(0) for m in hits
            if any(a <= m.start() < b for a, b in spans) or beside_quote(line, m.start(), m.end())]


ALLOWABLE = ("private name", "agent process", "note link")


def allowed_entry(path: str, token: str) -> str | None:
    """the allow-list entry of this repository that allows the token in the path, if any"""
    return next((h for h in (allow_hash(sc, token) for sc in scopes(path)) if h in ALLOW_STATE["allowed"]), None)


def kinds(line: str, path: str | None = None, spans: list[tuple[int, int]] | None = None) -> list[str]:
    """the kinds of internal word in a line (labels, not the words); a private name, an agent
    word or a note link that this repository's allow-list allows in `path` is not a finding"""
    tokens: dict[str, list[str]] = {}
    for label, pattern in FORBIDDEN:
        own = own_skill_prefixes(line) if label == "agent process" else []
        hits = [m.group(0) for m in pattern.finditer(line) if not any(a <= m.start() < b for a, b in own)]
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


def repo_name(repo: str) -> str | None:
    """the repository's name from its origin URL (".../<name>.git"), or None"""
    r = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"], capture_output=True, text=True)
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
    g.add_argument("--allow-hash", nargs=2, metavar=("SCOPE", "NAME"),
                   help="print the allow-list entry for NAME in SCOPE (a repository-relative file, or a "
                        "directory prefix ending in /)")
    a = ap.parse_args(argv)
    if a.allow_hash is not None:
        path, name = a.allow_hash
        allowable = (hashlib.sha256(" ".join(name.split()).lower().encode("utf-8")).hexdigest() in PRIVATE_NAME_HASHES
                     or any(p.fullmatch(name) for label, p in FORBIDDEN if label in ("agent process", "note link")))
        if not allowable:
            print("error: NAME is not a name, agent word or note link this check finds (give it as one word, the "
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
    ALLOW_STATE["allowed"] = dict(ALLOWED.get(name, {})) if name else {}
    ALLOW_STATE["repo"] = name
    ALLOW_STATE["used"] = {}
    try:
        CONFIG.update(load_config(a.repo, a.tree if a.tree is not None else a.range.split("..")[-1] or "HEAD"))
        if a.tree is not None:
            findings, compared = scan_tree(a.repo, a.tree)
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
