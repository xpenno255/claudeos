"""Uptime/service monitors — Uptime Kuma pattern on the ClaudeOS chassis.

Every CHECK_INTERVAL seconds each enabled monitor is probed concurrently
(one dead box must never delay the rest past the shared timeout):

  http    — GET the URL; up when the response status is < 400
  keyword — http, and the body must contain the given text
  tcp     — plain socket connect to host:port
  dns     — the hostname resolves

Monitor definitions live in data/monitors.json (no secrets, so outside
the encrypted store). Status + response-time history is kept in ring
buffers (~24 h) for sparklines and uptime %. A monitor alerts through
app.notify after FAILS_TO_ALERT consecutive failures and again on the
first success after a down alert.
"""

import json
import os
import secrets
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from . import notify, oplog
from .httpclient import request
from .store import DATA_DIR

PATH = os.path.join(DATA_DIR, "monitors.json")

CHECK_INTERVAL = 30
CHECK_TIMEOUT = 6
HISTORY_LEN = 2880  # ~24 h at 30 s
FAILS_TO_ALERT = 2  # consecutive failures before the down alert fires

TYPES = ("http", "keyword", "tcp", "dns")

_lock = threading.Lock()
_state: dict = {}    # id -> {"ok","ms","error","ts","since","fails","alerted"}
_history: dict = {}  # id -> deque[(ts, ok, ms)]


# ------------------------------------------------------------------ CRUD

def _load() -> list:
    if not os.path.exists(PATH):
        return []
    with open(PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(mons: list) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mons, f, indent=2)
    os.replace(tmp, PATH)


def _validate(cfg: dict) -> dict:
    name = str(cfg.get("name") or "").strip()
    typ = str(cfg.get("type") or "").strip()
    target = str(cfg.get("target") or "").strip()
    if not name:
        raise ValueError("name is required")
    if typ not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    if not target:
        raise ValueError("target is required")
    out = {
        "name": name,
        "type": typ,
        "target": target,
        "enabled": cfg.get("enabled") is not False,
        "verify_tls": cfg.get("verify_tls") is True,
    }
    if typ == "keyword":
        keyword = str(cfg.get("keyword") or "").strip()
        if not keyword:
            raise ValueError("keyword is required for keyword monitors")
        out["keyword"] = keyword
    if typ == "tcp":
        host, _, port = target.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError("tcp target must be host:port")
    return out


def create(cfg: dict) -> dict:
    mon = _validate(cfg)
    mon["id"] = secrets.token_hex(4)
    with _lock:
        mons = _load()
        mons.append(mon)
        _save(mons)
    return mon


def update(mid: str, cfg: dict) -> dict:
    with _lock:
        mons = _load()
        for i, m in enumerate(mons):
            if m["id"] == mid:
                # partial update: existing values are the defaults
                merged = {**m, **{k: v for k, v in cfg.items() if v is not None}}
                mon = _validate(merged)
                mon["id"] = mid
                mons[i] = mon
                _save(mons)
                # a paused monitor keeps its config but drops live status
                if not mon["enabled"]:
                    _state.pop(mid, None)
                return mon
    raise LookupError(f"unknown monitor: {mid}")


def delete(mid: str) -> None:
    with _lock:
        mons = _load()
        if not any(m["id"] == mid for m in mons):
            raise LookupError(f"unknown monitor: {mid}")
        _save([m for m in mons if m["id"] != mid])
        _state.pop(mid, None)
        _history.pop(mid, None)


def list_monitors() -> list:
    """Configs merged with live state, 24 h uptime % and avg response."""
    with _lock:
        mons = _load()
        out = []
        for m in mons:
            st = _state.get(m["id"], {})
            points = list(_history.get(m["id"], ()))
            ok_points = [p for p in points if p[1]]
            entry = {
                **m,
                "ok": st.get("ok"),
                "ms": st.get("ms"),
                "error": st.get("error"),
                "ts": st.get("ts"),
                "since": st.get("since"),
                "uptime_pct": round(100 * len(ok_points) / len(points), 2) if points else None,
                "avg_ms": round(sum(p[2] for p in ok_points) / len(ok_points), 1) if ok_points else None,
            }
            out.append(entry)
        return out


def history() -> dict:
    """Response-time sparkline data: id -> [(ts, ok, ms), ...]."""
    with _lock:
        return {mid: list(points) for mid, points in _history.items()}


# ---------------------------------------------------------------- checks

def _probe(mon: dict) -> float:
    """Run one check; returns response time in ms, raises on failure."""
    typ, target = mon["type"], mon["target"]
    t0 = time.monotonic()
    if typ in ("http", "keyword"):
        url = target if target.startswith("http") else "http://" + target
        body = request("GET", url, verify_tls=mon.get("verify_tls", False),
                       timeout=CHECK_TIMEOUT)
        if typ == "keyword":
            text = body if isinstance(body, str) else json.dumps(body or "")
            if mon["keyword"] not in text:
                raise ValueError(f'keyword "{mon["keyword"]}" not found in response')
    elif typ == "tcp":
        host, _, port = target.rpartition(":")
        socket.create_connection((host, int(port)), timeout=CHECK_TIMEOUT).close()
    elif typ == "dns":
        socket.getaddrinfo(target, None)
    return (time.monotonic() - t0) * 1000


def _record(mon: dict, ok: bool, ms: float | None, error: str | None) -> None:
    mid, now = mon["id"], time.time()
    with _lock:
        st = _state.get(mid) or {"ok": None, "since": now, "fails": 0, "alerted": False}
        if st["ok"] != ok:
            st["since"] = now  # status flipped — restart the up/down clock
        st.update(ok=ok, ms=ms, error=error, ts=now)
        st["fails"] = 0 if ok else st["fails"] + 1
        fails, alerted = st["fails"], st["alerted"]
        if ok and alerted:
            st["alerted"] = False
        elif not ok and not alerted and fails >= FAILS_TO_ALERT:
            st["alerted"] = True
        _state[mid] = st
        _history.setdefault(mid, deque(maxlen=HISTORY_LEN)).append(
            (now, ok, round(ms, 1) if ms is not None else None))

    if ok and alerted:
        oplog.add("info", "monitor", f"{mon['name']} recovered")
        notify.send(f"Monitor {mon['name']} recovered",
                    f"{mon['type']} check on {mon['target']} succeeding again "
                    f"({ms:.0f} ms)", priority="default", tags=["white_check_mark"])
    elif not ok and not alerted and fails == FAILS_TO_ALERT:
        oplog.add("warn", "monitor", f"{mon['name']} is down: {error}")
        notify.send(f"Monitor {mon['name']} is DOWN",
                    f"{mon['type']} check on {mon['target']} failed "
                    f"{fails}x: {error}", priority="high", tags=["rotating_light"])


def check_all() -> None:
    with _lock:
        mons = [m for m in _load() if m.get("enabled") is not False]
    if not mons:
        return

    def run(mon):
        try:
            _record(mon, True, _probe(mon), None)
        except Exception as e:  # noqa: BLE001 — any probe failure = down
            _record(mon, False, None, str(e))

    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="claudeos-mon") as ex:
        list(ex.map(run, mons))


def start() -> None:
    def loop():
        while True:
            try:
                check_all()
            except Exception as e:  # noqa: BLE001
                oplog.add("error", "monitor", f"check loop error: {e}")
            time.sleep(CHECK_INTERVAL)

    threading.Thread(target=loop, name="claudeos-monitors", daemon=True).start()
