# Research: read-mostly GitHub issues client

**Retrieved**: 2026-07-25. REST behaviour shifts — GitHub ships changes to rate
limits, headers and endpoint semantics without notice to clients, and a second
API version (`2026-03-10`) is already live alongside `2022-11-28`. Re-verify
anything load-bearing before relying on it a year from now.

**Question**: [#16](https://github.com/xpenno255/claudeos/issues/16) — what does a
polling, read-mostly GitHub issues client need to get right, running unattended
from a homelab against `api.github.com` through `app/httpclient.py` (stdlib only)?

**Sources**: `docs.github.com` REST reference and the GitHub changelog only. No
blogs, no Stack Overflow. Where the docs are silent, this file says so rather than
filling the gap.

**Method note — two kinds of claim in this file.** Claims marked **[docs]** carry a
link to the GitHub page that owns them. Claims marked **[observed]** were measured
directly against `api.github.com` on 2026-07-25 using `xpenno255/claudeos` itself
and a stdlib `urllib` client. Observed behaviour is evidence, not a contract:
GitHub has not documented it and may change it. Never build a correctness argument
on an **[observed]** line alone — the observations here are included because
several of them contradict what you would guess, and because the token type used
for them (a `gh` CLI OAuth token, resource `core`) is not a fine-grained PAT.

---

## 1. Rate limits

### Primary limit

**[docs]** "All of these requests count towards your personal rate limit of 5,000
requests per hour."
([Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2022-11-28))
The page frames this as the limit for "a personal access token" without separating
fine-grained from classic — see **Not confirmed**.

**[observed]** `x-ratelimit-limit: 5000`, `x-ratelimit-resource: core` on
`GET /repos/xpenno255/claudeos/issues`. The runtime headers are the authoritative
answer for whatever token you actually hold; treat 5,000 as the expected value and
the header as the truth.

Whether the repo is private makes no documented difference to the budget. The
rate-limit page does not distinguish public from private repositories.

### What each relevant endpoint costs

Every REST request costs exactly **one** unit of the 5,000/hour primary budget —
there is no per-endpoint weighting on the primary limit. The *secondary* limit is
points-based and does weight by method.

**[docs]** Points table
([Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2022-11-28)):
most REST `GET`, `HEAD`, `OPTIONS` = **1 point**; most REST `POST`, `PATCH`, `PUT`,
`DELETE` = **5 points**.

| Call | Primary | Secondary points |
|---|---|---|
| `GET /repos/{o}/{r}/issues` | 1 request | 1 |
| `GET /repos/{o}/{r}/issues/{n}` | 1 request | 1 |
| `GET /repos/{o}/{r}/issues/comments` (repo-wide) | 1 request | 1 |
| `GET /repos/{o}/{r}/issues/{n}/comments` | 1 request | 1 |
| `POST /repos/{o}/{r}/issues/{n}/comments` | 1 request | 5 |
| `POST /repos/{o}/{r}/issues/{n}/labels` | 1 request | 5 |
| `DELETE /repos/{o}/{r}/issues/{n}/labels/{name}` | 1 request | 5 |
| any of the above returning `304` | **0 requests** | see §2 / Not confirmed |
| `GET /rate_limit` | **0 requests** | counts |

**[docs]** `GET /rate_limit`: "Calling this endpoint does not count against your
primary rate limit, but it can count against your secondary rate limit."
([Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2022-11-28);
confirmed in the changelog entry
[Rate limits for /rate_limit REST API endpoint](https://github.blog/changelog/2023-10-18-rate-limits-for-rate_limit-rest-api-endpoint/):
"Requests to the endpoint will not consume the primary rate limit quotas for the
authenticated user. However, making a very high number of requests to the endpoint
in a short period of time will trigger the secondary rate limits.")

### Headers that report the budget

**[docs]** ([Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2022-11-28))

| Header | Meaning (verbatim) |
|---|---|
| `x-ratelimit-limit` | "The maximum number of requests that you can make per hour" |
| `x-ratelimit-remaining` | "The number of requests remaining in the current rate limit window" |
| `x-ratelimit-used` | "The number of requests you have made in the current rate limit window" |
| `x-ratelimit-reset` | "The time at which the current rate limit window resets, in UTC epoch seconds" |
| `x-ratelimit-resource` | "The rate limit resource that the request counted against" |

Header names are lower-case on the wire (HTTP/2). `x-ratelimit-reset` is **UTC
epoch seconds**, not a delta — do not treat it as a duration.

### Secondary rate limits — distinct from primary

Secondary limits are a separate mechanism with separate triggers. **[docs]** you
can hit one by
([Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2022-11-28)):

- concurrency — "No more than 100 concurrent requests are allowed";
- per-endpoint throughput — "No more than 900 points per minute are allowed for
  REST API endpoints";
- content creation — "In general, no more than 80 content-generating requests per
  minute and no more than 500 content-generating requests per hour are allowed",
  and "Content creation limits include actions taken on the GitHub web interface as
  well as via the REST API and GraphQL API";
- CPU — "No more than 90 seconds of CPU time per 60 seconds of real time".

**[docs]** Creating a comment is explicitly flagged: "This endpoint triggers
notifications. Creating content too quickly using this endpoint may result in
secondary rate limiting."
([Create an issue comment](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28#create-an-issue-comment))

### 403 vs 429, and `retry-after`

Both statuses mean the same thing and are used for both limit types — **the status
code does not tell you which limit you hit.** **[docs]**, verbatim
([Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2022-11-28)):

- "If you exceed your primary rate limit, you will receive a `403` or `429`
  response, and the `x-ratelimit-remaining` header will be `0`."
- "If you exceed a secondary rate limit, you will receive a `403` or `429` response
  and an error message that indicates that you exceeded a secondary rate limit."

So the discriminator is `x-ratelimit-remaining == 0` (primary) versus the response
body message (secondary) — not the status code.

Correct client behaviour, in documented precedence order
([Best practices for using the REST API](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api?apiVersion=2022-11-28)):

1. "If the `retry-after` response header is present, you should not retry your
   request until after that many seconds has elapsed."
2. "If the `x-ratelimit-remaining` header is `0`, you should not retry your request
   until after the time, in UTC epoch seconds, specified by the
   `x-ratelimit-reset` header."
3. "Otherwise, wait for at least one minute before retrying."
4. "If your request continues to fail due to a secondary rate limit, wait for an
   exponentially increasing amount of time between retries, and throw an error
   after a specific number of retries."

And the stick: **[docs]** "Continuing to make requests while you are rate limited
may result in the banning of your integration." A naive retry loop is not merely
wasteful here, it is an account risk.

Two further documented obligations for a well-behaved client:

- **[docs]** "To avoid exceeding secondary rate limits, you should make requests
  serially instead of concurrently."
- **[docs]** "If you are making a large number of `POST`, `PATCH`, `PUT`, or
  `DELETE` requests, wait at least one second between each request."

---

## 2. Conditional requests — the decisive fact

**A `304` does not cost a primary request.** **[docs]**, verbatim, and this is the
sentence the whole polling design rests on
([Best practices for using the REST API](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api?apiVersion=2022-11-28)):

> "Making a conditional request does not count against your primary rate limit if a
> `304` response is returned and the request was made while correctly authorized."

Note the two conditions in that sentence: the response must actually be `304`, and
the request must be "correctly authorized". A conditional request that returns
`200` costs a request like any other. **[docs]** "Most endpoints return an `etag`
header, and many endpoints return a `last-modified` header" — send them back as
`if-none-match` and `if-modified-since` respectively (same source).

**[observed]** Confirmed on the live API. `x-ratelimit-used` was **unchanged**
across a `304`, for both validators:

| Request | Status | `x-ratelimit-used` |
|---|---|---|
| `GET /issues?per_page=2&state=all` | 200 | 11 |
| same + `If-None-Match: <etag>` | **304** | **11** |
| `GET /issues/16` | 200 | 53 |
| same + `If-Modified-Since: <last-modified>` | **304** | **53** |

**[observed]** Which validators each endpoint actually offers — this is uneven and
matters:

| Endpoint | `etag` | `last-modified` |
|---|---|---|
| `GET /repos/{o}/{r}/issues` (list) | yes | **no** |
| `GET /repos/{o}/{r}/issues/{n}` (single) | yes | yes |
| `GET /repos/{o}/{r}/issues/comments` (repo-wide) | yes | **no** |
| `GET /repos/{o}/{r}/issues/{n}/comments` | yes | **no** |

So **`ETag`/`If-None-Match` is the only validator available on all three list
endpoints a poller cares about.** Build the poller on ETags; treat
`Last-Modified` as a bonus on single-issue reads.

**[observed]** The `304` response carries `etag` and the `x-ratelimit-*` headers but
**no `link` header** — which is harmless, since a `304` means there is nothing to
paginate.

**[observed]** The issues-list `ETag` appears to be **derived from the response body,
not bound to the request URL**. Requests differing in `since` and in `per_page`
returned byte-identical ETags, and an ETag obtained from one query validated
(`304`) against a different query whose result set happened to be identical:

| Request | Result |
|---|---|
| A: `?state=all&per_page=100&since=2026-01-01T00:00:00Z` | 200, etag `d2bf279c…` |
| B: `?state=all&per_page=100&since=2026-01-02T00:00:00Z` | 200, etag `d2bf279c…` (same) |
| C: B's URL + `If-None-Match: <etag from A>` | **304** |
| E: `?…&per_page=50&since=2026-01-01T00:00:00Z` | 200, etag `d2bf279c…` (same) |

This is undocumented and should not be *relied* on, but it has a useful
consequence: a `since` cursor that advances every poll does **not** automatically
defeat the ETag, so long as the returned set is unchanged. Cache the ETag keyed by
the exact request URL anyway — that is the conservative reading of HTTP semantics
and stays correct if GitHub changes this.

**[observed]** `GET /issues` also returns `cache-control: private, max-age=60,
s-maxage=60`. GitHub does not document what this implies for issues data freshness,
so read it only as a hint that sub-minute polling is unlikely to surface anything
that minute-granularity polling would miss.

**[docs]** GitHub's own stated preference, for the record: "You should subscribe to
webhook events instead of polling the API for data."
([Best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api?apiVersion=2022-11-28))
A homelab box with no inbound ingress cannot receive webhooks, so polling is the
right call here — but it is polling *against* documented advice, which is another
reason to keep the request count near zero via ETags.

---

## 3. Listing and pagination

### `since` — what it keys off

**[docs]** On `GET /repos/{owner}/{repo}/issues`, verbatim:
`since` = "Only show results that were last updated after the given time. This is a
timestamp in ISO 8601 format: YYYY-MM-DDTHH:MM:SSZ."
([List repository issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues))

It keys off **last-updated**, i.e. the issue's `updated_at` — *not* creation time,
and *not* a change feed. Consequences:

- It is a filter, not a cursor: it cannot tell you *what* changed on an issue, only
  that something did.
- It cannot report deletions, and an issue whose `updated_at` moves backwards is not
  a thing, so a monotonic watermark is safe.
- Boundary handling is on you: `since` is documented as "after the given time", so
  re-sending the last-seen `updated_at` should exclude the item you already have —
  but same-second collisions mean you should de-duplicate by issue number and
  `updated_at` rather than trusting the boundary. See **Not confirmed** for the
  inclusive/exclusive question.

**[observed]** **A new comment does bump the parent issue's `updated_at`.** Measured
on issue #16 of this repo: before posting a comment, `updated_at` was
`2026-07-25T13:51:58Z` with `comments: 0`; immediately after posting one it was
`2026-07-25T14:05:15Z` with `comments: 1`, while `created_at` stayed
`2026-07-25T13:51:58Z`. So `GET /issues?since=…` *will* surface an issue that
received a new comment.

That does **not** make the repo-wide comments poll redundant, for three reasons:
the issue record tells you only that *something* changed, not that it was a comment
or which one; the issue payload does not carry comment bodies, so you would need a
follow-up request per changed issue anyway; and whether *editing* an existing comment
bumps the parent issue's `updated_at` was not tested (see **Not confirmed**). Polling
both streams costs one extra conditional request per cycle — and that request is free
whenever it returns `304`.

**[docs]** Also note what the endpoint returns: "GitHub's REST API considers every
pull request an issue, but not every issue is a pull request. For this reason,
'Issues' endpoints may return both issues and pull requests in the response. You can
identify pull requests by the `pull_request` key."
([List repository issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues))
A client that wants issues only must filter on the presence of `pull_request`
itself; there is no query parameter for it.

Other useful parameters on the same endpoint **[docs]**: `state` (default `open` —
so a poller wanting closed issues too must pass `state=all`), `labels` ("A list of
comma separated label names"), `sort` (`created` | `updated` | `comments`, default
`created`), `direction` (default `desc`), `per_page` ("The number of results per
page (max 100)"). For a polling client, `sort=updated&direction=asc` plus `since`
gives oldest-change-first, which is the order you want for advancing a watermark
safely — you can checkpoint after each page.

### `Link`-header pagination

**[docs]** ([Using pagination in the REST API](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api?apiVersion=2022-11-28))

- The `link` header carries URLs with `rel` values `"prev"`, `"next"`, `"first"`,
  `"last"`.
- "If the endpoint does not support pagination, or if all results fit on a single
  page, the `link` header will be omitted." So **absence of `link` is normal**, not
  an error.
- Not all four rels always appear: "the link to the previous page won't be included
  if you are on the first page of results, and the link to the last page won't be
  included if it can't be calculated." Absence of `rel="next"` is the end-of-results
  signal — **do not** rely on `rel="last"` existing.
- Follow the URLs, don't build them: "You can use the URLs from the `link` header to
  request another page of results."
- And the reason that matters: "The query parameters in the `link` URLs may differ
  between endpoints, however each paginated endpoint will use the `page`,
  `before`/`after`, or `since` query parameters." Pagination style is
  **per-endpoint** and can be cursor-based.

**[observed]** That warning is live on exactly the endpoints in scope — the two
list endpoints paginate *differently*:

- `GET /repos/{o}/{r}/issues` returned a **cursor**:
  `link: <…/issues?per_page=2&state=all&after=Y3Vyc29yOnYyOpLPAAABn5mMK0DPAAAAASiYHVM%3D&page=2>; rel="next"`
  — an opaque `after=` cursor, and no `rel="last"`.
- `GET /repos/{o}/{r}/issues/comments` returned **offset** pagination:
  `rel="next"` with `page=2` and `rel="last"` with `page=6`, no cursor.

A client that increments `page=` by hand will therefore work on one of these
endpoints and be subtly wrong on the other. Parse the `link` header.

### Per-issue comments vs the repo-wide comments endpoint

This is the single biggest efficiency lever after ETags.

| | `GET /repos/{o}/{r}/issues/{n}/comments` | `GET /repos/{o}/{r}/issues/comments` |
|---|---|---|
| Scope | one issue | **all issues and PRs in the repo** |
| `since` | **yes** [docs] | **yes** [docs] |
| `sort` / `direction` | no | **yes** — `sort` = `created` \| `updated` (default `created`); `direction` = `asc` \| `desc`, "ignored without the `sort` parameter" [docs] |
| Cost to sweep a repo | N requests for N issues | 1 request per 100 comments |
| Pagination style | — | offset (`page=`) [observed] |

([List issue comments](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28#list-issue-comments),
[List issue comments for a repository](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28#list-issue-comments-for-a-repository))

**Both** support `since`, with the same documented wording — "Only show results that
were last updated after the given time." On the repo-wide endpoint `since` filters
on the *comment's* `updated_at`, which is what a poller wants: it catches an edited
comment, not just new ones.

The repo-wide endpoint is the right primitive for this client. Polling it with
`since` + `sort=updated&direction=asc` finds new and edited comments across every
issue in one conditional request, instead of one request per watched issue. The
per-issue endpoint is then only needed for backfilling the full thread of a specific
issue on demand.

**[docs]** Ordering default, both endpoints: comments are ordered by **ascending
`id`** by default. Since `id` is monotonically increasing per comment created, the
default order is effectively creation order — see §5.

---

## 4. Least-privilege fine-grained PAT permissions

The answer is unusually tidy: **one permission covers all three needs.**

| Need | Permission (GitHub's literal name) | Access level |
|---|---|---|
| Read issues + read comments | **Issues** repository permission | **read** |
| Write issue comments | **Issues** repository permission | **write** |
| Add / remove labels on an issue | **Issues** repository permission | **write** |

**[docs]** The
[permissions reference](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens?apiVersion=2022-11-28)
("For each permission granted to a fine-grained personal access token, these are the
REST API endpoints that the app can use") lists under the **Issues** repository
permission:

- `read`: `GET /repos/{owner}/{repo}/issues`,
  `GET /repos/{owner}/{repo}/issues/{issue_number}`,
  `GET /repos/{owner}/{repo}/issues/comments`,
  `GET /repos/{owner}/{repo}/issues/{issue_number}/comments`,
  `GET /repos/{owner}/{repo}/labels`
- `write`: `POST /repos/{owner}/{repo}/issues/{issue_number}/comments`,
  `POST /repos/{owner}/{repo}/issues/{issue_number}/labels`,
  `DELETE /repos/{owner}/{repo}/issues/{issue_number}/labels/{name}`

The docs express levels as `(read)` and `(write)`; the token-creation UI presents the
same choices as read-only versus read-and-write — **[docs]** "Permissions can be set
to `read`, `write`, or `admin`, but not every permission supports each of those
levels."
([Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens))

### Do label writes need more than comment writes? No.

**Explicitly confirmed: they do not.** Both `POST …/issues/{n}/labels` and
`DELETE …/issues/{n}/labels/{name}` sit at **Issues: write** — the same level as
creating a comment. There is no separate "Labels" permission, and *attaching or
detaching* a label on an issue is an Issues-write operation.

The distinction to keep in mind is a different one: **managing the repo's label set**
(creating, renaming or deleting a label definition — `POST /repos/{o}/{r}/labels`,
`PATCH /repos/{o}/{r}/labels/{name}`, `DELETE /repos/{o}/{r}/labels/{name}`) is also
write-level, but it is a repo-configuration action rather than an issue action. A
client that only applies pre-existing labels never needs to touch those endpoints,
and `Issues: write` is sufficient without them.

### Is `issues:write` separable from `pull_requests`?

**Yes for granting, no for effect — and this is the subtlety worth understanding.**

You grant exactly one permission: **Issues: write**. You do *not* need to grant
`Pull requests` at all. In that sense they are fully separable.

But because issues and pull requests **share one number space** — **[docs]**
"GitHub's REST API considers every pull request an issue, but not every issue is a
pull request"
([List repository issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues))
— the issue-shaped endpoints operate on PRs too. `Issues: write` therefore lets the
token comment on, and label, *pull requests*, by passing a PR's number to an
`/issues/{issue_number}/…` path. The permission boundary does not follow the
issue/PR distinction, because the endpoints don't either.

**[observed]** GitHub confirms this directly in an undocumented response header,
`x-accepted-github-permissions`, which lists the permissions an endpoint accepts:

| Request | `x-accepted-github-permissions` |
|---|---|
| `GET /repos/{o}/{r}/issues` | `issues=read` |
| `GET /repos/{o}/{r}/issues/{n}` | `issues=read` |
| `GET /repos/{o}/{r}/issues/comments` | `issues=read; pull_requests=read` |
| `GET /repos/{o}/{r}/issues/{n}/comments` | `issues=read; pull_requests=read` |
| `GET /repos/{o}/{r}/labels` | `issues=read; pull_requests=read` |
| `POST /repos/{o}/{r}/issues/{n}/comments` | `issues=write; pull_requests=write` |
| `POST /repos/{o}/{r}/issues/{n}/labels` | `issues=write; pull_requests=write` |
| `DELETE /repos/{o}/{r}/issues/{n}/labels/{name}` | `issues=write; pull_requests=write` |

The `;`-separated list is a set of **alternatives** — either permission satisfies the
endpoint. Note the practical asymmetry: the two *list* endpoints (`/issues` and
`/issues/{n}`) accept **only** `issues`, so `Pull requests` alone is not a substitute
for this client's needs. This header is a convenient way to check a permission
assumption without reading the reference, but it is not in the docs — don't
build tooling that depends on it.

### Also required

- **Metadata: read.** The fine-grained PAT UI enables the Metadata repository
  permission automatically whenever another repository permission is selected, so in
  practice you get it. The docs pages checked do not state this as a rule — see
  **Not confirmed** — so treat "Issues: write + Metadata: read" as the set to
  configure and verify in the UI.
- **[docs]** Token scoping is itself part of least privilege: "Each token is limited
  to access resources owned by a single user or organization" and "Each token can be
  further limited to only access specific repositories for that user or
  organization."
  ([Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens))
  Scope the token to the one private repo.

**Final answer**: *Issues: write* (which subsumes the read need) + *Metadata: read*,
scoped to the single repository. Nothing else. No `Pull requests`, no `Contents`, no
`Administration`.

---

## 5. Idempotency affordances

GitHub offers **no idempotency key** on `POST …/issues/{n}/comments`. Retrying a
comment POST after an ambiguous failure will create a duplicate. Everything below is
about *recognising your own prior comment* so the client can decide between "create"
and "update" (`PATCH /repos/{o}/{r}/issues/comments/{comment_id}`) instead of blindly
posting.

### What the comment object gives you

**[docs]** ([List issue comments for a repository](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28))
each comment carries `id`, `node_id`, `user` (with `login`, `id`, `type`),
`author_association`, `created_at`, `updated_at`, `body`, and
`performed_via_github_app`.

**[observed]** A real comment from this repo:

```
id: 5078213382          node_id: IC_kwDOTMtmKs8AAAABLq9jBg
user.login: xpenno255   user.id: 47892554   user.type: "User"
author_association: "OWNER"
performed_via_github_app: null
created_at == updated_at when never edited
```

**Author identity — the reliable primitive.** Match on **`user.id`** (a stable
integer, `47892554` here), not `user.login`. Logins can be renamed; the numeric id
cannot. The client should resolve its own identity once at startup via `GET /user`
and cache the id.

**`user.type`.** **[observed]** `"User"` for a PAT-authored comment. **[observed]**
`GET /users/dependabot[bot]` returns `"type": "Bot"`, so `Bot` is a real value used
for app/bot accounts. **[docs]** the comments reference does **not** document or
enumerate `type` — see **Not confirmed**. For this client `user.type` is not useful
anyway: a fine-grained PAT posts as the *human owner*, so its comments are
indistinguishable by `type` (and by `user.id`) from comments the human wrote by hand
in the web UI. **This is the crux of the whole question.**

**`performed_via_github_app`.** **[observed]** `null` for a PAT-authored comment,
and it is present in the default response (no custom media type needed). It is
populated only when a GitHub App performed the action, so for a PAT client it is
always `null` — **useless as a self-marker here.** It would become the clean
discriminator only if ClaudeOS were re-platformed onto a GitHub App instead of a PAT.
**[docs]** the field is listed in the schema but carries no description.

**`author_association`.** `"OWNER"` here — describes the author's relationship to the
repo, not which tool wrote the comment. Not a discriminator.

### So: yes, an HTML-comment marker in the body is the pragmatic answer

Because a fine-grained PAT is indistinguishable from its human owner on every
identity field GitHub exposes, **there is no API-provided way for this client to tell
its own comments from the owner's.** The body is the only channel that carries
client-controlled provenance.

The pragmatic design: prefix every comment the client writes with an HTML comment,
e.g. `<!-- claudeos:poller v1 id=<stable-key> -->`. HTML comments do not render in
GitHub's markdown output, `body` is returned verbatim by the API, and the marker can
carry a stable key (issue number + purpose) so the client can find *the* comment it
owns for a given job and `PATCH` it rather than appending a new one. Combine both
signals: filter to `user.id == self`, then match the marker in `body`.

Caveat worth stating: the marker is visible to anyone who views the raw markdown or
edits the comment, and a human can delete it, so the lookup must degrade gracefully
(marker missing → treat as "no prior comment" and create one).

### Ordering and `since` help

- **[docs]** Comments are ordered by **ascending `id`** by default on both comment
  list endpoints. `id` increases with creation, so the default order is creation
  order — the client's own most recent comment is the last matching entry in the
  default listing.
- **[docs]** The repo-wide endpoint supports `sort=updated&direction=desc`, so
  "what changed since I last looked, newest first" is one request.
- `created_at != updated_at` identifies an **edited** comment. A client that PATCHes
  its own comment will see `updated_at` advance while `id` stays fixed — so `id` is
  the durable handle to persist locally once found, turning subsequent updates into a
  direct `PATCH` with no search at all. That is the cheapest idempotency story
  available: **remember the `comment_id`.**

---

## Not confirmed

Listed deliberately. Each of these is a place where a design decision could be made
on a guess — do not.

1. **Whether a fine-grained PAT gets the same 5,000/hour budget as a classic PAT.**
   The rate-limit page says "personal access token" and never distinguishes the two,
   and never uses the phrase "fine-grained personal access token". The observed
   `x-ratelimit-limit: 5000` came from a `gh` CLI OAuth token, **not** a fine-grained
   PAT. Mitigation: read `x-ratelimit-limit` at runtime rather than hardcoding 5,000.
2. **Whether a `304` counts against the *secondary* rate limit.** The docs sentence
   exempts `304`s from the *primary* limit only, and says nothing about secondary
   points. Assume a `304` still costs its 1 secondary point. (At 1 point per GET
   against a 900-points-per-minute ceiling this is not a practical constraint, but
   don't state it as free.)
3. **Whether `since` is strictly exclusive of the boundary timestamp.** Documented as
   "last updated after the given time"; the reference does not address equal
   timestamps or sub-second precision. Not tested. De-duplicate client-side.
4. **Whether *editing* an existing comment bumps the parent issue's `updated_at`.**
   *Creating* one does — that was measured (§3). Editing was not tested, and neither
   behaviour is documented on either reference page. This is precisely why the design
   polls the repo-wide comments endpoint (whose `since` filters on the comment's own
   `updated_at`) rather than inferring comment activity from the issue record.
5. **Whether label add/remove counts as "content-generating"** for the 80/minute,
   500/hour secondary limit. The docs define neither the term nor its membership;
   only comment creation is explicitly flagged. Assume yes and rate-limit writes.
6. **Whether `Metadata: read` is formally mandatory** for a fine-grained PAT with
   `Issues` selected. Neither the permissions reference nor the PAT management page
   states it as a rule; the reference has a Metadata section but no mandatory note.
   The behaviour is well known in practice but is not sourced here. Verify in the UI.
7. **The documented enumeration of `user.type`.** `"User"` and `"Bot"` were both
   observed from the live API, but the issue-comments reference does not describe or
   enumerate the field. Don't switch on values you haven't seen.
8. **Documented values of `x-ratelimit-resource`.** The rate-limit page names the
   header but does not enumerate its values on the page; `core` was observed for all
   endpoints in scope.
9. **`x-accepted-github-permissions` is entirely undocumented.** Every row of that
   table in §4 is observation. The permissions reference is the citable source; the
   header merely agrees with it.
10. **Whether the content-derived ETag behaviour in §2 is stable.** Observed, not
    documented, and exactly the kind of internal detail GitHub changes freely.

---

## Implications for ClaudeOS

**1. Polling is affordable — because of the 304, not because of the 5,000 budget.**
A poll cycle for this client is **two conditional GETs**: `GET /issues?since=…` and
`GET /issues/comments?since=…`, each with a cached `If-None-Match`. When nothing has
changed both return `304`, and per §2 that costs **zero** primary requests. The idle
steady state of the poller consumes **no rate-limit budget at all**.

**2. Affordable interval: 60 seconds.** The arithmetic, worst case, with *every*
cycle returning `200` and one page each:

| Interval | Cycles/hour | Requests/hour (worst case, 2 per cycle) | % of 5,000 |
|---|---|---|---|
| 60s | 60 | 120 | 2.4% |
| 30s | 120 | 240 | 4.8% |
| 15s | 240 | 480 | 9.6% |
| 5s | 720 | 1,440 | 29% |

Even 15s fits, and the *realistic* cost is far lower because a homelab issue tracker
is idle almost all the time, so nearly every cycle is a free `304`. Recommendation:
**60s**, because the observed `cache-control: max-age=60` means faster polling
probably reveals nothing new, GitHub's documented advice is to avoid polling
altogether, and the headroom is better spent on burst capacity than on latency
nobody will notice. Anything below 15s is pointless. Secondary limits are not a
factor: 1 point per GET against "900 points per minute" leaves the interval
unconstrained from that direction.

**3. `app/httpclient.py` needs three changes, and one of them is a correctness bug
for this use case.**

- **`304` arrives as an exception.** **[observed]** `urllib.request.urlopen` raises
  `urllib.error.HTTPError` with `code == 304` for a Not Modified response — verified
  with the stdlib directly. The current `request()` therefore converts every free
  `304` into a raised `HttpError`. Since `HttpError` already carries `.status` and
  `.headers`, callers *can* handle it, but a conditional-request helper that returns
  a sentinel (or a `(status, body, headers)` triple) is the right shape — otherwise
  the happy path of the poller is an exception path.
- **`verify_tls` defaults to `False`.** That default exists because "homelab gear
  almost always runs self-signed TLS", but it is wrong for `api.github.com`. Every
  GitHub call must pass `verify_tls=True`. Consider a thin `app/github.py` wrapper
  that hardcodes it so no call site can get it wrong.
- **Headers.** `request()` defaults `Accept: application/json`; GitHub wants
  **[docs]** `application/vnd.github+json`
  ([Getting started with the REST API](https://docs.github.com/en/rest/using-the-rest-api/getting-started-with-the-rest-api?apiVersion=2022-11-28)).
  Also send `X-GitHub-Api-Version: 2022-11-28` — **[docs]** "Requests without the
  `X-GitHub-Api-Version` header will default to use the `2022-11-28` version", and
  "If you specify an API version that is no longer supported, you will receive a
  `410 Gone` response"; pinning explicitly is what stops a future default from moving
  under the client
  ([API Versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions?apiVersion=2022-11-28)).
  Note `2026-03-10` is now also live, and `2022-11-28` is supported until **March 10,
  2028**. And **[docs]** "All API requests must include a valid `User-Agent` header" —
  urllib supplies `Python-urllib/3.x`, which satisfies it, but a descriptive UA like
  `claudeos/1.0` is what the docs ask for.
- `request()` also has no query-string helper and no retry/backoff. Backoff is
  mandatory per §1, and given "Continuing to make requests while you are rate limited
  may result in the banning of your integration", it belongs in the GitHub wrapper,
  not at each call site.

**4. Persist an ETag per request URL, and a `since` watermark.** The poller needs a
tiny bit of durable state: `{url → etag}` plus the last-seen `updated_at` per stream.
Advance the watermark only after a page is fully processed, and use
`sort=updated&direction=asc` so partial progress is always safe to resume. On `304`,
change nothing.

**5. Use the repo-wide comments endpoint, not per-issue.**
`GET /repos/{o}/{r}/issues/comments?since=…` covers every issue in one conditional
request; the per-issue endpoint would cost one request per watched issue and scale
with the tracker. Reserve `GET /issues/{n}/comments` for on-demand full-thread reads.

**6. Parse the `link` header; never increment `page=` yourself.** **[observed]** the
issues list uses an opaque `after=` cursor while the repo-wide comments list uses
`page=` offsets. Hand-built pagination will silently misbehave on one of them.
Absence of `rel="next"` is the terminator.

**7. Writes need their own discipline, separate from the read loop.** Writes cost 5
secondary points each, comment creation is explicitly flagged as notification-
triggering and secondary-rate-limit-prone, and **[docs]** requires waiting "at least
one second between each request" for bulk mutations. Serialise writes, space them,
and never retry a comment POST without first searching for the marker — there is no
idempotency key (§5).

**8. Identity: remember `comment_id`, and write a marker.** A fine-grained PAT posts
as its human owner, so no GitHub field distinguishes the client's comments from the
user's. Persist the `comment_id` of anything the client creates and `PATCH` it
thereafter; write an HTML-comment marker in the body as the recovery path when local
state is lost. Filter candidates by `user.id`, then by marker.

**9. Token: `Issues: write` + `Metadata: read`, scoped to the one repo.** One
permission covers reading issues and comments, writing comments, and adding/removing
labels. Be aware it also permits commenting on and labelling pull requests, since
they share the issue number space — if that is unwanted, it must be enforced in the
client by checking the `pull_request` key, because no permission draws that line.
