"""The weekly report's schedule: a failure must not become a billed retry loop.

`CLAUDE.md` sets the bar for a test seam at failure modes that are **silent and
expensive**. This is the module that defined the phrase. A scheduled report whose
AI call raised never advanced its schedule, so the five-minute scheduler
re-attempted the same slot for the rest of the week — up to 2,016 times (#27) —
and the failure that loops deterministically is also the one that gets billed:
truncation generates a full output cap every attempt. At ~$0.43 a call that is
roughly $870 for one week, with nothing but a growing pile of identical ops-log
lines to show for it.

So the tests here are about *not spending money*. `generate()` takes its snapshot,
its analysis call and its clock as arguments; nothing below touches the network,
an API key, or the real time. Only the schedule is under test — the digest's
contents, the prompt and the notification are not.
"""

import datetime
import importlib
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

from app import reports as _reports  # noqa: E402
from app import store as _store  # noqa: E402


REPORT = {"grade": "A", "summary": "all quiet", "highlights": [], "findings": []}


def snapshot():
    """Stands in for collect() — no connectors, no network."""
    return {"stub": True}


def working(calls=None):
    def _analyse(_data):
        if calls is not None:
            calls.append(1)
        return dict(REPORT)
    return _analyse


def broken(calls=None, error=None):
    """The expensive failure: output was generated and billed, then rejected.

    `ValueError` is the real class for a truncated or refused analysis — the
    branch that costs a full output cap every single time it is retried.
    """
    def _analyse(_data):
        if calls is not None:
            calls.append(1)
        raise error or ValueError("analysis was truncated — try again")
    return _analyse


