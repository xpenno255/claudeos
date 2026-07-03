"""Ops log: every action and notable event, in memory + appended to disk.

This is the audit trail for anything ClaudeOS (or an agent driving it)
does to the homelab.
"""

import json
import os
import threading
import time
from collections import deque

from .store import DATA_DIR

LOG_PATH = os.path.join(DATA_DIR, "opslog.jsonl")

_lock = threading.Lock()
_recent: deque = deque(maxlen=250)


def _load_recent() -> None:
    if not os.path.exists(LOG_PATH):
        return
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
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
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass
    return entry


def recent(limit: int = 100) -> list:
    with _lock:
        return list(_recent)[-limit:][::-1]
