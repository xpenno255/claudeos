"""Ops log: every action and notable event, in memory + appended to disk.

This is the audit trail for anything ClaudeOS (or an agent driving it)
does to the homelab.
"""

import json
import os
import threading
import time
from collections import deque

from . import store

_lock = threading.Lock()
_recent: deque = deque(maxlen=250)


def _log_path() -> str:
    """Resolved per call rather than bound at import.

    `store.DATA_DIR` is rebound whenever the data directory is reconfigured,
    which is how the tests keep their state out of the real one. A path computed
    once at import survives that change and keeps pointing at the old directory
    — so a test run appended imaginary failures to the owner's real ops log, and
    `reports` fed them to the model through `recent_warnings` as fact (#66).
    Reading `store.DATA_DIR` through the module, not as a copied value, is what
    makes redirecting the directory sufficient.
    """
    return os.path.join(store.DATA_DIR, "opslog.jsonl")


def _load_recent() -> None:
    path = _log_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-250:]
        for line in lines:
            try:
                _recent.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except OSError:
        pass


_load_recent()


def add(level: str, system: str, message: str) -> dict:
    entry = {"ts": time.time(), "level": level, "system": system, "message": message}
    with _lock:
        _recent.append(entry)
        try:
            os.makedirs(store.DATA_DIR, exist_ok=True)
            with open(_log_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass
    return entry


def recent(limit: int = 100) -> list:
    with _lock:
        return list(_recent)[-limit:][::-1]
