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
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Imported before `app`: this sets CLAUDEOS_DATA, and `store` binds
# DATA_DIR at import — after it, the redirect is too late (#66).
from tests import restore_data_dir

from app import labissues as _labissues  # noqa: E402
from app import oplog as _oplog  # noqa: E402
from app import store as _store  # noqa: E402
from app import triagelog as _triagelog  # noqa: E402
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
        self.triagelog = importlib.reload(_triagelog)
        self.labissues = importlib.reload(_labissues)
        self.tracker = Tracker()

    def tearDown(self):
        restore_data_dir()
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

    def test_a_reply_truncated_mid_tool_call_does_not_poison_the_transcript(self):
        """The first live run died on this. A reply can hit the token cap while
        it is still emitting tool calls: stop_reason is max_tokens, but the
        content carries tool_use blocks the loop will never dispatch. Left in
        the transcript they are unpaired, and the API rejects the next request
        with "tool_use ids were found without tool_result blocks"."""
        client = FakeClient([
            FakeReply([FakeBlock(type="text", text="checking the mesh"),
                       FakeBlock(type="tool_use", name="ha_zha_devices", input={}, id="tu_a"),
                       FakeBlock(type="tool_use", name="ha_error_log", input={}, id="tu_b")],
                      stop_reason="max_tokens"),
            FakeReply([FakeBlock(type="text", text=json.dumps(VERDICT))]),
        ])

        result = self.labissues.triage(1, issue=ISSUE, client=client,
                                       comment=self.tracker.comment,
                                       label=self.tracker.label)

        sent = [b for call in client.calls for m in call["messages"]
                for b in (m["content"] if isinstance(m["content"], list) else [])]
        unpaired = [b for b in sent
                    if (b.get("type") if isinstance(b, dict) else getattr(b, "type", None))
                    == "tool_use"]
        self.assertEqual(unpaired, [], "an undispatched tool_use reached the wire")
        self.assertTrue(result["ok"], f"the run failed: {result['error']}")
        self.assertEqual(result["verdict"]["verdict"], VERDICT["verdict"])

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


