"""Behaviour of the lab-issues module, driven through its public interface.

Every dependency it reaches for is an argument — the GitHub caller for the
sweep, the analysis run and the two tracker writes for triage — so every test
here runs against fakes: no network, no Anthropic call, no credentials. Tests
assert on what the module returns and on what it would have written to the
tracker, never on internals.

`importlib.reload` in setUp gives each test a clean module-level cache without
reaching into private state.
"""

import importlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import labissues as _labissues  # noqa: E402
from app import oplog as _oplog  # noqa: E402
from app import store as _store  # noqa: E402
from app import verdict as _verdict  # noqa: E402
from app import tools as _tools  # noqa: E402
from app.httpclient import HttpError  # noqa: E402


ISSUE = {"number": 1, "title": "Utility tumble dryer plug fails firmware updates",
         "state": "open", "labels": [], "updated_at": "2026-07-25T14:17:53Z",
         "html_url": "https://github.com/xpenno255/homelab/issues/1"}


def fake(status=200, payload=None, headers=None, raises=None):
    """A stand-in for the GitHub caller: fetch(etag) -> (status, payload, headers)."""
    calls = []

    def _fetch(etag=None):
        calls.append(etag)
        if raises is not None:
            raise raises
        return status, payload, headers or {}

    _fetch.calls = calls
    return _fetch


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.labissues = importlib.reload(_labissues)

    def test_a_successful_fetch_populates_the_snapshot(self):
        self.labissues.sweep(fetch=fake(200, [ISSUE], {"ETag": 'W/"abc"'}))

        snap = self.labissues.snapshot()
        self.assertEqual([i["number"] for i in snap["issues"]], [1])
        self.assertEqual(snap["issues"][0]["title"], ISSUE["title"])
        self.assertIsNone(snap["error"])

    def test_a_304_sends_the_validator_and_leaves_the_cache_intact(self):
        """The whole point of the ETag: an unchanged repo costs nothing and
        must not be mistaken for an empty one."""
        self.labissues.sweep(fetch=fake(200, [ISSUE], {"ETag": 'W/"abc"'}))

        unchanged = fake(304, None, {"ETag": 'W/"abc"'})
        self.labissues.sweep(fetch=unchanged)

        self.assertEqual(unchanged.calls, ['W/"abc"'], "stored ETag was not sent")
        snap = self.labissues.snapshot()
        self.assertEqual([i["number"] for i in snap["issues"]], [1])
        self.assertIsNone(snap["error"])

    def test_a_rejected_token_records_the_error_and_keeps_the_last_good_issues(self):
        """A revoked token must never read as "no open issues". It re-raises so
        the sweeper thread logs it.

        The friendly wording ("mint a new fine-grained token") is applied a
        layer below this seam, where the repo name is known — so this asserts
        the structural contract, not the prose.
        """
        self.labissues.sweep(fetch=fake(200, [ISSUE], {"ETag": 'W/"abc"'}))

        revoked = HttpError(401, "HTTP 401", "", headers={})
        with self.assertRaises(HttpError):
            self.labissues.sweep(fetch=fake(raises=revoked))

        snap = self.labissues.snapshot()
        self.assertEqual([i["number"] for i in snap["issues"]], [1], "last good issues lost")
        self.assertTrue(snap["error"], "a rejected token left no error to show")
        self.assertIsNone(snap["backoff_until"], "401 is not a rate limit")

    def test_rate_limit_headers_are_read_regardless_of_case(self):
        """HTTP/2 puts header names on the wire in lower case. Production hides
        that behind a case-insensitive HTTPMessage; a test double must not be
        the only reason the lookup works."""
        reset = int(time.time()) + 300
        exhausted = HttpError(429, "too many", "", headers={
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)})

        with self.assertRaises(HttpError):
            self.labissues.sweep(fetch=fake(raises=exhausted))

        self.assertEqual(self.labissues.snapshot()["backoff_until"], float(reset))

    def test_a_malformed_payload_degrades_without_crashing_the_sweep(self):
        """GitHub answering 200 with something that is not a list of issues —
        an error object, a truncated body — must not poison the cache."""
        self.labissues.sweep(fetch=fake(200, [ISSUE], {"ETag": 'W/"abc"'}))

        with self.assertRaises(ValueError):
            self.labissues.sweep(fetch=fake(200, {"message": "Not Found"}, {}))

        snap = self.labissues.snapshot()
        self.assertEqual([i["number"] for i in snap["issues"]], [1], "cache was poisoned")
        self.assertIn("unexpected", snap["error"].lower())

    def test_pull_requests_are_not_lab_issues(self):
        """GitHub's issues endpoint returns PRs too — they carry a
        pull_request key. Triaging a PR as a homelab problem is nonsense."""
        pr = {**ISSUE, "number": 2, "title": "Bump a dependency",
              "pull_request": {"url": "https://api.github.com/…/pulls/2"}}

        self.labissues.sweep(fetch=fake(200, [ISSUE, pr], {"ETag": 'W/"abc"'}))

        self.assertEqual([i["number"] for i in self.labissues.snapshot()["issues"]], [1])

    def test_a_rate_limited_sweep_backs_off_instead_of_hammering(self):
        """GitHub tells you when the window resets. Ignoring that and retrying
        every minute is how an integration gets banned."""
        reset = int(time.time()) + 300
        exhausted = HttpError(403, "forbidden", "", headers={
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)})

        with self.assertRaises(HttpError):
            self.labissues.sweep(fetch=fake(raises=exhausted))

        too_soon = fake(200, [ISSUE], {})
        self.labissues.sweep(fetch=too_soon)

        self.assertEqual(too_soon.calls, [], "swept again while still rate-limited")
        self.assertIn("rate limit", self.labissues.snapshot()["error"].lower())

    def test_a_304_is_a_check_not_a_change(self):
        """"Last checked" and "last changed" are different questions — a 304
        answers the first and says nothing about the second."""
        self.labissues.sweep(fetch=fake(200, [ISSUE], {"ETag": 'W/"abc"'}))
        first = self.labissues.snapshot()

        self.labissues.sweep(fetch=fake(304, None, {"ETag": 'W/"abc"'}))
        after = self.labissues.snapshot()

        self.assertGreater(after["checked"], first["checked"], "a 304 did not count as a check")
        self.assertEqual(after["changed"], first["changed"], "a 304 is not a change")

    def test_only_the_fields_the_app_needs_are_kept(self):
        """A GitHub issue object is mostly noise — reactions, timelines, the
        author's avatar. Keeping it whole bloats the cache and the API."""
        fat = {**ISSUE, "body": "the dryer plug again",
               "labels": [{"name": "claudeos:triaged", "color": "0E8A16", "id": 9}],
               "reactions": {"+1": 3}, "user": {"login": "xpenno255", "id": 1},
               "timeline_url": "https://api.github.com/…/timeline"}

        self.labissues.sweep(fetch=fake(200, [fat], {"ETag": 'W/"abc"'}))

        kept = self.labissues.snapshot()["issues"][0]
        self.assertEqual(kept["number"], 1)
        self.assertEqual(kept["title"], ISSUE["title"])
        self.assertEqual(kept["labels"], ["claudeos:triaged"], "labels should be plain names")
        for noise in ("reactions", "user", "timeline_url"):
            self.assertNotIn(noise, kept)


