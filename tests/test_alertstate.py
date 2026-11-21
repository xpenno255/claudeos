"""Alert latches across a restart, and the poller and SMART branches that read them.

`CLAUDE.md` sets the bar at failure modes that are **silent and expensive**.
Both halves of this clear it, and both were seen on the live instance.

**A restart forgot every alert (#73).** The poller, SMART, the monitors and the
registry held their last-alerted state in module globals, and every deploy is a
restart (15 since August). Home Assistant was down at boot twice on 9 Nov and
never alerted, because the poller needed a previous "up" to fall from. "Docker
is DOWN" went out 13 seconds before a restart and its "recovered" never came.
And SMART treated a disk it had never seen as having nothing to compare against,
so a disk already failing at boot was swept forever and alerted zero times. That
is the expensive one: SMART pages `urgent` because the window to act is short.

**The poller paged on every blip (#72).** 39 `high` DOWN/recovered pairs in 14
days, each over in 30–100 seconds. Silent is the wrong word for that, but it is
how silence gets made: an owner trained to ignore "is DOWN" ignores the real one.
Waiting for a second failed poll was not enough, because a routine restart
outlasts a minute, so the wait is now a duration (`outage.DOWN_AFTER_S`, five
minutes, with a per-system override). That makes this a feature built to create
silence, and what is tested is where the silence stops: a failure that outlasts
the wait must still page, at `high`, exactly once. A failure that ends inside it
must leave a count behind, or raising the threshold has only hidden the flapping.

The seam: `app/alertstate.py` keeps the latches in one file under the data
directory, and resolves that path on every call. A restart is simulated by
reloading the modules over the same `CLAUDEOS_DATA`, which empties every global
and leaves only what was written to disk, so a latch that survives here survives
a deploy. The connectors, Proxmox and the notifier are substituted, so nothing
touches the network or sends anything, and no test waits.
"""

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Imported before `app`: this sets CLAUDEOS_DATA, and `store` binds
# DATA_DIR at import — after it, the redirect is too late (#66).
from tests import restore_data_dir

from app import alertstate as _alertstate  # noqa: E402
from app import oplog as _oplog  # noqa: E402
from app import poller as _poller  # noqa: E402
from app import smart as _smart  # noqa: E402
from app import store as _store  # noqa: E402


