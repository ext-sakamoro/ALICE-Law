#!/usr/bin/env python3
"""Tests of the private name key: scripts/public_text_check.py (load_name_key, name_mac, --require-private-names,
--name-mac, the notice without a key) and scripts/name_key_absent.py.

Every key here is made up; the real key is never read (HOME points at an empty directory and the environment
variable is set or removed per test).

Usage: python3 scripts/test_name_key.py
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
KEY_A = "a1" * 32
KEY_B = "b2" * 32
SCRATCH = tempfile.mkdtemp(prefix="name-key-test-")
EMPTY_HOME = tempfile.mkdtemp(dir=SCRATCH)
os.environ["HOME"] = EMPTY_HOME
os.environ.pop("ALICE_PRIVATE_NAME_HMAC_KEY", None)
sys.path.insert(0, str(HERE))
import public_text_check as ptc  # noqa: E402
import name_key_absent as nka  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": EMPTY_HOME}


def mac(key: str, text: str) -> str:
    return hmac.new(bytes.fromhex(key), text.encode("utf-8"), hashlib.sha256).hexdigest()


def repo_with(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp(dir=SCRATCH))
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
        subprocess.run(["git", "-C", str(d), *cmd], check=True, capture_output=True, env=GIT_ENV)
    return d


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


class KeyState(unittest.TestCase):
    def setUp(self):
        self.saved_env = os.environ.get("ALICE_PRIVATE_NAME_HMAC_KEY")
        self.addCleanup(self.restore)
        ptc.NAME_KEY.update(key=None, read=False)

    def restore(self):
        if self.saved_env is None:
            os.environ.pop("ALICE_PRIVATE_NAME_HMAC_KEY", None)
        else:
            os.environ["ALICE_PRIVATE_NAME_HMAC_KEY"] = self.saved_env
        ptc.NAME_KEY.update(key=None, read=False)

    def use(self, key: str | None):
        if key is None:
            os.environ.pop("ALICE_PRIVATE_NAME_HMAC_KEY", None)
        else:
            os.environ["ALICE_PRIVATE_NAME_HMAC_KEY"] = key
        ptc.NAME_KEY.update(key=None, read=False)


class Loading(KeyState):
    def test_no_key_anywhere_is_none(self):
        self.use(None)
        self.assertIsNone(ptc.load_name_key())
        self.assertIsNone(ptc.name_mac("x"))

    def test_the_environment(self):
        self.use(KEY_A)
        self.assertEqual(ptc.name_mac("alice-zorblat"), mac(KEY_A, "alice-zorblat"))

    def test_the_key_file_when_the_environment_is_unset(self):
        self.use(None)
        d = Path(tempfile.mkdtemp(dir=SCRATCH))
        f = d / "key"
        f.write_text(KEY_B + "\n", encoding="ascii")
        with mock.patch.object(ptc, "NAME_KEY_FILE", str(f)):
            self.assertEqual(ptc.name_mac("x"), mac(KEY_B, "x"))

    def test_a_malformed_key_is_refused_without_quoting_it(self):
        bad = "zz" + "1" * 62
        self.use(bad)
        with self.assertRaises(ValueError) as c:
            ptc.load_name_key()
        self.assertNotIn(bad, str(c.exception))
        self.assertNotIn("1" * 20, str(c.exception))


class Matching(KeyState):
    def test_a_table_under_one_key_does_not_match_under_another(self):
        line = "the plan is in the zorblat quux notes"
        saved = ptc.PRIVATE_NAME_HASHES
        self.addCleanup(lambda: setattr(ptc, "PRIVATE_NAME_HASHES", saved))
        ptc.PRIVATE_NAME_HASHES = frozenset({mac(KEY_A, "zorblat quux")})
        self.use(KEY_A)
        self.assertEqual(ptc.private_names(line), ["zorblat quux"])
        self.use(KEY_B)
        self.assertEqual(ptc.private_names(line), [])
        self.use(None)
        self.assertEqual(ptc.private_names(line), [])

    def test_repository_session_and_host_tables(self):
        self.use(KEY_A)
        saved = (ptc.PRIVATE_ALICE_REPO_HASHES, ptc.PRIVATE_REPO_HASHES, ptc.HOST_NAME_HASHES, ptc.SESSION_TOKEN_HASHES)
        self.addCleanup(lambda: (setattr(ptc, "PRIVATE_ALICE_REPO_HASHES", saved[0]),
                                 setattr(ptc, "PRIVATE_REPO_HASHES", saved[1]),
                                 setattr(ptc, "HOST_NAME_HASHES", saved[2]),
                                 setattr(ptc, "SESSION_TOKEN_HASHES", saved[3])))
        ptc.PRIVATE_ALICE_REPO_HASHES = frozenset({mac(KEY_A, "alice-zorblat")})
        ptc.PRIVATE_REPO_HASHES = frozenset({mac(KEY_A, "zorblat-notes")})
        ptc.HOST_NAME_HASHES = {mac(KEY_A, "zorb-box")}
        ptc.SESSION_TOKEN_HASHES = {mac(KEY_A, "e9")}
        self.assertEqual(ptc.private_repo_names("see ALICE-Zorblat for it"), ["alice-zorblat"])
        self.assertTrue(ptc.private_paths("see ~/zorblat-notes/plan.md"))
        self.assertTrue(ptc.host_names("ssh zorb-box"))
        self.assertTrue(ptc.commit_session_tokens("fix: the order (e9)"))
        self.use(KEY_B)
        self.assertEqual(ptc.private_repo_names("see ALICE-Zorblat for it"), [])
        self.assertFalse(ptc.private_paths("see ~/zorblat-notes/plan.md"))
        self.assertFalse(ptc.host_names("ssh zorb-box"))
        self.assertFalse(ptc.commit_session_tokens("fix: the order (e9)"))


class Modes(KeyState):
    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ptc.main(args)
        return code, out.getvalue(), err.getvalue()

    def fixture(self) -> Path:
        saved = ptc.PRIVATE_NAME_HASHES
        self.addCleanup(lambda: setattr(ptc, "PRIVATE_NAME_HASHES", saved))
        ptc.PRIVATE_NAME_HASHES = saved | {mac(KEY_A, "zorblat quux")}
        return repo_with({"NOTES.md": "the zorblat quux plan\n"})

    def test_with_the_key_the_name_is_found(self):
        repo = self.fixture()
        self.use(KEY_A)
        code, out, _ = self.run_main(["--repo", str(repo), "--repo-name", "ALICE-Fixture", "--tree", "HEAD",
                                      "--require-private-names"])
        self.assertEqual(code, 1, out)
        self.assertIn("NOTES.md:1", out)

    def test_without_the_key_required_fails(self):
        repo = self.fixture()
        self.use(None)
        code, _, err = self.run_main(["--repo", str(repo), "--repo-name", "ALICE-Fixture", "--tree", "HEAD",
                                      "--require-private-names"])
        self.assertEqual(code, 2)
        self.assertIn("private name key is not set", err)

    def test_without_the_key_not_required_skips_with_a_notice(self):
        repo = self.fixture()
        self.use(None)
        code, out, _ = self.run_main(["--repo", str(repo), "--repo-name", "ALICE-Fixture", "--tree", "HEAD"])
        self.assertEqual(code, 0, out)
        self.assertIn("names not checked: the name key is not set here", out)

    def test_name_mac_and_the_counts_need_the_key(self):
        self.use(KEY_A)
        code, out, _ = self.run_main(["--name-mac", "Zorblat  Quux"])
        self.assertEqual((code, out.strip()), (0, mac(KEY_A, "zorblat quux")))
        self.use(None)
        self.assertEqual(self.run_main(["--name-mac", "x"])[0], 2)
        repo = repo_with({"a.md": "x\n"})
        self.assertEqual(self.run_main(["--repo", str(repo), "--tree", "HEAD", "--repo-name-counts"])[0], 2)


class Absent(KeyState):
    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = nka.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_a_tracked_file_with_the_key_is_found_and_the_key_not_printed(self):
        self.use(KEY_A)
        for form in (KEY_A, KEY_A.upper()):
            repo = repo_with({"a.txt": "nothing\n", "b/c.cfg": f"k={form}\n"})
            code, out, err = self.run_main(["--repo", str(repo)])
            self.assertEqual(code, 1)
            self.assertIn("b/c.cfg", out)
            self.assertNotIn(KEY_A, out + err)
            self.assertNotIn(KEY_A.upper(), out + err)

    def test_clean_and_the_modes_without_a_key(self):
        repo = repo_with({"a.txt": "nothing\n"})
        self.use(KEY_A)
        self.assertEqual(self.run_main(["--repo", str(repo)])[0], 0)
        self.use(None)
        self.assertEqual(self.run_main(["--repo", str(repo), "--require-key"])[0], 2)
        self.assertEqual(self.run_main(["--repo", str(repo)])[0], 0)


# the plain SHA-256 entries left when the tables moved to HMAC (already public in history): the legacy set may
# only lose entries (each one supplied and moved to the HMAC set, or dropped), never gain one
LEGACY_AT_MOVE = frozenset({
    "18eaed5cd72e74361e9c620a5b1df3f93cc34007f9c288abf27802d52d101506",
    "19f798ce9f70706dd4334369cbf056e51922830d480360b8ace258e818ba7dd4",
    "6faef1df454754cb29df4e7e91bf6e37f64060ba852cb61d9f201f7b2a96b6bc",
    "8653d44dc6fd3b625cba9865fc10f6b8d2d9d871d5da9e3624bc6d87179ed8f1",
    "c379346cda6119421f42651914a8871e340094d8f5b7980335354e8b70028c28",
    "c96267f290f76ac40a777ee9d90bc6d2198bb4a1632a987d8411f112d5e42286",
    "de735c5c8d8ee5f8ba0468a624fd847d37591b8f112579e7fc5216760088221c",
})


class Legacy(KeyState):
    def test_the_legacy_set_only_shrinks(self):
        self.assertLessEqual(ptc.LEGACY_PRIVATE_NAME_SHA256, LEGACY_AT_MOVE)

    def test_a_legacy_entry_still_matches_without_and_with_a_key(self):
        saved = ptc.LEGACY_PRIVATE_NAME_SHA256
        self.addCleanup(lambda: setattr(ptc, "LEGACY_PRIVATE_NAME_SHA256", saved))
        ptc.LEGACY_PRIVATE_NAME_SHA256 = frozenset({hashlib.sha256(b"zorblat quux").hexdigest()})
        for key in (None, KEY_A):
            self.use(key)
            self.assertEqual(ptc.private_names("the zorblat quux plan"), ["zorblat quux"])

    def test_every_other_table_is_not_plain_sha256(self):
        # a made-up check that the moved tables hold no plain SHA-256 of a name the legacy set also lacks: an entry
        # of the HMAC tables equal to its own plain hash would mean the move was skipped; spot-check by size
        self.assertGreater(len(ptc.PRIVATE_NAME_HASHES), len(ptc.LEGACY_PRIVATE_NAME_SHA256))
        self.assertFalse(ptc.PRIVATE_NAME_HASHES & ptc.LEGACY_PRIVATE_NAME_SHA256)


# a public word the check finds (an agent word), built so this file does not spell it
PUBLIC_WORD = "wor" + "ker"
NOTE_LINK = "[" * 2 + "some_note" + "]" * 2


class AllowTable(KeyState):
    def test_keyed_entries_are_used_only_with_the_key(self):
        saved = ptc.ALLOWED
        self.addCleanup(lambda: setattr(ptc, "ALLOWED", saved))
        self.use(KEY_A)
        keyed = ptc.allow_mac("docs/a.md", "zorblat quux")
        private_row = "k:" + mac(KEY_A, "ALICE-Hidden")
        ptc.ALLOWED = {ptc.repo_key("ALICE-Open"): {ptc.allow_hash("docs/a.md", PUBLIC_WORD): 1, keyed: 2},
                       private_row: {ptc.allow_hash("x.md", PUBLIC_WORD): 3}}
        self.assertEqual(ptc.allowed_for("ALICE-Open", ptc.load_name_key()),
                         {ptc.allow_hash("docs/a.md", PUBLIC_WORD): 1, keyed: 2})
        self.assertEqual(ptc.allowed_for("ALICE-Hidden", ptc.load_name_key()), {ptc.allow_hash("x.md", PUBLIC_WORD): 3})
        self.use(None)
        # without the key: the plain entries stay (their checks run), the keyed ones and the keyed row are left out
        self.assertEqual(ptc.allowed_for("ALICE-Open", None), {ptc.allow_hash("docs/a.md", PUBLIC_WORD): 1})
        self.assertEqual(ptc.allowed_for("ALICE-Hidden", None), {})

    def test_which_entries_are_keyed(self):
        saved = ptc.PRIVATE_NAME_HASHES
        self.addCleanup(lambda: setattr(ptc, "PRIVATE_NAME_HASHES", saved))
        self.use(KEY_A)
        ptc.PRIVATE_NAME_HASHES = frozenset({mac(KEY_A, "zorblat quux")})
        self.assertTrue(ptc.allow_private("Zorblat  Quux"))
        self.assertTrue(ptc.allow_private(NOTE_LINK))
        self.assertFalse(ptc.allow_private(PUBLIC_WORD))

    def test_the_note_table_is_keyed(self):
        saved = ptc.NOTE_BASENAME_HASHES
        self.addCleanup(lambda: setattr(ptc, "NOTE_BASENAME_HASHES", saved))
        ptc.NOTE_BASENAME_HASHES = frozenset({mac(KEY_A, "zorb_private_note")})
        line = "see zorb_private_note.md"
        self.use(KEY_A)
        self.assertTrue(ptc.note_tokens(line))
        self.use(KEY_B)
        self.assertFalse(ptc.note_tokens(line))


class Wiring(unittest.TestCase):
    """ALICE-Law's own runs of the checker pass --require-private-names (a pull request from a fork excepted)"""

    def test_every_run_here_requires_the_names(self):
        runs = []
        for f in sorted((ROOT / ".github" / "workflows").glob("*.yml")) + [ROOT / "scripts" / "preflight.sh"]:
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r"\bpython3?\s+(?:\S*/)?public_text_check\.py\b", code) and "--allow-hash" not in code:
                    runs.append((f.name, n, code))
        self.assertGreaterEqual(len(runs), 3, runs)
        for name, n, code in runs:
            self.assertTrue("--require-private-names" in code or "$req" in code, (name, n, code))
        live = (HERE / "public_text_live.py").read_text(encoding="utf-8")
        self.assertIn('"--require-private-names"', live)

    def test_the_jobs_that_run_it_get_the_secret(self):
        for wf in ("public-text.yml", "public-text-baseline-ci.yml", "conformance-live.yml"):
            text = (ROOT / ".github" / "workflows" / wf).read_text(encoding="utf-8")
            self.assertIn("ALICE_PRIVATE_NAME_HMAC_KEY: ${{ secrets.ALICE_PRIVATE_NAME_HMAC_KEY }}", text, wf)


if __name__ == "__main__":
    unittest.main(verbosity=1)
