"""What needs the owner, gathered from what the app already knows (#75).

The dashboard used to read green whenever the APIs answered: five "UP" tiles
while backups were failing, Telegram was dropping alerts and a `serious` lab
issue sat open. Every one of those facts was already held somewhere in this
process; nothing put them in one place. This does, and only that — **no source
here polls, sweeps or calls out**. Each reads a cache, a file or the poller's
snapshot, so the strip costs nothing to refresh and cannot itself become load
on the lab. Where a module's own getter would refresh over the network
(`smart.get`, `registry.get`), its `peek()` is read instead.

**A source that fails is an item, never an absence.** `collect` wraps each
one, and an exception (or a malformed answer) becomes a `warn` saying that the
source could not be checked. The strip's quiet state is "nothing needs you",
and the one way this module could lie is by reaching it because a check broke —
"could not look" reading as "nothing wrong" is the rule this app keeps
everywhere else (CONTEXT.md → backup job, verdict, nowhere to go).

Severities are `critical`, `warn` and `info`. `info` never makes the strip
non-quiet: it is for facts worth a glance (an image update, a muted backup
still failing underneath) that the owner has no duty to act on now.
"""

import re
import time
from collections import namedtuple

from . import backups, labissues, monitors, notify, oplog, poller, registry, reports, smart, store
from .connectors import CONNECTORS

SEVERITIES = ("critical", "warn", "info")
_ORDER = {s: i for i, s in enumerate(SEVERITIES)}

# The hash route each connector's operations tab lives at — the same mapping
# as `public/js/meta.js`, which the server cannot import.
CONNECTOR_TABS = {
    "unifi": "#/ops/network", "proxmox": "#/ops/compute", "docker": "#/ops/containers",
    "homeassistant": "#/ops/home", "synology": "#/ops/nas",
}

# Fullness thresholds for volumes and root disks. Higher than the meters' amber
# (75%), which colours a bar; this interrupts somebody, and a homelab disk at
# 80% is a normal state for months.
DISK_WARN_PCT = 85
DISK_CRIT_PCT = 95

# A connector whose snapshot is this old has not been polled for ten intervals:
# the poll loop has stalled, and every tile above is showing old data as live.
POLL_STALE_S = 10 * poller.POLL_INTERVAL

# A weekly report that has not landed in eight days missed its slot.
REPORT_MAX_AGE_S = 8 * 86400

# How far back the ops log is read for failed deliveries.
NOTIFY_WINDOW_S = 86400

# A SMART cache this old means two sweeps have not run.
SMART_STALE_S = 2 * smart.SWEEP_INTERVAL

Source = namedtuple("Source", "name label system tab read")


def item(system: str, severity: str, text: str, tab: str | None) -> dict:
    return {"system": system, "severity": severity, "text": text, "tab": tab}


def _n(count: int, noun: str, plural: str | None = None) -> str:
    return f"{count} {noun if count == 1 else (plural or noun + 's')}"


def _names(rows: list, key: str = "name", limit: int = 3) -> str:
    names = [str(r.get(key) or "?") for r in rows[:limit]]
    more = len(rows) - len(names)
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


def _short(e: Exception, limit: int = 200) -> str:
    s = str(e) or type(e).__name__
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _configured(system_id: str) -> bool:
    s = store.get_system(system_id, reveal_secrets=False)
    return bool(s and s.get("host"))


def _age(seconds: float) -> str:
    seconds = max(0, seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86400)} days"


# ---------------------------------------------------------------- sources