class ScheduleTest(unittest.TestCase):
    """`reports` resolves its path at import, so a temp CLAUDEOS_DATA only takes
    effect after a reload — which also gives each test an empty report store."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CLAUDEOS_DATA"] = self.tmp
        importlib.reload(_store)
        self.reports = importlib.reload(_reports)
        # A slot that has already passed: today's weekday, an hour ago.
        self.now = datetime.datetime.now().replace(minute=30, second=0, microsecond=0)
        self.reports.set_config({"enabled": True, "day": self.now.weekday(),
                                 "hour": max(0, self.now.hour - 1)})

    def tearDown(self):
        restore_data_dir()
        shutil.rmtree(self.tmp, ignore_errors=True)
        importlib.reload(_store)

    # ------------------------------------------------------------- helpers

    def clock(self, minutes=0):
        return (self.now + datetime.timedelta(minutes=minutes)).timestamp()

    def config(self):
        return self.reports.get_state()["config"]

    def due(self, minutes=0):
        return self.reports._due(self.config(), self.clock(minutes))

    def run_scheduled(self, analyse, minutes=0):
        """One scheduled run, as the scheduler would make it."""
        try:
            return self.reports.generate("scheduled", snapshot=snapshot,
                                         analyse=analyse, now=self.clock(minutes))
        except Exception:
            return None

    def drain(self, analyse, days=6):
        """Every tick the scheduler would take over `days`, honouring `_due()`.

        Six days by default, not seven: at seven the *next* weekly slot arrives
        and correctly gets a retry budget of its own, so a longer window measures
        two slots and reads as a regression when it is not one.
        """
        fired = []
        tick_minutes = self.reports.TICK / 60
        for i in range(1, int(days * 24 * 60 / tick_minutes) + 1):
            at = i * tick_minutes
            if self.due(at):
                self.run_scheduled(analyse, at)
                fired.append(at)
        return fired

    # ------------------------------------------------ the storm, and its bound

    def test_a_failed_scheduled_run_does_not_leave_the_slot_due(self):
        """The whole bug in one assertion: before the fix, `last_run` was written
        only after a successful analysis, so the slot stayed outstanding and the
        next tick five minutes later tried again."""
        self.assertTrue(self.due(), "the slot should start out due")

        self.run_scheduled(broken())

        self.assertFalse(self.due(1), "a failed run left the slot due")

    def test_a_slot_of_ticks_costs_at_most_the_attempt_bound(self):
        """The headline. 12 ticks an hour is 1,728 opportunities over six days to
        re-run a deterministic failure; at ~$0.43 a call that is ~$740 for this
        one slot alone."""
        attempts = []

        self.drain(broken(attempts))

        self.assertLessEqual(len(attempts), self.reports.MAX_ATTEMPTS,
                             f"{len(attempts)} paid attempts against one slot")
        self.assertEqual(len(attempts), self.reports.MAX_ATTEMPTS,
                         "it should still use its full retry budget")

    def test_attempts_are_spaced_out_rather_than_fired_back_to_back(self):
        """Retrying is for transients — a rate limit or a blip. Three calls in
        fifteen minutes would not outlast either."""
        self.run_scheduled(broken())

        self.assertFalse(self.due(self.reports.RETRY_AFTER / 60 - 1),
                         "retried before the backoff elapsed")
        self.assertTrue(self.due(self.reports.RETRY_AFTER / 60 + 1),
                        "never retried at all — a transient failure loses the week")

    def test_the_bound_survives_a_restart(self):
        """The attempt count is the only thing standing between a permanent
        failure and the storm, so it cannot live in memory — a crash-loop would
        reset it every time the process came back."""
        for i in range(self.reports.MAX_ATTEMPTS):
            self.run_scheduled(broken(), minutes=i * self.reports.RETRY_AFTER / 60)
        self.assertFalse(self.due(999))

        self.reports = importlib.reload(_reports)

        self.assertFalse(self.due(999), "the attempt bound was lost on reload")

    def test_the_next_weekly_slot_re_arms(self):
        """The fix must suppress the retry, not the feature."""
        self.drain(broken(), days=1)
        self.assertFalse(self.due(60 * 24), "still retrying the old slot")

        self.assertTrue(self.due(60 * 24 * 7 + 60),
                        "a new week never became due — reports are off forever")

    # ------------------------------------------------------- the success path

    def test_a_successful_scheduled_run_still_advances_the_schedule(self):
        self.run_scheduled(working())

        self.assertFalse(self.due(1))
        self.assertGreater(self.config()["last_run"], 0)

    def test_a_success_clears_the_attempt_state_it_inherited(self):
        """Two failures then a success: the next slot must start from a clean
        count, or a single later failure would exhaust the budget immediately."""
        self.run_scheduled(broken())
        self.run_scheduled(broken(), minutes=self.reports.RETRY_AFTER / 60 + 1)

        self.run_scheduled(working(), minutes=self.reports.RETRY_AFTER / 30 + 2)

        cfg = self.config()
        self.assertEqual(cfg["attempts"], 0)
        self.assertIsNone(cfg["last_error"], "a resolved failure still shows in the UI")

    # -------------------------------------------------------- the manual path

    def test_a_manual_run_does_not_touch_the_schedule(self):
        """The run-now control deliberately leaves the schedule alone. A fix that
        advanced it for every caller would mean one manual run suppresses that
        week's scheduled report — the same bug, inverted."""
        self.reports.generate("manual", snapshot=snapshot, analyse=working(),
                              now=self.clock())

        self.assertEqual(self.config()["last_run"], 0)
        self.assertTrue(self.due(1), "a manual run suppressed the scheduled report")

    def test_a_failed_manual_run_does_not_consume_the_retry_budget(self):
        """Pressing the button and getting an error is not the scheduler's
        problem, and must not eat the week's automatic attempts."""
        with self.assertRaises(ValueError):
            self.reports.generate("manual", snapshot=snapshot, analyse=broken(),
                                  now=self.clock())

        self.assertEqual(self.config()["attempts"], 0)
        self.assertTrue(self.due(1))

    # ----------------------------------------------------------- visibility

    def test_a_failure_is_visible_beyond_a_single_log_line(self):
        """A report that never generates cannot carry its own failure into the
        weekly digest, which is where warnings are normally read."""
        self.run_scheduled(broken(error=ValueError("the model declined")))

        err = self.config()["last_error"]
        self.assertIn("the model declined", err["message"])
        self.assertEqual(err["attempt"], 1)

    def test_saving_the_schedule_preserves_the_bookkeeping(self):
        """`set_config` rebuilds the record from the caller's payload, so anything
        it forgets to carry forward is destroyed by an ordinary schedule edit —
        and dropping `last_run` would make the current slot due all over again.
        Exactly the shape of #39, in a different module."""
        self.run_scheduled(working())
        before = self.config()

        self.reports.set_config({"enabled": True, "day": 3, "hour": 9})

        after = self.config()
        self.assertEqual(after["last_run"], before["last_run"])
        self.assertEqual(after["last_attempt"], before["last_attempt"])
        self.assertEqual(after["day"], 3, "the edit itself must still apply")

    def test_a_pending_failure_survives_a_schedule_edit_too(self):
        self.run_scheduled(broken())

        self.reports.set_config({"enabled": True, "day": 3, "hour": 9})

        self.assertIsNotNone(self.config()["last_error"])
        self.assertEqual(self.config()["attempts"], 1)


