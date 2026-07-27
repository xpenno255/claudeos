"""Expected-offline windows: when a system being unreachable is not a fault.

A NAS on a DSM power schedule is off every night on purpose. Without this, the
poller reads that as a `True→False` transition and fires a `high` alert at the
same volume as a failing disk, the dashboard shows a red blinking tile, and the
weekly digest reports a `serious` finding — every single night. Alerts that are
wrong on a schedule are the ones people learn to ignore.

**The window suppresses the alert; it must never suppress the fault.** A system
that is still unreachable once its window has ended has not gone to sleep, it
has failed to wake, and that is a storage outage nobody is watching for. So the
tolerated period runs from `offline_from` to `offline_to` *plus a grace* for
boot time, and one second past that the ordinary DOWN path resumes at full
volume. The feature exists to remove noise, not coverage.

Generic rather than Synology-specific on purpose: the poller reads a setting and
knows nothing about which system it belongs to, which is the same reason
`metrics()` and `report_slice()` moved behind the connector seam (#1, #2).

Times are `HH:MM` in the server's local timezone, matching what DSM's own
schedule UI shows the owner. A window may cross midnight — an overnight
power-down is the whole point — so `23:00`→`07:00` is normal, not an error.
"""

import time

DEFAULT_GRACE_MIN = 15

# Anything unparseable is treated as "no window configured" rather than raising:
# a typo in a Setup field must not be able to stop the poller.
_MAX_MIN = 24 * 60


def _minutes(hhmm) -> int | None:
    """`"23:00"` -> 1380. None if it is not a time."""
    if not isinstance(hhmm, str):
        return None
    parts = hhmm.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def window(settings: dict) -> tuple | None:
    """`(start, end, grace)` in minutes-of-day, or None if not configured.

    A window whose ends are equal is treated as unconfigured rather than as
    either "always off" or "never off" — both readings are defensible, which is
    exactly why neither should be guessed at.
    """
    if not isinstance(settings, dict):
        return None
    start = _minutes(settings.get("offline_from"))
    end = _minutes(settings.get("offline_to"))
    if start is None or end is None or start == end:
        return None
    # `or DEFAULT` would be wrong here: 0 is falsy but is a deliberate answer,
    # and turning "no grace" into fifteen minutes silently extends the very
    # window this module exists to bound.
    raw = settings.get("offline_grace_min")
    if raw is None or raw == "":
        grace = DEFAULT_GRACE_MIN
    else:
        try:
            grace = int(raw)
        except (TypeError, ValueError):
            grace = DEFAULT_GRACE_MIN
    return start, end, max(0, grace)


def _contains(start: int, end: int, now: int) -> bool:
    """Is `now` inside [start, end)? Handles a window crossing midnight."""
    if start <= end:
        return start <= now < end
    return now >= start or now < end          # wraps past midnight


def status(settings: dict, now=None) -> dict | None:
    """Where `now` sits relative to the window, or None if there isn't one.

    `tolerated` is the question the poller actually asks: may this system be
    unreachable right now without anybody being told? True inside the window and
    through the grace that follows it, False everywhere else — including, and
    this is the point, the moment the grace runs out.
    """
    w = window(settings)
    if w is None:
        return None
    start, end, grace = w
    lt = time.localtime(now if now is not None else time.time())
    minute = lt.tm_hour * 60 + lt.tm_min

    in_window = _contains(start, end, minute)
    in_grace = (not in_window) and grace > 0 and _contains(end, (end + grace) % _MAX_MIN, minute)
    return {
        "in_window": in_window,
        "in_grace": in_grace,
        "tolerated": in_window or in_grace,
        "from": _fmt(start),
        "to": _fmt(end),
        "grace_min": grace,
    }


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def reason(st: dict) -> str:
    """The line a human reads on the tile and in the ops log."""
    if st.get("in_grace"):
        return f"expected back since {st['to']} — waking (grace {st['grace_min']}m)"
    return f"scheduled offline {st['from']}–{st['to']}"
