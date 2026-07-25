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

from . import oplog, store, sweeper, toolloop, tools
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
MARKER = "claudeos-triage"
BLOCK_VERSION = 1
# Lower than chat's 15: a triage run is unattended, so nobody is watching the
# spend. The daily ledger that bounds it across runs is a separate concern.
TRIAGE_ITERATIONS = 10

# Closed vocabularies. Four verdicts because a refuted hypothesis is a useful
# result rather than a failure, and "looked, found nothing" is not the same
# answer as "could not tell". A status per finding because one real run mixed
# success, no_data and truncated, and a single figure at the top would have
# hidden that a key field came back empty. `excluded` names evidence
# deliberately not used, with its reason.
VERDICTS = ("diagnosed", "refuted", "inconclusive", "no_fault_found")
CONFIDENCES = ("low", "medium", "high")
SEVERITIES = ("critical", "serious", "warning", "info")
EVIDENCE_STATUSES = ("success", "no_data", "truncated", "excluded")
# A kind, because the honest output of a real run was a diagnostic and not a
# repair — a shape that assumes a fix will have one manufactured for it.
REMEDIATION_KINDS = ("fix", "diagnostic", "none")


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


# ----------------------------------------------------------------- the verdict

TRIAGE_TAIL = """## Your job

You are triaging one issue from this homelab's issue tracker, raised by the person who owns the lab. You are working unattended: there is nobody to ask a question of, and nothing you say reaches them until you finish.

Work out what is actually wrong. Form hypotheses, then use the tools to test them against the lab's real state. Name the hypotheses you rule out — a hypothesis you can eliminate is a useful result, not a failure, and it saves the owner from checking it themselves.

Your tools are read-only. You cannot change anything, restart anything or apply a fix, and you must not claim to have done so. Nor can you ask for permission: if the only way forward is a change, the honest answer is a diagnostic — the exact next step for a human to take.

Stop when the evidence stops paying for itself. An issue you cannot settle is a real outcome; say what you checked, what you found, and what would settle it.

## Your answer

Your final answer is a structured record. The fields:

- `summary` — the comment a human reads on the issue. Markdown prose, addressed to the lab's owner. Lead with the answer, then the evidence for it, then what to do next. Quote what you actually saw. Hedge anything you did not directly confirm, and say plainly which areas went unverified and why.
- `verdict` — `diagnosed` when you have identified the cause. `refuted` when the issue's own stated cause is wrong, whether or not you found the real one. `no_fault_found` when you looked where the issue points and everything there is healthy. `inconclusive` when you could not tell.
- `confidence` — in the verdict, not in the lab.
- `severity` — the impact on the home if this is left alone, not how interesting it is.
- `refuted` — each hypothesis you ruled out, named in a short phrase.
- `evidence` — one entry per finding that carries weight, with the tool it came from and that result's own status: `success`, `no_data` (the query worked and found nothing — never report this as healthy), `truncated` (you saw part of it), or `excluded` for something you looked at and deliberately did not rely on, with the reason in the note.
- `remediation` — `kind` is `fix` for a change that resolves it, `diagnostic` for the next step that would narrow it down, `none` when there is nothing to do. `text` is what a human should actually do, specific enough to follow. ClaudeOS never runs it; a person reads it and decides."""

TRIAGE_PROMPT = f"{toolloop.BASE_PROMPT}\n\n{TRIAGE_TAIL}"

# The shape asked of the model. Deliberately not the same shape as the machine
# block: `summary` is prose for the comment and never goes in the block, and
# `executable` is absent because ClaudeOS stamps it — the model does not get to
# say whether this app will run something.
VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "verdict", "confidence", "severity", "refuted",
                 "evidence", "remediation"],
    "properties": {
        "summary": {"type": "string",
                    "description": "Markdown prose for the lab's owner, posted as the "
                                   "issue comment. Answer first, then the evidence."},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "refuted": {"type": "array", "items": {"type": "string"},
                    "description": "Each hypothesis ruled out, as a short named phrase."},
        "evidence": {
            "type": "array",
            "description": "One entry per finding that carries weight.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["tool", "status", "note"],
                "properties": {
                    "tool": {"type": "string",
                             "description": "The tool this finding came from."},
                    "status": {"type": "string", "enum": list(EVIDENCE_STATUSES)},
                    "note": {"type": "string",
                             "description": "What it showed. For `excluded`, why it "
                                            "was not relied on."},
                },
            },
        },
        "remediation": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "text"],
            "properties": {
                "kind": {"type": "string", "enum": list(REMEDIATION_KINDS)},
                "text": {"type": "string",
                         "description": "What a human should do. Empty when kind is "
                                        "`none`."},
            },
        },
    },
}

_BLOCK_RE = re.compile(r"<!--\s*" + re.escape(MARKER) + r"\s*(.*?)-->", re.DOTALL)


