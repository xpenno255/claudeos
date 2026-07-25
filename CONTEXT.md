# ClaudeOS — domain context

The glossary for this repo. Terms are added lazily, as they actually get resolved —
absence from this file means the term has not needed pinning down yet, not that it
is undefined.

Architecture and the JSON API surface live in `README.md`; agent conventions in
`CLAUDE.md` and `docs/agents/`. Decisions live in `docs/adr/`.

## Glossary

### Connector

**A polled lab system with up/down semantics.** The five members of `CONNECTORS`
(`app/connectors/__init__.py`): UniFi, Proxmox, Docker, Home Assistant, Synology.

A connector is a piece of the homelab whose reachability is itself meaningful — when
it stops answering, something is wrong with the lab. `poller.poll_once()` sweeps every
connector on a 30s interval, records ok/error plus sparkline metrics, and alerts on a
`True→False` transition.

**Not every external system ClaudeOS talks to is a connector.** `ai`, `registries`, the
five notification channels, and `labissues` are all configured on Setup and hold
encrypted credentials, but none is polled and none has up/down semantics. Encrypted
storage comes from `store.SECRET_FIELDS`, the Setup card from `FORMS` in
`public/js/views/setup.js`, and the TEST button from the if/elif chain in
`server.route_system_test` — all independent of connector membership.

_Avoid_: "integration", "system", or "adapter" as a synonym. **System** is the broader
word (anything with a `SECRET_FIELDS` entry and a Setup card); a connector is the subset
that is polled. See ADR-0001.

### Lab issue

**A homelab problem raised by a human as a GitHub issue in the dedicated private lab
repo** (`xpenno255/homelab`), for ClaudeOS to triage.

Deliberately distinct from a **ClaudeOS issue** — a development ticket in
`xpenno255/claudeos` about the app's source code. They live in separate repos precisely
so the two never share a number space or a queue. A lab issue is about the lab's state;
a ClaudeOS issue is about this codebase.

ClaudeOS never opens a lab issue; it reads, triages, and comments.

### Read-only

In the context of triage, **read-only always means against the lab** —
`tools.schemas(include_writes=False)`, no `WRITE_TOOLS`. It does not mean ClaudeOS makes
no writes at all: posting a triage comment and applying labels are writes *against the
tracker*. These two boundaries are distinct and conflating them causes confusion.
