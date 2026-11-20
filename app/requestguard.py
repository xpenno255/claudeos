"""Refuse the requests a browser makes on someone else's behalf (#70).

ClaudeOS has no authentication, and ADR-0003 keeps it that way: whoever can
reach the LAN address can drive the lab. That was accepted. What was never
accepted is that **any web page the owner opens** could do the same, and two
holes allowed it:

- **Cross-site requests.** A page on any site can `fetch()` a LAN address with
  a `text/plain` body. That skips the CORS preflight, and `_read_body` parses
  JSON whatever the Content-Type says, so every write route accepted it.
- **DNS rebinding.** A page on `attacker.example` re-points its own name at the
  LAN address and becomes same-origin, so it can *read* responses too. That is
  enough to drive the Ops Chat approval flow end to end. The tell is the `Host`
  header, which still carries the attacker's name.

So two checks run before routing:

1. `Host` must be a name an outsider cannot own. That means an IP literal, a
   single-label name (`localhost`, `docker`), or a reserved private suffix
   (`.local`, `.lan`, `.home`, `.home.arpa`, `.localdomain`, `.internal`,
   `.localhost`). Rebinding needs
   a registered domain, and none of those can be registered. A real domain in
   front of ClaudeOS (a reverse proxy) goes in `CLAUDEOS_ALLOWED_HOSTS`, a
   comma-separated list.
2. A request that can change something (anything but GET/HEAD/OPTIONS) must not
   come from another origin. A browser sends `Origin` on every such request,
   and it must match `Host`. **A request with no `Origin` is allowed**, because
   that is curl, a cron job's backup ping (ADR-0002) and the container
   healthcheck. None of them is a browser, so none carries the owner's ambient
   access to the LAN.

Pure functions of header values, so the tests need no socket.
"""

import ipaddress
import os
import urllib.parse

_PRIVATE_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".localdomain",
                     ".internal", ".localhost")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_DEFAULT_PORT = {"http": "80", "https": "443"}


def _split_host(value):
    """`Host` header value -> (lowercased hostname, port or None)."""
    v = (value or "").strip().lower()
    if not v:
        return None, None
    if v.startswith("["):  # [v6]:port
        end = v.find("]")
        if end < 0:
            return None, None
        rest = v[end + 1:]
        return v[1:end], (rest[1:] if rest.startswith(":") else None)
    if v.count(":") == 1:
        host, port = v.split(":")
        return host, port or None
    return v, None


def _configured_hosts() -> set:
    raw = os.environ.get("CLAUDEOS_ALLOWED_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def host_allowed(host_header) -> bool:
    host, _ = _split_host(host_header)
    if host is None:
        # No Host at all. That's an HTTP/1.0 client, never a browser, and a
        # browser is the only thing a rebinding attack can use.
        return not (host_header or "").strip()
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host in _configured_hosts():
        return True
    if "." not in host:
        return True
    return host.endswith(_PRIVATE_SUFFIXES)


def origin_allowed(method: str, host_header, origin, sec_fetch_site=None) -> bool:
    if (method or "").upper() in _SAFE_METHODS:
        return True
    if (sec_fetch_site or "").strip().lower() == "cross-site":
        return False
    if origin is None:
        return True
    o = origin.strip().lower()
    if not o or o == "null":
        # "null" is what sandboxed frames and file:// pages send. It's a
        # browser, and it's not us.
        return False
    u = urllib.parse.urlsplit(o)
    try:
        oport = str(u.port) if u.port else _DEFAULT_PORT.get(u.scheme)
    except ValueError:
        return False
    host, hport = _split_host(host_header)
    if not u.hostname or host is None:
        return False
    # A Host without a port came through a proxy on the scheme's default port.
    return u.hostname == host and oport == (hport or oport)


def refusal(method: str, host_header, origin, sec_fetch_site=None):
    """Why this request is refused, or None to let it through."""
    if not host_allowed(host_header):
        host, _ = _split_host(host_header)
        return (f"refused: unrecognised Host {host!r}. If you reach ClaudeOS by "
                "this name, add it to CLAUDEOS_ALLOWED_HOSTS")
    if not origin_allowed(method, host_header, origin, sec_fetch_site):
        return "refused: cross-origin request"
    return None
