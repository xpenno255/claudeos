"""Three invariants of backup tracking: status, persistence, and baselines.

`CLAUDE.md` sets the bar at failure modes that are **silent and expensive**, and
this module is made of them. Every other surface in the app measures
reachability, which announces itself; a backup's failure mode is an *absence*,
and the whole point of the feature is that an absence is invisible until you
need the thing that is missing. A job wrongly showing `ok` reports safety that
does not exist — the most expensive lie the app can tell.

That is not hypothetical here. Probing the live cluster before this was built
found **25 consecutive nightly vzdump failures**, back three weeks, with nothing
anywhere recording it.

The three seams are the ones `docs/spec-backups.md` nominated, and each is here
for a reason the spec argues:

1. **Status evaluation with the clock injected.** The entire feature is a
   comparison against wall-clock time. `evaluate(jobs, now)` takes `now` as an
   argument so grace boundaries and the `never` case are testable with no
   waiting and no real time.
2. **Persistence across restart.** The state that matters outlives the process.
   This is the concrete defect that ruled out reusing `monitors.py`, whose state
   is module-level: invisible at a 30-second cadence, fatal at 26 hours.
3. **Anomaly baselines with thin history.** A new job alerting on its own first
   run is the fastest way to make the feature untrusted and then ignored, which
   costs the alert channel its meaning — the #41 reasoning.

Nothing here touches the network or the clock.
"""

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import backups as _backups  # noqa: E402
from app import store as _store  # noqa: E402

HOUR = 3600
DAY = 24 * HOUR
T0 = 1_785_000_000  # a fixed instant; nothing here reads the real clock


class BackupsTestCase(unittest.TestCase):
    """`backups` resolves its path at import, so a temp CLAUDEOS_DATA only takes
    effect after a reload — which also gives each test an empty store."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CLAUDEOS_DATA"] = self.tmp
        importlib.reload(_store)
        self.b = importlib.reload(_backups)

    def tearDown(self):
        os.environ.pop("CLAUDEOS_DATA", None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        importlib.reload(_store)
        importlib.reload(_backups)

    def daily(self, **kw):
        job = self.b.add_job({"name": "nightly db", "schedule_hours": 24})
        if kw:
            self.b.update_job(job["id"], kw)
        return self.b.get_job(job["id"])


# --------------------------------------------------------------- invariant 1

class StatusTest(BackupsTestCase):
    """A job's status is a claim about safety. Every wrong answer is silent."""

    def test_a_job_that_never_ran_is_never_not_ok(self):
        """The likeliest silent failure: a job added, never wired up, sitting
        green forever having received nothing. `CONTEXT.md` sets the same rule
        for untriaged versus no_fault_found — "nobody has looked" must never
        render as "looked, nothing wrong"."""
        job = self.daily()
        st = self.b.evaluate([job], now=T0)[0]
        self.assertEqual(st["status"], "never")
        self.assertNotEqual(st["status"], "ok")

    def test_one_second_inside_the_grace_period_is_ok(self):
        job = self.daily()
        self.b.record_run(job["id"], ok=True, at=T0)
        # default grace for a 24h job is 26.4h (schedule x 1.1)
        grace_s = self.b.grace_seconds(job)
        st = self.b.evaluate([job], now=T0 + grace_s - 1)[0]
        self.assertEqual(st["status"], "ok")

    def test_one_second_outside_the_grace_period_is_stale(self):
        job = self.daily()
        self.b.record_run(job["id"], ok=True, at=T0)
        grace_s = self.b.grace_seconds(job)
        st = self.b.evaluate([job], now=T0 + grace_s + 1)[0]
        self.assertEqual(st["status"], "stale")

    def test_the_default_grace_is_the_schedule_plus_a_tenth(self):
        """26.4h for a daily job: long enough that a late run does not alert,
        short enough that a missed one is caught the same day."""
        self.assertAlmostEqual(self.b.grace_seconds(self.daily()), 24 * HOUR * 1.1)

    def test_an_explicit_grace_overrides_the_derived_one(self):
        job = self.daily(grace_hours=48)
        self.assertAlmostEqual(self.b.grace_seconds(job), 48 * HOUR)

    def test_a_reported_failure_is_failed_immediately(self):
        """A job that says it failed must not wait out its grace period — the
        answer is already known."""
        job = self.daily()
        self.b.record_run(job["id"], ok=False, at=T0, detail="tar exited 2")
        st = self.b.evaluate([job], now=T0 + 60)[0]
        self.assertEqual(st["status"], "failed")

    def test_a_later_success_clears_an_earlier_failure(self):
        job = self.daily()
        self.b.record_run(job["id"], ok=False, at=T0)
        self.b.record_run(job["id"], ok=True, at=T0 + HOUR)
        self.assertEqual(self.b.evaluate([job], now=T0 + 2 * HOUR)[0]["status"], "ok")

    def test_a_failure_after_a_success_is_failed_not_ok(self):
        job = self.daily()
        self.b.record_run(job["id"], ok=True, at=T0)
        self.b.record_run(job["id"], ok=False, at=T0 + HOUR)
        self.assertEqual(self.b.evaluate([job], now=T0 + 2 * HOUR)[0]["status"], "failed")

    def test_muted_is_reported_but_its_real_status_is_kept(self):
        """Muting hides the alert, not the fact — story 18."""
        job = self.daily(muted=True)
        self.b.record_run(job["id"], ok=True, at=T0 - 10 * DAY)
        st = self.b.evaluate([job], now=T0)[0]
        self.assertEqual(st["status"], "muted")
        self.assertEqual(st["real_status"], "stale",
                         "the underlying state must survive muting")

    def test_a_weekly_job_is_not_judged_by_a_daily_standard(self):
        weekly = self.daily(schedule_hours=168)
        self.b.record_run(weekly["id"], ok=True, at=T0)
        weekly = self.b.get_job(weekly["id"])
        self.assertEqual(self.b.evaluate([weekly], now=T0 + 3 * DAY)[0]["status"], "ok")