def _backups(now: float) -> list:
    ov = backups.overview()
    jobs = ov.get("jobs") or []
    tab = "#/ops/backups"
    by: dict = {}
    for j in jobs:
        by.setdefault(j.get("status"), []).append(j)
    out = []

    bad = by.get("failed", []) + by.get("stale", [])
    if bad:
        parts = [f"{len(by[s])} {s}" for s in ("failed", "stale") if by.get(s)]
        out.append(item("backups", "critical",
                        f"{_n(len(bad), 'backup')} not succeeding ({', '.join(parts)}): {_names(bad)}",
                        tab))
    # `never` and `unprotected` are warnings, not blanks: a job never wired up
    # and a guest with no job at all are the silent failures the tab exists for.
    for status, one, many in (
            ("unprotected", "guest has no backup job", "guests have no backup job"),
            ("never", "backup job has never reported a run", "backup jobs have never reported a run"),
            ("anomaly", "backup succeeded with an unusual size", "backups succeeded with an unusual size")):
        rows = by.get(status, [])
        if rows:
            out.append(item("backups", "warn",
                            f"{len(rows)} {one if len(rows) == 1 else many}: {_names(rows)}", tab))

    # Muting hides the alert, never the fact: counted, and what it is hiding
    # named, but as info — the owner has already said they know.
    muted = by.get("muted", [])
    if muted:
        hiding = [j for j in muted if j.get("real_status") not in ("ok", "muted", None)]
        text = f"{_n(len(muted), 'backup job')} muted"
        if hiding:
            text += f", {len(hiding)} not ok underneath: " + ", ".join(
                f"{j.get('name')} ({j['real_status']})" for j in hiding[:3])
        out.append(item("backups", "info", text, tab))

    err = ov.get("discovery_error")
    if err:
        out.append(item("backups", "warn",
                        "Backup discovery is failing, so the backup list may be incomplete: "
                        f"{err.get('message') if isinstance(err, dict) else err}", tab))
    elif not jobs:
        out.append(item("backups", "info",
                        "No backups are tracked — nothing is being watched, which is not the same as safe",
                        tab))
    return out


def _monitors(now: float) -> list:
    mons = [m for m in monitors.list_monitors() if m.get("enabled") is not False]
    down = [m for m in mons if m.get("ok") is False]
    if not down:
        return []
    if len(down) == 1:
        m = down[0]
        text = f"Monitor {m.get('name')} is down" + (f": {m['error']}" if m.get("error") else "")
    else:
        text = f"{len(down)} monitors are down: {_names(down)}"
    return [item("monitors", "critical", text, "#/ops/uptime")]


# The two ops-log lines `notify._fan_out` writes: one per failed attempt, and one
# when an alert is gone for good. Matched by text because the ops log is the only
# record of delivery there is.
_LOST = re.compile(r' was not delivered via (?P<chans>.+?) after \d+ attempts')
_SENT = re.compile(r'" sent via (?P<chans>.+)$')
_ATTEMPT = re.compile(r'^(?P<chan>.+?) delivery failed \(attempt \d+\)')


def _notify(now: float) -> list:
    st = notify.state()
    out = []
    gap = st.get("gap")
    if gap:
        n = int(gap.get("count") or 0)
        text = (f"{_n(n, 'alert')} had nowhere to go — no notification channel is configured, "
                f"so {'it was' if n == 1 else 'they were'} discarded")
        if gap.get("last_title"):
            text += f". Most recent: “{gap['last_title']}”"
        out.append(item("notify", "critical", text, "#/setup"))

    lost: dict = {}       # channel -> alerts lost in the window
    failed: dict = {}     # channel -> failed attempts in the window
    latest: dict = {}     # channel -> newest event: "ok" | "lost" | "attempt"
    for e in oplog.recent(250):  # newest first
        if e.get("system") != "notify" or now - float(e.get("ts") or 0) > NOTIFY_WINDOW_S:
            continue
        msg = str(e.get("message") or "")
        m = _LOST.search(msg)
        if m:
            for c in m["chans"].split(", "):
                lost[c] = lost.get(c, 0) + 1
                latest.setdefault(c, "lost")
            continue
        m = _SENT.search(msg)
        if m:
            for c in m["chans"].split(", "):
                latest.setdefault(c, "ok")
            continue
        m = _ATTEMPT.match(msg)
        if m:
            failed[m["chan"]] = failed.get(m["chan"], 0) + 1
            latest.setdefault(m["chan"], "attempt")

    for chan, n in sorted(lost.items()):
        if latest.get(chan) != "ok":
            out.append(item("notify", "critical",
                            f"{chan} is not delivering: {_n(n, 'alert')} lost in the last 24 h, "
                            "and nothing has got through since", "#/setup"))
        else:
            out.append(item("notify", "warn",
                            f"{chan} lost {_n(n, 'alert')} in the last 24 h (delivering again since)",
                            "#/setup"))
    # Failed attempts with nothing lost are a flaky channel, not a dead one — but
    # "retries got through" is only said once a later send proves it.
    for chan, n in sorted(failed.items()):
        if chan not in lost:
            tail = "; it has delivered since" if latest.get(chan) == "ok" else ""
            out.append(item("notify", "info",
                            f"{chan}: {_n(n, 'delivery attempt')} failed in the last 24 h{tail}",
                            "#/setup"))
    return out