class RunTriageTest(IsolatedDataDirTest):
    """`run_triage()` — the composition every caller wants around `triage()`:
    one run at a time, and the verdict kept where the queue can read it.

    Both halves are here rather than in `triage()` because that function's
    contract says the caller owns concurrency and selection — the automatic
    sweep in #36 will want the same composition, and writing it twice is how the
    two would drift.
    """

    def dispatch(self, **kw):
        kw.setdefault("issue", ISSUE)
        kw.setdefault("analyse", analysis())
        kw.setdefault("comment", self.tracker.comment)
        kw.setdefault("label", self.tracker.label)
        return self.labissues.run_triage(kw.pop("number", 1), **kw)

    # ------------------------------------------------------------ the record

    def test_a_finished_run_is_readable_as_a_queue_summary(self):
        """Without this the queue can only show that a label exists — "somebody
        looked" — and has to re-read GitHub to recover a verdict this process
        produced itself a minute earlier."""
        self.dispatch()

        summary = self.triagelog.summaries()["1"]
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["verdict"], "refuted")
        self.assertEqual(summary["severity"], "warning")
        self.assertEqual(summary["confidence"], "medium")
        self.assertEqual(summary["refuted"], len(VERDICT["refuted"]))
        self.assertEqual(summary["usd"], SPEND["usd"])

    def test_a_failed_run_is_recorded_as_well(self):
        """"The machinery broke" is a state the queue has to render, and it is
        not "nobody has looked". Recording only successes would leave a failed
        run looking untriaged, under a live trigger button that spends again."""
        self.dispatch(analyse=analysis(raises=self.labissues.TriageFailed("the API is down")))

        summary = self.triagelog.summaries()["1"]
        self.assertFalse(summary["ok"])
        self.assertIn("the API is down", summary["error"])

    def test_a_verdict_posted_without_its_label_is_visible_in_the_summary(self):
        """An unmarked issue is eligible again, so it will be re-triaged and
        re-paid for. That cannot be a detail only the ops log knows."""
        tracker = Tracker(label_raises=ConnectionError("403"))

        self.dispatch(comment=tracker.comment, label=tracker.label)

        self.assertIs(self.triagelog.summaries()["1"]["labelled"], False)

    def test_re_triaging_replaces_the_earlier_record(self):
        """Removing the label is the documented way to ask for another look, and
        what the queue must then show is the new answer, not the old one."""
        self.dispatch()
        self.dispatch(analyse=analysis({**VERDICT, "verdict": "diagnosed",
                                        "severity": "critical"}))

        self.assertEqual(self.triagelog.summaries()["1"]["verdict"], "diagnosed")
        self.assertEqual(len(self.triagelog.summaries()), 1)

    def test_the_summary_leaves_the_evidence_behind(self):
        """The queue polls every 30 seconds and one record can carry kilobytes
        of evidence notes. A row renders a verdict, a severity and a count."""
        self.dispatch()

        self.assertNotIn("evidence", self.triagelog.summaries()["1"])
        self.assertEqual(len(self.triagelog.get(1)["verdict"]["evidence"]),
                         len(VERDICT["evidence"]),
                         "the full record must keep what the summary drops")

    def test_an_unreadable_record_file_does_not_take_the_queue_down(self):
        """These records are a convenience; GitHub holds the real verdict. A
        half-written file costs a re-read, and must not stop the page loading."""
        with open(self.triagelog.PATH, "w", encoding="utf-8") as f:
            f.write('{"runs": {"1": ')      # truncated mid-write

        self.assertEqual(self.triagelog.summaries(), {})
        self.dispatch()
        self.assertEqual(self.triagelog.summaries()["1"]["verdict"], "refuted")

    # ------------------------------------------------------------- the slot

    def test_a_second_run_is_refused_while_one_is_in_flight(self):
        """Two concurrent runs are two unattended agentic passes billed in
        parallel, against a lab one of them may be misreading because the other
        is mid-investigation. The refusal is what the queue turns into a
        waiting row."""
        started, release, done = threading.Event(), threading.Event(), []

        def slow(issue):
            started.set()
            release.wait(5)
            return dict(VERDICT), dict(SPEND)

        first = threading.Thread(target=lambda: done.append(self.dispatch(analyse=slow)))
        first.start()
        try:
            self.assertTrue(started.wait(5), "the first run never started")
            self.assertEqual(self.labissues.running()["number"], 1,
                             "the run in flight must be nameable, for the other tab")

            second = Tracker()
            with self.assertRaises(ValueError) as caught:
                self.dispatch(number=2, issue={**ISSUE, "number": 2},
                              comment=second.comment, label=second.label)

            self.assertIn("already in progress", str(caught.exception))
            self.assertEqual(second.comments, [], "the refused run still posted a comment")
            self.assertEqual(second.labelled, [], "the refused run still marked an issue")
            self.assertNotIn("2", self.triagelog.summaries(),
                             "a run that never happened must not leave a record")
        finally:
            release.set()
            first.join(5)

        self.assertTrue(done and done[0]["ok"])
        self.assertIsNone(self.labissues.running()["number"],
                          "the slot was not released after the run")

    def test_the_slot_is_released_when_a_run_raises(self):
        """A precondition that fails part-way must not wedge triage for the life
        of the process — the next attempt has to be able to start."""
        with self.assertRaises(LookupError):
            self.dispatch(issue=None, comment=None, label=None)   # unconfigured

        self.assertIsNone(self.labissues.running()["number"])
        self.assertTrue(self.dispatch()["ok"], "the slot stayed held after a failure")


# ---------------------------------------------------------------- #36: the sweep

def issues(*specs):
    """Open issues in the cache's shape.

    `specs` are `(number, labels, updated)`, with an optional fourth element
    overriding `created_at` for the age-based tests.
    """
    out = []
    for spec in specs:
        n, labels, updated = spec[0], spec[1], spec[2]
        # Minutes old by default, lower numbers older — so "oldest first" still
        # means issue #1 while nothing is old enough to count as a backlog.
        default = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                time.gmtime(time.time() - (60 - n) * 60))
        created = spec[3] if len(spec) > 3 else default
        out.append({"number": n, "title": f"issue {n}", "state": "open",
                    "labels": list(labels), "body": "", "created_at": created,
                    "updated_at": updated or created,
                    "html_url": f"https://github.com/x/y/issues/{n}"})
    return out