class IsolatedDataDirTest(unittest.TestCase):
    """Base for tests that exercise the default path, where the module resolves
    its own config and writes its own ops-log lines.

    Both resolve their paths at import, so a temp CLAUDEOS_DATA only takes
    effect after a reload — which also gives each test a clean module-level
    cache without reaching into private state.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CLAUDEOS_DATA"] = self.tmp
        self.store = importlib.reload(_store)
        importlib.reload(_oplog)
        self.labissues = importlib.reload(_labissues)
        self.tracker = Tracker()

    def tearDown(self):
        os.environ.pop("CLAUDEOS_DATA", None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        importlib.reload(_store)
        importlib.reload(_oplog)


class UnusableConfigTest(IsolatedDataDirTest):
    """The default path, where sweep resolves its own config. A sweep that
    cannot even build a request must fail *quietly and visibly*: quietly
    because the sweeper would otherwise log the same line every 60s forever,
    visibly because the UI has to say why nothing is being fetched."""

    def test_an_unconfigured_install_does_not_raise(self):
        self.labissues.sweep()
        self.assertIn("not configured", self.labissues.snapshot()["error"])

    def test_a_repo_pasted_as_a_url_does_not_raise_either(self):
        """Pasting the browser URL instead of owner/name is the obvious
        mistake, and it must not become a per-minute ops-log line."""
        self.store.save_system(
            "labissues", {"repo": "https://github.com/xpenno255/homelab", "token": "x"})

        self.labissues.sweep()

        self.assertIn("owner/name", self.labissues.snapshot()["error"])


# --------------------------------------------------------------------- triage

# The verdict a run produces: the shape the model is asked for, before ClaudeOS
# reduces it to the block that goes on the issue. Deliberately a `refuted` one
# with a mixed-status evidence list and a diagnostic remediation — the three
# things the format exists to be able to say.
VERDICT = {
    "summary": "The plug is fine. Firmware updates fail because the ZHA "
               "coordinator cannot reach it during the transfer.",
    "verdict": "refuted",
    "confidence": "medium",
    "severity": "warning",
    "refuted": ["the plug's radio has failed", "the plug is off the mesh"],
    "evidence": [
        {"tool": "get_ops_log", "status": "success",
         "note": 'the log says "transfer aborted" three times'},
        {"tool": "ha_zha_devices", "status": "no_data",
         "note": "LQI came back empty, so signal strength is unverified"},
        {"tool": "get_metric_history", "status": "truncated",
         "note": "only the last 6 hours were returned"},
        {"tool": "docker_container_logs", "status": "excluded",
         "note": "two warnings alongside belong to a different device"},
    ],
    "remediation": {"kind": "diagnostic",
                    "text": "Enable ZHA debug logging, retry the update, and check "
                            "whether the coordinator logs a timeout."},
}
SPEND = {"input": 41_200, "output": 900, "usd": 0.09123}


class Tracker:
    """A stand-in for the issue tracker: records the writes triage would make."""

    def __init__(self, comment_raises=None, label_raises=None):
        self.comments, self.labels = [], []
        self._comment_raises = comment_raises
        self._label_raises = label_raises

    def comment(self, number, body):
        self.comments.append((number, body))
        if self._comment_raises is not None:
            raise self._comment_raises
        return {"html_url": f"https://github.com/x/y/issues/{number}#issuecomment-9"}

    def label(self, number, names):
        if self._label_raises is not None:
            raise self._label_raises
        self.labels.append((number, list(names)))

    @property
    def labelled(self) -> list:
        return [n for n, _ in self.labels]

    @property
    def body(self) -> str:
        return self.comments[-1][1]


def analysis(verdict=None, spend=None, raises=None):
    """A stand-in for the agentic run: analyse(issue) -> (verdict, spend)."""
    def _run(issue):
        if raises is not None:
            raise raises
        return dict(verdict or VERDICT), dict(spend or SPEND)
    return _run


class TriageTest(IsolatedDataDirTest):

    def run_triage(self, **kw):
        kw.setdefault("issue", ISSUE)
        kw.setdefault("analyse", analysis())
        kw.setdefault("comment", self.tracker.comment)
        kw.setdefault("label", self.tracker.label)
        return self.labissues.triage(kw.pop("number", 1), **kw)

    # ------------------------------------------------------------ round-trip

    def test_the_machine_block_round_trips(self):
        """The comment is the only place a verdict is stored, so what parses back
        out of it has to be what went in. Anything that silently changes here —
        a dropped field, a coerced enum — is a verdict the UI will misreport."""
        result = self.run_triage()

        parsed = _verdict.parse_verdict(self.tracker.body)
        self.assertEqual(parsed, result["verdict"], "block did not survive the trip")
        self.assertEqual(parsed["verdict"], "refuted")
        self.assertEqual(parsed["confidence"], "medium")
        self.assertEqual(parsed["severity"], "warning")
        self.assertEqual(parsed["refuted"], VERDICT["refuted"])
        self.assertEqual([e["status"] for e in parsed["evidence"]],
                         ["success", "no_data", "truncated", "excluded"])
        self.assertEqual(parsed["remediation"]["kind"], "diagnostic")
        self.assertEqual(parsed["cost"]["usd"], SPEND["usd"])

    def test_all_four_verdicts_survive_and_stay_distinct(self):
        """Four values exist because a refuted hypothesis is a result and
        "looked, found nothing" is not "could not tell". They are only worth
        having if each one comes back as itself."""
        seen = []
        for value in ("diagnosed", "refuted", "inconclusive", "no_fault_found"):
            tracker = Tracker()
            self.run_triage(analyse=analysis({**VERDICT, "verdict": value}),
                            comment=tracker.comment, label=tracker.label)
            seen.append(_verdict.parse_verdict(tracker.body)["verdict"])

        self.assertEqual(seen, ["diagnosed", "refuted", "inconclusive", "no_fault_found"])

    def test_a_note_that_closes_the_comment_early_still_round_trips(self):
        """Evidence notes quote log lines, and a log line can contain `-->` —
        which would end the HTML comment early and spill the JSON onto the issue
        for a human to read."""
        note = 'the log says "state -->  unavailable"'
        verdict = {**VERDICT, "evidence": [{"tool": "get_ops_log",
                                            "status": "success", "note": note}]}

        self.run_triage(analyse=analysis(verdict))

        self.assertNotIn("-->", self.tracker.body.split("<!--")[1].rsplit("-->", 1)[0],
                         "an unescaped --> would close the block early")
        parsed = _verdict.parse_verdict(self.tracker.body)
        self.assertEqual(parsed["evidence"][0]["note"], note)

    # --------------------------------------------------------- the failure path

    def test_a_label_write_that_fails_is_reported_not_raised(self):
        """The marker is the one write that must not fail silently: unmarked
        means #36 re-triages this issue, and pays for it, on every sweep. It
        cannot be forced, so it must be loud — and it must not raise, because
        the run happened and the verdict is already posted."""
        t = Tracker(label_raises=ConnectionError("GitHub refused the label (403)"))

        out = self.labissues.triage(1, issue=dict(ISSUE), analyse=analysis(),
                                    comment=t.comment, label=t.label)

        self.assertEqual(len(t.comments), 1, "the verdict should still be posted")
        self.assertFalse(out["labelled"], "an unwritten marker was reported as written")
        self.assertIn("403", out["unlabelled"])

    def test_cost_output_is_summed_across_the_whole_run(self):
        """input and usd accumulate over the run; output must too, or the
        recorded figures cannot be reconciled against each other."""
        t = Tracker()
        spend = {"input": 40_000, "output": 1_500, "usd": 0.21}

        out = self.labissues.triage(1, issue=dict(ISSUE),
                                    analyse=analysis(spend=spend),
                                    comment=t.comment, label=t.label)

        self.assertEqual(out["verdict"]["cost"]["output"], 1_500)

    def test_a_run_that_dies_mid_way_still_marks_the_issue(self):
        """The retry storm this feature must not reproduce: an unmarked failure
        is picked up, and paid for, on every sweep from then on. The marker is
        the thing that has to survive a failure — not the verdict."""
        died = self.labissues.TriageFailed("APIConnectionError: connection reset",
                                           {"input": 9_000, "output": 120, "usd": 0.02})

        result = self.run_triage(analyse=analysis(raises=died))

        self.assertEqual(self.tracker.labelled, [1], "a failed run left the issue unmarked")
        self.assertEqual(self.tracker.labels[0][1], [self.labissues.TRIAGED_LABEL])
        self.assertFalse(result["ok"])
        self.assertIn("connection reset", result["error"])

    def test_an_unexpected_failure_marks_the_issue_too(self):
        """Not just the failure the run knows how to report — anything at all.
        A bug in the analysis path must not be the reason an issue re-triages
        forever."""
        result = self.run_triage(analyse=analysis(raises=RuntimeError("boom")))

        self.assertEqual(self.tracker.labelled, [1])
        self.assertIn("boom", result["error"])

    def test_a_failed_run_still_reports_what_it_spent(self):
        """A run that died after twenty tool calls cost money. Discarding that
        understates the feature's cost and leaves a per-day ledger unbuildable
        from what the issue records."""
        died = self.labissues.TriageFailed("stopped on the context budget",
                                           {"input": 96_000, "output": 400, "usd": 0.21})

        self.run_triage(analyse=analysis(raises=died))

        cost = _verdict.parse_verdict(self.tracker.body)["cost"]
        self.assertEqual(cost["usd"], 0.21)
        self.assertEqual(cost["input"], 96_000)

    def test_a_failed_run_reads_back_as_inconclusive_with_its_reason(self):
        """A failure is not a diagnosis and must never parse as one. It is the
        one verdict ClaudeOS writes itself, so it says why in the block as well
        as in the prose."""
        died = self.labissues.TriageFailed("Claude declined to answer this request.")

        self.run_triage(analyse=analysis(raises=died))

        parsed = _verdict.parse_verdict(self.tracker.body)
        self.assertEqual(parsed["verdict"], "inconclusive")
        self.assertEqual(parsed["confidence"], "low")
        self.assertIn("declined", parsed["error"])
        self.assertIn("could not finish", self.tracker.body)

    def test_the_marker_goes_on_even_when_the_comment_does_not(self):
        """Losing the verdict costs one run. Losing the marker costs a run per
        sweep, forever — so the comment failing does not stop the labelling."""
        result = self.run_triage(comment=Tracker(
            comment_raises=ConnectionError("GitHub refused the request (403)")).comment)

        self.assertEqual(self.tracker.labelled, [1])
        self.assertTrue(result["labelled"])
        self.assertIn("403", result["unposted"])

    # ------------------------------------------------------- degrading gracefully

    def test_a_comment_with_no_machine_block_is_not_a_crash(self):
        """Most comments on a lab issue are written by a human. Reading one has
        to answer "no verdict here", not raise."""
        for body in ("just tried it again and it worked", "", None,
                     "<!-- claudeos-triage not json at all -->",
                     "<!-- claudeos-triage {\"v\": 1, truncated",
                     "<!-- claudeos-triage [1, 2, 3] -->",
                     "<!-- claudeos-triage {\"v\": 99, \"verdict\": \"diagnosed\"} -->"):
            with self.subTest(body=body):
                self.assertIsNone(_verdict.parse_verdict(body))

    def test_a_verdict_the_model_invented_lands_on_the_cautious_answer(self):
        """The vocabularies are closed and the source is a language model. An
        unrecognised verdict has to read as "could not tell" — never as a
        diagnosis the UI will show as settled."""
        self.run_triage(analyse=analysis({
            **VERDICT, "verdict": "probably_the_router", "confidence": "certain",
            "severity": "apocalyptic", "sabotage": True,
            "evidence": [{"tool": "get_ops_log", "status": "made_up", "note": "x"}],
            "remediation": {"kind": "reboot_everything", "text": "turn it off"}}))

        parsed = _verdict.parse_verdict(self.tracker.body)
        self.assertEqual(parsed["verdict"], "inconclusive")
        self.assertEqual(parsed["confidence"], "low")
        self.assertEqual(parsed["severity"], "info")
        self.assertEqual(parsed["evidence"][0]["status"], "excluded")
        self.assertEqual(parsed["remediation"]["kind"], "none")
        self.assertNotIn("sabotage", parsed, "an invented field reached the block")

    def test_the_remediation_is_never_executable(self):
        """ClaudeOS does not run the remediation, so that flag is stamped here
        and is not the model's claim to make."""
        self.run_triage(analyse=analysis({
            **VERDICT, "remediation": {"kind": "fix", "text": "restart the coordinator",
                                       "executable": True}}))

        parsed = _verdict.parse_verdict(self.tracker.body)
        self.assertEqual(parsed["remediation"]["kind"], "fix")
        self.assertIs(parsed["remediation"]["executable"], False)

    # ------------------------------------------------------------ the comment

    def test_the_human_reads_prose_and_never_sees_the_block(self):
        self.run_triage()

        prose, _, hidden = self.tracker.body.partition("<!--")
        self.assertIn("coordinator cannot reach it", prose)
        self.assertNotIn("no_fault_found", prose, "vocabulary leaked into the prose")
        self.assertTrue(hidden.strip().endswith("-->"))

    def test_a_run_that_returns_no_summary_still_posts_something(self):
        self.run_triage(analyse=analysis({**VERDICT, "summary": "   "}))

        self.assertTrue(self.tracker.body.strip())
        self.assertIsNotNone(_verdict.parse_verdict(self.tracker.body))

    # ---------------------------------------------------------- preconditions

    def test_without_the_sdk_triage_is_a_precondition_not_a_crash(self):
        """The agentic loop needs the SDK. Plain system python must be told so in
        words, the way chat already is — not fail somewhere inside a run."""
        self.labissues.HAS_SDK = False

        with self.assertRaises(LookupError) as caught:
            self.labissues.triage(1, comment=self.tracker.comment,
                                  label=self.tracker.label, issue=ISSUE)

        self.assertIn("anthropic sdk", str(caught.exception).lower())
        self.assertEqual(self.tracker.labels, [], "a refused precondition wrote to GitHub")

    def test_a_nonsense_issue_number_is_refused_before_anything_is_written(self):
        for number in ("not-a-number", 0, -3):
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    self.run_triage(number=number)
        self.assertEqual(self.tracker.labels, [])


