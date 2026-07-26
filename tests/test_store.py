"""One invariant of the encrypted store: saving never silently loses a secret.

`CLAUDE.md` says coverage here is deliberately narrow and that a module earns a
test by having failure modes that are **silent and expensive**. This one does,
and it cost a real credential to find out (#39): editing a hostname on any Setup
card destroyed that system's password, said nothing, and surfaced minutes later
as a connector that would not connect. Nothing else in `store.py` is covered,
and this file is not an invitation to change that.

Both halves of the bug are represented, because either alone is enough to lose a
credential: a secret sent empty, and a secret not sent at all.
"""

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store as _store  # noqa: E402


class SaveSystemTest(unittest.TestCase):
    """`store` resolves its paths at import, so a temp CLAUDEOS_DATA only takes
    effect after a reload — which also gives each test its own master key."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CLAUDEOS_DATA"] = self.tmp
        self.store = importlib.reload(_store)
        self.store.save_system("unifi", {"host": "10.0.0.1", "username": "admin",
                                         "password": "s3cret"})

    def tearDown(self):
        os.environ.pop("CLAUDEOS_DATA", None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        importlib.reload(_store)

    def secret(self):
        return (self.store.get_system("unifi", reveal_secrets=True) or {}).get("password")

    def test_a_payload_that_omits_the_secret_keeps_it(self):
        """The reported bug. The Setup form sends no key at all for a blank
        secret box, so the stored password has to survive a payload that never
        mentions it — which the old loop, running only over what was sent,
        could not do."""
        self.store.save_system("unifi", {"host": "10.0.0.9", "username": "admin"})

        self.assertEqual(self.secret(), "s3cret", "editing the host destroyed the password")
        self.assertEqual(self.store.get_system("unifi")["host"], "10.0.0.9")

    def test_a_payload_with_an_empty_secret_keeps_it_too(self):
        """The other half. "Leave blank to keep" has to mean keep whether the
        caller sends the blank or drops the key."""
        self.store.save_system("unifi", {"host": "10.0.0.9", "username": "admin",
                                         "password": ""})

        self.assertEqual(self.secret(), "s3cret")

    def test_a_new_secret_replaces_the_old_one(self):
        """Carrying values forward must not make a credential unchangeable."""
        self.store.save_system("unifi", {"host": "10.0.0.1", "username": "admin",
                                         "password": "rotated"})

        self.assertEqual(self.secret(), "rotated")

    def test_a_secret_is_never_returned_to_the_browser(self):
        self.store.save_system("unifi", {"host": "10.0.0.9", "username": "admin"})

        summary = self.store.public_summary()["unifi"]["settings"]
        self.assertEqual(summary["password"], "•••••")
        self.assertNotIn("s3cret", str(self.store.public_summary()))

    def test_an_omitted_non_secret_field_is_still_removed(self):
        """The carry-forward is for secrets only. If it quietly became a blanket
        merge, a field could never be cleared and a stale host would outlive the
        edit that removed it."""
        self.store.save_system("unifi", {"host": "10.0.0.9"})

        entry = self.store.get_system("unifi")
        self.assertNotIn("username", entry, "an omitted plain field was resurrected")
        self.assertEqual(self.secret(), "s3cret", "…and the secret still survives")

    def test_every_secret_field_of_a_multi_secret_system_survives(self):
        """`registries` and `pushover` hold two secrets each; carrying forward
        one and dropping the other would be the same bug, half fixed."""
        self.store.save_system("pushover", {"token": "tok", "user_key": "usr"})

        self.store.save_system("pushover", {})

        kept = self.store.get_system("pushover", reveal_secrets=True)
        self.assertEqual(kept, {"token": "tok", "user_key": "usr"})


if __name__ == "__main__":
    unittest.main()