# --------------------------------------------------------------- invariant 2

class PersistenceTest(BackupsTestCase):
    """The defect that sank the monitors.py approach: state in module globals is
    invisible at 30s and fatal at 26h. After a restart a job either false-alarms
    or sails silently past a missed run."""

    def test_jobs_and_history_survive_a_reload(self):
        job = self.daily()
        self.b.record_run(job["id"], ok=True, at=T0, size_bytes=1000)
        self.b.record_run(job["id"], ok=True, at=T0 + DAY, size_bytes=1100)

        reloaded = importlib.reload(_backups)          # simulates a restart
        j = reloaded.get_job(job["id"])
        self.assertIsNotNone(j, "the job itself must survive")
        self.assertEqual(len(reloaded.runs(job["id"])), 2)
        self.assertEqual(reloaded.evaluate([j], now=T0 + DAY + HOUR)[0]["status"], "ok")

    def test_a_restart_does_not_resurrect_a_missed_run(self):
        """The failure mode stated plainly: a job stale before the restart is
        still stale after it, rather than being handed a fresh clock."""
        job = self.daily()
        self.b.record_run(job["id"], ok=True, at=T0)
        reloaded = importlib.reload(_backups)
        j = reloaded.get_job(job["id"])
        self.assertEqual(reloaded.evaluate([j], now=T0 + 10 * DAY)[0]["status"], "stale")

    def test_the_token_survives_and_still_resolves(self):
        job = self.daily()
        token = job["token"]
        reloaded = importlib.reload(_backups)
        self.assertEqual(reloaded.job_for_token(token)["id"], job["id"])

    def test_history_is_capped_but_keeps_the_newest(self):
        job = self.daily()
        for i in range(self.b.KEEP_RUNS + 25):
            self.b.record_run(job["id"], ok=True, at=T0 + i * HOUR, size_bytes=i)
        runs = self.b.runs(job["id"])
        self.assertEqual(len(runs), self.b.KEEP_RUNS)
        self.assertEqual(runs[-1]["size_bytes"], self.b.KEEP_RUNS + 24,
                         "the cap must drop the oldest, not the newest")

    def test_an_unreadable_store_does_not_take_the_page_down(self):
        with open(self.b.PATH, "w", encoding="utf-8") as f:
            f.write("{ truncated")
        reloaded = importlib.reload(_backups)
        self.assertEqual(reloaded.list_jobs(), [])


