"""Tiny outbound HTTP helper on top of urllib.

Homelab gear almost always runs self-signed TLS, so every request takes a
verify_tls flag. All requests carry a short timeout so one dead box never
hangs the poller or an API call.
"""

import json
import ssl
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 6


class HttpError(Exception):
    def __init__(self, status: int, message: str, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


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
        raise HttpError(e.code, f"HTTP {e.code} from {url}", body) from e
    except urllib.error.URLError as e:
        raise ConnectionError(f"cannot reach {url}: {e.reason}") from e
    except TimeoutError as e:
        raise ConnectionError(f"timeout reaching {url}") from e


def _parse(body: str):
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body
