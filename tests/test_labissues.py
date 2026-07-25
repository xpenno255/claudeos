"""Behaviour of the lab-issues sweep, driven through its public interface.

The module takes its GitHub caller as an argument, so every test here runs
against a fake returning canned payloads and ETags: no network, no store, no
credentials. Tests assert on what `snapshot()` reports — never on internals.

`importlib.reload` in setUp gives each test a clean module-level cache without
reaching into private state.
"""

import importlib
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import labissues as _labissues  # noqa: E402
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

    def test_a_failed_fetch_records_the_error_and_keeps_the_last_good_issues(self):
        """A rejected token must never read as "no open issues". It raises too,
        so the sweeper thread records it in the ops log."""
        self.labissues.sweep(fetch=fake(200, [ISSUE], {"ETag": 'W/"abc"'}))

        rejected = LookupError("the token cannot see this repository")
        with self.assertRaises(LookupError):
            self.labissues.sweep(fetch=fake(raises=rejected))

        snap = self.labissues.snapshot()
        self.assertEqual([i["number"] for i in snap["issues"]], [1], "last good issues lost")
        self.assertIn("cannot see", snap["error"])

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

        self.assertGreaterEqual(after["checked"], first["checked"])
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


if __name__ == "__main__":
    unittest.main()