# --------------------------------------------------------------- invariant 3

class AnomalyTest(BackupsTestCase):
    """A heuristic that fires on thin evidence trains the owner to ignore it,
    and an ignored channel is worth less than no channel — #41's lesson."""

    def sized(self, job, sizes, start=T0):
        for i, s in enumerate(sizes):
            self.b.record_run(job["id"], ok=True, at=start + i * HOUR, size_bytes=s)

    def test_no_anomaly_before_the_minimum_history(self):
        job = self.daily()
        self.sized(job, [1000] * (self.b.MIN_BASELINE_RUNS - 1) + [5])
        st = self.b.evaluate([job], now=T0 + self.b.MIN_BASELINE_RUNS * HOUR)[0]
        self.assertEqual(st["status"], "ok",
                         "a collapse is not flagged until a baseline exists")
        self.assertFalse(st.get("baseline_ready"))

    def test_a_collapse_is_flagged_once_the_baseline_exists(self):
        job = self.daily()
        self.sized(job, [1000] * self.b.MIN_BASELINE_RUNS + [5])
        st = self.b.evaluate([job], now=T0 + (self.b.MIN_BASELINE_RUNS + 1) * HOUR)[0]
        self.assertEqual(st["status"], "anomaly")

    def test_ordinary_growth_is_not_an_anomaly(self):
        """The band is asymmetric on purpose: shrinking is the failure being
        hunted, growth is usually just growth."""
        job = self.daily()
        self.sized(job, [1000] * self.b.MIN_BASELINE_RUNS + [1800])
        st = self.b.evaluate([job], now=T0 + (self.b.MIN_BASELINE_RUNS + 1) * HOUR)[0]
        self.assertEqual(st["status"], "ok")

    def test_one_outlier_does_not_poison_the_baseline(self):
        """Median, not mean — one 40KB truncated run must not drag down the
        baseline that later runs are judged against."""
        job = self.daily()
        self.sized(job, [1000, 1000, 40, 1000, 1000, 1000, 1000])
        self.assertEqual(self.b.baseline(self.b.runs(job["id"])), 1000)

    def test_a_run_with_no_size_neither_counts_nor_breaks(self):
        """A bare `curl -X POST` ping is the documented minimum integration, so
        sizeless runs are normal and must not be treated as zero bytes."""
        job = self.daily()
        for i in range(self.b.MIN_BASELINE_RUNS + 2):
            self.b.record_run(job["id"], ok=True, at=T0 + i * HOUR)
        st = self.b.evaluate([job], now=T0 + 10 * HOUR)[0]
        self.assertEqual(st["status"], "ok")
        self.assertFalse(st.get("baseline_ready"))

    def test_a_failed_run_is_failed_even_if_its_size_looks_fine(self):
        job = self.daily()
        self.sized(job, [1000] * self.b.MIN_BASELINE_RUNS)
        self.b.record_run(job["id"], ok=False, at=T0 + 99 * HOUR, size_bytes=1000)
        st = self.b.evaluate([job], now=T0 + 100 * HOUR)[0]
        self.assertEqual(st["status"], "failed", "failure outranks a healthy size")


# ------------------------------------------------- discovery: two claims only

