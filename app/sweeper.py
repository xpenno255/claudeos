"""The background-sweep chassis: one named daemon thread on a cadence.

The poller, the uptime monitors, the SMART sweep and the container-registry
check all want the same shape — call one function forever, survive whatever it
throws, record the failure to the ops log — and differ only in the callable,
the thread name, the log tag and the interval. Those four things are all
`spawn` takes.

Caching, staleness and alerting stay with the callers: only two of the four
modules cache, and all four alert on deliberately different policies.
`reports.py` also stays hand-rolled — it is schedule-driven and sleeps first.
"""

import threading
import time

from . import oplog


def spawn(name: str, work, interval, *, system: str, error: str) -> threading.Thread:
    """Run `work()` forever in a daemon thread named `claudeos-<name>`.

    `interval` is seconds: either a number or a zero-argument callable
    returning the current value. It is resolved before every sleep rather than
    captured once, so a caller with a user-configurable cadence can pass a
    getter and have a change land on the next tick.

    A raising pass is logged as `"<error>: <exception>"` against the ops-log
    `system` tag and the loop continues — one bad sweep must never be the end
    of the thread.
    """
    def loop():
        while True:
            try:
                work()
            except Exception as e:  # noqa: BLE001
                oplog.add("error", system, f"{error}: {e}")
            time.sleep(interval() if callable(interval) else interval)

    t = threading.Thread(target=loop, name=f"claudeos-{name}", daemon=True)
    t.start()
    return t
