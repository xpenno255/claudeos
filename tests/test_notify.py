"""One invariant of the notifier: an alert that cannot be delivered leaves a record.

`CLAUDE.md` sets the bar for a test here at failure modes that are **silent and
expensive**, and this module earned one by being both at once (#41). With no
channel configured, `_fan_out` iterated an empty list and returned, and the
ops-log line was written only when at least one channel had *succeeded* — so
every alert this app raised was discarded with nothing anywhere recording that it
happened. The install in use was in exactly that state, which means the poller's
DOWN alerts, the uptime monitors, the weekly digest and SMART's `urgent` failing-
disk warning had all been going nowhere, silently, since they shipped.

Expensive is the disk: SMART sends at `urgent` because the window to act is
short, and a warning nobody receives spends that window.

The zero-channel path was all this file covered at first, on the reasoning that a
configured channel that fails logs per-channel and is not the silent case. That
turned out wrong in practice, so `RetryTest` covers the second path. On
2026-11-20 every Telegram call from the live instance was timing out. The last
alert that got through was 31 hours earlier, and nothing showed the gap, because
the only record was ops-log lines, and #41 already established that nobody reads
those unprompted. Worse, every caller latches its transition *before* sending, so
an alert lost to one blip is never raised again. Retries and a final "not
delivered" line are tested. Nothing else in `notify.py` is, and this file is not
an invitation to change that. No network: the gap is recorded before any sender
is reached, and every test that needs a delivery to succeed or fail substitutes
the sender.
"""

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Imported before `app`: this sets CLAUDEOS_DATA, and `store` binds
# DATA_DIR at import — after it, the redirect is too late (#66).
from tests import restore_data_dir

from app import notify as _notify  # noqa: E402
from app import oplog as _oplog  # noqa: E402
from app import store as _store  # noqa: E402