class _FakeProxmox:
    """Shaped from a live probe of the real cluster on 2026-07-27, including the
    detail the spec got wrong: vzdump tasks carry no per-guest id."""
    jobs = [{"id": "backup-bdf0c1a3-ab3d", "enabled": 1, "schedule": "23:00",
             "vmid": "100,101", "storage": "pbs-spark", "type": "vzdump"}]
    tasks = [{"node": "proxmox", "upid": "UPID:...", "started": T0,
              "ended": T0 + 7, "duration_s": 7, "ok": False,
              "status": "could not activate storage 'pbs-spark'"}]
    unprotected: list = []
    fail = False

    @classmethod
    def backup_jobs(cls, s):
        if cls.fail:
            raise ConnectionError("cannot reach proxmox")
        return cls.jobs

    @classmethod
    def nodes(cls, s):
        return [{"node": "proxmox"}]

    @classmethod
    def vzdump_tasks(cls, s, node, limit=50):
        return cls.tasks

    @classmethod
    def unprotected_guests(cls, s):
        return cls.unprotected

    @classmethod
    def guests(cls, s):
        return [{"vmid": 100, "name": "HAOS"}, {"vmid": 101, "name": "docker"}]


class DiscoveryTest(BackupsTestCase):
    """The spec excludes Proxmox *parsing* from testing and that stands. These
    two are not parsing — they are safety claims that fail silently."""

    def setUp(self):
        super().setUp()
        _FakeProxmox.fail = False
        _FakeProxmox.unprotected = []

    def test_a_repeated_sweep_does_not_duplicate_one_nightly_run(self):
        """The sweep runs every 30 minutes and will see the same nightly task
        dozens of times. Duplicates would silently corrupt the size baseline and
        the trend line — wrong numbers that look like data."""
        for _ in range(5):
            self.b.discover_proxmox({}, _FakeProxmox)
        job = next(j for j in self.b.list_jobs() if j.get("kind") == "proxmox")
        self.assertEqual(len(self.b.runs(job["id"])), 1)

    def test_an_unreachable_proxmox_is_not_a_clean_bill_of_health(self):
        """Story 26. Discovery raising must leave the known jobs standing, not
        quietly empty the list into something that reads as "nothing wrong"."""
        self.b.discover_proxmox({}, _FakeProxmox)
        before = len(self.b.list_jobs())
        self.assertGreater(before, 0)

        _FakeProxmox.fail = True
        with self.assertRaises(ConnectionError):
            self.b.discover_proxmox({}, _FakeProxmox)
        self.assertEqual(len(self.b.list_jobs()), before,
                         "an outage must not delete what we already knew")

    def test_a_failing_vzdump_run_is_failed_and_carries_its_error(self):
        self.b.discover_proxmox({}, _FakeProxmox)
        st = next(s for s in self.b.evaluate(self.b.list_jobs())
                  if s["kind"] == "proxmox")
        self.assertEqual(st["status"], "failed")
        self.assertIn("pbs-spark", st["detail"])

    def test_an_unprotected_guest_becomes_its_own_row(self):
        _FakeProxmox.unprotected = [{"vmid": 102, "name": "scratch"}]
        self.b.discover_proxmox({}, _FakeProxmox)
        rows = [s for s in self.b.evaluate(self.b.list_jobs())
                if s["status"] == "unprotected"]
        self.assertEqual(len(rows), 1)
        self.assertIn("102", rows[0]["name"])

    def test_a_job_removed_from_the_cluster_disappears(self):
        self.b.discover_proxmox({}, _FakeProxmox)
        original = _FakeProxmox.jobs
        try:
            _FakeProxmox.jobs = []
            self.b.discover_proxmox({}, _FakeProxmox)
            self.assertEqual([j for j in self.b.list_jobs()
                              if j.get("kind") == "proxmox"], [])
        finally:
            _FakeProxmox.jobs = original

    def test_a_heartbeat_job_is_never_removed_by_discovery(self):
        """Discovery owns discovered rows only. Deleting the owner's own job
        because a cluster sweep came back thin would destroy real config."""
        mine = self.daily()
        self.b.discover_proxmox({}, _FakeProxmox)
        original = _FakeProxmox.jobs
        try:
            _FakeProxmox.jobs = []
            self.b.discover_proxmox({}, _FakeProxmox)
            self.assertIsNotNone(self.b.get_job(mine["id"]))
        finally:
            _FakeProxmox.jobs = original


if __name__ == "__main__":
    unittest.main()
