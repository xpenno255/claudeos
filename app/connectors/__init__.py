"""The connector seam: one adapter per monitored system.

`CONNECTORS` is the central seam of this codebase. The poller and the server
treat every entry as an adapter satisfying the interface below and know nothing
else about it, so adding a system should touch the new connector file and the
registration dict — and nothing else.

That interface used to exist only as folklore, recoverable by reading the other
four connectors. It is written down because adding the fifth (Synology) meant
reverse-engineering all of it, and because a caller cannot use an adapter
correctly without knowing which exceptions mean what.

## Required functions

Every module in `CONNECTORS` exports exactly these four:

    test(settings)    -> {"ok": bool, "detail": str}
        Prove the credentials work, for the Setup page's TEST button. Cheapest
        call that authenticates; never mutates anything.

    summary(settings) -> dict
        The system's current state as the dashboard tile and the poller want it.
        One round of calls — this runs every POLL_INTERVAL seconds against a
        real lab, so it is the wrong place to be thorough.

    metrics(summary)  -> dict of {name: number | None}
        The sparkline series this system wants recorded, derived from the dict
        `summary` just returned. Takes the summary rather than the settings so
        it cannot make a second call: whatever the poller already paid for is
        all it gets.

        A `None` value is dropped rather than recorded as zero, so a metric that
        is merely unavailable this pass leaves a gap instead of a false floor.
        Metric names are the keys the frontend reads out of `/api/history` and
        are effectively public — renaming one silently empties a chart.

    report_slice(settings) -> dict
        What this system contributes to the weekly AI digest, and how much of
        it: the connector decides what is worth a paragraph and what is noise,
        because it is the only thing that knows. Runs weekly, not every 30
        seconds, so unlike `summary` it can afford several calls.

        Wrap each one in `_report.soft`, which turns a failure into
        `{"error": ...}` in place. The report prompt is told that an errored
        section means the system was unreachable and that this is itself a
        finding, so one dead call must not cost the other four systems their
        slice.

Anything beyond these four (`devices`, `guests`, `storage`, `error_log` …) is
that connector's own surface, reached only by routes that already know which
system they are talking to. The seam is the four.

A connector never reaches into app state for its slice. Where the digest wants
something a *different* module owns — the SMART sweep's cache, the container
registry check — `reports.py` attaches it after the fact, so the dependency
runs inwards and not the other way about.

## Settings

Whatever the Setup page stored for that system id, from
`store.get_system(id, reveal_secrets=True)`, so **secrets arrive already
decrypted** and a connector never touches the crypto. Two keys are conventional
across all of them:

    host        the base URL or host:port. The poller treats a system with no
                `host` as unconfigured and skips it without calling anything.
    verify_tls  false by default — homelab boxes overwhelmingly have
                self-signed certificates, and defaulting to strict would mean
                every install starts broken.

The rest is per-system and named by whatever that system calls it
(`token_secret` for Proxmox, `bot_token` for Telegram); `store.SECRET_FIELDS`
is the list of which of those are secret.

## Exceptions are part of the interface

`server._dispatch` maps exceptions to HTTP status, so which one a connector
raises is a caller-visible decision, not an implementation detail:

    LookupError              -> 404   asked for something that isn't there
    ValueError               -> 400   the request itself was wrong
    httpclient.HttpError     -> 502   the upstream answered, with an error
    ConnectionError          -> 502   the upstream could not be reached
    anything else            -> 500   a bug, and logged with a traceback

`httpclient.request` already raises `ConnectionError` for unreachable hosts and
timeouts and `HttpError` for HTTP status, so a connector that leaves those alone
is correct by default. Raise `ValueError` for a bad argument and `LookupError`
for a missing entity; do not translate a network failure into either, because
the poller reads any exception as "this system is offline" and a 400 is not.

## Not a connector

`labissues` (GitHub) deliberately is not one — see ADR-0001. It is not part of
the lab being monitored, and forcing it through this interface would have meant
a `summary()` nobody wanted on a dashboard tile.
"""

from . import unifi, proxmox, docker, homeassistant, synology

CONNECTORS = {
    "unifi": unifi,
    "proxmox": proxmox,
    "docker": docker,
    "homeassistant": homeassistant,
    "synology": synology,
}
