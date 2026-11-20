"""Request bodies: "absent" and "unparseable" are different answers (#71).

`_read_body` used to return `None` for both, and two routes read that `None` as
a meaningful empty payload:

- **The backup ping recorded a success.** An empty body is the documented
  minimum ping (ADR-0002), so `{}` means "it worked". But `-d '{"ok":'$rc'}'`
  with an empty `$rc`, or a `detail` with an unescaped quote, is invalid JSON
  from a job that was most likely reporting a failure. It held a dead job green,
  which is the exact false reassurance the Backups feature exists to prevent.
- **A Setup save wiped the system.** Non-secret fields are replaced wholesale
  (`store.save_system`), so `{}` deleted `host` and everything else and
  answered 200.

Now an unparseable body is `UNPARSEABLE`. The server answers 400 for it on
every route except the ping, which records it as a failed run instead: an
unreadable ping is still a ping.
"""

import json


class _Unparseable:
    __slots__ = ()

    def __repr__(self):
        return "UNPARSEABLE"

    def __bool__(self):
        # Truthy, so a stray `body or {}` can't quietly turn it back into {}.
        return True


UNPARSEABLE = _Unparseable()


def parse(raw: bytes):
    """`None` for no body, the decoded JSON value, or `UNPARSEABLE`."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return UNPARSEABLE
