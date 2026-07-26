"""Lab issues — ClaudeOS's link to the private GitHub lab repo.

A **lab issue** is a homelab problem a human raises as a GitHub issue in a
dedicated private repo, for ClaudeOS to triage (see CONTEXT.md). This module
owns every call ClaudeOS makes to that repo. It is deliberately **not** a
connector (ADR-0001): nothing here is polled, and GitHub being briefly
unreachable is not a lab incident.

It owns the credential read, the Setup-page connection test, the
ETag-conditional sweep that keeps a local picture of the repo's open issues, and
the triage run: an agentic pass over one issue with read-only access to the lab,
whose verdict is posted back as a comment.

Its public entry points take their dependencies as arguments — `sweep(fetch=…)`,
`triage(number, analyse=…, comment=…, label=…)`. That is a departure from the
prevailing style in this codebase and it is the point: the failure modes here
(re-triage loops, an unmarked failure, a verdict that will not parse back) are
silent and expensive, so they are tested at this interface with no network, no
Anthropic call and no credentials.

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

import json
import re
import threading
import time

from . import notify as notify_mod
from . import oplog, store, sweeper, triagelog, verdict, toolloop, tools
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

# Triage needs the agentic loop, which needs the SDK. Re-exported so the route
# and the UI can state the precondition instead of discovering it as a failure.
HAS_SDK = toolloop.HAS_SDK
MODEL = toolloop.MODEL

TRIAGED_LABEL = "claudeos:triaged"
# Lower than chat's 15: a triage run is unattended, so nobody is watching the
# spend. The daily ledger that bounds it across runs is a separate concern.
TRIAGE_ITERATIONS = 10
TRIAGE_SECONDS = 300        # wall clock: nobody is watching an unattended run

# Closed vocabularies. Four verdicts because a refuted hypothesis is a useful
# result rather than a failure, and "looked, found nothing" is not the same
# answer as "could not tell". A status per finding because one real run mixed
# success, no_data and truncated, and a single figure at the top would have
# hidden that a key field came back empty. `excluded` names evidence
# deliberately not used, with its reason.
# A kind, because the honest output of a real run was a diagnostic and not a
# repair — a shape that assumes a fix will have one manufactured for it.


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


def _post(path: str, token: str, body: dict):
    """POST to an api.github.com path — a write against the *tracker*.

    Distinct from the lab being read-only during triage: commenting and
    labelling are the two writes this feature makes, and they touch GitHub, not
    the homelab (CONTEXT.md, "read-only").
    """
    return request("POST", API_BASE + path, headers=_headers(token), json_body=body,
                   verify_tls=True, timeout=TIMEOUT)


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


# --------------------------------------------------------------- the triage run

class TriageFailed(Exception):
    """A run that died part-way, carrying what it had already spent.

    The spend rides along because it happened: an error that discards it
    understates the cost of the feature, and a per-day ledger cannot be built on
    numbers that only exist for runs that succeeded.
    """

    def __init__(self, message: str, spend: dict | None = None):
        super().__init__(message)
        self.spend = spend or verdict.normalised_cost(None)


def _issue_prompt(issue: dict) -> str:
    labels = ", ".join(issue.get("labels") or []) or "none"
    body = (issue.get("body") or "").strip() or "(the issue has no description)"
    return (f"Triage this lab issue.\n\n"
            f"Issue #{issue.get('number')}: {issue.get('title')}\n"
            f"Opened: {issue.get('created_at')}\n"
            f"Last updated: {issue.get('updated_at')}\n"
            f"Labels: {labels}\n\n"
            f"--- what the owner wrote ---\n{body}")


def _analyse(issue: dict, client=None) -> tuple:
    """Run the agentic pass over one issue. Returns (verdict, spend).

    Read-only against the lab twice over: the schema list excludes the write
    tools, so the model is never shown one, and no approval hook is passed, so
    the loop refuses a write even if one somehow reached it.

    Raises TriageFailed on every ending that is not a verdict — including the
    loop's own error path — carrying the spend so the caller can still record it.
    """
    client = client or toolloop.new_client()
    messages = [{"role": "user", "content": _issue_prompt(issue)}]
    spend = verdict.normalised_cost(None)

    for event, payload in toolloop.run(
            client, messages,
            schemas=tools.schemas(include_writes=False),
            system=toolloop.system_blocks(verdict.TRIAGE_PROMPT),
            final_schema=verdict.VERDICT_SCHEMA,
            max_iterations=TRIAGE_ITERATIONS, max_seconds=TRIAGE_SECONDS):
        if event not in ("finished", "suspended"):
            continue
        usage = payload.get("usage")
        spend = verdict.normalised_cost({
            "input": toolloop.prompt_tokens(usage) if usage else 0,
            # Summed across the run, like "input" and "usd". usage_last holds
            # only the final reply, so reading it here under-reported every
            # multi-turn run and would not reconcile against usd.
            "output": int(payload.get("output_tokens") or 0),
            "usd": payload.get("cost") or 0.0,
        })
        if payload.get("error"):
            raise TriageFailed(payload["error"], spend)
        if event == "suspended":
            raise TriageFailed("the run asked to make a change, which an unattended "
                               "triage cannot approve", spend)
        result = payload.get("structured")
        if not isinstance(result, dict):
            raise TriageFailed("the run ended without a verdict", spend)
        return result, spend

    raise TriageFailed("the run produced no result at all", spend)


def _fetch_issue(repo: str, token: str, number: int) -> dict:
    """One issue, fresh from GitHub rather than from the sweep cache.

    The cache holds the first page of open issues as of the last sweep; triage
    is worth one request to read the body as it stands now, and it works for an
    issue the sweep never listed.
    """
    try:
        issue = _get(f"/repos/{repo}/issues/{number}", token)
    except HttpError as e:
        if e.status == 404:
            raise LookupError(
                f"no issue #{number} in {repo} — it may have been deleted, or the "
                f"token cannot see this repository") from e
        raise _explain(e, repo) from e
    if not isinstance(issue, dict) or not issue.get("number"):
        raise ConnectionError(f"unexpected response from GitHub for issue #{number}")
    if "pull_request" in issue:
        raise ValueError(f"#{number} in {repo} is a pull request, not a lab issue")
    return _project(issue)


def _failure_prose(number: int, error: str) -> str:
    return (f"**ClaudeOS could not finish triaging this issue.** The run failed: "
            f"{error}\n\nThe `{TRIAGED_LABEL}` label has still been applied. That is "
            f"deliberate: an unmarked failure would be picked up and paid for again "
            f"on every sweep from now on. Remove the label to try again.")


def triage(number, *, issue=None, analyse=None, comment=None, label=None,
           client=None) -> dict:
    """Triage one issue on demand: gather evidence, post the verdict, mark it done.

    Every dependency is injectable, and the defaults are the real thing:

      * `issue` — the issue dict (`_project` shape). Default: fetched from GitHub.
      * `analyse(issue) -> (verdict, spend)` — the agentic run; raises
        TriageFailed if it dies part-way. Default: `_analyse`.
      * `comment(number, body)` — posts the comment. Default: GitHub.
      * `label(number, names)` — applies the labels. Default: GitHub.
      * `client` — the Anthropic client the default `analyse` drives. Passing one
        exercises the real run, and its read-only guarantee, without an API key.

    Returns what happened, including on the failure path: a run that dies is
    still a completed triage as far as the tracker is concerned, so it reports
    rather than raises. Exceptions are reserved for preconditions that stopped
    it starting — no SDK, no credentials, no such issue.

    **This does not decide whether the issue should be triaged.** It applies
    `claudeos:triaged` when it finishes, but it never reads it: selecting what
    to triage, and the concurrency and budget that bound a sweep, belong to the
    caller.
    """
    if analyse is None and client is None and not HAS_SDK:
        raise LookupError(
            "triage needs the anthropic SDK — start ClaudeOS with .venv/bin/python3 "
            "server.py (the issue sweep and the AI analysis features work either way)")
    try:
        number = int(number)
    except (TypeError, ValueError):
        raise ValueError(f"issue number must be a number — got {number!r}") from None
    if number <= 0:
        raise ValueError(f"issue number must be positive — got {number}")

    repo = token = None
    if issue is None or comment is None or label is None:
        repo, token = settings()          # LookupError / ValueError if unusable
    if issue is None:
        issue = _fetch_issue(repo, token, number)
    analyse = analyse or (lambda i: _analyse(i, client=client))
    comment = comment or (lambda n, body: _post_comment(repo, token, n, body))
    label = label or (lambda n, names: _add_labels(repo, token, n, names))

    error = None
    try:
        result, spend = analyse(issue)
    except TriageFailed as e:
        result, spend, error = None, e.spend, str(e)
    except Exception as e:  # noqa: BLE001 — any failure still has to mark the issue
        result, spend, error = None, verdict.normalised_cost(None), f"{type(e).__name__}: {e}"

    block = verdict.machine_block(result, cost=spend, error=error)
    prose = (_failure_prose(number, error) if error
             else (str((result or {}).get("summary") or "").strip()
                   or "(the run returned no summary)"))
    body = verdict.comment_body(block, prose)

    posted, unposted = None, None
    try:
        posted = comment(number, body)
    except Exception as e:  # noqa: BLE001 — the marker matters more than the comment
        unposted = f"{type(e).__name__}: {e}"
        oplog.add("error", "labissues",
                  f"triage #{number}: verdict could not be posted: {unposted}")

    # Last, and unconditionally. The marker goes on even when the run failed and
    # even when the comment did not post: an issue left unmarked is re-triaged,
    # and paid for, on every sweep from then on — the retry storm recorded on the
    # weekly report's last-run timestamp, which is written only on success.
    labelled, unlabelled = True, None
    try:
        label(number, [TRIAGED_LABEL])
    except Exception as e:  # noqa: BLE001
        # The one call that must not fail silently. Unmarked means #36 re-triages
        # this issue, and pays for it, on every sweep from now on. Nothing here
        # can force the label on, so make it loud rather than raising: the run
        # did happen and the verdict is already posted.
        labelled = False
        unlabelled = f"{type(e).__name__}: {e}"
        oplog.add("error", "labissues",
                  f"triage #{number}: NOT marked {TRIAGED_LABEL} — it will be "
                  f"re-triaged until this is fixed: {unlabelled}")

    oplog.add("warn" if error else "action", "labissues",
              f"triage #{number}: {block['verdict']} "
              f"({block['confidence']} confidence, {block['severity']}) "
              f"${block['cost']['usd']:.4f}" + (f" — {error}" if error else ""))
    return {"number": number, "title": issue.get("title"), "ok": error is None,
            # The prose as well as the block. The block deliberately has no
            # `summary` — it is the half a human reads — but the detail card
            # renders both, and re-reading GitHub for text this process just
            # wrote would be silly.
            "verdict": block, "summary": prose, "error": error, "unposted": unposted,
            "labelled": labelled, "unlabelled": unlabelled, "label": TRIAGED_LABEL,
            "comment_url": posted.get("html_url") if isinstance(posted, dict) else None}


def _post_comment(repo: str, token: str, number: int, body: str) -> dict:
    try:
        return _post(f"/repos/{repo}/issues/{number}/comments", token, {"body": body})
    except HttpError as e:
        raise _explain(e, repo) from e


def _add_labels(repo: str, token: str, number: int, names: list) -> list:
    """Apply labels to an issue. GitHub creates a label that does not exist yet,
    so `claudeos:triaged` needs no setup step in the lab repo."""
    try:
        return _post(f"/repos/{repo}/issues/{number}/labels", token,
                     {"labels": list(names)})
    except HttpError as e:
        raise _explain(e, repo) from e


# ------------------------------------------------------- one run at a time

# `triage()` deliberately owns neither concurrency nor the local record — its
# docstring says the caller decides both, because the automatic sweep (#36) has
# to. But every caller wants the same two things around it, so the composition
# lives here rather than being written twice and drifting.
#
# The slot is held for the whole run, which is minutes; `_run` is guarded
# separately so `running()` answers immediately instead of blocking behind it.
_slot = threading.Lock()
_run_lock = threading.Lock()
_run: dict = {"number": None, "since": None}


def running() -> dict:
    """Which issue is being triaged right now, if any.

    Read by the queue view, so a second browser tab shows the row as running
    rather than as untriaged with a live trigger button.
    """
    with _run_lock:
        return dict(_run)


def run_triage(number, **kwargs) -> dict:
    """`triage()` with the one-run-at-a-time rule applied and the result stored.

    Two concurrent runs are two unattended agentic passes billed in parallel,
    against a lab whose state one of them may be misreading because the other is
    mid-investigation. Refusing the second is cheaper than either.

    Refuses rather than queues: the caller knows whether waiting makes sense.
    The queue view queues client-side, and #36's sweep will simply try again on
    its next pass.
    """
    # Coerced up front, before the slot is taken: the route hands over a string
    # captured from the URL, and `running()` is compared against a row's issue
    # number in the view, where "12" and 12 are not the same row.
    try:
        number = int(number)
    except (TypeError, ValueError):
        raise ValueError(f"issue number must be a number — got {number!r}") from None

    if not _slot.acquire(blocking=False):
        raise ValueError(f"a triage run is already in progress (issue "
                         f"#{running()['number']}) — runs are one at a time, so the "
                         f"lab is not being read by two investigations at once")
    with _run_lock:
        _run.update(number=number, since=time.time())

    try:
        result = triage(number, **kwargs)
    finally:
        with _run_lock:
            _run.update(number=None, since=None)
        _slot.release()

    # Charged first, and in its own try. The record is a convenience GitHub can
    # replace; the charge is money that has already left, and putting the two in
    # one block let a failed record write swallow it.
    #
    # In `run_triage` rather than `triage()` for the same reason as the record:
    # `triage()` reports what one run did, and what a day of runs has cost is
    # the caller's ledger. Charged whether the run finished or died — the tokens
    # were billed either way.
    try:
        triagelog.spend((result.get("verdict") or {}).get("cost", {}).get("usd") or 0.0)
    except Exception as e:  # noqa: BLE001
        oplog.add("error", "labissues",
                  f"triage #{number}: ${(result.get('verdict') or {}).get('cost', {}).get('usd', 0):.4f} "
                  f"spent but NOT charged to today's budget: {type(e).__name__}: {e}")

    try:
        triagelog.record(number, result)
    except Exception as e:  # noqa: BLE001 — the run happened; the record is a convenience
        # GitHub already has the verdict, which is the copy that matters. Losing
        # the local one costs a re-read, so this is a warning and not an error.
        oplog.add("warn", "labissues",
                  f"triage #{number}: verdict not stored locally: {type(e).__name__}: {e}")
    return result


# ------------------------------------------------------ triage without asking

# Its own thread rather than a step inside `sweep`. A run takes minutes, and
# bolting it onto the 60s sweep would freeze the queue's own refresh for the
# duration — the page would show a stale backlog while the thing it is waiting
# for happens. The two share nothing but the cache they read.
AUTO_INTERVAL = 60


def eligible(issues: list, *, label: str = TRIAGED_LABEL,
             records: dict | None = None, seen_at=None) -> list:
    """The open issues that have not been triaged, oldest first.

    **The gate is the label, and only the label.** Not a timestamp: posting the
    triage comment bumps the issue's own `updated_at`, so a watermark anchored
    at fetch time matches forever and re-triages indefinitely, while one
    advanced to post time silently swallows every human comment that lands in
    the window. Idempotency here has to be a predicate on content.

    Two things then guard the gate itself, because the label is read from a
    *cached copy* of the tracker and a wrong read here is a second paid run:

    * **A run we could not mark is not retried.** When the label write fails,
      `triage()` reports `labelled: false` and the issue stays label-free
      forever — so the label alone would re-run it every pass, which is exactly
      the retry storm this feature was designed against. A local record saying
      "we ran this and could not mark it" stops that. Removing the label by
      hand still works: that record says `labelled: true`.
    * **A label the cache cannot have seen yet is not believed.** The sweep and
      this pass are separate threads on the same interval, so a run finishing at
      T can be followed by a pass reading a cache last filled before T — the
      label is on the issue, absent from our copy, and the issue is triaged
      twice. `seen_at` is when that copy was last actually refreshed; a record
      newer than it means the copy is too old to rule on.

    Oldest first so a backlog drains in the order it arrived.
    """
    records = records or {}
    out = []
    for i in issues or []:
        if i.get("state") == "closed" or label in (i.get("labels") or []):
            continue
        rec = records.get(str(i.get("number")))
        if rec:
            if rec.get("labelled") is False:
                continue
            if not seen_at or (rec.get("ts") or 0) > seen_at:
                continue
        out.append(i)
    return sorted(out, key=lambda i: (i.get("created_at") or "", i.get("number") or 0))


def auto_triage_once(*, issues=None, seen_at=None, run=None, notifier=None) -> dict:
    """One pass of the unattended sweep: triage the oldest eligible issue, or
    explain why not.

    Takes at most one issue per pass. A backlog of eight drains over eight
    passes rather than firing eight expensive runs at once — which also means
    there is never a second run to race the ledger.

    Returns `{"ran": number|None, "skipped": reason|None}`; `skipped` is None
    when there was simply nothing to do, because a stalled queue and an idle one
    must not look the same.
    """
    # The SDK is the default runner's precondition, not the pass's — the same
    # split `triage()` makes, and what lets this be driven by a fake.
    if run is None and not HAS_SDK:
        # Stated once, not once a minute. Triage is simply unavailable, exactly
        # as chat is, and the queue says so from `triage_available`.
        if triagelog.mark("logged_nosdk"):
            oplog.add("warn", "labissues",
                      "automatic triage is off: the anthropic SDK is not installed")
        return {"ran": None, "skipped": "nosdk"}

    if issues is None:
        snap = snapshot()
        issues, seen_at = snap["issues"], snap["changed"]
    run = run or run_triage
    notifier = notifier or notify_mod.send

    if running()["number"] is not None:
        # A manual run holds the slot. Not an error and not worth a log line —
        # the next pass is 60 seconds away.
        return {"ran": None, "skipped": "busy"}

    todo = eligible(issues, records=triagelog.summaries(), seen_at=seen_at)
    if not todo:
        return {"ran": None, "skipped": None}

    budget = triagelog.ledger()
    if budget["state"] != "ok":
        _budget_says_no(budget, len(todo), notifier)
        return {"ran": None, "skipped": "budget"}

    number = todo[0]["number"]
    try:
        run(number)
    except ValueError as e:
        # The slot was taken between the check and the call, or the number was
        # rejected. Either way the next pass tries again; nothing was spent.
        oplog.add("warn", "labissues", f"automatic triage skipped #{number}: {e}")
        return {"ran": None, "skipped": "busy"}
    except (LookupError, ConnectionError) as e:
        # A precondition, not a run: no credentials, a revoked token, GitHub
        # down. Nothing was spent and nothing was marked, so this issue is still
        # eligible — correct, and not a retry of a *run*.
        #
        # Logged once a day, not once a pass. A revoked token does not resolve
        # itself, and 1,440 copies of the same line is how an ops log stops
        # being read — the same reason `sweep()` records its own failures in the
        # cache rather than the log. Telling somebody is #38's job.
        if triagelog.mark("logged_start_error"):
            oplog.add("error", "labissues",
                      f"automatic triage cannot start: {e} — retrying quietly each "
                      f"minute; this line will not repeat today")
        return {"ran": None, "skipped": "error"}
    return {"ran": number, "skipped": None}


def _budget_says_no(budget: dict, waiting: int, notifier) -> None:
    """Say it once per day, at the volume the band deserves.

    The notification deliberately claims only what is true: the sweep is
    blocked and issues are waiting. It does *not* claim a run overshot —
    hand-triggered runs are charged to the same ledger, so the money may have
    been spent by somebody who meant to.
    """
    if budget["state"] == "soft" and triagelog.mark("logged"):
        oplog.add("warn", "labissues",
                  f"automatic triage paused on budget: ${budget['usd']:.2f} of today's "
                  f"${budget['soft']:.2f} soft limit spent — {waiting} issue(s) waiting "
                  f"until midnight")
    if budget["state"] in ("hard", "stopped") and triagelog.mark("notified"):
        oplog.add("error", "labissues",
                  f"automatic triage stopped on budget: ${budget['usd']:.2f} spent today, "
                  f"past the ${budget['hard']:.2f} hard limit — {waiting} issue(s) waiting")
        notifier(title="ClaudeOS: lab triage stopped on budget",
                 message=(f"Triage has spent ${budget['usd']:.2f} today, past the "
                          f"${budget['hard']:.2f} hard limit. Nothing will be triaged "
                          f"automatically until the day resets; {waiting} issue(s) are "
                          f"waiting. Triggering a run by hand still works."),
                 priority="high", tags=["money_with_wings"])


# ----------------------------------------------------------- the whole verdict

def verdict_for(number, *, comments=None) -> dict:
    """Everything the detail card renders for one issue.

    Prefers the local record, falls back to reading it back out of the issue's
    own comments. The fallback is not a nicety: the record is a convenience a
    `data/` wipe may destroy, and the machine block exists precisely so the
    verdict survives in the place a human can also read it. An issue carrying
    the triaged label whose verdict is only on GitHub must still open.

    `source` says which it was, because "we ran this" and "we read this back"
    are different claims and the card says so.
    """
    number = int(number)
    about = _issue_facts(number)
    stored = triagelog.get(number)
    if stored and isinstance(stored.get("verdict"), dict):
        # A record written before the prose was kept locally still has prose —
        # on GitHub, in the comment, which is the copy that was always the real
        # one. Worth one request on a detail view to not show a verdict with its
        # reasoning missing.
        prose = stored.get("summary") or _prose_from_comments(number, comments)
        return {**about, "number": number, "source": "local",
                "title": stored.get("title") or about.get("title"),
                "verdict": stored["verdict"], "summary": prose,
                "ok": bool(stored.get("ok")), "error": stored.get("error"),
                "labelled": stored.get("labelled", True),
                "comment_url": stored.get("comment_url"), "ts": stored.get("ts")}

    found = _latest_triage_comment(number, comments)
    if not found:
        return {**about, "number": number, "source": "none", "verdict": None, "summary": "",
                "ok": None, "error": None, "labelled": None,
                "comment_url": None, "ts": None}

    block, comment = found
    return {**about, "number": number, "source": "github", "verdict": block,
            "summary": verdict.prose_of(comment.get("body")),
            "ok": "error" not in block, "error": block.get("error"),
            "labelled": None, "comment_url": comment.get("html_url"), "ts": None}


def _latest_triage_comment(number: int, comments=None) -> tuple | None:
    """`(block, comment)` for the newest comment carrying a machine block.

    Last one wins: an issue triaged twice carries two blocks, and the current
    verdict is the later one. `comments(n) -> list` is the seam; the default
    resolves the repo and token and asks GitHub.
    """
    if comments is None:
        repo, token = settings()

        def comments(n):  # noqa: E306 — the default, bound to this run's config
            return _list_comments(repo, token, n)

    found = None
    for c in comments(number) or []:
        if not isinstance(c, dict):
            continue
        block = verdict.parse_verdict(c.get("body"))
        if block:
            found = (block, c)
    return found


def _prose_from_comments(number: int, comments=None) -> str:
    """The prose of the newest triage comment, or "" if it cannot be had.

    Best effort by design: this only ever supplements a verdict already in hand,
    so GitHub being unreachable must cost the reasoning, not the card.
    """
    try:
        found = _latest_triage_comment(number, comments)
        return verdict.prose_of(found[1].get("body")) if found else ""
    except Exception:  # noqa: BLE001 — a missing paragraph is not a failed page
        return ""


def _issue_facts(number: int) -> dict:
    """The issue's own title and URL, from the sweep cache — best effort.

    Best effort on purpose: the card is about the verdict, and an unconfigured
    or not-yet-swept install must still render one it holds locally rather than
    failing on a missing link.
    """
    for i in snapshot()["issues"]:
        if i.get("number") == number:
            return {"title": i.get("title"), "issue_url": i.get("html_url")}
    try:
        repo, _ = settings()
        return {"title": None, "issue_url": f"https://github.com/{repo}/issues/{number}"}
    except (LookupError, ValueError):
        return {"title": None, "issue_url": None}


def _list_comments(repo: str, token: str, number: int) -> list:
    """Every comment on one issue. Only ever called for a detail view, so the
    page-size cap is the same judgement as the sweep's: an issue with more than
    100 comments is not one this card is going to render usefully anyway."""
    try:
        got = _get(f"/repos/{repo}/issues/{number}/comments?per_page={PAGE_SIZE}", token)
    except HttpError as e:
        if e.status == 404:
            raise LookupError(f"no issue #{number} in {repo}") from e
        raise _explain(e, repo) from e
    return got if isinstance(got, list) else []


def start() -> None:
    sweeper.spawn("labissues", sweep, SWEEP_INTERVAL,
                  system="labissues", error="lab issues sweep failed")
    sweeper.spawn("labtriage", auto_triage_once, AUTO_INTERVAL,
                  system="labissues", error="automatic triage pass failed")