def _smart(now: float) -> list:
    if not _configured("proxmox"):
        return []
    c = smart.peek()
    tab = "#/ops/compute"
    out = []
    if c.get("error"):
        out.append(item("proxmox", "warn",
                        f"SMART sweep failed, so disk health is unknown: {c['error']}", tab))
    elif not c.get("ts"):
        out.append(item("proxmox", "info", "Disk SMART health has not been checked yet", tab))
    elif now - c["ts"] > SMART_STALE_S:
        out.append(item("proxmox", "warn",
                        f"Disk SMART health was last checked {_age(now - c['ts'])} ago", tab))
    disks = c.get("disks") or []
    for status, sev, word in (("fail", "critical", "failing"), ("warn", "warn", "warning")):
        rows = [d for d in disks if d.get("status") == status]
        if rows:
            where = ", ".join(f"{d.get('devpath')} on {d.get('node')}" for d in rows[:3])
            more = f" and {len(rows) - 3} more" if len(rows) > 3 else ""
            out.append(item("proxmox", sev, f"{_n(len(rows), 'disk')} {word} SMART: {where}{more}", tab))
    return out


def _fullness(system: str, what: str, pct, tab: str) -> list:
    if not isinstance(pct, (int, float)):
        return []
    if pct >= DISK_CRIT_PCT:
        return [item(system, "critical", f"{what} is {pct:.0f}% full", tab)]
    if pct >= DISK_WARN_PCT:
        return [item(system, "warn", f"{what} is {pct:.0f}% full", tab)]
    return []


def _snapshot_data(system_id: str) -> dict | None:
    """The connector's last good summary, or None when it has none — down,
    asleep or unconfigured are the connectors source's business, not this one's."""
    st = poller.snapshot().get(system_id) or {}
    return (st.get("data") or {}) if st.get("ok") is True else None


def _synology(now: float) -> list:
    d = _snapshot_data("synology")
    if d is None:
        return []
    tab = CONNECTOR_TABS["synology"]
    out = []
    if d.get("storage_error"):
        # Couldn't look: the volume and disk checks below have nothing to read.
        out.append(item("synology", "warn",
                        f"NAS storage health is unreadable: {d['storage_error']}", tab))
    for v in d.get("volumes") or []:
        name = v.get("name") or v.get("id") or "?"
        if v.get("status") not in ("normal", None):
            # `attention` is DSM asking to be looked at, a warning; anything
            # else (degraded, crashed) has lost redundancy or data. The same
            # grading as the NAS tab's pill (public/js/severity.js, #86), so
            # the strip and the tab it links to cannot disagree.
            sev = "warn" if v["status"] == "attention" else "critical"
            out.append(item("synology", sev, f"NAS volume {name} is {v['status']}", tab))
        out += _fullness("synology", f"NAS volume {name}", v.get("pct"), tab)
    n = int(d.get("disks_abnormal") or 0)
    if n:
        out.append(item("synology", "warn",
                        f"{_n(n, 'NAS disk')} not reporting a normal status", tab))
    if d.get("temp_warning"):
        out.append(item("synology", "warn",
                        f"NAS reports a temperature warning ({d.get('temp_c', '?')}°C)", tab))
    return out


