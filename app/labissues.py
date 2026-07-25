"""Lab issues — ClaudeOS's link to the private GitHub lab repo.

A **lab issue** is a homelab problem a human raises as a GitHub issue in a
dedicated private repo, for ClaudeOS to triage (see CONTEXT.md). This module
owns every call ClaudeOS makes to that repo. It is deliberately **not** a
connector (ADR-0001): nothing here is polled, and GitHub being briefly
unreachable is not a lab incident.

Today it owns the credential read, the Setup-page connection test, and the
ETag-conditional sweep that keeps a local picture of the repo's open issues.
Triage arrives in a later ticket.

Two deviations from the rest of the app's outbound HTTP:

- **TLS verification is on.** `httpclient.request` defaults it off on purpose
  because homelab gear runs self-signed certificates; api.github.com is the
  exact opposite case, so every call here passes verify_tls=True.
- **A 404 is the interesting failure.** For a *private* repo GitHub answers a
  token that is not scoped to it with 404, never 403 — indistinguishable, at
  the status code, from a typo in the repo name. Left unexplained that shows
  up later as a sweep that silently finds nothing forever, so the test says so
  in words.
"""

import re
import threading
import time

from . import oplog, store, sweeper
from .httpclient import HttpError, request

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"  # pinned: GitHub versions its REST API by date
SWEEP_INTERVAL = 60         # a conditional 304 is free, so this costs ~2.4%/hr
PAGE_SIZE = 100             # GitHub's maximum
TIMEOUT = 15

# owner/name, GitHub's own character set for both halves
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

PAT_HINT = ("Repository access → Only select repositories, "
            "Permissions → Issues: Read and write")


def _hdr(hdrs, name: str) -> str:
    """Read one response header, whatever the container.

    Production hands us an `http.client.HTTPMessage`, which is already
    case-insensitive; a plain dict is not, and HTTP/2 puts these header names
    on the wire in lower case. Normalising here means a test double behaves
    like the real thing instead of passing by luck of capitalisation.
    """
    if hdrs is None:
        return ""
    got = hdrs.get(name)
    if got is None and hasattr(hdrs, "items"):
        got = next((v for k, v in hdrs.items() if k.lower() == name.lower()), None)
    return (got or "").strip()


def _headers(token: str) -> dict:
    # caller headers win over httpclient's Accept: application/json default
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "Authorization": f"Bearer {token}",
        "User-Agent": "ClaudeOS",
    }


def settings() -> tuple[str, str]:
    """(repo, token) from the encrypted store, validated.

    The token is decrypted server-side only and never leaves this process:
    the browser sees it through store.public_summary(), which masks it.
    """
    s = store.get_system("labissues", reveal_secrets=True) or {}
    repo = (s.get("repo") or "").strip().strip("/")
    token = (s.get("token") or "").strip()
    if not repo or not token:
        missing = " and ".join(
            m for m in (("the lab repository" if not repo else None),
                        ("an access token" if not token else None)) if m)
        raise LookupError(f"Lab Issues is not configured — add {missing} on the Setup page")
    if repo.startswith(("http://", "https://", "github.com/")):
        raise ValueError(f'lab repository must be just owner/name, not a URL — got "{repo}"')
    if not REPO_RE.match(repo):
        raise ValueError(f'lab repository must look like owner/name '
                         f'(e.g. xpenno255/homelab) — got "{repo}"')
    return repo, token


def _get(path: str, token: str, *, return_headers: bool = False):
    """GET an api.github.com path with TLS verification ON."""
    return request("GET", API_BASE + path, headers=_headers(token),
                   verify_tls=True, timeout=TIMEOUT, return_headers=return_headers)


