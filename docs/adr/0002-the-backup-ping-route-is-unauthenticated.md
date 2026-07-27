# ADR-0002: The backup ping route is unauthenticated

- **Status:** Accepted
- **Date:** 2026-07-27
- **Decided in:** [Build the Backups tab](https://github.com/xpenno255/claudeos/issues/50),
  specified in `docs/spec-backups.md`, ratified by the maintainer before merge

## Context

The Backups feature needs a backup job to report its own outcome. The caller is a
cron script on another machine — a `curl` line pasted at the end of an existing
backup — and a cron script has no session, cannot log in, and cannot be handed a
cookie.

That makes `POST /api/backups/<token>/ping` different in kind from every other
write in this app. Everything else that mutates state — restarting a container,
muting a job, saving a Setup card — happens because a human is in front of the
browser. This is the first endpoint where a **machine** holds a credential.

## Decision

**The ping route is unauthenticated. The 32-hex token in the path is the only
credential**, and it is generated per job with `secrets.token_hex(16)`.

This was specified, implemented, and then explicitly put to the maintainer as a
posture decision rather than an implementation detail — because a decision like
this becoming the norm by default is how a codebase acquires a security stance
nobody chose. Two alternatives were offered and declined: moving the token to an
`X-Backup-Token` header, and restricting the route by source IP.

## Why

**The app has no authentication anywhere.** Anyone who can reach the LAN address
can already open the dashboard and restart a VM. This endpoint does not open a
new door; it is the first one a machine walks through.

**The blast radius is one job's history.** A token permits exactly one thing:
recording a run against the job it belongs to. It reads nothing, deletes nothing,
and reaches no other system. The realistic harm is **false reassurance** — holding
a dead backup green — which is worth naming precisely because that is the failure
this whole feature exists to prevent.

**It is the conventional shape.** Heartbeat services (healthchecks.io and
equivalents) all use an unguessable URL, because it is the only thing a two-line
shell integration can carry.

The token is defended in depth rather than by authentication:

- 128 bits of randomness, compared with `secrets.compare_digest`
- an unknown token returns a plain `404`, identical to any other unmatched route,
  so the endpoint cannot be probed for valid tokens
- **POST only** — a GET that mutates state gets fetched by link previewers,
  crawlers and anything that unfurls a URL, and each would hold a dead job green
- never logged: `Handler.log_message` is a no-op, and now says that this is
  load-bearing rather than tidiness (see ADR context in #45)
- never returned by the job list, and not echoed by unrelated edits such as a
  mute toggle
- one click to revoke, via regenerate

## Consequences

- **A token in a URL path is a leak shape**, and this repo has already been bitten
  by it: #45 was a Telegram bot token reaching the browser, `data/opslog.jsonl`
  and — through `recent_warnings` — the Anthropic API. Anything that restores
  per-request logging must scrub the path first; `httpclient.safe_url` exists for
  this and already understands credential-bearing URLs.
- **The route must stay cheap and side-effect-light.** It is reachable by anything
  on the LAN, so it must not become a place where expensive or destructive work
  can be triggered by an unauthenticated caller.
- **If authentication is ever added to ClaudeOS, this route needs an explicit
  exemption**, not an accidental one — a cron job still will not have a session.
- The decision is scoped to *this* route. It is not a precedent for unauthenticated
  writes generally, and a second one should be argued on its own terms.