def _proxmox(now: float) -> list:
    d = _snapshot_data("proxmox")
    if d is None:
        return []
    tab = CONNECTOR_TABS["proxmox"]
    out = []
    if d.get("perms_hint"):
        out.append(item("proxmox", "warn",
                        "Proxmox token lacks permissions, so node vitals are unreadable", tab))
    for n in d.get("nodes") or []:
        name = n.get("node") or "?"
        if n.get("status") not in ("online", None):
            out.append(item("proxmox", "critical", f"Proxmox node {name} is {n['status']}", tab))
        if n.get("maxdisk"):
            out += _fullness("proxmox", f"Root disk on {name}",
                             100.0 * (n.get("disk") or 0) / n["maxdisk"], tab)
    return out


def _docker(now: float) -> list:
    d = _snapshot_data("docker")
    if d is None:
        return []
    tab = CONNECTOR_TABS["docker"]
    out = []
    if d.get("host_error"):
        out.append(item("docker", "warn", f"Docker host vitals are unreadable: {d['host_error']}", tab))
    for disk in (d.get("host") or {}).get("disks") or []:
        out += _fullness("docker", f"Docker host disk {disk.get('mount')}", disk.get("pct"), tab)
    return out


def _labissues(now: float) -> list:
    tab = "#/labissues"
    try:
        labissues.settings()
    except LookupError:
        return []  # not set up: not watching issues is a choice
    except ValueError as e:
        return [item("labissues", "warn", f"Lab Issues is misconfigured: {e}", tab)]
    snap = labissues.snapshot()
    out = []
    if snap.get("error"):
        out.append(item("labissues", "warn",
                        f"The lab issue queue could not be read: {snap['error']}"
                        + ("" if snap.get("issues") else " — the queue is unknown, not empty"), tab))
    elif not snap.get("checked"):
        out.append(item("labissues", "info", "The lab issue queue has not been read yet", tab))

    sec = labissues.report_section(issues=snap.get("issues") or [], now=now)
    for u in sec.get("unresolved_diagnosed") or []:
        sev = {"critical": "critical", "serious": "warn"}.get(u.get("severity"))
        if sev:
            out.append(item("labissues", sev,
                            f"Lab issue #{u.get('number')} diagnosed {u['severity']} and still open: "
                            f"{u.get('title')}", tab))
    stale = sec.get("untriaged_too_long") or []
    if stale:
        out.append(item("labissues", "warn",
                        f"{_n(len(stale), 'lab issue')} untriaged for over "
                        f"{sec.get('untriaged_threshold_hours', 24)} h — triage may have stopped", tab))
    if sec.get("budget_state") in ("hard", "stopped"):
        out.append(item("labissues", "warn",
                        "Today's triage budget is spent, so automatic triage is blocked until tomorrow",
                        tab))
    return out


def _reports(now: float) -> list:
    st = reports.get_state()
    cfg = st.get("config") or {}
    tab = "#/ops/reports"
    if not cfg.get("enabled"):
        return [item("reports", "warn", "The weekly health report is switched off", tab)]
    out = []
    err = cfg.get("last_error")
    if err:
        out.append(item("reports", "warn",
                        f"The last scheduled health report failed: "
                        f"{err.get('message') if isinstance(err, dict) else err}", tab))
    stamps = [r.get("ts") for r in st.get("reports") or [] if isinstance(r.get("ts"), (int, float))]
    last = max(stamps + [cfg.get("last_run") or 0])
    if not last:
        out.append(item("reports", "info", "The weekly health report is on but has not run yet", tab))
    elif now - last > REPORT_MAX_AGE_S:
        out.append(item("reports", "warn", f"The last health report is {_age(now - last)} old", tab))
    return out