class AutoTriageTest(IsolatedDataDirTest):
    """The unattended sweep. Every test here is about something that costs money
    when it goes wrong, which is why this module has a seam at all."""

    def setUp(self):
        super().setUp()
        self.ran = []

    def runner(self, cost=0.40, ok=True):
        """A stand-in for a triage run: records the issue and what it spent."""
        def _run(number, **kw):
            self.ran.append(number)
            block = _verdict.machine_block(
                None if not ok else dict(VERDICT),
                cost={"input": 1000, "output": 100, "usd": cost},
                error=None if ok else "the API is down")
            result = {"number": number, "title": "t", "ok": ok, "verdict": block,
                      "error": None if ok else "the API is down", "unposted": None,
                      "labelled": True, "unlabelled": None,
                      "label": self.labissues.TRIAGED_LABEL, "comment_url": None,
                      "summary": "prose"}
            self.triagelog.record(number, result)
            self.triagelog.spend(block["cost"]["usd"])
            return result
        return _run

    def sweep_once(self, *specs, **kw):
        kw.setdefault("run", self.runner())
        return self.labissues.auto_triage_once(issues=issues(*specs), **kw)

    # ------------------------------------------------------------ selection

    def test_an_untriaged_issue_is_picked_up_without_being_asked(self):
        self.sweep_once((1, [], None))

        self.assertEqual(self.ran, [1])

    def test_a_marked_issue_is_never_triaged_again(self):
        """The label is the whole idempotency mechanism. If it stops gating,
        every sweep pays to re-triage the entire backlog, forever."""
        self.sweep_once((1, [self.labissues.TRIAGED_LABEL], None))

        self.assertEqual(self.ran, [], "a marked issue was triaged again")

    def test_removing_the_label_makes_an_issue_eligible_again(self):
        """The documented way to ask for another look."""
        self.sweep_once((1, [self.labissues.TRIAGED_LABEL], None))
        self.sweep_once((1, [], None))

        self.assertEqual(self.ran, [1])

    def test_a_new_comment_does_not_cause_a_re_triage(self):
        """**The single most important test in this ticket.** Posting the triage
        comment bumps the issue's own updated_at, so any watermark on time either
        re-triages forever or silently swallows the human comments that arrive in
        the window. Idempotency is a predicate on content — the label — and this
        test fails the moment someone reintroduces a timestamp."""
        marked = [self.labissues.TRIAGED_LABEL]
        self.sweep_once((1, marked, "2026-07-25T00:00:00Z"))
        # A human replies; the issue's timestamp moves far past anything the
        # sweep could have recorded.
        self.sweep_once((1, marked, "2099-01-01T00:00:00Z"))

        self.assertEqual(self.ran, [], "a comment bump re-triaged a marked issue")

    def test_only_one_issue_is_taken_per_pass(self):
        """A backlog of eight drains over eight passes rather than firing eight
        expensive runs at once."""
        self.sweep_once((1, [], None), (2, [], None), (3, [], None))

        self.assertEqual(len(self.ran), 1)

    def test_the_oldest_untriaged_issue_goes_first(self):
        self.sweep_once((3, [], None), (1, [], None), (2, [], None))

        self.assertEqual(self.ran, [1], "the queue is not draining oldest first")

    def test_a_pass_does_nothing_while_another_run_holds_the_slot(self):
        """Manual and automatic runs share one slot, so the sweep must not even
        try while a human-triggered run is in flight."""
        started, release = threading.Event(), threading.Event()

        def slow(issue):
            started.set()
            release.wait(5)
            return dict(VERDICT), dict(SPEND)

        manual = threading.Thread(target=lambda: self.labissues.run_triage(
            9, issue=ISSUE, analyse=slow,
            comment=self.tracker.comment, label=self.tracker.label))
        manual.start()
        try:
            self.assertTrue(started.wait(5))
            outcome = self.labissues.auto_triage_once(issues=issues((1, [], None)),
                                                      run=self.labissues.run_triage)
            self.assertEqual(self.ran, [])
            self.assertEqual(outcome["skipped"], "busy")
        finally:
            release.set()
            manual.join(5)

    # --------------------------------------------------------------- budget

    def test_a_pass_is_skipped_once_the_soft_limit_is_reached(self):
        self.triagelog.spend(self.triagelog.SOFT_USD)

        outcome = self.sweep_once((1, [], None))

        self.assertEqual(self.ran, [], "a run started over the soft limit")
        self.assertEqual(outcome["skipped"], "budget")
        self.assertEqual(self.triagelog.ledger()["state"], "soft")

    def test_the_soft_limit_logs_once_a_day_and_not_once_a_minute(self):
        """The sweep runs every minute. A line per skipped pass is 1,440 lines a
        day of the same sentence, which is how an ops log stops being read."""
        self.triagelog.spend(self.triagelog.SOFT_USD)

        for _ in range(3):
            self.sweep_once((1, [], None))

        lines = [e for e in _oplog.recent(200)
                 if "budget" in e.get("message", "") and e.get("system") == "labissues"]
        self.assertEqual(len(lines), 1, f"logged {len(lines)} times, not once")

    def test_the_hard_limit_notifies(self):
        """Soft only skips, and a skipped sweep is invisible. Crossing hard means
        one run overshot badly enough to need a human to know."""
        sent = []
        self.triagelog.spend(self.triagelog.HARD_USD)

        self.sweep_once((1, [], None), notifier=lambda **kw: sent.append(kw))

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["priority"], "high")
        self.assertEqual(self.triagelog.ledger()["state"], "hard")

    def test_the_hard_limit_notifies_once_a_day_and_not_once_a_minute(self):
        sent = []
        self.triagelog.spend(self.triagelog.HARD_USD)

        for _ in range(3):
            self.sweep_once((1, [], None), notifier=lambda **kw: sent.append(kw))

        self.assertEqual(len(sent), 1)

    def test_twice_the_hard_limit_stops_triage_until_the_day_resets(self):
        self.triagelog.spend(self.triagelog.HARD_USD * 2)

        self.sweep_once((1, [], None))

        self.assertEqual(self.ran, [])
        self.assertEqual(self.triagelog.ledger()["state"], "stopped")

    def test_the_ledger_resets_when_the_day_rolls_over(self):
        """"Until reset" has to mean something. Yesterday's spend must not keep
        triage switched off this morning."""
        yesterday = time.time() - 86400
        self.triagelog.spend(self.triagelog.HARD_USD * 2, ts=yesterday)

        self.assertEqual(self.triagelog.ledger()["usd"], 0.0)
        self.sweep_once((1, [], None))
        self.assertEqual(self.ran, [1])

    def test_a_failed_run_still_costs_the_budget(self):
        """A run that dies has already been billed for every token it spent. A
        ledger that only counts successes under-reports exactly the runs that
        are most likely to repeat."""
        self.sweep_once((1, [], None), run=self.runner(cost=0.5, ok=False))

        self.assertAlmostEqual(self.triagelog.ledger()["usd"], 0.5)

    def test_a_failed_run_is_not_retried(self):
        """The weekly report's retry storm is the precedent this must not
        reproduce: the marker goes on either way, so the issue leaves the queue."""
        self.sweep_once((1, [], None), run=self.runner(ok=False))
        # The failed run marked the issue, exactly as a successful one does.
        self.sweep_once((1, [self.labissues.TRIAGED_LABEL], None))

        self.assertEqual(self.ran, [1], "a failed run was retried")

    # -------------------------------------------- the gate reads a cached copy

    def test_a_run_whose_label_write_failed_is_not_run_again(self):
        """The retry storm this feature exists not to reproduce, in its subtlest
        form. When the label write fails the issue stays label-free forever, so
        a label-only gate re-runs and re-pays for it every single pass. The
        record says we already looked; that has to count."""
        def unmarkable(number, **kw):
            self.ran.append(number)
            self.triagelog.record(number, {
                "number": number, "ok": True, "labelled": False,
                "verdict": _verdict.machine_block(dict(VERDICT), cost=dict(SPEND))})
            return {}

        self.sweep_once((1, [], None), run=unmarkable)
        # The label never landed, so the issue still looks untriaged on GitHub.
        self.sweep_once((1, [], None))
        self.sweep_once((1, [], None))

        self.assertEqual(self.ran, [1], "an unmarked run was retried and paid for again")

    def test_an_issue_triaged_since_the_cache_was_read_is_not_run_twice(self):
        """The sweep and this pass are separate threads on the same interval, so
        a run finishing at T can be followed by a pass reading a cache filled
        before T: the label is on the issue and absent from our copy. Believing
        that copy buys a second run of the same investigation."""
        self.sweep_once((1, [], None), seen_at=time.time() - 600)

        # Same stale copy — the sweep has not refreshed since the run.
        self.sweep_once((1, [], None), seen_at=time.time() - 600)

        self.assertEqual(self.ran, [1], "a stale cache caused a second paid run")

    def test_a_label_removed_after_a_fresh_read_makes_it_eligible_again(self):
        """The other side of that rule: once the cache has actually been read
        again and the label is gone, a human has removed it and means it."""
        self.sweep_once((1, [], None), seen_at=time.time() - 600)

        self.sweep_once((1, [], None), seen_at=time.time() + 1)

        self.assertEqual(self.ran, [1, 1])

    def test_a_pass_with_nothing_to_do_is_not_a_budget_skip(self):
        """A stalled queue and an idle one must not look the same to the UI."""
        outcome = self.sweep_once((1, [self.labissues.TRIAGED_LABEL], None))

        self.assertIsNone(outcome["skipped"])
        self.assertEqual(self.triagelog.ledger()["state"], "ok")


