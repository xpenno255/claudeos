"""Scheduled AI health reports.

A collector pulls a compact snapshot of the whole lab (gateway health,
security events, Proxmox nodes/storage, Docker fleet, HA/ZHA, uptime
monitors, recent warnings), Claude turns it into a graded digest with
ranked findings, and the result is delivered through the notification
layer and kept in data/reports.json (last KEEP reports).

What each system contributes is that connector's `report_slice`, not this
module's business — `collect()` iterates `CONNECTORS` and knows no summary
shapes. What stays here is what belongs to no single connector: the uptime
monitors, the week's warnings, the lab issue queue, the metric aggregates,
and the two app-module caches attached to a system's block.

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

from . import ai, labissues, monitors, notify, oplog, poller, registry, smart, store
from .connectors import CONNECTORS
from .connectors._report import soft
from .store import DATA_DIR

PATH = os.path.join(DATA_DIR, "reports.json")
KEEP = 12
TICK = 300  # scheduler check interval, seconds

# How hard a failed weekly slot is retried before it is abandoned until the next
# one. The numbers matter less than the fact that they are finite: a scheduled
# report that failed used to be re-attempted every TICK for the rest of the week
# — up to 2,016 times — and the expensive failure mode is also the deterministic
# one, so every one of those attempts was billed (#27).
#
# Three tries half an hour apart recovers a rate limit or a network blip, which
# is the whole point of retrying at all, and costs at most ~3 calls if the
# failure is permanent.
MAX_ATTEMPTS = 3
RETRY_AFTER = 1800

DEFAULT_CONFIG = {
    "enabled": False, "day": 0, "hour": 8,   # Monday 08:00
    "last_run": 0,          # last time a scheduled report SUCCEEDED
    "last_attempt": 0,      # last time one was tried, successful or not
    "attempts": 0,          # tries against `attempt_slot`
    "attempt_slot": 0,      # the weekly slot `attempts` is counting
    "last_error": None,     # {"message", "ts", "attempt"} — surfaced in the UI
}

_lock = threading.Lock()
_running = threading.Event()  # one report generation at a time


def _load() -> dict:
    if not os.path.exists(PATH):
        return {"config": dict(DEFAULT_CONFIG), "reports": []}
    with open(PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    # Merged, not defaulted: a config written before the retry bookkeeping
    # existed is missing those keys, and every reader would otherwise have to
    # carry its own fallback for each one.
    d["config"] = {**DEFAULT_CONFIG, **(d.get("config") or {})}
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
        # Carry the bookkeeping forward. `out` is built fresh from the caller's
        # payload, so anything not copied here is destroyed by saving the
        # schedule — and dropping `last_run` would make the current slot due
        # again, which is the retry storm this module was just fixed for.
        for k in ("last_run", "last_attempt", "attempts", "attempt_slot", "last_error"):
            out[k] = d["config"].get(k, DEFAULT_CONFIG[k])
        d["config"] = out
        _save(d)
    return out


# ---------------------------------------------------------------- collect

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

    # What is interesting about a system is that system's own business: each
    # connector curates its slice, and a new one appears here without this
    # module being touched.
    for sid, mod in CONNECTORS.items():
        if (s := _sys(sid)):
            data["systems"][sid] = mod.report_slice(s)

    # Two exceptions, attached rather than pushed behind the seam. Both are
    # reported inside a connector's block because that is where a reader looks
    # for them, but neither is that connector's to produce: the SMART sweep
    # reads Proxmox's disks on its own schedule through `app/smart.py`, and the
    # registry check is Docker's images seen from outside Docker, with its own
    # credentials. A connector that fetched them would be reaching into app
    # state, which is the coupling this seam exists to prevent.
    if "proxmox" in data["systems"]:
        disks = soft(smart.get)
        data["systems"]["proxmox"]["disk_smart"] = (
            disks.get("disks", disks) if isinstance(disks, dict) else disks)

    if "docker" in data["systems"]:
        ups = soft(registry.get)
        data["systems"]["docker"]["image_updates_available"] = (
            [i["ref"] for i in ups.get("images", []) if i.get("status") == "update"]
            if isinstance(ups, dict) else ups)

    mons = monitors.list_monitors()
    data["uptime_monitors"] = [
        {k: m.get(k) for k in ("name", "type", "target", "ok", "uptime_pct", "avg_ms", "error")}
        for m in mons] or "none configured"

    week_ago = time.time() - 7 * 86400
    data["recent_warnings"] = [
        {"level": e["level"], "system": e["system"], "message": e["message"]}
        for e in oplog.recent(250)
        if e.get("level") in ("warn", "error") and e.get("ts", 0) > week_ago][:60]

    # A top-level section, alongside the monitors and the warnings — not a
    # per-connector block, because lab issues are not a connector (ADR-0001).
    data["lab_issues"] = soft(labissues.report_section)

    data["metric_stats_last_hour"] = _metric_stats()
    return data


# --------------------------------------------------------------- generate

def generate(trigger: str = "manual", *, snapshot=None, analyse=None, now=None) -> dict:
    """Collect, ask Claude for the digest, store it and push a summary.

    `snapshot()`, `analyse(data)` and `now` are the seam: the default snapshot
    sweeps every connector over the network and the default analysis is a paid
    Opus call, so the failure paths — the expensive half of this module — are
    tested with neither.

    A scheduled run **counts its attempt before it spends anything**. Everything
    else here is ordinary; that one ordering is the fix for #27.
    """
    if _running.is_set():
        raise ValueError("a report is already being generated — wait for it to finish")
    clock = time.time() if now is None else now
    _running.set()
    try:
        attempt = _count_attempt(clock) if trigger == "scheduled" else 0
        try:
            data = (snapshot or collect)()
            report = (analyse or ai.analyze_health)(data)
        except Exception as e:  # noqa: BLE001 — recorded, then re-raised for the caller
            if trigger == "scheduled":
                _record_failure(e, attempt, clock)
            raise
        report["id"] = secrets.token_hex(4)
        report["ts"] = clock
        report["trigger"] = trigger
        with _lock:
            d = _load()
            d["reports"] = ([report] + d["reports"])[:KEEP]
            if trigger == "scheduled":
                # The slot is settled: clear the attempt state so a later failure
                # on the NEXT slot starts from a clean count, and drop the stale
                # error so the UI stops reporting a problem that is over.
                d["config"].update(last_run=clock, attempts=0, attempt_slot=0,
                                   last_error=None)
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

def _slot(cfg: dict, now: float) -> float:
    """The most recent weekly slot at or before `now`, as an epoch timestamp.

    Shared by the due predicate and the attempt counter so the two can never
    disagree about which week's report they are talking about.
    """
    dt = datetime.datetime.fromtimestamp(now)
    days_back = (dt.weekday() - cfg.get("day", 0)) % 7
    slot = (dt - datetime.timedelta(days=days_back)).replace(
        hour=cfg.get("hour", 8), minute=0, second=0, microsecond=0)
    if slot.timestamp() > now:  # report day, but the hour hasn't come yet
        slot -= datetime.timedelta(days=7)
    return slot.timestamp()


def _count_attempt(now: float) -> int:
    """Charge this attempt to the current slot. Returns the attempt number.

    **Written before the work, not after.** The bug this replaces wrote its
    bookkeeping only on the success path, so a failure left the slot looking
    untouched and the scheduler re-attempted it every TICK for the rest of the
    week. Recording first means a crash, a kill, or a raised exception all leave
    the same trace: this slot has been tried.

    The counter is stamped with the slot it belongs to rather than being reset on
    a schedule boundary — so it needs nothing to run at the right moment to
    expire, and a new slot starts from zero simply by not matching.
    """
    with _lock:
        d = _load()
        cfg = d["config"]
        slot = _slot(cfg, now)
        if cfg.get("attempt_slot") != slot:
            cfg["attempts"], cfg["attempt_slot"] = 0, slot
        cfg["attempts"] = int(cfg.get("attempts") or 0) + 1
        cfg["last_attempt"] = now
        _save(d)
        return cfg["attempts"]


def _record_failure(e: Exception, attempt: int, now: float) -> None:
    """Keep the failure where a human can find it, and say when we gave up.

    The ops log alone is not enough here: a report that never generates cannot
    carry its own failure into the weekly digest, which is where warnings are
    normally read. So it also lands in the config, which `get_state()` returns
    and the Reports panel renders.
    """
    message = f"{type(e).__name__}: {e}"
    with _lock:
        d = _load()
        d["config"]["last_error"] = {"message": message, "ts": now, "attempt": attempt}
        _save(d)
    if attempt >= MAX_ATTEMPTS:
        oplog.add("error", "reports",
                  f"scheduled report failed {attempt}x — giving up until the next "
                  f"weekly slot: {message}")


def _due(cfg: dict, now: float) -> bool:
    """Is this week's scheduled report outstanding, and worth another try?

    Three ways to be not-due beyond "not that time yet": it already succeeded,
    it has used up its attempts for this slot, or the last attempt was too
    recent. Only the first of those existed before, which is why one failure
    used to mean an unbounded, billed retry loop.
    """
    if not cfg.get("enabled"):
        return False
    slot = _slot(cfg, now)
    if slot > now or cfg.get("last_run", 0) >= slot:
        return False

    tried = int(cfg.get("attempts") or 0) if cfg.get("attempt_slot") == slot else 0
    if tried >= MAX_ATTEMPTS:
        return False
    if tried and now - (cfg.get("last_attempt") or 0) < RETRY_AFTER:
        return False
    return True


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
