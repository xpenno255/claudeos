"""Lab issues — ClaudeOS's link to the private GitHub lab repo.

A **lab issue** is a homelab problem a human raises as a GitHub issue in a
dedicated private repo, for ClaudeOS to triage (see CONTEXT.md). This module
owns every call ClaudeOS makes to that repo. It is deliberately **not** a
connector (ADR-0001): nothing here is polled, and GitHub being briefly
unreachable is not a lab incident.

Today it owns exactly the credential read plus the Setup-page connection
test. The sweep loop, ETag cache and triage arrive in later tickets.

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

from . import store
from .httpclient import HttpError, request

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"  # pinned: GitHub versions its REST API by date
TIMEOUT = 15

# owner/name, GitHub's own character set for both halves
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

PAT_HINT = ("Repository access → Only select repositories, "
            "Permissions → Issues: Read and write")


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
    hdrs = e.headers
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
        if hdrs is not None and (hdrs.get("X-RateLimit-Remaining") or "").strip() == "0":
            reset = (hdrs.get("X-RateLimit-Reset") or "?").strip()
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
    remaining = (hdrs.get("X-RateLimit-Remaining") or "?").strip() if hdrs else "?"
    return {"ok": True,
            "detail": f"{full} reachable ({visibility}) — issues readable, {backlog}; "
                      f"{remaining} API requests left this hour"}
