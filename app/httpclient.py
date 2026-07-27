"""Tiny outbound HTTP helper on top of urllib.

Homelab gear almost always runs self-signed TLS, so every request takes a
verify_tls flag. All requests carry a short timeout so one dead box never
hangs the poller or an API call.
"""

import json
import re
import ssl
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 6

# A secret that rides in the URL instead of a header ends up in every error
# message built from that URL — and those messages go three places that all
# outlive the request: the browser, `data/opslog.jsonl` on disk, and the weekly
# AI report, which ships warn/error lines to the Anthropic API as
# `recent_warnings`. A failed Telegram delivery used to put the bot token in all
# three.
#
# Scrubbed here because this module is the single place that formats a URL into
# an exception, so no caller can forget to. `synology._redact` predates this and
# stays: DSM's `account`/`_sid` parameters are its own, and it is already right.
_URL_SECRETS = (
    # Telegram carries the bot token as a path segment: /bot<id>:<secret>/method
    (re.compile(r"/bot\d+:[A-Za-z0-9_-]+"), "/bot•••"),
    # credential-bearing query parameters, whatever the service calls them
    (re.compile(r"(?i)\b(pass|passwd|password|token|api_?key|apikey|secret|"
                r"access_token|auth|sig|signature)=[^&\s]*"), r"\1=•••"),
)


def safe_url(url: str) -> str:
    """A URL with any embedded credential replaced, for logs and error text."""
    for pattern, replacement in _URL_SECRETS:
        url = pattern.sub(replacement, url)
    return url


class HttpError(Exception):
    def __init__(self, status: int, message: str, body: str = "", headers=None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.headers = headers  # e.g. WWW-Authenticate for registry token flows


def _ctx(verify_tls: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json_body: dict | None = None,
    verify_tls: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    return_headers: bool = False,
):
    """Fire a request and return parsed JSON (or raw text if not JSON).

    Raises HttpError with the upstream status/body on HTTP errors and
    ConnectionError-ish exceptions on network failures.
    """
    hdrs = {"Accept": "application/json", **(headers or {})}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx(verify_tls)) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            parsed = _parse(body)
            if return_headers:
                # resp.headers is an http.client.HTTPMessage — keep it whole
                # so callers can use get_all() (e.g. multiple Set-Cookie).
                return parsed, resp.headers
            return parsed
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise HttpError(e.code, f"HTTP {e.code} from {safe_url(url)}", body,
                        headers=e.headers) from e
    except urllib.error.URLError as e:
        raise ConnectionError(f"cannot reach {safe_url(url)}: {e.reason}") from e
    except TimeoutError as e:
        raise ConnectionError(f"timeout reaching {safe_url(url)}") from e


def _parse(body: str):
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body
