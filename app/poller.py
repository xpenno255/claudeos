"""Background poller.

Every POLL_INTERVAL seconds, pull a summary from each configured system,
cache the latest snapshot, and append that connector's chosen metrics to
per-system ring buffers so the dashboard can draw sparklines without
hammering the lab.

*Which* metrics is the connector's business, not this module's: it calls
`mod.metrics(summary)` and records whatever comes back. The poller therefore
knows no summary shapes at all, and a new system needs nothing here.

A system alerts DOWN once it has been failing for its wait (`outage.down_after`,
five minutes unless its settings say otherwise) and "back up" on the first
success after that. A failure that ends sooner pages nobody and is counted for
the weekly update instead. Whether a DOWN is outstanding is an `alertstate`
latch rather than anything held here, so it survives a restart (#73): a system
down at boot alerts like any other, and a DOWN sent just before a deploy still
gets its "back up" afterwards.
"""

import threading
import time
from collections import deque

from . import alertstate, notify, offhours, oplog, outage, store, sweeper
from .connectors import CONNECTORS, TABS

POLL_INTERVAL = 30
HISTORY_LEN = 120  # ~1 hour at 30s

_lock = threading.Lock()
_latest: dict = {}    # system_id -> {"ok", "ts", "fails", "failing_since", "data" | "error"}
_history: dict = {}   # system_id -> {metric: deque[(ts, value)]}


def _record(system_id: str, metrics: dict) -> None:
    ts = time.time()
    hist = _history.setdefault(system_id, {})
    for k, v in metrics.items():
        if v is None:
            continue
        hist.setdefault(k, deque(maxlen=HISTORY_LEN)).append((ts, v))


def poll_once(now=None) -> None:
    """One pass over every connector. `now` is the clock, for the tests: the
    wait before DOWN is a duration, and nothing here should have to sleep."""
    clock = time.time if now is None else (lambda: now)
    for system_id, mod in CONNECTORS.items():
        settings = store.get_system(system_id, reveal_secrets=True)
        if not settings or not settings.get("host"):
            with _lock:
                _latest[system_id] = {"ok": None, "ts": clock(), "error": "not configured"}
            continue
        prev = _latest.get(system_id, {})
        was_ok = prev.get("ok")
        # distinguishes "never seen / not configured" from "asleep on schedule",
        # which both carry ok=None but mean opposite things when it ends
        was_scheduled = bool(prev.get("scheduled_off"))
        label = store.SYSTEM_LABELS.get(system_id, system_id)
        key = f"poller:{system_id}"
        link = TABS.get(system_id)
        try:
            s = mod.summary(settings)
            t = clock()
            with _lock:
                _latest[system_id] = {"ok": True, "ts": t, "fails": 0, "data": s}
                _record(system_id, mod.metrics(s))
            if was_ok is False:
                oplog.add("info", system_id, "connection recovered")
            # Recovery is owed only if a DOWN went out, and the latch is what
            # knows that, including a DOWN sent by the process before a restart.
            # It also knows when the outage began, which the message says.
            began = alertstate.since("poller", system_id)
            if alertstate.claim("poller", system_id, None) == "down":
                notify.send(f"{label.upper()} · back up", outage.was_down(began, t),
                            priority="default", tags=["white_check_mark"], key=key,
                            link=link)
            elif was_ok is False and prev.get("failing_since"):
                # Over before it was worth a page, so nobody was told and nobody
                # is owed a recovery. Counted, so the weekly update can say it
                # happened six times. Not a system waking from a scheduled
                # window: that carries ok=None and failed at nothing.
                outage.note_blip(label, prev["failing_since"], t)
        except Exception as e:  # noqa: BLE001 — any connector failure = offline
            t = clock()
            fails = int(prev.get("fails") or 0) + 1
            # Kept through a scheduled window too, so a system that sleeps all
            # night and then fails to wake alerts on the first poll past its
            # grace: by then it has been unreachable for hours, not for a blip.
            failing_since = prev.get("failing_since") or t
            sched = offhours.status(settings)
            if sched and sched["tolerated"]:
                # Unreachable, but expected to be. Recorded as a third state
                # rather than as ok=False: it is neither healthy nor broken, and
                # calling it broken every night is how an alert gets ignored.
                # Nothing alerts from here, and a system that wakes on time owes
                # no "back up", because no DOWN was latched for it.
                with _lock:
                    _latest[system_id] = {"ok": None, "ts": t,
                                          "scheduled_off": True, "fails": fails,
                                          "failing_since": failing_since,
                                          "error": offhours.reason(sched)}
                # once per descent into the window, not once per 30s poll
                if not was_scheduled:
                    oplog.add("info", system_id, offhours.reason(sched))
                continue
            with _lock:
                _latest[system_id] = {"ok": False, "ts": t, "fails": fails,
                                      "failing_since": failing_since, "error": str(e)}
            if was_ok is not False:
                oplog.add("warn", system_id, f"poll failed: {e}")
            if t - failing_since < outage.down_after(settings):
                continue
            # The latch decides, not `was_ok`. A system already down when
            # ClaudeOS starts has no earlier "up" to fall from, and used to never
            # alert at all (#73). One whose DOWN is still latched from before a
            # restart must not be announced twice.
            if alertstate.claim("poller", system_id, "down",
                                since=failing_since) is alertstate.NO_CHANGE:
                continue
            if was_scheduled:
                # The window closed on a system that never woke. The grace has
                # run out, so this is a failure to wake, and it alerts exactly
                # like any other outage. Suppressing the noise must never
                # suppress the fault.
                detail = f"did not come back after its scheduled window — {outage.brief(e)}"
            else:
                detail = f"{outage.brief(e)}\n{outage.since_line(failing_since, t)}"
            notify.send(f"{label.upper()} · down", detail,
                        priority="high", tags=["rotating_light"], key=key, link=link)


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
