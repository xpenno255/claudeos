"""Alert latches that survive a restart: what each subject was last alerted as.

Every alerting module in this app alerts on a *transition*, which means it has
to remember the state it last announced. The poller, SMART, the uptime monitors
and the registry used to remember it in module globals, and every deploy is a
restart, so every deploy forgot (#73):

* a system already down at boot never alerted, because there was no previous
  "up" for it to have fallen from;
* a DOWN sent just before a restart never got its "recovered", because the
  process that owed it was gone;
* a disk already failing at boot was never alerted at all, because SMART treated
  a disk it had never seen as having nothing to report.

`backups.py` already got this right with a latch persisted beside its jobs, and
this module is that latch made available to everything else.

**A latch records an alert, not an observation.** Absent means "nothing has been
announced", which is the healthy rest state of every subject, so the file only
ever holds subjects that currently owe somebody a recovery. A subject seen for
the first time in a bad state therefore alerts, and one seen for the first time
healthy says nothing — which is the right answer both times.

The file is `alerts.json` in the data directory::

    {
      "poller":   {"homeassistant": {"state": "down", "since": 1763661811.2}},
      "smart":    {"pve:/dev/sda": {"state": "fail", "since": 1763661900.0}},
      "monitor":  {"a1b2c3d4": {"state": "down", "since": 1763662000.5}},
      "registry": {"lscr.io/linuxserver/plex:latest": {"state": "update", "since": 1763662100.0}}
    }

Namespace, then subject, then the state last alerted and when. `since` is for a
human reading the file; nothing decides on it.

The path is resolved on every call, never bound at import. `store.DATA_DIR` is
rebound when the data directory is redirected, and a path copied out of it at
import keeps pointing at the old one — which is how a test run once wrote 368
imaginary failures into the owner's real ops log (#66).
"""

import json
import os
import threading
import time

from . import oplog, store

FILENAME = "alerts.json"

# Returned by `claim` when the state asked for is the state already latched.
NO_CHANGE = object()

_lock = threading.Lock()
# path -> latches. The file is the record across restarts; this copy is what a
# running process decides from, so a full disk costs persistence, not a page
# re-sent on every poll because the write never landed.
_cache: dict = {}
_complained: set = set()  # paths already reported unreadable, so it is said once


def path() -> str:
    return os.path.join(store.DATA_DIR, FILENAME)


def _latches(p: str) -> dict:
    """The latches for `p`, from memory or from disk. Caller holds `_lock`."""
    if p in _cache:
        return _cache[p]
    d = {}
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError) as e:
            if p not in _complained:
                _complained.add(p)
                # Losing this costs at most one repeated DOWN per subject and a
                # recovery nobody is told about, which is worth saying out loud.
                oplog.add("warn", "alerts",
                          f"alert state unreadable, starting fresh: {e}")
            d = {}
    if not isinstance(d, dict):
        d = {}
    d = {ns: subjects for ns, subjects in d.items() if isinstance(subjects, dict)}
    _cache[p] = d
    return d


def _write(p: str, d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, p)
    except OSError as e:
        # The alert this write belongs to still goes out: bookkeeping must never
        # be what breaks the alert path. What is lost is only the restart case.
        oplog.add("warn", "alerts", f"could not save alert state: {e}")


def get(namespace: str, subject: str) -> str | None:
    """The state last alerted for `subject`, or None if nothing is owed."""
    with _lock:
        entry = _latches(path()).get(namespace, {}).get(subject)
    return entry.get("state") if isinstance(entry, dict) else None


def entries(namespace: str) -> dict:
    """`{subject: state}` for every latched subject in `namespace`."""
    with _lock:
        subjects = _latches(path()).get(namespace, {})
        return {k: v.get("state") for k, v in subjects.items() if isinstance(v, dict)}


def claim(namespace: str, subject: str, state: str | None):
    """Move `subject`'s latch to `state`, and say whether this call moved it.

    Returns the previous state (None if nothing was latched) when this call is
    the one that changed it, and `NO_CHANGE` when `state` was already latched.
    `state=None` clears the latch: the subject is back to rest and owes nothing.

    Read, decide and write happen in one lock acquisition, for the reason
    `backups._claim_transition` gives: the poller, SMART and the registry each
    run on a background sweeper *and* on demand from a route, and two threads
    deciding from the same stale latch would both send.
    """
    with _lock:
        p = path()
        d = _latches(p)
        subjects = d.get(namespace, {})
        entry = subjects.get(subject)
        prev = entry.get("state") if isinstance(entry, dict) else None
        if prev == state:
            return NO_CHANGE
        if state is None:
            subjects.pop(subject, None)
            if not subjects:
                d.pop(namespace, None)
        else:
            d.setdefault(namespace, subjects)[subject] = {"state": state,
                                                          "since": time.time()}
        _write(p, d)
        return prev