def _registry(now: float) -> list:
    if not _configured("docker"):
        return []
    c = registry.peek()
    tab = CONNECTOR_TABS["docker"]
    out = []
    if c.get("error"):
        out.append(item("docker", "warn",
                        f"The image update check failed, so updates are unknown: {c['error']}", tab))
    ups = [i for i in c.get("images") or [] if i.get("status") == "update"]
    if ups:
        out.append(item("docker", "info", f"{_n(len(ups), 'container image update')} available", tab))
    return out


def _connectors(now: float) -> list:
    snap = poller.snapshot()
    out = []
    for sid in CONNECTORS:
        if not _configured(sid):
            continue
        label = store.SYSTEM_LABELS.get(sid, sid)
        tab = CONNECTOR_TABS.get(sid)
        st = snap.get(sid)
        if not st:
            out.append(item(sid, "info", f"{label} has not been polled yet", tab))
            continue
        ts = st.get("ts")
        if isinstance(ts, (int, float)) and now - ts > POLL_STALE_S:
            out.append(item(sid, "warn",
                            f"{label} was last polled {_age(now - ts)} ago — polling may have stopped",
                            tab))
        # Asleep on schedule is a third state, not a fault (CONTEXT.md →
        # expected-offline window): no item, the tile already says so.
        if st.get("ok") is False:
            out.append(item(sid, "critical", f"{label} is unreachable: {st.get('error')}", tab))
    return out


SOURCES = [
    Source("connectors", "system polling", "poller", None, _connectors),
    Source("backups", "backups", "backups", "#/ops/backups", _backups),
    Source("monitors", "uptime monitors", "monitors", "#/ops/uptime", _monitors),
    Source("notify", "notification delivery", "notify", "#/setup", _notify),
    Source("smart", "disk SMART health", "proxmox", "#/ops/compute", _smart),
    Source("proxmox", "Proxmox nodes", "proxmox", "#/ops/compute", _proxmox),
    Source("synology", "NAS storage", "synology", "#/ops/nas", _synology),
    Source("docker", "Docker host disks", "docker", "#/ops/containers", _docker),
    Source("labissues", "lab issues", "labissues", "#/labissues", _labissues),
    Source("reports", "the weekly report", "reports", "#/ops/reports", _reports),
    Source("registry", "image updates", "docker", "#/ops/containers", _registry),
]


# ---------------------------------------------------------------- aggregate

def _valid(it) -> bool:
    return (isinstance(it, dict) and it.get("severity") in SEVERITIES
            and isinstance(it.get("text"), str) and it["text"].strip() != ""
            and isinstance(it.get("system"), str))


def collect(sources=None, *, now=None) -> dict:
    """Every source's items, worst first, with when they were gathered.

    `sources` and `now` are the seam: the defaults read live app state, and the
    one property worth testing — a broken source is reported, never omitted — is
    about the wrapper, not about any source.

    A source's answer is all or nothing: if it raises, or returns anything that
    is not a list of well-formed items, none of it is used and one `warn` says
    the source could not be checked. Half an answer from a broken check is not
    something to reassure anybody with.
    """
    now = time.time() if now is None else float(now)
    items, unchecked = [], []
    for src in SOURCES if sources is None else sources:
        try:
            got = src.read(now)
            if not isinstance(got, list) or not all(_valid(i) for i in got):
                raise TypeError(f"returned a malformed answer ({type(got).__name__})")
        except Exception as e:  # noqa: BLE001 — one broken source must not hide the rest
            unchecked.append(src.name)
            items.append({**item(src.system, "warn",
                                 f"Could not check {src.label} — {_short(e)}. "
                                 "Its state is unknown, not clear.", src.tab),
                          "unchecked": True})
            continue
        items.extend(got)
    items.sort(key=lambda i: _ORDER.get(i["severity"], len(_ORDER)))
    return {"items": items, "checked": now, "unchecked": unchecked,
            "sources": [s.name for s in (SOURCES if sources is None else sources)]}