# ------------------------------------------------------------------- the run

class FakeUsage:
    input_tokens = 1_200
    output_tokens = 90
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 4_000


class FakeBlock:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeReply:
    def __init__(self, content, stop_reason="end_turn"):
        self.content, self.stop_reason, self.usage = content, stop_reason, FakeUsage()


class FakeStream:
    def __init__(self, reply):
        self._reply = reply

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(())

    def get_final_message(self):
        return self._reply


class FakeMessages:
    def __init__(self, replies, calls):
        self._replies, self.calls = list(replies), calls

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self._replies.pop(0))


class FakeClient:
    """A stand-in for the Anthropic client: scripted replies, recorded requests."""

    def __init__(self, replies):
        self.calls: list = []
        self.messages = FakeMessages(replies, self.calls)


class TriageRunTest(IsolatedDataDirTest):
    """The default run, driven through triage with a fake client in place of the
    API. Covers the two properties that are safety claims rather than
    conveniences: no write tool is ever offered, and the run ends in data."""

    def test_the_run_offers_no_write_tool_and_ends_in_a_parsed_verdict(self):
        # A tool call, then prose (the ordinary ending — the model has stopped
        # asking for tools well before the ceiling), then the structured answer.
        # The tool name is deliberately unknown, so dispatch returns an error
        # envelope instead of touching the lab.
        client = FakeClient([
            FakeReply([FakeBlock(type="tool_use", name="no_such_tool",
                                 input={}, id="tu_1")], stop_reason="tool_use"),
            FakeReply([FakeBlock(type="text", text="I think the plug is fine.")]),
            FakeReply([FakeBlock(type="text", text=json.dumps(VERDICT))]),
        ])

        result = self.labissues.triage(1, issue=ISSUE, client=client,
                                       comment=self.tracker.comment,
                                       label=self.tracker.label)

        writes = {t["name"] for t in _tools.WRITE_TOOLS}
        offered = {t["name"] for call in client.calls for t in call.get("tools", [])}
        self.assertEqual(offered & writes, set(), "a write tool was offered to the model")
        self.assertTrue(offered, "no tools were offered at all")

        # The final call drops tools and asks for the schema instead.
        final = client.calls[-1]
        self.assertNotIn("tools", final, "the final call could still request a tool")
        self.assertEqual(final["output_config"]["format"]["schema"],
                         _verdict.VERDICT_SCHEMA)
        for call in client.calls[:-1]:
            self.assertNotIn("format", call["output_config"],
                             "a mid-run call asked for the verdict schema")

        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(_verdict.parse_verdict(self.tracker.body)["verdict"],
                         "refuted")
        self.assertEqual(self.tracker.labelled, [1])

    def test_an_api_failure_becomes_a_marked_issue_not_an_exception(self):
        """The loop reports a dead API as a terminal event with the run's
        accounting, and triage has to turn that into a marked issue and a
        readable comment — not an exception nobody is there to catch."""
        class DeadClient:
            class messages:  # noqa: N801
                @staticmethod
                def stream(**kwargs):
                    raise RuntimeError("the API is down")

        result = self.labissues.triage(1, issue=ISSUE, client=DeadClient(),
                                       comment=self.tracker.comment,
                                       label=self.tracker.label)

        self.assertFalse(result["ok"])
        self.assertIn("the API is down", result["error"])
        self.assertEqual(self.tracker.labelled, [1], "a dead API left the issue unmarked")
        self.assertEqual(
            _verdict.parse_verdict(self.tracker.body)["verdict"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
