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

EXEMPT = {"scripts/public_text_check.py", "scripts/test_public_text_check.py", "scripts/public_text_private_repos.txt"}
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")

# ---------------------------------------------------------------- vocabulary (from docs_lint.py)

A = r"(?<![A-Za-z])"
Z = r"(?![A-Za-z])"


def words(*alts: str) -> str:
    return A + "(?:" + "|".join(alts) + ")" + Z


FORBIDDEN = [
    ("agent process", re.compile(words(r"workers?", r"subagents?", r"multi[- ]?agents?", r"skills?")
                                 # `git worktree add` is a command, not a description of how the work was done
                                 + r"|(?<!git )" + words(r"worktrees?")
                                 + r"|ワーカー|マルチエージェント|エージェント|調停役|並走\s*session|他\s*session|session\s*名|この\s*(?:session|セッション)")),
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
    ("internal note", re.compile(r"(?<![A-Za-z0-9_])(?:feedback|discipline|success|handoff)_[a-z0-9_]{4,}"
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
                          + r"|raspberry\s*pi)")),
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
SESSION_ID_RE = re.compile(IDS + r"\s*[、,]?\s*(?:の\s*)?" + REVIEW_WORD
                           + r"|(?<![A-Za-z])(?i:sessions?)\s*[:=]?\s*" + ID + r"|セッション\s*[:=：]?\s*" + ID)
# labels of review findings ("F1", "F2 / F3") named as such
REVIEW_LABEL_RE = re.compile(r"(?<![A-Za-z0-9])F\d+(?:\s*/\s*F\d+)*\s*(?:の\s*)?(?:レビュー|指摘|(?i:review))")


def session_ids(line: str) -> bool:
    """an id-like token next to a word that names a review or a session"""
    return bool(SESSION_ID_RE.search(line))


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
        "fd9a9d9abcdb50151ee94eb1e9e162ca570d43fc68c7a6c23ea42f57436871ab": 2,
    },
    "ALICE-SDF": {
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
        "ab6f260bbe48e591409dcd53d07c2044e0a4da028ef1d51bc2ae01ff4009ba58": 9,
        "2d5c1dc549cf7601676a0cb7f5452d20be3fa5f1d0f55140305152ca6be22625": 1,
        "e7fc9e6005c1c2d4cad2be2070cced19d2ce2874f93d046f0cabe7194671d81c": 2,
        "43e6578d81a78bd7891d1c713ab9d4dacae113f6ca20b2c38db6ed8133c68f1a": 1,
        "f41eaca7cb84ee285cdb120649aeab70224ff4da7efde8f691abf381744bbc4e": 1,
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


ALLOW_STATE: dict = {"allowed": {}, "used": {}}


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
# (one SHA-256 per line in scripts/public_text_private_repos.txt, kept next to this file)
_REPO_FILE = __import__("pathlib").Path(__file__).with_name("public_text_private_repos.txt")
PRIVATE_REPO_HASHES: set[str] = set(_REPO_FILE.read_text(encoding="utf-8").split()) if _REPO_FILE.exists() else set()


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


ALLOWABLE = ("private name", "agent process", "note link")


def allowed_entry(path: str, token: str) -> str | None:
    """the allow-list entry of this repository that allows the token in the path, if any"""
    return next((h for h in (allow_hash(sc, token) for sc in scopes(path)) if h in ALLOW_STATE["allowed"]), None)


def kinds(line: str, path: str | None = None) -> list[str]:
    """the kinds of internal word in a line (labels, not the words); a private name, an agent
    word or a note link that this repository's allow-list allows in `path` is not a finding"""
    tokens: dict[str, list[str]] = {}
    for label, pattern in FORBIDDEN:
        hits = [m.group(0) for m in pattern.finditer(line)]
        if not hits:
            continue
        if label == "agent process" and all(h.lower() in ("worker", "workers") for h in hits) \
                and worker_technical(path, line):
            continue
        if label == "note link" and path is not None and (path.endswith(".toml") or path.rsplit("/", 1)[-1] == "Cargo.lock") \
                and TOML_TABLE_RE.match(line):
            continue  # a TOML array-of-tables header ("[[bin]]", "[[kv_namespaces]]")
        tokens.setdefault(label, []).extend(hits)
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
    for n, line in enumerate(lines, 1):
        found = kinds(line, path)
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


def scan_tree(repo: str, ref: str) -> tuple[list[str], int]:
    findings, compared = [], 0
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
    for commit in git(repo, "rev-list", "--no-merges", rng).splitlines():
        if upstream_commit(repo, commit):
            continue  # the upstream project's commit in a fork (up to the fork point)
        compared += 1
        short = commit[:12]
        findings += scan_text(f"commit {short} message", git(repo, "log", "-1", "--format=%B", commit))
        # the paths the commit adds or changes, from a NUL-separated listing (not the diff headers,
        # which quote non-ASCII names and end a name with a space in a tab)
        changed = paths_z(git(repo, "diff-tree", "--root", "--no-commit-id", "-r", "-z", "--name-only",
                              "--no-renames", "--diff-filter=d", commit))
        if REPO_CONFIG in changed:
            keys = config_changes(repo, commit)
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
                findings += [f"commit {short} {path}:{n}: {k}" for k in kinds(text, path)]
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
    CLASS_USED.clear()
    SKIPPED.clear()
    if a.suggest_allow is not None:
        return suggest_allow(a.repo, a.suggest_allow[0], a.suggest_allow[1])
    name = a.repo_name or repo_name(a.repo)
    ALLOW_STATE["allowed"] = dict(ALLOWED.get(name, {})) if name else {}
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
    if SKIPPED:
        print(f"public text: not read: {len(SKIPPED)} entries (first: {SKIPPED[0]})")
    if not zstd_available():
        print(f"public text: Python {sys.version_info.major}.{sys.version_info.minor} has no zstd reader: "
              f"a zstd stream is an unscanned container (Python 3.14 reads it)")
    print(f"public text: {len(findings)} findings in {what}" + (f" ({allowed} allowed mentions)" if allowed else ""))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
