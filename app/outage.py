"""When a failing check becomes DOWN, and what a short failure leaves behind.

**The wait.** A system or monitor is DOWN once it has been failing for
`DOWN_AFTER_S`, not on a count of failed polls. The count was two, about a
minute, and a minute is shorter than a routine restart: Home Assistant applying
an update, UniFi upgrading itself just after midnight. Each of those paged
`high` and then "recovered" a minute later, several times in an evening, which
is how an owner learns to swipe "is DOWN" away. Five minutes rides out a restart
and still reports a real outage while it is one. The cost is stated rather than
hidden: a real outage is announced five minutes after it starts.

The wait is per subject (`down_after_min` in a system's or a monitor's
settings), because a host that takes ten minutes to reboot and a service that
should never be away for one are both reasonable things to own.

**The blip.** A failure that ends inside the wait pages nobody, and that must
not make it invisible: six one-minute 502s in an evening is not an update, it is
something flapping. So each one is recorded here, and the weekly update counts
them per subject (`reports.weekly_lines`). Raising the threshold moved these out
of the owner's pocket; this is what stops it moving them out of sight.

The blip file is `blips.json` in the data directory. Its path is resolved on
every call, never bound at import, for the reason `alertstate` gives (#66).
"""

import http
import json
import math
import os
import re
import threading
import time

from . import store

# How long a subject fails before it is DOWN, unless its settings say otherwise.
DOWN_AFTER_S = 300
_MAX_MIN = 24 * 60

BLIPS_FILE = "blips.json"
# A little over the week the digest reads, so a digest delivered late still has
# its whole week to count.
BLIPS_KEEP_S = 8 * 86400
BLIPS_MAX = 2000

_lock = threading.Lock()


def down_after(settings) -> float:
    """Seconds this subject may fail before it is DOWN.

    Anything unparseable falls back to the default rather than raising: a typo
    in a Setup field must not be able to stop the poller, and must not be able
    to silence a system either, which is what reading it as "never" would do.
    Zero is a deliberate answer (alert on the first failure) and is kept.
    """
    raw = settings.get("down_after_min") if isinstance(settings, dict) else None
    if raw is None or raw == "" or isinstance(raw, bool):
        return DOWN_AFTER_S
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        return DOWN_AFTER_S
    if not math.isfinite(minutes) or not 0 <= minutes <= _MAX_MIN:
        return DOWN_AFTER_S
    return minutes * 60


# ------------------------------------------------------------------ wording

_HTTP = re.compile(r"^HTTP (\d{3}) from \S+$")
_UNREACHABLE = re.compile(r"^cannot reach \S+?: (.+)$")
_TIMEOUT = re.compile(r"^timeout reaching \S+$")
_URL = re.compile(r"\s*\b(?:from|at|to|reaching)?\s*https?://\S+")
_ERRNO = re.compile(r"\[Errno -?\d+\]\s*")


def brief(error) -> str:
    """An error as it should read on a phone: what went wrong, without the URL.

    The alert already names the system, so the address adds nothing, wraps to
    two lines and arrives as a tappable link to an API endpoint. The ops log
    keeps the full text for whoever is debugging.
    """
    text = str(error).strip()
    if m := _HTTP.match(text):
        code = int(m[1])
        try:
            return f"HTTP {code} ({http.HTTPStatus(code).phrase.lower()})"
        except ValueError:
            return f"HTTP {code}"
    if m := _UNREACHABLE.match(text):
        return f"unreachable: {_ERRNO.sub('', m[1])}"
    if _TIMEOUT.match(text):
        return "timed out"
    return (_ERRNO.sub("", _URL.sub("", text)).strip() or "unreachable")[:200]


def span(seconds) -> str:
    """A duration the way somebody would say it: 45s, 9m, 2h 05m, 3d 4h."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 2 * 86400:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def hhmm(ts) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def since_line(began, now, verb="unreachable") -> str:
    """The second line of a DOWN alert: `since 17:12 · unreachable for 5m`."""
    return f"since {hhmm(began)} · {verb} for {span(now - began)}"


def was_down(began, now) -> str:
    """The body of a recovery: how long it was away, and when."""
    if not began or began > now:
        return "responding again"
    if now - began >= 86400:  # two clock times a day apart say nothing
        return f"was down {span(now - began)}"
    return f"was down {span(now - began)} ({hhmm(began)}–{hhmm(now)})"


# -------------------------------------------------------------------- blips

def _path() -> str:
    return os.path.join(store.DATA_DIR, BLIPS_FILE)


def _read(p: str) -> list:
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    return [b for b in d if isinstance(b, dict)] if isinstance(d, list) else []


def note_blip(label: str, began: float, ended: float) -> None:
    """Record a failure that ended before it was worth a page. Never raises."""
    with _lock:
        p = _path()
        kept = [b for b in _read(p)
                if ended - float(b.get("end") or 0) < BLIPS_KEEP_S][-(BLIPS_MAX - 1):]
        kept.append({"label": label, "start": began, "end": ended})
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(kept, f)
            os.replace(tmp, p)
        except OSError:
            pass  # a count for the weekly update must never break a poll


def blips(since: float) -> dict:
    """`{label: {"count", "longest"}}` for blips that ended after `since`."""
    with _lock:
        rows = _read(_path())
    out: dict = {}
    for b in rows:
        try:
            start, end = float(b["start"]), float(b["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= since:
            continue
        agg = out.setdefault(str(b.get("label") or "?"), {"count": 0, "longest": 0.0})
        agg["count"] += 1
        agg["longest"] = max(agg["longest"], end - start)
    return out