class _NotifyFixture(unittest.TestCase):
    """Shared setup only — no tests of its own, so nothing here runs twice.

    All three modules resolve paths under `DATA_DIR` at import, so a temp
    `CLAUDEOS_DATA` only takes effect after a reload, innermost first."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CLAUDEOS_DATA"] = self.tmp
        self.store = importlib.reload(_store)
        self.oplog = importlib.reload(_oplog)
        self.notify = importlib.reload(_notify)

    def tearDown(self):
        restore_data_dir()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for mod in (_store, _oplog, _notify):
            importlib.reload(mod)

    def raise_alert(self, title="proxmox is DOWN", priority="high"):
        """Synchronously, so the assertions do not race the daemon thread."""
        self.notify.send(title, "a message", priority, background=False)

    def log_lines(self):
        return [e["message"] for e in self.oplog.recent(50) if e["system"] == "notify"]

    def configure_a_channel(self):
        self.store.save_system("webhook", {"host": "http://example.invalid/hook"})


class ZeroChannelTest(_NotifyFixture):
    """The reported bug and its boundaries."""

    # ------------------------------------------------------------ the record

    def test_an_undeliverable_alert_is_written_to_the_ops_log(self):
        """The reported bug: with nowhere to send, the old code wrote no line at
        all, because the only line it had was guarded on a successful send."""
        self.assertEqual(self.notify.channels(), [], "fixture must start unconfigured")
        self.raise_alert()
        self.assertTrue(
            any("nowhere to go" in m and "proxmox is DOWN" in m for m in self.log_lines()),
            f"no ops-log line names the discarded alert: {self.log_lines()}")

    def test_an_undeliverable_alert_is_counted_and_named(self):
        """The dashboard needs to say what was lost, not merely that something
        was — a count on its own is not actionable."""
        self.raise_alert()
        gap = self.notify.alerting_gap()
        self.assertIsNotNone(gap)
        self.assertEqual(gap["count"], 1)
        self.assertEqual(gap["last_title"], "proxmox is DOWN")
        self.assertEqual(gap["last_priority"], "high")
        self.assertIsNotNone(gap["last_ts"])

    def test_nothing_is_claimed_before_anything_is_dropped(self):
        """A fresh unconfigured install has lost nothing, and must not be nagged
        about a channel it may have deliberately chosen not to have."""
        self.assertIsNone(self.notify.alerting_gap())

    def test_distinct_alerts_accumulate(self):
        self.raise_alert("proxmox is DOWN")
        self.raise_alert("disk /dev/sda is FAILING", "urgent")
        gap = self.notify.alerting_gap()
        self.assertEqual(gap["count"], 2)
        self.assertEqual(gap["last_title"], "disk /dev/sda is FAILING")

    def test_a_flapping_system_counts_once_per_cooldown(self):
        """`send` mutes an identical title for COOLDOWN_S before `_fan_out` is
        reached. The count inherits that, which it must: the poller retries every
        30s, and a per-retry count would read as hundreds of distinct losses."""
        for _ in range(5):
            self.raise_alert("proxmox is DOWN")
        self.assertEqual(self.notify.alerting_gap()["count"], 1)

    def test_the_count_survives_a_restart(self):
        """Deploys are `docker compose pull && up -d`, so an in-memory count
        would forget precisely the alerts dropped overnight."""
        self.raise_alert()
        self.notify = importlib.reload(_notify)
        self.assertEqual(self.notify.alerting_gap()["count"], 1)

    def test_an_unreadable_gap_file_does_not_break_the_alert_path(self):
        """The file is a convenience over the ops log, which already holds every
        drop. Losing it must cost a count, never an exception out of `send`."""
        with open(self.notify.GAP_PATH, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        self.raise_alert()
        self.assertEqual(self.notify.alerting_gap()["count"], 1)

    # ------------------------------------------------------ closing the gap

    def test_configuring_a_channel_closes_the_gap(self):
        """Drops stay on record, but the banner asks for a channel and must stop
        the moment there is one — a warning for something already done is worse
        than no warning."""
        self.raise_alert()
        self.assertIsNotNone(self.notify.alerting_gap())
        self.configure_a_channel()
        self.assertIsNone(self.notify.alerting_gap())

    def test_a_delivered_alert_forgets_the_earlier_drops(self):
        """Once something has got through, the drops describe a configuration
        that no longer exists, so removing the channel again must not resurrect
        a stale count as if it were news."""
        self.raise_alert()
        self.configure_a_channel()
        self.notify._SENDERS["webhook"] = lambda *a, **k: None
        self.raise_alert("a different alert")
        self.store.delete_system("webhook")
        self.assertEqual(self.notify.channels(), [])
        self.assertIsNone(self.notify.alerting_gap())


class StateTest(_NotifyFixture):
    """`state()` exists because the weekly report was inferring alerting from
    stale ops-log lines and announced "no notification channel configured"
    while Telegram was delivering (#53).

    It is tested for one thing beyond correctness: **it must not carry a
    secret.** Its whole purpose is to be serialised into the report prompt and
    sent to the Anthropic API, which is exactly the path #45 leaked a bot token
    down. ntfy's topic *is* its credential, so a state that named channels by
    their settings rather than their labels would leak one every week.
    """

    def test_it_reports_a_live_channel(self):
        self.configure_a_channel()
        st = self.notify.state()
        self.assertTrue(st["any_configured"])
        self.assertEqual(st["channels"], ["Webhook"])
        self.assertEqual(st["paused"], [])

    def test_it_reports_nothing_configured_as_nothing(self):
        st = self.notify.state()
        self.assertFalse(st["any_configured"])
        self.assertEqual(st["channels"], [])

    def test_a_paused_channel_is_neither_live_nor_absent(self):
        """Switching a channel off is a deliberate act. Reporting it as absent
        would be as wrong as reporting it as working."""
        self.store.save_system("webhook", {"host": "http://x.invalid", "enabled": False})
        st = self.notify.state()
        self.assertEqual(st["channels"], [])
        self.assertEqual(st["paused"], ["Webhook"])
        self.assertFalse(st["any_configured"])

    def test_it_carries_the_gap_so_lost_alerts_are_reportable(self):
        self.raise_alert()
        self.assertIsNotNone(self.notify.state()["gap"])

    def test_no_secret_reaches_the_state(self):
        """The report prompt goes to the Anthropic API. ntfy's topic is its only
        credential, so it must never appear here — #45's lesson, one layer up."""
        self.store.save_system("ntfy", {"topic": "s3cret-topic-value"})
        self.store.save_system("telegram", {"bot_token": "8000000000:AAsecrethalf",
                                            "chat_id": "123"})
        blob = json.dumps(self.notify.state())
        for secret in ("s3cret-topic-value", "AAsecrethalf", "8000000000:AAsecrethalf"):
            self.assertNotIn(secret, blob, f"{secret!r} must not reach the report")


if __name__ == "__main__":
    unittest.main()


class RetryTest(_NotifyFixture):
    """A configured channel that fails is retried before the alert is given up.

    Every caller alerts once per transition and has already recorded that
    transition, so this send is the only one there will ever be. Sleeps are
    substituted, so nothing here waits."""

    def setUp(self):
        super().setUp()
        self.configure_a_channel()
        self.waits = []

    def fan_out(self, sender):
        self.notify._SENDERS["webhook"] = sender
        self.notify._fan_out("Backup failed: orca-hub", "m", "high", None,
                             delays=self.notify.RETRY_DELAYS_S, sleep=self.waits.append)

    def test_a_blip_is_retried_and_the_alert_arrives(self):
        calls = []

        def flaky(s, title, message, *a):
            calls.append(message)
            if len(calls) == 1:
                raise ConnectionError("timeout reaching https://api.telegram.org/bot•••/sendMessage")
        self.fan_out(flaky)
        self.assertEqual(len(calls), 2, "the failed channel must be tried again")
        self.assertEqual(self.waits, [self.notify.RETRY_DELAYS_S[0]])
        self.assertIn("delivered late", calls[1],
                      "a retry can arrive after a newer alert and must say when it was raised")
        self.assertNotIn("delivered late", calls[0])
        self.assertTrue(any("sent via Webhook" in m for m in self.log_lines()))
        self.assertFalse(any("was not delivered" in m for m in self.log_lines()))

    def test_an_alert_that_never_gets_through_says_so_once(self):
        """Per-attempt warnings read like routine noise. The alert being lost is
        a different fact and gets its own line, naming the alert."""
        def dead(*a):
            raise ConnectionError("timeout")
        self.fan_out(dead)
        self.assertEqual(self.waits, list(self.notify.RETRY_DELAYS_S))
        lost = [e for e in self.oplog.recent(50)
                if e["system"] == "notify" and "was not delivered" in e["message"]]
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0]["level"], "error", "the weekly report sweeps warn and error")
        self.assertIn("Backup failed: orca-hub", lost[0]["message"])

    def test_a_working_channel_is_not_sent_twice_while_another_retries(self):
        self.store.save_system("ntfy", {"topic": "t"})
        got = {"ntfy": 0, "webhook": 0}

        def ok(*a):
            got["ntfy"] += 1
        self.notify._SENDERS["ntfy"] = ok

        def dead(*a):
            got["webhook"] += 1
            raise ConnectionError("timeout")
        self.fan_out(dead)
        self.assertEqual(got["ntfy"], 1, "a duplicate push is its own failure")
        self.assertEqual(got["webhook"], 1 + len(self.notify.RETRY_DELAYS_S))

    def test_a_caller_that_waits_is_not_held_for_the_retries(self):
        """`background=False` blocks its caller, so it gets one attempt."""
        def dead(*a):
            raise ConnectionError("timeout")
        self.notify._SENDERS["webhook"] = dead
        self.raise_alert()
        self.assertTrue(any("was not delivered" in m and "after 1 attempts" in m
                            for m in self.log_lines()))
