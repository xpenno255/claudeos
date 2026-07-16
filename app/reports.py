"""Scheduled AI health reports.

A collector pulls a compact snapshot of the whole lab (gateway health,
security events, Proxmox nodes/storage, Docker fleet, HA/ZHA, uptime
monitors, recent warnings), Claude turns it into a graded digest with
ranked findings, and the result is delivered through the notification
layer and kept in data/reports.json (last KEEP reports).

Scheduling is a lightweight stdlib loop: every few minutes it checks
whether the configured weekly slot (day + hour, server-local time) has
passed since the last run.
"""

import datetime
import json
import os
import secrets
import threading
import time

from . import ai, monitors, notify, oplog, poller, store
from .connectors import docker, homeassistant, proxmox, unifi
from .store import DATA_DIR

PATH = os.path.join(DATA_DIR, "reports.json")
KEEP = 12
TICK = 300  # scheduler check interval, seconds

DEFAULT_CONFIG = {"enabled": False, "day": 0, "hour": 8, "last_run": 0}  # Monday 08:00

_lock = threading.Lock()
_running = threading.Event()  # one report generation at a time


def _load() -> dict:
    if not os.path.exists(PATH):
        return {"config": dict(DEFAULT_CONFIG), "reports": []}
    with open(PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("config", dict(DEFAULT_CONFIG))
    d.setdefault("reports", [])
    return d


def _save(d: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, PATH)


def get_state() -> dict:
    with _lock:
        d = _load()
    d["running"] = _running.is_set()
    return d


def set_config(cfg: dict) -> dict:
    out = {}
    out["enabled"] = cfg.get("enabled") is True
    day = cfg.get("day", 0)
    hour = cfg.get("hour", 8)
    if not (isinstance(day, int) and 0 <= day <= 6):
        raise ValueError("day must be 0 (Monday) … 6 (Sunday)")
    if not (isinstance(hour, int) and 0 <= hour <= 23):
        raise ValueError("hour must be 0…23")
    out["day"], out["hour"] = day, hour
    with _lock:
        d = _load()
        out["last_run"] = d["config"].get("last_run", 0)
        d["config"] = out
        _save(d)
    return out


# ---------------------------------------------------------------- collect

def _try(fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 — a dead system is itself a finding
        return {"error": str(e)}


def _sys(system_id: str) -> dict | None:
    s = store.get_system(system_id, reveal_secrets=True)
    return s if s and s.get("host") else None


def _metric_stats() -> dict:
    """Aggregate the poller ring buffers to min/avg/max/latest per metric."""
    out = {}
    for sid, metrics in poller.history().items():
        agg = {}
        for name, points in metrics.items():
            vals = [v for _, v in points if isinstance(v, (int, float))]
            if vals:
                agg[name] = {"min": round(min(vals), 1), "max": round(max(vals), 1),
                             "avg": round(sum(vals) / len(vals), 1),
                             "latest": round(vals[-1], 1), "samples": len(vals)}
        if agg:
            out[sid] = agg
    return out


def collect() -> dict:
    """Compact lab snapshot for the AI digest. Every section degrades to
    an {"error": ...} instead of failing the whole report."""
    data = {"generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "systems": {}}

    if (s := _sys("unifi")):
        ins = _try(unifi.insights, s)
        sec = _try(unifi.events, s, ["SECURITY"], 0, 10)
        anoms = _try(unifi.anomalies, s)
        data["systems"]["unifi"] = {
            "summary": _try(unifi.summary, s),
            "gateway": ins.get("gateway"),
            "port_issues": (ins.get("port_issues") or [])[:5],
            "firmware_updates": ins.get("updates"),
            "security_events_total": sec.get("total"),
            "recent_security_events": [e.get("message") for e in (sec.get("events") or [])[:8]],
            "client_anomalies": anoms[:10] if isinstance(anoms, list) else anoms,
        }

    if (s := _sys("proxmox")):
        data["systems"]["proxmox"] = {
            "summary": _try(proxmox.summary, s),
            "nodes": _try(proxmox.nodes, s),
            "storage": _try(proxmox.storage, s),
        }

    if (s := _sys("docker")):
        summ = _try(docker.summary, s)
        conts = _try(docker.containers, s)
        not_running = ([c.get("name") for c in conts if c.get("state") != "running"]
                       if isinstance(conts, list) else conts)
        data["systems"]["docker"] = {"summary": summ, "not_running": not_running}

    if (s := _sys("homeassistant")):
        ha = {"summary": _try(homeassistant.summary, s)}
        zha = _try(homeassistant.zha_devices, s)
        if isinstance(zha, list):
            ha["zha"] = {
                "devices": len(zha),
                "offline": [d.get("name") for d in zha if d.get("available") is False][:15],
                "weak_links": [d.get("name") for d in zha
                               if d.get("available") is not False
                               and d.get("lqi") is not None and d.get("lqi") < 80][:15],
            }
        else:
            ha["zha"] = zha
        data["systems"]["homeassistant"] = ha

    mons = monitors.list_monitors()
    data["uptime_monitors"] = [
        {k: m.get(k) for k in ("name", "type", "target", "ok", "uptime_pct", "avg_ms", "error")}
        for m in mons] or "none configured"

    week_ago = time.time() - 7 * 86400
    data["recent_warnings"] = [
        {"level": e["level"], "system": e["system"], "message": e["message"]}
        for e in oplog.recent(250)
        if e.get("level") in ("warn", "error") and e.get("ts", 0) > week_ago][:60]

    data["metric_stats_last_hour"] = _metric_stats()
    return data


# --------------------------------------------------------------- generate

def generate(trigger: str = "manual") -> dict:
    """Collect, ask Claude for the digest, store it and push a summary."""
    if _running.is_set():
        raise ValueError("a report is already being generated — wait for it to finish")
    _running.set()
    try:
        data = collect()
        report = ai.analyze_health(data)
        report["id"] = secrets.token_hex(4)
        report["ts"] = time.time()
        report["trigger"] = trigger
        with _lock:
            d = _load()
            d["reports"] = ([report] + d["reports"])[:KEEP]
            if trigger == "scheduled":
                d["config"]["last_run"] = time.time()
            _save(d)

        grade = report.get("grade", "?")
        findings = report.get("findings") or []
        worst = {"critical", "serious"} & {f.get("severity") for f in findings}
        top = "; ".join(f.get("title", "") for f in findings[:3])
        notify.send(
            f"Homelab health report — grade {grade}",
            (report.get("summary", "") + (f" Top findings: {top}" if top else ""))[:900],
            priority="high" if worst else "default",
            tags=["clipboard"])
        oplog.add("action", "reports",
                  f"health report generated ({trigger}): grade {grade}, {len(findings)} finding(s)")
        return report
    finally:
        _running.clear()


# -------------------------------------------------------------- scheduler

def _due(cfg: dict, now: float) -> bool:
    if not cfg.get("enabled"):
        return False
    dt = datetime.datetime.fromtimestamp(now)
    days_back = (dt.weekday() - cfg.get("day", 0)) % 7
    slot = (dt - datetime.timedelta(days=days_back)).replace(
        hour=cfg.get("hour", 8), minute=0, second=0, microsecond=0)
    if slot.timestamp() > now:  # report day, but the hour hasn't come yet
        slot -= datetime.timedelta(days=7)
    return cfg.get("last_run", 0) < slot.timestamp() <= now


def start() -> None:
    def loop():
        while True:
            time.sleep(TICK)
            try:
                with _lock:
                    cfg = _load()["config"]
                if _due(cfg, time.time()):
                    generate("scheduled")
            except Exception as e:  # noqa: BLE001
                oplog.add("error", "reports", f"scheduled report failed: {e}")

    threading.Thread(target=loop, name="claudeos-reports", daemon=True).start()