# ------------------------------------------------------- #37: the whole verdict

class VerdictDetailTest(IsolatedDataDirTest):
    """`verdict_for()` — everything the detail card renders, from the local
    record when there is one and from GitHub when there is not."""

    def test_a_stored_run_is_returned_whole(self):
        self.labissues.run_triage(1, issue=ISSUE, analyse=analysis(),
                                  comment=self.tracker.comment, label=self.tracker.label)

        got = self.labissues.verdict_for(1)

        self.assertEqual(got["source"], "local")
        self.assertEqual(got["verdict"]["verdict"], "refuted")
        self.assertEqual([e["status"] for e in got["verdict"]["evidence"]],
                         ["success", "no_data", "truncated", "excluded"])
        self.assertEqual(got["verdict"]["refuted"], VERDICT["refuted"])
        self.assertEqual(got["verdict"]["remediation"]["kind"], "diagnostic")

    def test_the_prose_survives_into_the_record(self):
        """The machine block deliberately has no `summary` — the prose is the
        half a human reads. The card renders both, so the record has to keep it."""
        self.labissues.run_triage(1, issue=ISSUE, analyse=analysis(),
                                  comment=self.tracker.comment, label=self.tracker.label)

        self.assertEqual(self.labissues.verdict_for(1)["summary"], VERDICT["summary"])

    def test_a_record_written_before_the_prose_was_kept_recovers_it_from_github(self):
        """Records predating the prose field still have prose — in the comment,
        which was always the copy that mattered. Showing a verdict with its
        reasoning missing, when one request would fetch it, is not good enough."""
        self.labissues.run_triage(1, issue=ISSUE, analyse=analysis(),
                                  comment=self.tracker.comment, label=self.tracker.label)
        stored = self.triagelog.get(1)
        del stored["summary"]                       # as an older ClaudeOS wrote it
        self.triagelog.record(1, stored)

        got = self.labissues.verdict_for(
            1, comments=lambda n: [{"body": self.tracker.body}])

        self.assertEqual(got["source"], "local", "the local record is still the record")
        self.assertEqual(got["summary"], VERDICT["summary"])

    def test_the_card_still_opens_when_the_missing_prose_cannot_be_fetched(self):
        """GitHub being unreachable costs the reasoning, not the page."""
        self.labissues.run_triage(1, issue=ISSUE, analyse=analysis(),
                                  comment=self.tracker.comment, label=self.tracker.label)
        stored = self.triagelog.get(1)
        del stored["summary"]
        self.triagelog.record(1, stored)

        def dead(_n):
            raise ConnectionError("GitHub is down")

        got = self.labissues.verdict_for(1, comments=dead)

        self.assertEqual(got["summary"], "")
        self.assertEqual(got["verdict"]["verdict"], "refuted")

    def test_a_verdict_this_install_never_ran_is_read_back_from_github(self):
        """GitHub is the source of truth: a data/ wipe, or a run from another
        install, leaves a labelled issue whose verdict is only in the comment."""
        block = _verdict.machine_block(dict(VERDICT), cost=dict(SPEND))
        body = _verdict.comment_body(block, "the prose a human reads")

        got = self.labissues.verdict_for(
            1, comments=lambda n: [{"body": "a human asking a question"},
                                   {"body": body,
                                    "html_url": "https://github.com/x/y/issues/1#c9"}])

        self.assertEqual(got["source"], "github")
        self.assertEqual(got["verdict"], block)
        self.assertEqual(got["summary"], "the prose a human reads")
        self.assertEqual(got["comment_url"], "https://github.com/x/y/issues/1#c9")

    def test_the_most_recent_block_wins_when_an_issue_was_triaged_twice(self):
        older = _verdict.comment_body(
            _verdict.machine_block({**VERDICT, "verdict": "inconclusive"}), "first look")
        newer = _verdict.comment_body(
            _verdict.machine_block({**VERDICT, "verdict": "diagnosed"}), "second look")

        got = self.labissues.verdict_for(
            1, comments=lambda n: [{"body": older}, {"body": newer}])

        self.assertEqual(got["verdict"]["verdict"], "diagnosed")

    def test_an_issue_with_no_verdict_anywhere_says_so_rather_than_inventing_one(self):
        got = self.labissues.verdict_for(1, comments=lambda n: [{"body": "just a human"}])

        self.assertIsNone(got["verdict"])
        self.assertEqual(got["source"], "none")


