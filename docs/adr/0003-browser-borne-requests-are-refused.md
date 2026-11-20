# ADR-0003: Browser-borne requests are refused; LAN reach is still trusted

- **Status:** Accepted
- **Date:** 2026-11-20
- **Decided in:** [#70](https://github.com/xpenno255/claudeos/issues/70), from the
  2026-11-20 review. The owner chose it as the first fix.

## Context

ClaudeOS has no authentication. The README warns that anyone who can reach port
8321 can drive the lab, and ADR-0002 built on that: "anyone who can reach the LAN
address can already open the dashboard and restart a VM."

The review found that the real exposure was wider: **any web page the owner opens
from the LAN** could do the same. The server ignored `Origin`, parsed JSON
whatever the Content-Type said, and never looked at `Host`. A cross-site
`text/plain` POST skips the CORS preflight, which allowed writes. DNS rebinding
made the attacker's page same-origin, which also let it read responses, enough
to drive the Ops Chat approval flow end to end.

## Decision

**Refuse requests a browser makes on another site's behalf, before routing**
(`app/requestguard.py`). Don't add authentication.

- `Host` must be a name an outsider cannot own: an IP literal, a single-label
  name, a reserved private suffix (`.local`, `.lan`, `.home`, `.home.arpa`,
  `.localdomain`, `.internal`,
  `.localhost`), or a domain listed in `CLAUDEOS_ALLOWED_HOSTS`.
- A non-GET request carrying an `Origin` (or `Sec-Fetch-Site: cross-site`) must
  match `Host`. `Origin: null` is refused.
- **A request with no `Origin` is allowed.** That covers curl, cron pings
  (ADR-0002) and the container healthcheck. None of them is a browser, so none
  carries the owner's ambient access.
- The UI can't be framed (`X-Frame-Options: DENY`, `frame-ancestors 'none'`).
- `/api/ha/service` accepts only what its one caller sends: `homeassistant.toggle`
  on an entity in `homeassistant.TOGGLE_DOMAINS`.

## Why not authentication

It is the larger change, and it doesn't fit the deployment: one owner, one
trusted VLAN, and a cron ping route that must stay session-free. The guard
closes the attack that the LAN model never accepted, without changing the one
it did.

## Consequences

- **The trust boundary is now exactly "can reach the LAN"**, as the README
  always claimed. A compromised device on the LAN can still drive the lab.
  Authentication remains the answer to that, and if it is ever added, ADR-0002's
  exemption for the ping route still applies.
- A name the guard doesn't recognise is refused with a message naming
  `CLAUDEOS_ALLOWED_HOSTS`, and each distinct refusal is written once to the ops
  log. Locking the owner out fails loudly, not silently.
- Non-browser clients are trusted by construction. That is the same line
  ADR-0002 drew, for the same reason.
