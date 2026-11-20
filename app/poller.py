"""Background poller.

Every POLL_INTERVAL seconds, pull a summary from each configured system,
cache the latest snapshot, and append that connector's chosen metrics to
per-system ring buffers so the dashboard can draw sparklines without
hammering the lab.

*Which* metrics is the connector's business, not this module's: it calls
`mod.metrics(summary)` and records whatever comes back. The poller therefore
knows no summary shapes at all, and a new system needs nothing here.

A system alerts DOWN after FAILS_TO_ALERT consecutive failed polls and
"recovered" on the first success after that. Whether a DOWN is outstanding is
an `alertstate` latch rather than anything held here, so it survives a restart
(#73): a system down at boot alerts like any other, and a DOWN sent just before
a deploy still gets its "recovered" afterwards.
"""

import threading
import time
from collections import deque

from . import alertstate, notify, offhours, oplog, store, sweeper
from .connectors import CONNECTORS

POLL_INTERVAL = 30
HISTORY_LEN = 120  # ~1 hour at 30s

# Consecutive failed polls before DOWN, about a minute at POLL_INTERVAL. The old
# threshold was one, and over 14 days it paged `high` 39 times for Home Assistant
# 502s and UniFi timeouts that were over in 30–100 seconds (#72). `high` is meant
# for lab-down and failing hardware, and paging it for blips trains the owner to
# ignore "is DOWN". The uptime monitors wait for the same two (`monitors.py`).
FAILS_TO_ALERT = 2

_lock = threading.Lock()
_latest: dict = {}    # system_id -> {"ok", "ts", "fails", "data" | "error"}
_history: dict = {}   # system_id -> {metric: deque[(ts, value)]}


def _record(system_id: str, metrics: dict) -> None:
    ts = time.time()
    hist = _history.setdefault(system_id, {})
    for k, v in metrics.items():
        if v is None:
            continue
        hist.setdefault(k, deque(maxlen=HISTORY_LEN)).append((ts, v))


def poll_once() -> None:
    for system_id, mod in CONNECTORS.items():
        settings = store.get_system(system_id, reveal_secrets=True)
        if not settings or not settings.get("host"):
            with _lock:
                _latest[system_id] = {"ok": None, "ts": time.time(), "error": "not configured"}
            continue
        prev = _latest.get(system_id, {})
        was_ok = prev.get("ok")
        # distinguishes "never seen / not configured" from "asleep on schedule",
        # which both carry ok=None but mean opposite things when it ends
        was_scheduled = bool(prev.get("scheduled_off"))
        label = store.SYSTEM_LABELS.get(system_id, system_id)
        key = f"poller:{system_id}"
        try:
            s = mod.summary(settings)
            with _lock:
                _latest[system_id] = {"ok": True, "ts": time.time(), "fails": 0, "data": s}
                _record(system_id, mod.metrics(s))
            if was_ok is False:
                oplog.add("info", system_id, "connection recovered")
            # Recovery is owed only if a DOWN went out, and the latch is what
            # knows that, including a DOWN sent by the process before a restart.
            # A single failed poll never alerted, so it recovers silently.
            if alertstate.claim("poller", system_id, None) == "down":
                notify.send(f"{label} recovered", "polling succeeded again",
                            priority="default", tags=["white_check_mark"], key=key)
        except Exception as e:  # noqa: BLE001 — any connector failure = offline
            # Counted through a scheduled window too, so a system that sleeps
            # all night and then fails to wake alerts on the first poll past its
            # grace: by then it has been unreachable for hours, not for one blip.
            fails = int(prev.get("fails") or 0) + 1
            sched = offhours.status(settings)
            if sched and sched["tolerated"]:
                # Unreachable, but expected to be. Recorded as a third state
                # rather than as ok=False: it is neither healthy nor broken, and
                # calling it broken every night is how an alert gets ignored.
                # Nothing alerts from here, and a system that wakes on time owes
                # no "recovered", because no DOWN was latched for it.
                with _lock:
                    _latest[system_id] = {"ok": None, "ts": time.time(),
                                          "scheduled_off": True, "fails": fails,
                                          "error": offhours.reason(sched)}
                # once per descent into the window, not once per 30s poll
                if not was_scheduled:
                    oplog.add("info", system_id, offhours.reason(sched))
                continue
            with _lock:
                _latest[system_id] = {"ok": False, "ts": time.time(), "fails": fails,
                                      "error": str(e)}
            if was_ok is not False:
                oplog.add("warn", system_id, f"poll failed: {e}")
            if fails < FAILS_TO_ALERT:
                continue
            # The latch decides, not `was_ok`. A system already down when
            # ClaudeOS starts has no earlier "up" to fall from, and used to never
            # alert at all (#73). One whose DOWN is still latched from before a
            # restart must not be announced twice.
            if alertstate.claim("poller", system_id, "down") is alertstate.NO_CHANGE:
                continue
            detail = str(e)
            if was_scheduled:
                # The window closed on a system that never woke. The grace has
                # run out, so this is a failure to wake, and it alerts exactly
                # like any other outage. Suppressing the noise must never
                # suppress the fault.
                detail = f"did not come back after its scheduled window — {e}"
            notify.send(f"{label} is DOWN", detail,
                        priority="high", tags=["rotating_light"], key=key)


def snapshot() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _latest.items()}


def history() -> dict:
    with _lock:
        return {
            sid: {metric: list(points) for metric, points in metrics.items()}
            for sid, metrics in _history.items()
        }


def start() -> None:
    sweeper.spawn("poller", poll_once, POLL_INTERVAL,
                  system="poller", error="poll loop error")