# ------------------------------------------- #38: telling somebody, and the report

def sink():
    """Collects notifications instead of sending them."""
    sent = []

    def _send(**kw):
        sent.append(kw)

    _send.sent = sent
    return _send


class VerdictNotificationTest(IsolatedDataDirTest):
    """Which verdicts are worth interrupting somebody for.

    The gate is severity, not the five-minute mute: mute keys on the exact
    title string and these titles carry issue numbers, so no two are ever equal
    and the mute can never collapse them. Get the gate wrong and the feature
    either goes silent or pages the owner about a plug.
    """

    def dispatch(self, over=None, notifier=None):
        return self.labissues.run_triage(
            1, issue=ISSUE, analyse=analysis({**VERDICT, **(over or {})}),
            comment=self.tracker.comment, label=self.tracker.label,
            notifier=notifier)

    def test_a_diagnosed_critical_verdict_notifies(self):
        told = sink()

        self.dispatch({"verdict": "diagnosed", "severity": "critical"}, notifier=told)

        self.assertEqual(len(told.sent), 1)
        self.assertIn("#1", told.sent[0]["title"], "the title must name the issue")

    def test_a_diagnosed_serious_verdict_notifies(self):
        told = sink()

        self.dispatch({"verdict": "diagnosed", "severity": "serious"}, notifier=told)

        self.assertEqual(len(told.sent), 1)

    def test_a_verdict_does_not_outrank_lab_down(self):
        """`high` is reserved for lab-down and failing hardware. The owner filed
        this issue themselves — they know about it — so a verdict on it must not
        arrive at the same volume as a dead gateway."""
        told = sink()

        self.dispatch({"verdict": "diagnosed", "severity": "critical"}, notifier=told)

        self.assertEqual(told.sent[0]["priority"], "default")

    def test_a_diagnosed_verdict_below_serious_stays_quiet(self):
        told = sink()

        self.dispatch({"verdict": "diagnosed", "severity": "warning"}, notifier=told)
        self.dispatch({"verdict": "diagnosed", "severity": "info"}, notifier=told)

        self.assertEqual(told.sent, [])

    def test_the_other_three_verdicts_stay_quiet_at_every_severity(self):
        """Refuted, inconclusive and no-fault-found are all steady state: useful
        to read, never worth an interruption."""
        told = sink()

        for value in ("refuted", "inconclusive", "no_fault_found"):
            for sev in ("critical", "serious", "warning", "info"):
                self.dispatch({"verdict": value, "severity": sev}, notifier=told)

        self.assertEqual(told.sent, [])

    def test_a_single_failed_run_notifies_nothing(self):
        """One failed run is not systemic. It goes to the ops log, which the
        weekly report already sweeps — so it is reported without paging anyone."""
        told = sink()

        self.labissues.run_triage(
            1, issue=ISSUE, analyse=analysis(raises=self.labissues.TriageFailed("api down")),
            comment=self.tracker.comment, label=self.tracker.label, notifier=told)

        self.assertEqual(told.sent, [])
        logged = [e for e in _oplog.recent(50)
                  if e["system"] == "labissues" and "api down" in e["message"]]
        self.assertEqual(len(logged), 1, "a failed run left no trace in the ops log")