def _explain(e: HttpError, repo: str) -> Exception:
    """Map a GitHub status onto this app's exception taxonomy, in words a
    homelab owner can act on. ConnectionError → 502, LookupError → 404,
    ValueError → 400 (see server._dispatch)."""
    if e.status == 401:
        return ConnectionError(
            "GitHub rejected the access token (401) — it is invalid, revoked or expired; "
            "mint a new fine-grained token and paste it again")
    if e.status == 404:
        # THE case this test exists for: a private repo the token cannot see
        # answers 404, not 403.
        return LookupError(
            f'GitHub answered "not found" for {repo} (404) — either the token cannot see this '
            f"repository, or the name is wrong. A private repo the token is not scoped to "
            f"looks exactly like a missing one, so check both: the spelling of owner/name, "
            f"and that the token grants {PAT_HINT} on this repo")
    if e.status == 403:
        if _hdr(e.headers, "X-RateLimit-Remaining") == "0":
            reset = _hdr(e.headers, "X-RateLimit-Reset") or "?"
            return ConnectionError(
                f"GitHub rate limit exhausted (403) — the token has no requests left this "
                f"hour (resets at unix {reset}); the credentials themselves look fine")
        return ConnectionError(
            f"GitHub refused the request for {repo} (403) — the token reaches the repository "
            f"but is missing a permission; it needs {PAT_HINT}")
    if e.status == 410:
        return ValueError(f"issues are disabled on {repo} (410) — turn them on in the "
                          f"repository's Settings → Features before ClaudeOS can triage")
    return ConnectionError(f"GitHub API error {e.status} for {repo}: {e.body[:200]}")


def test() -> dict:
    """Setup-page test: prove the stored token actually reaches the lab repo's
    issues. Two reads — repo metadata, then the open issues list — so a token
    that reaches the repo without the Issues permission is reported as such
    rather than as an unreachable repo.

    Returns {"ok": True, "detail": str}; raises on every failure path.
    """
    repo, token = settings()

    try:
        info = _get(f"/repos/{repo}", token) or {}
    except HttpError as e:
        raise _explain(e, repo) from e

    if info.get("has_issues") is False:
        raise ValueError(f"issues are disabled on {repo} — turn them on in the repository's "
                         f"Settings → Features before ClaudeOS can triage")

    try:
        issues, hdrs = _get(f"/repos/{repo}/issues?state=open&per_page=1", token,
                            return_headers=True)
    except HttpError as e:
        raise _explain(e, repo) from e

    full = info.get("full_name") or repo
    visibility = "private" if info.get("private") else "public"
    seen = len(issues or [])
    backlog = "at least 1 open issue" if seen else "no open issues right now"
    remaining = _hdr(hdrs, "X-RateLimit-Remaining") or "?"
    return {"ok": True,
            "detail": f"{full} reachable ({visibility}) — issues readable, {backlog}; "
                      f"{remaining} API requests left this hour"}


# ------------------------------------------------------------------ the sweep

_lock = threading.Lock()
_cache: dict = {"issues": [], "etag": None, "checked": None, "changed": None,
                "error": None, "backoff_until": None}


def snapshot() -> dict:
    """The current picture of the lab repo's open issues."""
    with _lock:
        return dict(_cache)


def _project(issue: dict) -> dict:
    """A GitHub issue reduced to what ClaudeOS actually uses.

    The wire object carries reactions, the author's avatar, half a dozen URLs
    and a timeline link; none of it reaches the queue view or the triage run,
    and all of it would sit in memory and cross the API on every poll.
    """
    return {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "body": issue.get("body"),
        "labels": [l.get("name") for l in (issue.get("labels") or [])
                   if isinstance(l, dict) and l.get("name")],
        "comments": issue.get("comments"),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "html_url": issue.get("html_url"),
    }


def _backoff_until(e) -> float | None:
    """When GitHub says to stop asking, until when.

    `retry-after` (seconds) wins where present — the docs put it first, and it
    is what secondary rate limits use. Otherwise a primary limit is in force
    only when `x-ratelimit-remaining` is exactly 0, and `x-ratelimit-reset` is
    an absolute UTC epoch, not a delta.
    """
    hdrs = getattr(e, "headers", None)
    if hdrs is None or getattr(e, "status", None) not in (403, 429):
        return None
    retry_after = _hdr(hdrs, "Retry-After")
    if retry_after.isdigit():
        return time.time() + int(retry_after)
    if _hdr(hdrs, "X-RateLimit-Remaining") == "0":
        reset = _hdr(hdrs, "X-RateLimit-Reset")
        if reset.isdigit():
            return float(reset)
    return None


