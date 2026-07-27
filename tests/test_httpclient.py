"""One invariant of the HTTP helper: a secret in a URL never reaches an error message.

`CLAUDE.md` sets the bar at failure modes that are **silent and expensive**, and
this is the same shape as #39 — a credential lost without a word — except this
one leaks rather than destroys.

Telegram carries its bot token as a URL path segment, so every error built from
that URL contained the token, and those errors go three places that all outlive
the request: the Setup page (where it was first spotted, rendered in full under
the card), `data/opslog.jsonl` on disk via `notify._fan_out`'s delivery-failure
line, and — because that line is `warn` — the weekly report's `recent_warnings`,
which is sent to the Anthropic API. A live bot token reached the browser before
anyone noticed; the other two were one failed delivery away.

Silent, because nothing about a redacted URL looks different until you read one
that isn't. Expensive, because the remedy is revoking a credential.

The scrubbing is tested at `httpclient` rather than per-connector because this
module is the single place that formats a URL into an exception. `urlopen` is
substituted, so no test here touches the network.
"""

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import httpclient  # noqa: E402

# Shaped like a real Telegram token — an id, a colon, then the secret half.
FAKE_TOKEN = "8000000000:AAFfakeFAKEfakeFAKEfakeFAKEfake12345"
SECRET_HALF = "AAFfakeFAKEfakeFAKEfakeFAKEfake12345"
TELEGRAM_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"


class SafeUrlTest(unittest.TestCase):

    def test_a_telegram_bot_token_is_scrubbed(self):
        """The reported bug. The token is a path segment, not a parameter, so
        nothing that only understood query strings would have caught it."""
        out = httpclient.safe_url(TELEGRAM_URL)
        self.assertNotIn(SECRET_HALF, out)
        self.assertNotIn(FAKE_TOKEN, out)
        # still recognisable as the call that failed
        self.assertIn("api.telegram.org", out)
        self.assertIn("sendMessage", out)

    def test_credential_query_parameters_are_scrubbed(self):
        for param in ("password", "passwd", "pass", "token", "api_key", "apikey",
                      "secret", "access_token", "auth", "sig", "signature"):
            with self.subTest(param=param):
                out = httpclient.safe_url(f"https://x.invalid/a?{param}=hunter2&ok=1")
                self.assertNotIn("hunter2", out)
                self.assertIn("ok=1", out, "non-secret parameters must survive")

    def test_scrubbing_is_case_insensitive(self):
        self.assertNotIn("hunter2", httpclient.safe_url("https://x.invalid/?Token=hunter2"))

    def test_an_innocuous_url_is_untouched(self):
        """Over-redaction costs debuggability, which is the whole point of
        putting the URL in the message."""
        url = "https://192.168.1.250:8006/api2/json/nodes?type=storage"
        self.assertEqual(httpclient.safe_url(url), url)


class RequestErrorTest(unittest.TestCase):
    """The scrub has to be on the paths that actually build messages — a correct
    `safe_url` nobody calls is the bug still shipping."""

    def _raising(self, exc):
        return mock.patch.object(httpclient.urllib.request, "urlopen", side_effect=exc)

    def assert_clean(self, err):
        self.assertNotIn(SECRET_HALF, str(err))
        self.assertNotIn(FAKE_TOKEN, str(err))

    def test_http_error_message_is_scrubbed(self):
        exc = urllib.error.HTTPError(TELEGRAM_URL, 403, "Forbidden", {},
                                     __import__("io").BytesIO(b'{"ok":false}'))
        with self._raising(exc):
            with self.assertRaises(httpclient.HttpError) as cm:
                httpclient.request("POST", TELEGRAM_URL, json_body={})
        self.assert_clean(cm.exception)
        self.assertEqual(cm.exception.status, 403)

    def test_unreachable_host_message_is_scrubbed(self):
        with self._raising(urllib.error.URLError("no route to host")):
            with self.assertRaises(ConnectionError) as cm:
                httpclient.request("POST", TELEGRAM_URL, json_body={})
        self.assert_clean(cm.exception)

    def test_timeout_message_is_scrubbed(self):
        """The exact error on the screenshot that reported this — `timeout
        reaching https://api.telegram.org/bot<token>/sendMessage`."""
        with self._raising(TimeoutError()):
            with self.assertRaises(ConnectionError) as cm:
                httpclient.request("POST", TELEGRAM_URL, json_body={})
        self.assert_clean(cm.exception)
        self.assertIn("timeout reaching", str(cm.exception))


class TelegramSenderTest(unittest.TestCase):
    """End of the chain: the sender whose failure is written to the ops log."""

    def test_a_failed_telegram_send_cannot_log_the_token(self):
        from app import notify
        exc = urllib.error.HTTPError(TELEGRAM_URL, 403, "Forbidden", {},
                                     __import__("io").BytesIO(b'{"ok":false}'))
        with mock.patch.object(httpclient.urllib.request, "urlopen", side_effect=exc):
            with self.assertRaises(httpclient.HttpError) as cm:
                notify._send_telegram({"bot_token": FAKE_TOKEN, "chat_id": "1"},
                                      "title", "message", "default", [])
        # this is the string _fan_out interpolates into the oplog line
        self.assertNotIn(SECRET_HALF, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