class CredentialAlertTest(IsolatedDataDirTest):
    """The token expires in July 2027, so this state is a certainty. It is the
    one lab-issues failure that is genuinely systemic: the feature has silently
    stopped working and nothing else will say so."""

    def test_a_rejected_token_notifies_at_high_priority(self):
        told = sink()

        with self.assertRaises(HttpError):
            self.labissues.sweep(fetch=fake(raises=HttpError(401, "unauthorised", "", headers={})),
                                 notifier=told)

        self.assertEqual(len(told.sent), 1)
        self.assertEqual(told.sent[0]["priority"], "high")

    def test_a_token_that_cannot_see_the_repo_notifies_too(self):
        """A private repo the token is not scoped to answers 404, not 403 — the
        same silent stop, wearing a different status code."""
        told = sink()

        with self.assertRaises(HttpError):
            self.labissues.sweep(fetch=fake(raises=HttpError(404, "not found", "", headers={})),
                                 notifier=told)

        self.assertEqual(len(told.sent), 1)

    def test_it_alerts_on_the_transition_not_once_a_minute(self):
        """The sweep runs every 60s and a revoked token does not heal itself.
        Alerting per pass is 1,440 pushes a day."""
        told = sink()
        dead = fake(raises=HttpError(401, "unauthorised", "", headers={}))

        for _ in range(3):
            with self.assertRaises(HttpError):
                self.labissues.sweep(fetch=dead, notifier=told)

        self.assertEqual(len(told.sent), 1)

    def test_a_rate_limit_is_not_a_credential_failure(self):
        """Exhausting the hourly budget is transient and already handled by the
        backoff. Paging somebody for it would train them to ignore this alert."""
        told = sink()
        reset = int(time.time()) + 300
        limited = HttpError(403, "forbidden", "", headers={
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)})

        with self.assertRaises(HttpError):
            self.labissues.sweep(fetch=fake(raises=limited), notifier=told)

        self.assertEqual(told.sent, [])

    def test_a_403_with_no_limit_headers_is_a_permissions_problem(self):
        """A 403 whose headers do not say the limit is exhausted is the token
        missing the Issues permission — permanent, and needs a person."""
        told = sink()

        with self.assertRaises(HttpError):
            self.labissues.sweep(fetch=fake(raises=HttpError(403, "forbidden", "", headers={})),
                                 notifier=told)

        self.assertEqual(len(told.sent), 1)
        self.assertIn("permission", told.sent[0]["message"])

    def test_a_transient_failure_pages_nobody(self):
        """ADR-0001: GitHub being briefly unreachable is not a lab incident. A
        timeout or a 5xx must not reach the red siren — it is exactly the alert
        the ADR refused to accept when it kept GitHub out of `CONNECTORS`."""
        told = sink()

        for boom in (HttpError(502, "bad gateway", "", headers={}),
                     ConnectionError("connection reset by peer")):
            with self.assertRaises(Exception):
                self.labissues.sweep(fetch=fake(raises=boom), notifier=told)

        self.assertEqual(told.sent, [])

    def test_each_stop_is_explained_as_itself(self):
        """A renamed repository is not a rejected token. Sending somebody to
        re-mint a working credential is worse than saying nothing."""
        for status, phrase in ((401, "rejected"), (404, "renamed or deleted")):
            self.labissues = importlib.reload(_labissues)   # re-arm the transition
            told = sink()
            with self.assertRaises(HttpError):
                self.labissues.sweep(fetch=fake(raises=HttpError(status, "x", "", headers={})),
                                     notifier=told)
            self.assertIn(phrase, told.sent[0]["message"], f"{status} was misexplained")

    def test_a_recovered_sweep_re_arms_the_alert(self):
        """Rotate the token and it works again; if it is revoked a second time,
        that is a second transition and worth a second alert."""
        told = sink()
        dead = fake(raises=HttpError(401, "unauthorised", "", headers={}))

        with self.assertRaises(HttpError):
            self.labissues.sweep(fetch=dead, notifier=told)
        self.labissues.sweep(fetch=fake(200, [ISSUE], {"ETag": 'W/"a"'}), notifier=told)
        with self.assertRaises(HttpError):
            self.labissues.sweep(fetch=dead, notifier=told)

        self.assertEqual(len(told.sent), 2)