def sweep(fetch=None) -> dict:
    """Refresh the cache once. Returns the resulting snapshot.

    `fetch(etag) -> (status, payload, headers)` is the seam: the default
    resolves the repo and token from the encrypted store and calls GitHub,
    while a caller passing its own bypasses configuration entirely.
    """
    if fetch is None:
        try:
            repo, token = settings()
        except (LookupError, ValueError) as e:
            # Unusable config — not set up yet (LookupError), or set up wrong
            # (ValueError: a pasted URL, a malformed owner/name). Record it and
            # return quietly rather than raising: no request can be built, so
            # retrying in 60s changes nothing, and the sweeper's handler would
            # write the identical ops-log line every minute forever. The UI
            # still sees the reason, which is the half that must not be quiet.
            with _lock:
                _cache["error"] = str(e)
            return snapshot()

        def fetch(etag):  # noqa: E306 — the default, bound to this run's config
            return _list_issues(repo, token, etag)

    with _lock:
        etag = _cache["etag"]
        held_off = _cache["backoff_until"] and time.time() < _cache["backoff_until"]
    if held_off:
        # Deliberately silent and deliberately not an error: we are waiting on
        # purpose, and the reason is already in _cache["error"].
        return snapshot()

    try:
        status, payload, headers = fetch(etag)
    except Exception as e:
        # Record it where the UI can see it, then re-raise so the sweeper
        # thread's own handler puts it in the ops log. What must NOT happen is
        # the cache being cleared: a failed sweep means the issue state is
        # unknown, which is a different thing from there being no issues.
        until = _backoff_until(e)
        with _lock:
            if until:
                _cache["backoff_until"] = until
                _cache["error"] = (f"GitHub rate limit reached — not asking again "
                                   f"until unix {int(until)}")
            else:
                _cache["error"] = str(e)
        raise

    if status != 304 and not isinstance(payload, list):
        # A 200 whose body is not a list is GitHub telling us something we did
        # not ask about — an error object, a truncated read. Refuse it rather
        # than caching it as "the issues".
        problem = f"unexpected response from GitHub: {type(payload).__name__}, expected a list"
        with _lock:
            _cache["error"] = problem
        raise ValueError(problem)

    now = time.time()
    with _lock:
        # 304 means "your copy is current" — the body is empty, so the issues
        # we already hold ARE the answer. Overwriting them here would turn a
        # free, successful sweep into an apparently empty repo.
        if status != 304:
            # The issues endpoint returns pull requests as well — they are the
            # ones carrying a pull_request key. A PR is not a lab issue.
            _cache["issues"] = [_project(i) for i in payload
                                if isinstance(i, dict) and "pull_request" not in i]
            _cache["etag"] = (headers or {}).get("ETag") or _cache["etag"]
            _cache["changed"] = now
        # Bumped on a 304 too: the answer was confirmed current, which is what
        # "last checked" means. "changed" deliberately does not move — the two
        # are different questions and the UI asks both.
        _cache["checked"] = now
        _cache["error"] = None
        _cache["backoff_until"] = None
    return snapshot()


def _list_issues(repo: str, token: str, etag: str | None):
    """One conditional GET of the repo's open issues.

    Returns `(status, payload, headers)`. A 304 arrives from `httpclient` as an
    `HttpError` because it raises on anything outside 2xx — here it is the
    success case, and the cheap one: it does not count against the rate limit.
    """
    headers = _headers(token)
    if etag:
        headers["If-None-Match"] = etag
    path = f"/repos/{repo}/issues?state=open&per_page={PAGE_SIZE}"
    try:
        payload, hdrs = request("GET", API_BASE + path, headers=headers, verify_tls=True,
                                timeout=TIMEOUT, return_headers=True)
    except HttpError as e:
        if e.status == 304:
            return 304, None, e.headers
        # Same actionable prose the Setup TEST button gives. Without this the
        # sweep reports "HTTP 401 from api.github.com…", which tells a homelab
        # owner nothing about minting a new token. The wire details ride along
        # so the caller can still read rate-limit headers off it.
        mapped = _explain(e, repo)
        mapped.status, mapped.headers = e.status, e.headers
        raise mapped from e
    # Pagination is not followed: a lab repo with more than 100 open incidents
    # has a bigger problem than this sweep. Say so rather than silently
    # truncating the backlog.
    if 'rel="next"' in (hdrs.get("Link") or ""):
        oplog.add("warn", "labissues",
                  f"more than {PAGE_SIZE} open lab issues — only the first page is shown")
    return 200, payload, hdrs


def start() -> None:
    sweeper.spawn("labissues", sweep, SWEEP_INTERVAL,
                  system="labissues", error="lab issues sweep failed")