class MeterTest(unittest.TestCase):
    """The meter, for one reason: **a wrong rate is invisible.**

    A retry loop announces itself — the ops log fills up and the bill arrives.
    Mispricing does not. The report is the single most expensive call this app
    makes (one Opus call over a 120k-character snapshot), and it went unmetered
    entirely until #60. The specific trap that ticket names is reusing
    `toolloop._PRICES`, which is Sonnet: that constant is right there, one import
    away, applies to the same `_usage` shape, and produces a number that looks
    entirely plausible while being roughly an order of magnitude too low. Nothing
    at runtime would ever contradict it.

    So what is pinned here is the arithmetic and the *absence* of a number —
    never the schedule, which the class above owns. `generate()`'s analysis seam
    means none of it costs anything to run.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CLAUDEOS_DATA"] = self.tmp
        importlib.reload(_store)
        self.reports = importlib.reload(_reports)

    def tearDown(self):
        restore_data_dir()
        shutil.rmtree(self.tmp, ignore_errors=True)
        importlib.reload(_store)

    def generate(self, usage):
        def _analyse(_data):
            r = dict(REPORT)
            if usage is not None:
                r["_usage"] = usage
            return r
        return self.reports.generate("manual", snapshot=snapshot, analyse=_analyse,
                                     now=1_784_220_000.0)

    def test_a_report_is_priced_at_the_opus_rate_not_the_sonnet_one(self):
        """The headline. 100k in / 10k out is $0.75 on Opus ($5/$25 per MTok) and
        $0.40 on Sonnet's introductory rate — so the wrong constant does not fail,
        it just quietly halves the number and keeps doing so forever."""
        report = self.generate({"input_tokens": 100_000, "output_tokens": 10_000})

        self.assertAlmostEqual(report["cost"]["usd"], 0.75, places=6)
        self.assertEqual(report["cost"]["input"], 100_000)
        self.assertEqual(report["cost"]["output"], 10_000)

    def test_the_rate_is_not_the_sonnet_rate_by_accident(self):
        """Guards the constant itself rather than a call through it: an edit that
        points this at `toolloop._PRICES` passes every other test here."""
        from app import toolloop

        self.assertNotEqual(self.reports.OPUS_USD_PER_MTOK, toolloop._PRICES["intro"])
        self.assertNotEqual(self.reports.OPUS_USD_PER_MTOK, toolloop._PRICES["list"])

    def test_the_model_the_price_was_checked_against_is_recorded(self):
        """A hand-checked rate outlives the model it was checked against, so the
        report says which one it priced — otherwise a future tier change is
        undetectable after the fact.

        This is the fallback path: a usage block that names no model is priced
        against the configured one. Accurate rather than a guess — pricing only
        ever happens at generation time, so the constant is the model that just
        ran. A *view* reading old data has no such guarantee, which is why it says
        "model unknown" instead of borrowing this fallback (#62).
        """
        report = self.generate({"input_tokens": 10, "output_tokens": 1})

        self.assertEqual(report["cost"]["model"], _reports.ai.MODEL)

    def test_the_cost_records_the_model_that_actually_ran(self):
        """And when the usage *does* name one, that wins over the constant.

        A stored report outlives the constant that produced it. Pricing against
        `ai.MODEL` would relabel every past report with whatever is configured at
        the moment of pricing — across a version bump, a claim that is simply
        untrue about output that already exists (#62).
        """
        report = self.generate({"input_tokens": 10, "output_tokens": 1,
                                "model": "claude-opus-4-8"})

        self.assertEqual(report["cost"]["model"], "claude-opus-4-8")
        self.assertNotEqual(report["cost"]["model"], _reports.ai.MODEL,
                            "the reported model was ignored in favour of the constant")

    def test_an_unmeasured_report_costs_none_not_zero(self):
        """Zero is a claim that the call was free. None is a claim that nobody
        looked — which is true, and is the difference between a total that is
        wrong and a total that says it is incomplete."""
        report = self.generate(None)

        self.assertIsNone(report["cost"])

    def test_a_malformed_usage_block_does_not_become_a_price(self):
        """`_usage` comes from whichever of `ai.py`'s two paths ran, and the raw
        fallback reads its numbers out of a JSON body that can carry nulls."""
        report = self.generate({"input_tokens": None, "output_tokens": 2594})

        self.assertIsNone(report["cost"])

    def test_the_total_says_how_much_of_it_is_unmeasured(self):
        """A sum over partly-unpriced history is the failure mode this whole
        module exists to avoid: it looks like an answer."""
        self.generate({"input_tokens": 100_000, "output_tokens": 10_000})
        self.generate(None)

        spend = self.reports.get_state()["spend"]
        self.assertAlmostEqual(spend["usd"], 0.75, places=6)
        self.assertEqual(spend["reports"], 1, "the unpriced report must not be counted")
        self.assertEqual(spend["unmetered"], 1, "…but it must be admitted to")


if __name__ == "__main__":
    unittest.main()