class TriageFailed(Exception):
    """A run that died part-way, carrying what it had already spent.

    The spend rides along because it happened: an error that discards it
    understates the cost of the feature, and a per-day ledger cannot be built on
    numbers that only exist for runs that succeeded.
    """

    def __init__(self, message: str, spend: dict | None = None):
        super().__init__(message)
        self.spend = spend or _spend(None)


def _spend(cost) -> dict:
    c = cost if isinstance(cost, dict) else {}
    return {"input": int(c.get("input") or 0),
            "output": int(c.get("output") or 0),
            "usd": round(float(c.get("usd") or 0.0), 6)}


def _one_of(value, allowed: tuple, fallback: str) -> str:
    return value if value in allowed else fallback


def machine_block(result: dict | None, *, cost=None, error: str | None = None) -> dict:
    """A verdict reduced to exactly the fields the hidden block carries.

    Built field by field rather than passed through, because the vocabularies
    are closed and the source is a language model: an invented verdict value or
    an extra key would reach the UI that parses this. Anything unrecognised
    lands on the cautious end — an unknown verdict is `inconclusive`, not a
    diagnosis.

    `executable` is stamped here, always false. The remediation is text and
    ClaudeOS does not run it, so that is not the model's claim to make.
    """
    r = result if isinstance(result, dict) else {}
    rem = r.get("remediation") if isinstance(r.get("remediation"), dict) else {}
    block = {
        "v": BLOCK_VERSION,
        "verdict": _one_of(r.get("verdict"), VERDICTS, "inconclusive"),
        "confidence": _one_of(r.get("confidence"), CONFIDENCES, "low"),
        "severity": _one_of(r.get("severity"), SEVERITIES, "info"),
        "refuted": [str(h).strip() for h in (r.get("refuted") or [])
                    if str(h).strip()],
        "evidence": [{"tool": str(e.get("tool") or "?"),
                      "status": _one_of(e.get("status"), EVIDENCE_STATUSES, "excluded"),
                      "note": str(e.get("note") or "")}
                     for e in (r.get("evidence") or []) if isinstance(e, dict)],
        "remediation": {"kind": _one_of(rem.get("kind"), REMEDIATION_KINDS, "none"),
                        "text": str(rem.get("text") or ""),
                        "executable": False},
        "cost": _spend(cost),
    }
    if error:
        # Only on the failure path. A block with an error and an `inconclusive`
        # verdict is how a failed run explains itself to whatever reads it back.
        block["error"] = str(error)
    return block


def comment_body(block: dict, prose: str) -> str:
    """The comment as posted: prose a human reads, then a block they never see.

    GitHub renders an HTML comment invisibly, so one comment serves both
    readers. The escape matters — a note containing `-->` would otherwise close
    the comment early and spill JSON onto the issue; as `\\u003e` it still
    parses back to the same string.
    """
    payload = json.dumps(block, indent=1, sort_keys=True).replace("-->", "--\\u003e")
    return f"{prose.strip()}\n\n<!-- {MARKER}\n{payload}\n-->\n"


def parse_verdict(body: str | None) -> dict | None:
    """The machine block in a comment, or None when there is not a usable one.

    Degrades instead of raising. A comment with no block is the normal case —
    humans write most of them — and a block that will not parse is a comment
    from an older ClaudeOS or a truncated write. Either is a fact the caller can
    render; neither should take down a sweep reading a whole issue's history.
    """
    m = _BLOCK_RE.search(body or "")
    if not m:
        return None
    try:
        block = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(block, dict) or block.get("v") != BLOCK_VERSION:
        return None
    return block


# --------------------------------------------------------------- the triage run

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
    spend = _spend(None)

    for event, payload in toolloop.run(
            client, messages,
            schemas=tools.schemas(include_writes=False),
            system=toolloop.system_blocks(TRIAGE_PROMPT),
            final_schema=VERDICT_SCHEMA,
            max_iterations=TRIAGE_ITERATIONS):
        if event not in ("finished", "suspended"):
            continue
        usage = payload.get("usage")
        spend = _spend({
            "input": toolloop.prompt_tokens(usage) if usage else 0,
            "output": (getattr(usage, "output_tokens", 0) or 0) if usage else 0,
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
        result, spend, error = None, _spend(None), f"{type(e).__name__}: {e}"

    block = machine_block(result, cost=spend, error=error)
    prose = (_failure_prose(number, error) if error
             else (str((result or {}).get("summary") or "").strip()
                   or "(the run returned no summary)"))
    body = comment_body(block, prose)

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
    label(number, [TRIAGED_LABEL])

    oplog.add("warn" if error else "action", "labissues",
              f"triage #{number}: {block['verdict']} "
              f"({block['confidence']} confidence, {block['severity']}) "
              f"${block['cost']['usd']:.4f}" + (f" — {error}" if error else ""))
    return {"number": number, "title": issue.get("title"), "ok": error is None,
            "verdict": block, "error": error, "unposted": unposted,
            "labelled": True, "label": TRIAGED_LABEL,
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


def start() -> None:
    sweeper.spawn("labissues", sweep, SWEEP_INTERVAL,
                  system="labissues", error="lab issues sweep failed")