class _Fixture(unittest.TestCase):
    """A private data directory per test, and a way to restart over it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CLAUDEOS_DATA"] = self.tmp
        self.store = importlib.reload(_store)
        importlib.reload(_oplog)
        self.alertstate = importlib.reload(_alertstate)
        self.sent = []

    def tearDown(self):
        mock.patch.stopall()
        restore_data_dir()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for mod in (_store, _oplog, _alertstate, _poller, _smart):
            importlib.reload(mod)

    def notify(self, title, message, **kw):
        self.sent.append((title, message, kw))

    def titles(self):
        return [t for t, _, _ in self.sent]

    def restart(self, *modules):
        """Everything in memory is gone; only the data directory is left."""
        self.alertstate = importlib.reload(_alertstate)
        return [importlib.reload(m) for m in modules]


class LatchTest(_Fixture):

    def test_a_transition_is_claimed_once(self):
        a = self.alertstate
        self.assertIsNone(a.claim("poller", "docker", "down"), "nothing was latched before")
        self.assertIs(a.claim("poller", "docker", "down"), a.NO_CHANGE,
                      "the second caller must not also send")
        self.assertEqual(a.claim("poller", "docker", None), "down",
                         "clearing says what it cleared, so recovery knows it is owed")
        self.assertIs(a.claim("poller", "docker", None), a.NO_CHANGE)

    def test_the_latch_survives_a_restart(self):
        self.alertstate.claim("poller", "docker", "down")
        self.alertstate.claim("smart", "pve:/dev/sda", "fail")
        self.restart()
        self.assertEqual(self.alertstate.get("poller", "docker"), "down")
        self.assertEqual(self.alertstate.get("smart", "pve:/dev/sda"), "fail")

    def test_the_file_holds_only_what_is_owed(self):
        """Healthy is the rest state and is stored as nothing, so a subject seen
        healthy for the first time and one that recovered look the same."""
        self.alertstate.claim("poller", "docker", "down")
        self.alertstate.claim("poller", "unifi", "down")
        self.alertstate.claim("poller", "unifi", None)
        with open(os.path.join(self.tmp, "alerts.json"), encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(set(on_disk), {"poller"})
        self.assertEqual(set(on_disk["poller"]), {"docker"})
        self.assertEqual(on_disk["poller"]["docker"]["state"], "down")

    def test_the_path_follows_a_redirect_without_a_reload(self):
        """#66: a path bound at import kept pointing at the real data directory
        after the tests had moved theirs. Moving `store.DATA_DIR` must be enough."""
        elsewhere = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)
        with mock.patch.object(self.store, "DATA_DIR", elsewhere):
            self.alertstate.claim("poller", "docker", "down")
        self.assertTrue(os.path.exists(os.path.join(elsewhere, "alerts.json")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "alerts.json")),
                         "the latch was written to the directory that had been left")

    def test_an_unreadable_file_starts_fresh_and_says_so(self):
        """Losing the file costs a repeated DOWN at most. Raising here would cost
        the alert itself, which is the wrong way round."""
        with open(os.path.join(self.tmp, "alerts.json"), "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertIsNone(self.alertstate.claim("poller", "docker", "down"))
        self.assertTrue(any("alert state unreadable" in e["message"]
                            for e in _oplog.recent(20)))
        self.restart()
        self.assertEqual(self.alertstate.get("poller", "docker"), "down",
                         "the next write repairs the file")


class _System:
    """A connector whose reachability the test drives directly."""
    alive = True

    @classmethod
    def summary(cls, settings):
        if not cls.alive:
            raise ConnectionError("502 Bad Gateway")
        return {}

    @staticmethod
    def metrics(summary):
        return {}


class PollerTest(_Fixture):

    WAIT = _poller.outage.DOWN_AFTER_S

    def setUp(self):
        super().setUp()
        self.poller = importlib.reload(_poller)
        self.settings = {"host": "h"}
        self.clock = 1_700_000_000.0
        self.substitute()

    def substitute(self):
        mock.patch.object(self.poller, "CONNECTORS", {"homeassistant": _System}).start()
        mock.patch.object(self.store, "get_system",
                          lambda sid, reveal_secrets=False: dict(self.settings)).start()
        mock.patch.object(self.poller.notify, "send", self.notify).start()

    def poll(self, alive, after=30):
        """One poll, `after` seconds on from the last. The poller takes its
        clock as an argument, so five minutes pass without anybody waiting."""
        _System.alive = alive
        self.clock += after
        self.poller.poll_once(now=self.clock)

    def outage(self, seconds):
        """Fail every 30s for `seconds`, starting now."""
        self.poll(False)
        for _ in range(int(seconds // 30)):
            self.poll(False)

    def restart(self):
        mock.patch.stopall()
        (self.poller,) = super().restart(_poller)
        self.substitute()

    # ------------------------------------------------- the wait before DOWN

    def test_a_restart_sized_outage_sends_nothing(self):
        """The reason for the wait: Home Assistant applying an update is away
        for a minute or two, and that used to page `high` and then "recovered"."""
        self.poll(True)
        self.outage(self.WAIT - 30)
        self.poll(True)
        self.assertEqual(self.sent, [])

    def test_a_failure_that_outlasts_the_wait_sends_down(self):
        """The boundary. Silent one poll short of the wait, and DOWN on the poll
        that reaches it: a wait that never ends is an outage nobody hears about."""
        self.poll(True)
        self.outage(self.WAIT - 30)
        self.assertEqual(self.sent, [], "one poll short of the wait")
        self.poll(False)
        self.assertEqual(self.titles(), ["HOME ASSISTANT · down"])
        _, message, kw = self.sent[0]
        self.assertEqual(kw["priority"], "high")
        self.assertEqual(kw["key"], "poller:homeassistant",
                         "without a key the mute cannot tell DOWN from a repeat of DOWN")
        self.assertIn("unreachable for 5m", message)

    def test_it_says_down_once_however_long_it_lasts(self):
        self.poll(True)
        self.outage(self.WAIT * 4)
        self.assertEqual(self.titles(), ["HOME ASSISTANT · down"])

    def test_the_wait_is_the_systems_own_if_it_has_one(self):
        self.settings["down_after_min"] = "10"
        self.poll(True)
        self.outage(self.WAIT)
        self.assertEqual(self.sent, [], "this system was given ten minutes")
        self.outage(self.WAIT)
        self.assertEqual(self.titles(), ["HOME ASSISTANT · down"])

    def test_a_wait_nobody_can_read_is_the_default_not_forever(self):
        """A typo in a Setup field must not be able to silence a system."""
        for junk in ("soon", "-5", "nan", "99999", True):
            self.assertEqual(self.poller.outage.down_after({"down_after_min": junk}),
                             self.WAIT, junk)
        self.assertEqual(self.poller.outage.down_after({"down_after_min": "0"}), 0,
                         "zero is a deliberate answer: alert on the first failure")

    def test_the_error_reads_without_its_url(self):
        brief = self.poller.outage.brief
        self.assertEqual(brief("HTTP 502 from https://ha.example.org/api/config"),
                         "HTTP 502 (bad gateway)")
        self.assertEqual(brief("cannot reach http://192.168.1.20:8123/api: "
                               "[Errno 111] Connection refused"),
                         "unreachable: Connection refused")
        self.assertEqual(brief("timeout reaching https://192.168.1.1/proxy/network"),
                         "timed out")

    # --------------------------------------------- what a short one leaves

    def test_a_blip_recovers_silently_and_is_counted(self):
        """No DOWN went out, so there is nothing to have recovered from. But six
        of these in an evening is something flapping, and the weekly update is
        now the only place the owner would learn that."""
        self.poll(True)
        self.outage(60)
        self.poll(True)
        self.assertEqual(self.sent, [])
        seen = self.poller.outage.blips(0)
        self.assertEqual(seen["Home Assistant"]["count"], 1)
        self.assertEqual(seen["Home Assistant"]["longest"], 90)

    def test_an_outage_that_paged_is_not_also_a_blip(self):
        self.poll(True)
        self.outage(self.WAIT)
        self.poll(True)
        self.assertEqual(self.poller.outage.blips(0), {})

    def test_recovery_is_sent_on_the_first_success_and_says_how_long(self):
        self.poll(True)
        self.outage(self.WAIT + 240)
        self.poll(True)
        self.assertEqual(self.titles(), ["HOME ASSISTANT · down", "HOME ASSISTANT · back up"])
        self.assertIn("was down 9m", self.sent[1][1])

    # ------------------------------------------------ across a restart (#73)

    def test_a_system_down_at_boot_alerts_once_it_has_outlasted_the_wait(self):
        """Never seen up, so there was no True->False transition, which is why
        Home Assistant down at boot on 9 Nov never alerted."""
        self.outage(self.WAIT - 30)
        self.assertEqual(self.sent, [])
        self.poll(False)
        self.assertEqual(self.titles(), ["HOME ASSISTANT · down"])

    def test_a_down_before_a_restart_still_gets_its_recovered(self):
        """9 Nov: "Docker is DOWN" at 18:03:31, restart at 18:03:44, and no
        "recovered" ever followed. The latch keeps when the outage began, so the
        process that sends the recovery can still say how long it was."""
        self.outage(self.WAIT)
        self.restart()
        self.poll(True)
        self.assertEqual(self.titles(), ["HOME ASSISTANT · down", "HOME ASSISTANT · back up"])
        self.assertIn("was down 5m", self.sent[1][1])

    def test_a_restart_does_not_say_down_twice(self):
        self.outage(self.WAIT)
        self.restart()
        self.outage(self.WAIT)
        self.assertEqual(self.titles(), ["HOME ASSISTANT · down"],
                         "the latch outlived the process, so the owner was already told")


# A disk Proxmox itself has given up on: health is not PASSED, so `_evaluate`
# calls it `fail` whatever the attribute table says.
FAILED_DISK = {"devpath": "/dev/sda", "model": "WDC WD40EFRX", "serial": "WD-1",
               "type": "hdd", "size": 4_000_787_030_016, "health": "FAILED",
               "wearout": "N/A", "used": "ZFS"}
HEALTHY_DISK = dict(FAILED_DISK, health="PASSED")


class SmartTest(_Fixture):
    """The sweep runs for real against a substituted Proxmox."""

    def setUp(self):
        super().setUp()
        self.smart = importlib.reload(_smart)
        self.disk = FAILED_DISK
        self.substitute()

    def substitute(self):
        px = self.smart.proxmox
        mock.patch.object(self.store, "get_system",
                          lambda sid, reveal_secrets=False: {"host": "h"}).start()
        mock.patch.object(px, "nodes", lambda s: [{"node": "pve"}]).start()
        mock.patch.object(px, "disk_list", lambda s, node: [dict(self.disk)]).start()
        mock.patch.object(px, "disk_smart",
                          lambda s, node, dev: {"type": "ata", "attributes": []}).start()
        mock.patch.object(self.smart.notify, "send", self.notify).start()

    def restart(self):
        mock.patch.stopall()
        (self.smart,) = super().restart(_smart)
        self.substitute()

    def test_a_disk_already_failing_at_first_sight_alerts(self):
        """The reproduction from #73: a FAILED disk swept twice sent nothing,
        because a disk never seen before had no previous status to differ from."""
        self.smart.sweep()
        self.smart.sweep()
        self.assertEqual(self.titles(), ["DISK · /dev/sda on pve · fail"])
        _, _, kw = self.sent[0]
        self.assertEqual(kw["priority"], "urgent")
        self.assertEqual(kw["key"], "smart:pve:/dev/sda")

    def test_a_healthy_disk_at_first_sight_says_nothing(self):
        self.disk = HEALTHY_DISK
        self.smart.sweep()
        self.assertEqual(self.sent, [])

    def test_a_restart_does_not_alert_the_same_disk_twice(self):
        self.smart.sweep()
        self.restart()
        self.smart.sweep()
        self.assertEqual(self.titles(), ["DISK · /dev/sda on pve · fail"])

    def test_a_failure_alerted_before_a_restart_gets_its_recovery(self):
        self.smart.sweep()
        self.restart()
        self.disk = HEALTHY_DISK
        self.smart.sweep()
        self.assertEqual(self.titles(), ["DISK · /dev/sda on pve · fail",
                                         "DISK · /dev/sda on pve · ok again"])


if __name__ == "__main__":
    unittest.main()
