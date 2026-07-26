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

Only the zero-channel path is covered. A channel that is configured and then
fails already logged per-channel and was never the silent case, and nothing else
in `notify.py` is tested — this file is not an invitation to change that. No
network: the gap is recorded before any sender is reached, and the one test that
needs a delivery to succeed substitutes the sender.
"""

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import notify as _notify  # noqa: E402
from app import oplog as _oplog  # noqa: E402
from app import store as _store  # noqa: E402


class ZeroChannelTest(unittest.TestCase):
    """All three modules resolve paths under `DATA_DIR` at import, so a temp
    `CLAUDEOS_DATA` only takes effect after a reload, innermost first."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CLAUDEOS_DATA"] = self.tmp
        self.store = importlib.reload(_store)
        self.oplog = importlib.reload(_oplog)
        self.notify = importlib.reload(_notify)

    def tearDown(self):
        os.environ.pop("CLAUDEOS_DATA", None)
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


if __name__ == "__main__":
    unittest.main()
