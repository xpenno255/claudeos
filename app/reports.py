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

**The weekly update** shares that slot and does not need the AI report. It is
the handful of facts deliberately *not* pushed as they happen: how many
container updates are waiting, and which systems blipped without paging
anybody. With the report enabled they are the last lines of its message; with
it disabled, or given up on for the week, they go out alone. Either way the
owner gets one message a week about housekeeping instead of one per image.
"""

import datetime
import json
import os
import secrets
import threading
import time

from . import (ai, backups, labissues, monitors, notify, offhours, oplog, outage,
               poller, registry, smart, store)
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

# What one report costs, per million tokens.
#
# A constant of its own rather than `toolloop.USD_PER_MTOK`, although today the
# two are equal because both callers run the same model. They were not always:
# the loop ran Sonnet while this ran Opus, and pricing a report at the loop's
# rate would have under-reported the most expensive call this app makes while
# looking entirely plausible (#60). The two models are named in two places
# (`ai.MODEL`, `toolloop.MODEL`) and can part again, so the rates stay in two
# places with them, and a test holds each pair together.
#
# claude-opus-5-5 list price, checked 2026-11-21 against
# platform.claude.com/docs/en/about-claude/pricing: $4 / $20 per MTok, down from
# the $5 / $25 of claude-opus-5 and claude-opus-4-8 before it. This is a number
# that has to be re-checked rather than derived, which is why every report
# records the model it was actually priced against instead of trusting this
# comment to stay true.
OPUS_USD_PER_MTOK = (4.0, 20.0)

# The bookkeeping `set_config` has to carry forward. `out` there is built fresh
# from the caller's payload, so anything not named here is destroyed by saving
# the schedule.
_BOOKKEEPING = ("last_run", "last_attempt", "attempts", "attempt_slot", "last_error",
                "weekly_last")

DEFAULT_CONFIG = {
    "enabled": False, "day": 5, "hour": 8,   # Saturday 08:00
    "weekly_last": 0,       # last time the weekly update went out, alone or not
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
    stored = d.get("config") or {}
    if ("weekly_last" not in stored and not stored.get("enabled")
            and (stored.get("day"), stored.get("hour")) == (0, 8)):
        # Written before the weekly update existed, with the report off and the
        # slot at what was then the default. Nobody chose Monday: the form saves
        # day and hour whenever it saves anything. So the slot follows the
        # default to Saturday. A report that was switched on keeps its day, and
        # so does anything saved from here on, which carries `weekly_last`.
        stored = {k: v for k, v in stored.items() if k not in ("day", "hour")}
    # Merged, not defaulted: a config written before the retry bookkeeping
    # existed is missing those keys, and every reader would otherwise have to
    # carry its own fallback for each one.
    d["config"] = {**DEFAULT_CONFIG, **stored}
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
    d["spend"] = _spend(d["reports"])
    return d


# ------------------------------------------------------------------ metering

def price(usage) -> dict | None:
    """Price one report from the usage `ai.ask_json` already attached.

    Returns None when there is nothing to price — a report stored before this
    existed, or an `analyse` that returned no usage. **None means "not
    measured", and that distinction is the point**: a zero would claim the most
    expensive call in the app was free, which is the shape of the blind spot
    this closes rather than a fix for it.

    Cache fields are not read because `ai.ask_json` does not report them: a
    report is one call with no prefix to reuse, so there is nothing to discount.

    The model recorded is the one the *API said it used*, taken from the usage
    block, falling back to the constant only if the response carried none. Naming
    `ai.MODEL` here instead would relabel this report with whatever is configured
    at the moment of pricing, which is a different claim and, across a version
    bump, a false one (#62).
    """
    if not isinstance(usage, dict):
        return None
    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    if not isinstance(inp, int) or not isinstance(out, int):
        return None
    in_rate, out_rate = OPUS_USD_PER_MTOK
    return {"input": inp, "output": out, "model": usage.get("model") or ai.MODEL,
            "usd": round((inp * in_rate + out * out_rate) / 1_000_000, 6)}


def _spend(reports: list) -> dict:
    """What the reports still on file have cost.

    Scoped to the kept reports, not to all time, and says so: `reports` counts
    what went into the total and `unmetered` counts entries carrying no cost, so
    a total assembled from half-measured history cannot read as complete.

    This is a meter, not a budget. #60 left the budget question open on the
    grounds that a weekly report is one predictable call while RUN REPORT NOW is
    a button a human can press repeatedly — deciding that wants real numbers,
    which is what this produces. Nothing here refuses to spend.
    """
    priced = [r["cost"] for r in reports
              if isinstance(r.get("cost"), dict) and isinstance(r["cost"].get("usd"), (int, float))]
    return {"usd": round(sum(c["usd"] for c in priced), 6),
            "reports": len(priced),
            "unmetered": len(reports) - len(priced),
            "kept": KEEP}


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
        for k in _BOOKKEEPING:
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
        if not (s := _sys(sid)):
            continue
        # A system inside its expected-offline window is reported as asleep, not
        # attempted. Calling it would produce a block of {"error": ...} that the
        # prompt is explicitly told to read as a finding, so the digest would
        # report a NAS on a power schedule as broken every week.
        sched = offhours.status(s)
        if sched and sched["tolerated"]:
            data["systems"][sid] = {"scheduled_offline": offhours.reason(sched),
                                    "note": "expected to be powered down — not a fault"}
            continue
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

    # Top-level, like the monitors and the lab issue queue: backups are not any
    # one system's business. An empty section is explicitly *not* good news, and
    # the section says so itself rather than trusting the model to infer it.
    data["backups"] = soft(backups.report_section)

    # Whether the report's own findings can reach anybody. Stated rather than
    # left to be inferred: without this the only trace of alerting in the
    # snapshot was whatever `recent_warnings` happened to hold, and a report
    # once announced "no notification channel configured" while Telegram was
    # delivering — reasoning correctly from a stale log line (#53).
    data["alerting"] = soft(notify.state)

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
        report["cost"] = price(report.get("_usage"))
        with _lock:
            d = _load()
            d["reports"] = ([report] + d["reports"])[:KEEP]
            if trigger == "scheduled":
                # The slot is settled: clear the attempt state so a later failure
                # on the NEXT slot starts from a clean count, and drop the stale
                # error so the UI stops reporting a problem that is over. The
                # weekly update rides in this message, so it is settled too.
                d["config"].update(last_run=clock, attempts=0, attempt_slot=0,
                                   last_error=None, weekly_last=clock)
            _save(d)

        grade = report.get("grade", "?")
        findings = report.get("findings") or []
        worst = {"critical", "serious"} & {f.get("severity") for f in findings}
        scheduled = trigger == "scheduled"
        # One phone screen: the counts, the three findings that lead, and where
        # the rest is. The prose summary stays on the Reports tab, which is
        # where somebody with time to read it will be.
        body = [_severity_line(findings)]
        body += [f"• {f.get('title', '')}"[:160] for f in findings[:3]]
        if scheduled:
            body += weekly_lines(clock)
        notify.send(
            f"{'WEEKLY LAB REPORT' if scheduled else 'LAB REPORT'} · grade {grade}",
            "\n".join(body)[:900],
            priority="high" if worst else "default",
            tags=["clipboard"], link="#/ops/reports")
        # The cost goes in the log line too, not only on the report: the ops log
        # is where spend is looked for after the fact, and triage already answers
        # "what did that run cost" there.
        spent = (report["cost"] or {}).get("usd")
        oplog.add("action", "reports",
                  f"health report generated ({trigger}): grade {grade}, "
                  f"{len(findings)} finding(s), "
                  + (f"${spent:.4f}" if spent is not None else "cost not measured"))
        return report
    finally:
        _running.clear()


def _severity_line(findings: list) -> str:
    """`1 critical · 2 serious · 3 warnings`, worst first, zeros left out."""
    parts = []
    for sev, plural in (("critical", "critical"), ("serious", "serious"),
                        ("warning", "warnings")):
        n = sum(1 for f in findings if f.get("severity") == sev)
        if n:
            parts.append(f"{n} {sev if n == 1 else plural}")
    return " · ".join(parts) or "nothing serious found"


# ---------------------------------------------------------- weekly update

WEEKLY_BLIP_LINES = 5


def weekly_lines(now: float) -> list:
    """What the week left that nobody was paged about. Empty if nothing did.

    Reads only what the app already knows: `registry.peek()` never sweeps, so a
    slow registry cannot hold the weekly message up, and the count is at most
    one sweep interval old.

    Images are counted, never listed. A list of `lscr.io/…:latest` names is
    twenty links on a phone, and the Containers tab already has it.
    """
    lines = []
    cache = registry.peek()
    waiting = sum(1 for i in cache.get("images") or [] if i.get("status") == "update")
    if waiting:
        lines.append(f"{waiting} container update{'' if waiting == 1 else 's'} to install")
    week = sorted(outage.blips(now - 7 * 86400).items(),
                  key=lambda kv: (-kv[1]["count"], kv[0]))
    for label, b in week[:WEEKLY_BLIP_LINES]:
        n = b["count"]
        lines.append(f"{label}: {n} short blip{'' if n == 1 else 's'} this week "
                     f"(longest {outage.span(b['longest'])})")
    return lines


def _weekly_due(cfg: dict, now: float) -> bool:
    """Is this week's update owed, with no report left to carry it?

    Not while the report for this slot may still run: it carries these lines,
    and sending them twice is the noise this exists to remove. Once the report
    is off, or has used up its attempts, the update goes alone, because a failed
    report must not take the housekeeping down with it.
    """
    slot = _slot(cfg, now)
    if slot > now or (cfg.get("weekly_last") or 0) >= slot:
        return False
    if cfg.get("enabled") and (cfg.get("last_run") or 0) < slot:
        tried = int(cfg.get("attempts") or 0) if cfg.get("attempt_slot") == slot else 0
        if tried < MAX_ATTEMPTS:
            return False
    return True


def _mark_weekly(now: float) -> None:
    with _lock:
        d = _load()
        d["config"]["weekly_last"] = now
        _save(d)


def send_weekly(now: float) -> bool:
    """Send the weekly update on its own. True if there was anything to say.

    Marked before it is sent, the ordering #27 was fixed with: whatever happens
    next, this slot has been dealt with and cannot come round again in five
    minutes.

    Nothing is sent with no channel configured. `notify.send` would record that
    as an alert lost, and raise a banner over a count of pending updates that
    the dashboard is already showing.
    """
    _mark_weekly(now)
    lines = weekly_lines(now)
    if not lines or not notify.channels():
        return False
    notify.send("WEEKLY UPDATE", "\n".join(lines), priority="low",
                tags=["clipboard"], link="#/ops/containers")
    oplog.add("info", "reports", "weekly update sent: " + "; ".join(lines))
    return True


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


def tick(now: float) -> None:
    """One pass of the scheduler: the report if it is due, else the update."""
    with _lock:
        cfg = _load()["config"]
    if not cfg.get("weekly_last"):
        # Never sent, which is every install on the day this arrives. Start the
        # count from now, so the first update comes at the next slot rather than
        # five minutes after a deploy on whatever day that happens to be.
        _mark_weekly(now)
        cfg["weekly_last"] = now
    if _due(cfg, now):
        generate("scheduled", now=now)
    elif _weekly_due(cfg, now):
        send_weekly(now)


def start() -> None:
    def loop():
        while True:
            time.sleep(TICK)
            try:
                tick(time.time())
            except Exception as e:  # noqa: BLE001
                oplog.add("error", "reports", f"scheduled report failed: {e}")

    threading.Thread(target=loop, name="claudeos-reports", daemon=True).start()
