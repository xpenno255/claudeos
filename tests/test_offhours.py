"""One invariant of expected-offline windows: silence is never open-ended.

`CLAUDE.md` sets the bar at failure modes that are **silent and expensive**, and
this feature is built to create silence, which makes it the exact shape of thing
that bar exists for. Suppressing "the NAS is unreachable" every night is the
point; suppressing it on the night the NAS fails to wake is a storage outage
nobody is watching, discovered days later. The whole feature turns on the
boundary between those two, so the boundary is what is tested.

Two halves:

`OffhoursTest` covers the window arithmetic in isolation, because an overnight
power-down means the window crosses midnight and off-by-one errors there are
invisible — the alerts simply stop happening at slightly the wrong times, and
nothing looks broken.

`PollerTest` covers the branch that acts on it, because a correct window nobody
consults is the bug still shipping. It substitutes the connector, the clock and
the notifier, so it touches no network and sends nothing.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import offhours, poller  # noqa: E402

# 2026-07-27 is a Monday; only the time-of-day matters to the window.
def at(hour, minute=0):
    """A POSIX timestamp for local `hour:minute` today, since windows are local."""
    import time
    lt = list(time.localtime())
    lt[3], lt[4], lt[5] = hour, minute, 0
    return time.mktime(tuple(lt))


OVERNIGHT = {"offline_from": "23:00", "offline_to": "07:00", "offline_grace_min": 15}


class OffhoursTest(unittest.TestCase):

    def test_no_window_configured_is_no_window(self):
        for settings in ({}, {"offline_from": "23:00"}, {"offline_to": "07:00"},
                         {"offline_from": "", "offline_to": ""},
                         {"offline_from": "not a time", "offline_to": "07:00"},
                         {"offline_from": "25:00", "offline_to": "07:00"}):
            with self.subTest(settings=settings):
                self.assertIsNone(offhours.status(settings, at(3)))

    def test_equal_ends_are_treated_as_unconfigured(self):
        """"Always off" and "never off" are both defensible readings of a
        zero-length window, which is exactly why neither is guessed at."""
        self.assertIsNone(offhours.status({"offline_from": "07:00",
                                           "offline_to": "07:00"}, at(3)))

    def test_a_window_crossing_midnight_holds_on_both_sides(self):
        for hour in (23, 0, 3, 6):
            with self.subTest(hour=hour):
                st = offhours.status(OVERNIGHT, at(hour))
                self.assertTrue(st["in_window"], f"{hour}:00 should be inside 23:00–07:00")
                self.assertTrue(st["tolerated"])

    def test_outside_the_window_nothing_is_tolerated(self):
        for hour in (8, 12, 18, 22):
            with self.subTest(hour=hour):
                st = offhours.status(OVERNIGHT, at(hour))
                self.assertFalse(st["in_window"])
                self.assertFalse(st["tolerated"], f"{hour}:00 must not be excused")

    def test_a_daytime_window_does_not_wrap(self):
        day = {"offline_from": "09:00", "offline_to": "17:00"}
        self.assertTrue(offhours.status(day, at(12))["in_window"])
        self.assertFalse(offhours.status(day, at(3))["in_window"])
        self.assertFalse(offhours.status(day, at(20))["in_window"])

    def test_the_window_is_half_open(self):
        """Start is inside, end is not — otherwise the minute the NAS is due
        back is still excused."""
        self.assertTrue(offhours.status(OVERNIGHT, at(23, 0))["in_window"])
        self.assertFalse(offhours.status(OVERNIGHT, at(7, 0))["in_window"])

    # ------------------------------------------------ the boundary that matters

    def test_grace_covers_boot_time_but_then_stops(self):
        just_after = offhours.status(OVERNIGHT, at(7, 10))
        self.assertFalse(just_after["in_window"])
        self.assertTrue(just_after["in_grace"], "10 min into a 15 min grace")
        self.assertTrue(just_after["tolerated"])

        expired = offhours.status(OVERNIGHT, at(7, 20))
        self.assertFalse(expired["in_grace"], "20 min is past a 15 min grace")
        self.assertFalse(expired["tolerated"],
                         "past the grace, an unreachable NAS is a failure to wake")

    def test_zero_grace_means_the_window_end_is_the_boundary(self):
        strict = dict(OVERNIGHT, offline_grace_min=0)
        self.assertFalse(offhours.status(strict, at(7, 1))["tolerated"])

    def test_a_junk_grace_falls_back_to_the_default(self):
        """A typo must not silently become an unbounded excuse."""
        for junk in ("", "soon", None, "-5"):
            with self.subTest(junk=junk):
                st = offhours.status(dict(OVERNIGHT, offline_grace_min=junk), at(3))
                self.assertGreaterEqual(st["grace_min"], 0)
                self.assertLessEqual(st["grace_min"], offhours.DEFAULT_GRACE_MIN)


class _Connector:
    """A system whose reachability the test drives directly."""
    alive = False

    @classmethod
    def summary(cls, settings):
        if not cls.alive:
            raise ConnectionError("no route to host")
        return {"cpu_pct": 5}

    @staticmethod
    def metrics(summary):
        return {}


_DeadConnector = _Connector  # the case this feature is about


class PollerTest(unittest.TestCase):

    def setUp(self):
        poller._latest.clear()
        poller._history.clear()
        _Connector.alive = False
        self.sent = []
        self.logged = []
        self.patches = [
            mock.patch.object(poller, "CONNECTORS", {"synology": _DeadConnector}),
            mock.patch.object(poller.store, "get_system",
                              lambda sid, reveal_secrets=False: dict(OVERNIGHT, host="h")),
            mock.patch.object(poller.notify, "send",
                              lambda title, msg, **kw: self.sent.append((title, msg, kw))),
            mock.patch.object(poller.oplog, "add",
                              lambda lvl, sys_, msg: self.logged.append((lvl, sys_, msg))),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def poll_at(self, state, alive=False):
        _Connector.alive = alive
        with mock.patch.object(poller.offhours, "status", lambda s, now=None: state):
            poller.poll_once()
        return poller._latest["synology"]

    ASLEEP = {"in_window": True, "in_grace": False, "tolerated": True,
              "from": "23:00", "to": "07:00", "grace_min": 15}
    EXPIRED = {"in_window": False, "in_grace": False, "tolerated": False,
               "from": "23:00", "to": "07:00", "grace_min": 15}

    def test_inside_the_window_it_is_asleep_not_down(self):
        st = self.poll_at(self.ASLEEP)
        self.assertIsNone(st["ok"], "asleep is neither healthy nor broken")
        self.assertTrue(st["scheduled_off"])
        self.assertEqual(self.sent, [], "a NAS asleep on schedule must not alert")

    def test_being_asleep_is_logged_once_not_every_poll(self):
        for _ in range(4):
            self.poll_at(self.ASLEEP)
        lines = [m for _, _, m in self.logged if "scheduled offline" in m]
        self.assertEqual(len(lines), 1, f"one line per descent, got {self.logged}")

    def test_a_nas_that_fails_to_wake_alerts(self):
        """The invariant. Asleep all night, then the grace runs out and it is
        still unreachable — that is an outage and it must be as loud as any
        other, or this feature has hidden a dead NAS."""
        self.poll_at(self.ASLEEP)
        self.assertEqual(self.sent, [])

        st = self.poll_at(self.EXPIRED)
        self.assertFalse(st["ok"])
        self.assertEqual(len(self.sent), 1, "failure to wake must alert")
        title, msg, kw = self.sent[0]
        self.assertIn("DOWN", title)
        self.assertIn("did not come back", msg)
        self.assertEqual(kw.get("priority"), "high", "as loud as any other outage")

    def test_it_does_not_alert_twice_for_the_same_failure(self):
        self.poll_at(self.ASLEEP)
        self.poll_at(self.EXPIRED)
        self.poll_at(self.EXPIRED)
        self.assertEqual(len(self.sent), 1, "one alert per transition, not per poll")

    # ------------------------------------------- on when it needn't be is fine

    def test_reachable_inside_the_window_is_simply_healthy(self):
        """The window says a system *may* be off, never that it must be. Powering
        the NAS on mid-window to use it is a normal thing to do and must not be
        remarked on — no alert, and no "recovered" either, because it did not
        come back from anything."""
        self.poll_at(self.ASLEEP)
        st = self.poll_at(self.ASLEEP, alive=True)
        self.assertTrue(st["ok"])
        self.assertFalse(st.get("scheduled_off"))
        self.assertEqual(self.sent, [], "a manual power-on is not an event")

    def test_powering_off_again_inside_the_window_is_silent(self):
        self.poll_at(self.ASLEEP, alive=True)
        st = self.poll_at(self.ASLEEP)
        self.assertIsNone(st["ok"])
        self.assertTrue(st["scheduled_off"])
        self.assertEqual(self.sent, [], "back to asleep, still inside the window")

    def test_a_fault_during_the_on_period_alerts_normally(self):
        """The other half of the owner's rule: off when it is supposed to be on
        is a possible fault, and the window must not have dulled that."""
        self.poll_at(self.EXPIRED, alive=True)
        st = self.poll_at(self.EXPIRED)
        self.assertFalse(st["ok"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][2].get("priority"), "high")

    def test_with_no_window_the_ordinary_down_path_is_unchanged(self):
        st = self.poll_at(None)
        self.assertFalse(st["ok"])
        self.assertEqual(len(self.sent), 0,
                         "first sight of a dead system is not a True->False transition")


if __name__ == "__main__":
    unittest.main()