class ReportSectionTest(IsolatedDataDirTest):
    """What the weekly digest is told about the lab issue queue.

    A top-level section, deliberately not routed through the per-connector slice
    mechanism #2 proposes — that iterates `CONNECTORS`, and lab issues are not a
    connector (ADR-0001). Routing through it would re-open that decision.
    """

    def section(self, *specs, **kw):
        return self.labissues.report_section(issues=issues(*specs), **kw)

    def rec(self, number, verdict_value="diagnosed", severity="warning", ok=True):
        self.triagelog.record(number, {
            "number": number, "title": f"issue {number}", "ok": ok,
            "labelled": True, "summary": "prose",
            "verdict": _verdict.machine_block(
                {**VERDICT, "verdict": verdict_value, "severity": severity},
                cost=dict(SPEND), error=None if ok else "the API is down")})

    def test_it_counts_the_queue_by_verdict(self):
        marked = [self.labissues.TRIAGED_LABEL]
        self.rec(1, "diagnosed")
        self.rec(2, "refuted")
        self.rec(3, "no_fault_found")

        out = self.section((1, marked, None), (2, marked, None), (3, marked, None),
                           (4, [], None))

        self.assertEqual(out["open"], 4)
        self.assertEqual(out["untriaged"], 1)
        self.assertEqual(out["by_verdict"],
                         {"diagnosed": 1, "refuted": 1, "no_fault_found": 1})

    def test_an_open_diagnosed_issue_is_reported_as_unresolved(self):
        """A diagnosed issue still open is a problem somebody has been told
        about and has not dealt with — the point of putting this in the digest."""
        marked = [self.labissues.TRIAGED_LABEL]
        self.rec(1, "diagnosed", "critical")

        out = self.section((1, marked, None))

        self.assertEqual(len(out["unresolved_diagnosed"]), 1)
        self.assertEqual(out["unresolved_diagnosed"][0]["number"], 1)
        self.assertEqual(out["unresolved_diagnosed"][0]["severity"], "critical")

    def test_a_refuted_issue_is_not_unresolved_work(self):
        marked = [self.labissues.TRIAGED_LABEL]
        self.rec(1, "refuted")

        self.assertEqual(self.section((1, marked, None))["unresolved_diagnosed"], [])

    def test_a_long_untriaged_issue_is_the_backlog_signal(self):
        """Automatic triage picks an issue up within a minute, so an issue still
        untriaged a day later means triage has quietly stopped. There is no
        notification for that — this is the only place it surfaces."""
        old = "2020-01-01T00:00:00Z"

        out = self.section((1, [], None, old), (2, [], None))

        stale = [i["number"] for i in out["untriaged_too_long"]]
        self.assertEqual(stale, [1], "an ancient untriaged issue was not flagged")

    def test_the_backlog_threshold_is_measured_in_utc(self):
        """GitHub stamps its timestamps with a `Z`. Reading them as local time
        lands an hour out under BST — invisible everywhere except at exactly
        this boundary, which is the only thing the number is used for."""
        now = 1785000000.0                        # a fixed instant
        just_under = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(now - (STALE := 24) * 3600 + 600))
        just_over = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                  time.gmtime(now - STALE * 3600 - 600))

        out = self.labissues.report_section(
            issues=issues((1, [], None, just_under), (2, [], None, just_over)), now=now)

        self.assertEqual([i["number"] for i in out["untriaged_too_long"]], [2])

    def test_failed_runs_are_counted_separately_from_verdicts(self):
        """A failed run has an `inconclusive` block by default; counting it as a
        verdict would report the machinery breaking as the machinery working."""
        marked = [self.labissues.TRIAGED_LABEL]
        self.rec(1, ok=False)

        out = self.section((1, marked, None))

        self.assertEqual(out["failed_runs"], 1)
        self.assertEqual(out["by_verdict"], {})

    def test_a_labelled_issue_with_no_record_is_counted_not_dropped(self):
        """After a `data/` wipe every verdict is on GitHub and none is here. A
        silent skip would report a queue of three as `by_verdict: {}` with
        `untriaged: 0` — "all triaged, nothing found" — which is the same lie as
        rendering an unreadable queue as an empty one."""
        marked = [self.labissues.TRIAGED_LABEL]

        out = self.section((1, marked, None), (2, marked, None), (3, marked, None),
                           records={})

        self.assertEqual(out["triaged_verdict_unknown"], 3)
        self.assertEqual(out["by_verdict"], {})
        self.assertEqual(out["untriaged"], 0, "they are triaged — just not by this install")

    def test_an_unreadable_timestamp_does_not_fabricate_a_stalled_backlog(self):
        """`untriaged_too_long` is the one signal the prompt reads as "triage has
        probably stopped". Measuring an unparseable date from 1970 would invent
        that finding out of one bad field."""
        out = self.section((1, [], None, "not a date"), (2, [], None, ""))

        self.assertEqual(out["untriaged_too_long"], [])
        self.assertEqual(out["untriaged"], 2, "…but they are still counted as untriaged")

    def test_an_unreadable_queue_is_reported_as_unknown_not_as_empty(self):
        """Same rule as the page: a sweep that cannot read the repo does not
        know the queue is empty, and the digest must not be told that it is."""
        with self.assertRaises(HttpError):
            self.labissues.sweep(fetch=fake(raises=HttpError(401, "nope", "", headers={})))

        out = self.labissues.report_section()

        self.assertIn("error", out)
        self.assertNotIn("open", out)


if __name__ == "__main__":
    unittest.main()
