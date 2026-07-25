"""Behaviour of the lab-issues sweep, driven through its public interface.

The module takes its GitHub caller as an argument, so every test here runs
against a fake returning canned payloads and ETags: no network, no store, no
credentials. Tests assert on what `snapshot()` reports — never on internals.

`importlib.reload` in setUp gives each test a clean module-level cache without
reaching into private state.
"""

import importlib
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import labissues as _labissues  # noqa: E402
from app import store as _store  # noqa: E402
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


class UnusableConfigTest(unittest.TestCase):
    """The default path, where sweep resolves its own config. A sweep that
    cannot even build a request must fail *quietly and visibly*: quietly
    because the sweeper would otherwise log the same line every 60s forever,
    visibly because the UI has to say why nothing is being fetched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CLAUDEOS_DATA"] = self.tmp
        self.store = importlib.reload(_store)
        self.labissues = importlib.reload(_labissues)

    def tearDown(self):
        os.environ.pop("CLAUDEOS_DATA", None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        importlib.reload(_store)

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


if __name__ == "__main__":
    unittest.main()
