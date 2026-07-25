# ADR-0001: GitHub is not a connector

- **Status:** Accepted
- **Date:** 2026-07-25
- **Decided in:** [Is GitHub a connector?](https://github.com/xpenno255/claudeos/issues/17), under the [Lab Issues wayfinder map](https://github.com/xpenno255/claudeos/issues/14)

## Context

The Lab Issues feature needs ClaudeOS to talk to the GitHub issues API: read issues
from a dedicated private lab repo, and post triage comments back. That integration
needs encrypted credential storage, a Setup card with a TEST button, and a periodic
refresh — which looks, at first glance, like exactly what a connector provides.

`CONNECTORS` (`app/connectors/__init__.py`) is the codebase's central seam. Its five
adapters — UniFi, Proxmox, Docker, Home Assistant, Synology — are all **lab systems**.
Issue #1 is currently trying to pin down that interface, which exists only as folklore;
issue #2 wants to push `report_slice()` behind it.

The question was whether GitHub should become the sixth adapter.

## Decision

**GitHub does not join `CONNECTORS`.** The integration lives in its own module,
`app/labissues.py`, as a peer of `app/registry.py` and `app/notify.py` — outside the
connector seam, owning its own sweep loop, cache, and Setup card.

`connector` therefore retains its narrow meaning: *a polled lab system with up/down
semantics*. See `CONTEXT.md`.

## Why

**Connector membership does not provide what the integration needs.** Each affordance
comes from somewhere else entirely:

| Affordance | Actually provided by | Non-connector members today |
| --- | --- | --- |
| Encrypted secrets | `store.SECRET_FIELDS` (`app/store.py`) | `ai`, `registries`, 5 notify channels |
| Setup card | `FORMS` in `public/js/views/setup.js` | `registries`, `ai`, `NOTIFY_FORMS` |
| TEST button | the if/elif chain in `server.route_system_test` | `ai`, `registries`, notify channels |
| Dashboard tile | `SYSTEMS` in `public/js/meta.js` | — (`BY_ID` holds the non-polled ones) |

`public/js/meta.js` already codifies the distinction in comments: *"not a polled system
— configured on Setup, used by analysis features"*.

**What membership does provide is wrong here.** Two things only: `poller.poll_once()`
iterating the registry every 30s, and one branch in `route_system_test`. The poller is
actively harmful for GitHub:

- It skips anything without `settings.get("host")` — GitHub has no host in that sense,
  so one would have to be fabricated.
- On a `True→False` transition it fires
  `notify.send(f"{label} is DOWN", priority="high", tags=["rotating_light"])`. A transient
  `api.github.com` blip would push a red-siren alert indistinguishable from Proxmox
  going down. **GitHub being briefly unreachable is not a lab incident.**
- Its 30s cadence is wrong: the GitHub research settled on 60s, and the API returns
  `Cache-Control: max-age=60`.

**It would make the seam shallower, against the grain of work in flight.** A sixth
adapter returning `{}` from `metrics()` (#1) and `{}` from `report_slice()` (#2), whose
`ok` flag means something categorically different from every other member, satisfies the
type signature but not the interface's intent. That is the shallow-module-with-config-flags
failure #3 warned about in the sweeper context.

**There is a closer precedent.** `app/registry.py` is a structural twin: an external HTTP
API, optional credentials in `SECRET_FIELDS`, its own 6-hourly sweeper and cache, a Setup
card with a TEST button, contributions to the weekly report, and pills rendered on a tab —
and it is not a connector.

## Consequences

- `app/labissues.py` owns its own sweep loop and cache. This makes it the **fifth**
  background sweeper, firing the tripwire recorded in #3 — resolved separately in
  [The fifth sweeper](https://github.com/xpenno255/claudeos/issues/18).
- `server.route_system_test` gains one more branch. Its if/elif chain now dispatches five
  kinds of thing. Accepted as the cheaper cost; formalising a second `INTEGRATIONS` seam
  was considered and rejected as a refactor of existing subsystems inside a feature effort,
  landing on top of #1 which is already reworking this area. **If a seventh non-connector
  integration appears, revisit** — the adapters for such a seam already number six.
- A new `labissues` entry in `store.SECRET_FIELDS` holds the GitHub PAT, encrypted like
  every other credential.
- System ids are matched by `(?P<id>[a-z]+)` in `server.ROUTES` — lowercase letters only.
  `labissues` complies; `lab-issues` and `lab_issues` would be silently unroutable.
- Issue #1 can continue to define the connector contract narrowly, without accommodating
  a member that is not a lab system.
