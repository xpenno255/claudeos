"""Background poller.

Every POLL_INTERVAL seconds, pull a summary from each configured system,
cache the latest snapshot, and append that connector's chosen metrics to
per-system ring buffers so the dashboard can draw sparklines without
hammering the lab.

*Which* metrics is the connector's business, not this module's: it calls
`mod.metrics(summary)` and records whatever comes back. The poller therefore
knows no summary shapes at all, and a new system needs nothing here.
"""

import threading
import time
from collections import deque

from . import notify, oplog, store, sweeper
from .connectors import CONNECTORS

POLL_INTERVAL = 30
HISTORY_LEN = 120  # ~1 hour at 30s

_lock = threading.Lock()
_latest: dict = {}    # system_id -> {"ok", "ts", "data" | "error"}
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
        was_ok = _latest.get(system_id, {}).get("ok")
        label = store.SYSTEM_LABELS.get(system_id, system_id)
        try:
            s = mod.summary(settings)
            with _lock:
                _latest[system_id] = {"ok": True, "ts": time.time(), "data": s}
                _record(system_id, mod.metrics(s))
            if was_ok is False:
                oplog.add("info", system_id, "connection recovered")
                notify.send(f"{label} recovered", "polling succeeded again",
                            priority="default", tags=["white_check_mark"])
        except Exception as e:  # noqa: BLE001 — any connector failure = offline
            with _lock:
                _latest[system_id] = {"ok": False, "ts": time.time(), "error": str(e)}
            if was_ok is not False:
                oplog.add("warn", system_id, f"poll failed: {e}")
            # only a True→False transition alerts, so a restart of ClaudeOS
            # itself never re-fires "down" for systems already offline
            if was_ok is True:
                notify.send(f"{label} is DOWN", str(e),
                            priority="high", tags=["rotating_light"])


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
