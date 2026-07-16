"""Background poller.

Every POLL_INTERVAL seconds, pull a summary from each configured system,
cache the latest snapshot, and append key metrics to per-system ring
buffers so the dashboard can draw sparklines without hammering the lab.
"""

import threading
import time
from collections import deque

from . import notify, oplog, store
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


def _metrics_from_summary(system_id: str, s: dict) -> dict:
    if system_id == "unifi":
        return {
            "clients": s.get("clients"),
            "latency_ms": s.get("isp_latency_ms"),
            "wan_rx_bps": s.get("rx_bytes_r"),
            "wan_tx_bps": s.get("tx_bytes_r"),
        }
    if system_id == "proxmox":
        mem_pct = None
        if s.get("mem_total"):
            mem_pct = 100.0 * s["mem_used"] / s["mem_total"]
        cpu = s.get("cpu_avg")
        return {
            "cpu_pct": round(cpu * 100, 1) if cpu is not None else None,
            "mem_pct": round(mem_pct, 1) if mem_pct is not None else None,
            "guests_running": s.get("guests_running"),
        }
    if system_id == "docker":
        host = s.get("host") or {}
        gpus = host.get("gpus") or []
        return {
            "running": s.get("containers_running"),
            "exited": s.get("containers_exited"),
            "host_cpu_pct": host.get("cpu_pct"),
            "host_mem_pct": host.get("mem_pct"),
            "gpu_util_pct": gpus[0].get("util_pct") if gpus else None,
            "gpu_vram_pct": gpus[0].get("mem_pct") if gpus else None,
        }
    if system_id == "homeassistant":
        return {
            "entities": s.get("entities_total"),
            "unavailable": s.get("unavailable"),
            "lights_on": s.get("lights_on"),
        }
    return {}


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
                _record(system_id, _metrics_from_summary(system_id, s))
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
    def loop():
        while True:
            try:
                poll_once()
            except Exception as e:  # noqa: BLE001
                oplog.add("error", "poller", f"poll loop error: {e}")
            time.sleep(POLL_INTERVAL)

    t = threading.Thread(target=loop, name="claudeos-poller", daemon=True)
    t.start()
