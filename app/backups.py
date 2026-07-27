"""Backup tracking: did this produce a good copy recently?

Every other surface in this app measures **reachability**, which announces
itself — a box stops answering and the poller notices within thirty seconds. A
backup's failure mode is an **absence**: nothing happens, nothing errors, and
the gap is invisible until the day you need the thing that is missing. Polling
cannot see it, so this module watches for outcomes instead.

Not a connector, per ADR-0001: `CONNECTORS` means a polled lab system with
up/down semantics, and a backup job is neither a lab system nor ever "up". The
precedent is `app/smart.py` — a standalone module that *calls* the Proxmox
connector rather than becoming one, sweeps on its own cadence, keeps its own
store, and contributes to the weekly digest through `reports.py`.

**This store holds secrets.** `data/backups.json` contains heartbeat tokens,
which are bearer credentials: anyone holding one can report a job healthy — see
ADR-0002 for why the ingest route is unauthenticated and what that does and does
not permit.
`monitors.py`'s header promises its store holds none, and that promise is not
inherited here — said plainly because the two modules otherwise look alike.

Two kinds of job, one shape:

  heartbeat  created by the owner, editable, deletable, holds a token
  proxmox    discovered from `/cluster/backup` each sweep; mutable only by
             muting, and it disappears from the list when it disappears from
             the cluster

History is **persisted, not in-memory**. That is the difference between a
30-second poll, where losing state costs one tick, and a 26-hour window, where
losing state means either a false alarm on boot or silently sailing past a
missed run. `monitors.py` keeps its state in module globals; at its cadence that
is invisible, and at this one it would be the whole bug.
"""

import json
import os
import secrets
import threading
import time

from . import notify, offhours, oplog, sweeper  # noqa: F401  (offhours: see sweep)
from .store import DATA_DIR

PATH = os.path.join(DATA_DIR, "backups.json")

# A quarter of a year of dailies: enough for a baseline and a trend line,
# bounded so a long-lived install does not accumulate forever.
KEEP_RUNS = 90

# Below this many *sized* successful runs, anomaly detection does not engage.
# A new job alerting on its own first run is the fastest way to make the whole
# feature untrusted, and an ignored channel is worth less than no channel (#41).
MIN_BASELINE_RUNS = 5

# Asymmetric on purpose: a backup collapsing to a fraction of its usual size is
# the failure being hunted; growth is usually just growth.
SHRINK_RATIO = 0.5
GROWTH_RATIO = 3.0

# Grace on top of the schedule, when the job does not set its own. A daily job
# gets 26.4h: long enough that a slow or slightly delayed run does not alert,
# short enough that a missed run is caught the same day.
GRACE_MULTIPLIER = 1.1

SWEEP_INTERVAL = 1800  # vzdump runs daily at most; polling harder buys nothing

# The closed status vocabulary, in the spirit of `verdict.py`. `never` is
# deliberately not `ok`: a job added and never wired up is the likeliest way
# this feature fails silently, so it gets its own state rather than an empty
# cell. `unprotected` exists because the alternative is a list that looks
# complete and is not.
STATUSES = ("ok", "stale", "failed", "anomaly", "never", "unprotected", "muted")

# Worst first, so the thing needing attention is at the top of the tab rather
# than alphabetically buried.
_SEVERITY = {"failed": 0, "stale": 1, "unprotected": 2, "never": 3,
             "anomaly": 4, "muted": 5, "ok": 6}

# Which transitions are worth interrupting somebody for, and how loudly. Keyed
# by the status being entered. Absent means silent.
_ALERT = {
    "stale": ("high", ["rotating_light"]),
    "failed": ("high", ["rotating_light"]),
    "anomaly": ("default", ["warning"]),
}

_lock = threading.RLock()


# ------------------------------------------------------------------ storage

def _blank() -> dict:
    return {"jobs": {}, "runs": {}}


def _load() -> dict:
    if not os.path.exists(PATH):
        return _blank()
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        # A truncated or hand-edited file must not take down the tab. Losing
        # this costs history, which is bad, but a page that will not render is
        # worse — and the loss is visible, where a blank page is not.
        oplog.add("warn", "backups", f"backup store unreadable, starting fresh: {e}")
        return _blank()
    if not isinstance(d, dict):
        return _blank()
    d.setdefault("jobs", {})
    d.setdefault("runs", {})
    if not isinstance(d["jobs"], dict):
        d["jobs"] = {}
    if not isinstance(d["runs"], dict):
        d["runs"] = {}
    return d


def _save(d: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, PATH)


# --------------------------------------------------------------------- jobs

def _job_id() -> str:
    return secrets.token_hex(8)


def add_job(fields: dict) -> dict:
    """Create a heartbeat job. The token is its only credential."""
    job = {
        "id": _job_id(),
        "kind": "heartbeat",
        "name": (fields.get("name") or "unnamed").strip(),
        "schedule_hours": _num(fields.get("schedule_hours"), 24),
        "grace_hours": _num(fields.get("grace_hours"), None),
        "muted": bool(fields.get("muted")),
        "token": secrets.token_hex(16),
        "created": time.time(),
        "alerted": None,      # the status last alerted on; the latch
    }
    with _lock:
        d = _load()
        d["jobs"][job["id"]] = job
        _save(d)
    return job


def update_job(job_id: str, fields: dict) -> dict:
    with _lock:
        d = _load()
        job = d["jobs"].get(job_id)
        if not job:
            raise LookupError(f"unknown backup job: {job_id}")
        for k in ("name", "schedule_hours", "grace_hours", "muted"):
            if k in fields:
                if k in ("schedule_hours", "grace_hours"):
                    job[k] = _num(fields[k], job.get(k))
                elif k == "muted":
                    job[k] = bool(fields[k])
                else:
                    job[k] = str(fields[k]).strip()
        _save(d)
        return job


def delete_job(job_id: str) -> None:
    with _lock:
        d = _load()
        if job_id not in d["jobs"]:
            raise LookupError(f"unknown backup job: {job_id}")
        if d["jobs"][job_id].get("kind") != "heartbeat":
            raise ValueError("discovered jobs cannot be deleted, only muted")
        d["jobs"].pop(job_id)
        d["runs"].pop(job_id, None)
        _save(d)


def regenerate_token(job_id: str) -> dict:
    with _lock:
        d = _load()
        job = d["jobs"].get(job_id)
        if not job:
            raise LookupError(f"unknown backup job: {job_id}")
        if job.get("kind") != "heartbeat":
            raise ValueError("only heartbeat jobs have a token")
        job["token"] = secrets.token_hex(16)
        _save(d)
        return job


def without_token(job: dict) -> dict:
    """A job safe to put in a response body.

    The token is a bearer credential — anyone holding one can report a backup
    healthy — so it crosses the wire only where the caller asked for it: on
    creation and on regenerate. Echoing it back from an unrelated edit like a
    mute toggle is the same carelessness #45 was about.
    """
    return {k: v for k, v in (job or {}).items() if k != "token"}


def get_job(job_id: str) -> dict | None:
    with _lock:
        return _load()["jobs"].get(job_id)


def list_jobs() -> list:
    with _lock:
        return list(_load()["jobs"].values())


def job_for_token(token: str) -> dict | None:
    """Resolve a ping token. Compared in constant time — the token is the only
    thing standing between a stranger and holding a dead job green."""
    if not token:
        return None
    with _lock:
        for job in _load()["jobs"].values():
            stored = job.get("token")
            if stored and secrets.compare_digest(str(stored), str(token)):
                return job
    return None


_FALSEY = {"false", "0", "no", "fail", "failed", "err", "error", ""}


def reported_ok(body: dict) -> bool:
    """Did the job say it succeeded?

    Absent means yes: a bare `curl -X POST` is the documented minimum
    integration and has no body at all.

    Everything else is read generously, because the caller is a shell script.
    `{"ok": 0}` and `{"ok": "false"}` are what `-d '{"ok":'"$rc"'}'` and an
    unquoted variable actually produce, and a strict `is False` check would read
    both as success — silently turning the one signal a failing job managed to
    send into a green row.
    """
    if "ok" not in body:
        return True
    v = body["ok"]
    if isinstance(v, str):
        return v.strip().lower() not in _FALSEY
    return bool(v)


def _num(v, default):
    if v is None or v == "":
        return default
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


# --------------------------------------------------------------------- runs

def record_run(job_id: str, *, ok: bool, at=None, size_bytes=None,
               duration_s=None, detail=None) -> dict:
    """Append one run. `at` is injectable so history can be tested without waiting."""
    run = {
        "ts": float(at if at is not None else time.time()),
        "ok": bool(ok),
        "size_bytes": int(size_bytes) if size_bytes not in (None, "") else None,
        "duration_s": float(duration_s) if duration_s not in (None, "") else None,
        "detail": (str(detail)[:500] if detail else None),
    }
    with _lock:
        d = _load()
        if job_id not in d["jobs"]:
            raise LookupError(f"unknown backup job: {job_id}")
        hist = d["runs"].setdefault(job_id, [])
        hist.append(run)
        # keep the newest: the cap exists to bound growth, never to drop news
        if len(hist) > KEEP_RUNS:
            d["runs"][job_id] = hist[-KEEP_RUNS:]
        _save(d)
    return run


def runs(job_id: str) -> list:
    with _lock:
        return list(_load()["runs"].get(job_id, []))


# ---------------------------------------------------------------- evaluation

def grace_seconds(job: dict) -> float:
    """How long after a run is due before its absence is a fault.

    Derived from the schedule rather than fixed, so a weekly job is not judged
    by a daily job's standard, and overridable because some jobs are erratic by
    nature.
    """
    explicit = job.get("grace_hours")
    if explicit:
        return float(explicit) * 3600.0
    return float(job.get("schedule_hours") or 24) * 3600.0 * GRACE_MULTIPLIER


def baseline(run_list: list):
    """Median size of the sized successes, or None if there are too few.

    Median rather than mean: one truncated 40 KB run must not drag down the
    baseline that later runs are judged against — that is the very failure
    being watched for, and letting it move the goalposts would hide the next one.
    """
    sizes = sorted(r["size_bytes"] for r in run_list
                   if r.get("ok") and r.get("size_bytes") is not None)
    if len(sizes) < MIN_BASELINE_RUNS:
        return None
    mid = len(sizes) // 2
    if len(sizes) % 2:
        return sizes[mid]
    return (sizes[mid - 1] + sizes[mid]) / 2


def _status_for(job: dict, hist: list, now: float) -> dict:
    last = hist[-1] if hist else None
    last_ok = next((r for r in reversed(hist) if r.get("ok")), None)

    if job.get("kind") == "unprotected":
        return {"status": "unprotected", "last_ok": None, "baseline_ready": False}

    # Whether a baseline exists is a fact about the history, not about today's
    # outcome — reporting False on a failed job made the tab claim "baseline
    # forming" for jobs with months of sizes behind them.
    has_baseline = baseline([r for r in hist if r.get("ok")]) is not None

    if last is None:
        return {"status": "never", "last_ok": None, "baseline_ready": False}

    # An explicit failure is answered immediately: the job has already told us
    # how it went, so waiting out the grace period only delays the news.
    if not last.get("ok"):
        return {"status": "failed", "last_ok": last_ok and last_ok["ts"],
                "detail": last.get("detail"), "baseline_ready": has_baseline}

    if last_ok and (now - last_ok["ts"]) > grace_seconds(job):
        return {"status": "stale", "last_ok": last_ok["ts"],
                "baseline_ready": has_baseline}

    # Succeeded and recent. The remaining question is whether what it produced
    # looks like what it usually produces.
    prior = [r for r in hist[:-1] if r.get("ok") and r.get("size_bytes") is not None]
    base = baseline(prior)
    size = last.get("size_bytes")
    out = {"status": "ok", "last_ok": last_ok and last_ok["ts"],
           "baseline": base, "baseline_ready": base is not None}
    if base and size is not None:
        if size < base * SHRINK_RATIO or size > base * GROWTH_RATIO:
            out["status"] = "anomaly"
            out["anomaly_detail"] = (
                f"{_bytes(size)} against a {_bytes(base)} baseline")
    return out


def evaluate(jobs: list, now=None) -> list:
    """Status for each job, worst first. `now` is injected so every boundary is
    testable without waiting — the whole feature is a comparison against the
    clock, and each of its wrong answers is silent."""
    now = float(now if now is not None else time.time())
    out = []
    for job in jobs:
        hist = runs(job["id"]) if job.get("id") else []
        st = _status_for(job, hist, now)
        real = st["status"]
        # Muting hides the alert, never the fact: the real status is kept so the
        # tab can show what is actually true beside the fact it is silenced.
        if job.get("muted"):
            st["status"] = "muted"
        st["real_status"] = real
        st.update({
            "id": job.get("id"), "name": job.get("name"), "kind": job.get("kind"),
            "schedule_hours": job.get("schedule_hours"),
            "grace_hours": job.get("grace_hours"),
            "muted": bool(job.get("muted")),
            "last_run": hist[-1]["ts"] if hist else None,
            "last_size": hist[-1].get("size_bytes") if hist else None,
            "runs": len(hist),
            "sizes": [r.get("size_bytes") for r in hist[-30:]],
            "source": job.get("source"),
        })
        out.append(st)
    out.sort(key=lambda s: (_SEVERITY.get(s["status"], 9), (s.get("name") or "").lower()))
    return out


def _bytes(n) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return str(n)


# ------------------------------------------------------------------- alerts

_NO_CHANGE = object()


def _claim_transition(job_id: str, status: str):
    """Claim the right to alert on `status`, atomically.

    Returns the previous latch value if this call is the one that moved it, and
    `_NO_CHANGE` otherwise — including for a muted job, whose latch is left
    untouched so unmuting still announces whatever it is doing.

    **Read, decide and write happen in one lock acquisition**, which is the
    whole point. `sweep()` runs on the background sweeper *and* on demand from
    `POST /api/backups/sweep`, so two threads can be in here at once; deciding
    from a snapshot fetched under an earlier lock lets both read the same stale
    latch and both fire, which is a duplicate page for one event.

    `monitors._record` has the same shape for the same reason: commit the state
    change under the lock, send the notification outside it.
    """
    with _lock:
        d = _load()
        job = d["jobs"].get(job_id)
        if not job or job.get("muted"):
            return _NO_CHANGE
        prev = job.get("alerted")
        if prev == status:
            return _NO_CHANGE
        job["alerted"] = status
        _save(d)
        return prev


def _alert_on_transition(job: dict, status: str) -> None:
    """Alert on a change of state, never on the state itself.

    A job that has been dead for a week must not notify every half hour.
    Recovery is announced once, from any alerting state, so a resolved alert
    does not need chasing.
    """
    prev = _claim_transition(job["id"], status)
    if prev is _NO_CHANGE:
        return
    name = job.get("name") or job["id"]
    if status in _ALERT:
        priority, tags = _ALERT[status]
        notify.send(f"Backup {status}: {name}",
                    f"{name} is {status}.", priority=priority, tags=tags)
    elif prev in _ALERT and status == "ok":
        notify.send(f"Backup recovered: {name}", f"{name} is ok again.",
                    priority="default", tags=["white_check_mark"])


# -------------------------------------------------------------------- sweep

# ---------------------------------------------------------------- discovery

def _upsert_discovered(key: str, fields: dict) -> str:
    """Create or refresh a discovered job, preserving mute and alert latch.

    Keyed by a stable id from the source rather than by name, so renaming a
    Proxmox job in the cluster does not silently orphan its history and hand
    the owner a fresh `never` where a healthy job used to be.
    """
    with _lock:
        d = _load()
        existing = next((j for j in d["jobs"].values()
                         if j.get("discovery_key") == key), None)
        if existing:
            existing.update(fields)
            _save(d)
            return existing["id"]
        job = {"id": _job_id(), "discovery_key": key, "muted": False,
               "alerted": None, "created": time.time(), **fields}
        d["jobs"][job["id"]] = job
        _save(d)
        return job["id"]


def _forget_vanished(seen_keys: set) -> None:
    """A discovered job that is gone from the cluster is gone from the list.

    Only discovered ones: a heartbeat job has no source to vanish from, and
    deleting one because a sweep failed would destroy the owner's own config.
    """
    with _lock:
        d = _load()
        drop = [jid for jid, j in d["jobs"].items()
                if j.get("discovery_key") and j["discovery_key"] not in seen_keys]
        for jid in drop:
            d["jobs"].pop(jid, None)
            d["runs"].pop(jid, None)
        if drop:
            _save(d)


def discover_proxmox(settings, connector) -> int:
    """Mirror `/cluster/backup` into discovered jobs and fold in vzdump history.

    Returns how many jobs were seen. Raises on a connection failure so the
    caller can record it — an unreachable Proxmox must never read as "no backups
    found", which would be a clean bill of health invented out of an outage
    (story 26).
    """
    configured = connector.backup_jobs(settings)
    node_list = [n["node"] for n in connector.nodes(settings)]
    tasks_by_node = {}
    for node in node_list:
        try:
            tasks_by_node[node] = connector.vzdump_tasks(settings, node)
        except Exception as e:  # noqa: BLE001 — one bad node must not lose the rest
            oplog.add("warn", "backups", f"vzdump history unavailable on {node}: {e}")
            tasks_by_node[node] = []

    seen = set()
    for jobcfg in configured:
        key = f"proxmox:{jobcfg.get('id')}"
        seen.add(key)
        vmids = str(jobcfg.get("vmid") or "").strip()
        covers = "all guests" if jobcfg.get("all") else (vmids or "unspecified")
        node = jobcfg.get("node") or (node_list[0] if node_list else "?")
        # One schedule per node makes this attribution exact; more than one
        # makes it a guess, because the task carries no job id (see connector).
        ambiguous = sum(1 for j in configured
                        if (j.get("node") or (node_list[0] if node_list else "?")) == node) > 1
        jid = _upsert_discovered(key, {
            "kind": "proxmox",
            "name": f"vzdump {covers} → {jobcfg.get('storage') or '?'}",
            "schedule_hours": _schedule_hours(jobcfg.get("schedule")),
            "source": {"node": node, "storage": jobcfg.get("storage"),
                       "vmid": vmids, "schedule": jobcfg.get("schedule"),
                       "enabled": bool(jobcfg.get("enabled", 1)),
                       "attribution": "ambiguous" if ambiguous else "exact"},
        })
        _merge_tasks(jid, tasks_by_node.get(node, []))

    # Guests with no job at all. The endpoint is verified, but a cluster that
    # lacks it must still answer the question rather than going quiet.
    try:
        unprotected = connector.unprotected_guests(settings)
        source = "backup-info"
    except Exception:  # noqa: BLE001
        covered = set()
        for j in configured:
            for v in str(j.get("vmid") or "").split(","):
                if v.strip():
                    covered.add(v.strip())
        unprotected = [g for g in connector.guests(settings)
                       if str(g.get("vmid")) not in covered] if not any(
                           j.get("all") for j in configured) else []
        source = "derived (backup-info unavailable)"
    for g in unprotected:
        vmid = g.get("vmid")
        key = f"proxmox-unprotected:{vmid}"
        seen.add(key)
        _upsert_discovered(key, {
            "kind": "unprotected",
            "name": f"{g.get('name') or 'guest'} ({vmid}) — no backup job",
            "schedule_hours": None,
            "source": {"vmid": vmid, "detected_by": source},
        })

    _forget_vanished(seen)
    return len(seen)


def _schedule_hours(schedule):
    """Best-effort hours between runs from a systemd-style calendar string.

    Only the shapes this lab produces are decoded — a bare `HH:MM` is daily.
    Anything unrecognised returns 24 rather than raising: a wrong grace period
    is recoverable and visible, a sweep that dies on a calendar expression is
    neither.
    """
    s = (schedule or "").strip().lower()
    if not s:
        return 24
    if s.startswith("*/") or "hourly" in s:
        return 1
    if any(d in s for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")):
        return 168
    return 24


def _merge_tasks(job_id: str, tasks: list) -> None:
    """Fold vzdump task outcomes into this job's run history, without duplicates.

    Keyed on the task's start time: the sweep runs every 30 minutes and the same
    nightly task will be seen dozens of times, which must produce one run and
    not dozens.
    """
    if not tasks:
        return
    with _lock:
        d = _load()
        hist = d["runs"].setdefault(job_id, [])
        known = {round(float(r["ts"])) for r in hist}
        added = 0
        for t in tasks:
            if not t.get("started") or round(float(t["started"])) in known:
                continue
            hist.append({
                "ts": float(t["started"]),
                "ok": bool(t.get("ok")),
                "size_bytes": None,   # vzdump task history carries no size
                "duration_s": t.get("duration_s"),
                "detail": None if t.get("ok") else str(t.get("status"))[:500],
            })
            added += 1
        if added:
            hist.sort(key=lambda r: r["ts"])
            d["runs"][job_id] = hist[-KEEP_RUNS:]
            _save(d)


# -------------------------------------------------------------------- sweep

def sweep() -> None:
    """Refresh discovered jobs, then alert on anything that changed state."""
    from . import store
    from .connectors import proxmox
    settings = store.get_system("proxmox", reveal_secrets=True)
    if settings and settings.get("host"):
        sched = offhours.status(settings)
        if sched and sched["tolerated"]:
            pass  # host is asleep on schedule; its backup history can wait
        else:
            try:
                discover_proxmox(settings, proxmox)
                _discovery_error(clear=True)
            except Exception as e:  # noqa: BLE001 — discovery must not stop alerting
                # Recorded, not just logged: an unreachable Proxmox leaves the
                # discovered jobs as they were, and both the tab and the digest
                # have to be able to say "we could not look" rather than showing
                # an absence that reads as reassurance.
                _discovery_error(e)
                oplog.add("warn", "backups", f"proxmox backup discovery failed: {e}")

    for st in evaluate(list_jobs()):
        job = get_job(st["id"])
        if job and not job.get("muted"):
            _alert_on_transition(job, st["status"])


def _discovery_error(err=None, *, clear=False):
    """Remember why discovery last failed, so the absence can be explained.

    Story 26: an unreachable Proxmox must never look like "no backups found".
    Without this the two states render identically on a fresh install — an empty
    list — and an outage would read as a clean bill of health, which is the one
    conclusion this module exists to prevent anybody drawing.
    """
    with _lock:
        d = _load()
        if clear:
            if d.pop("discovery_error", None) is not None:
                _save(d)
            return None
        if err is not None:
            d["discovery_error"] = {"message": str(err)[:300], "ts": time.time()}
            _save(d)
        return d.get("discovery_error")


def _size_trend(hist: list):
    """How this job's size has moved over the last week, or None.

    The digest asks whether a backup is quietly shrinking — the failure that a
    per-run anomaly check misses because no single run is far enough out of band.
    """
    week_ago = time.time() - 7 * 86400
    sized = [r for r in hist if r.get("ok") and r.get("size_bytes") is not None]
    recent = [r["size_bytes"] for r in sized if r["ts"] >= week_ago]
    older = [r["size_bytes"] for r in sized if r["ts"] < week_ago]
    if not recent or not older:
        return None
    now_avg = sum(recent) / len(recent)
    then_avg = sum(older) / len(older)
    if not then_avg:
        return None
    pct = round(100.0 * (now_avg - then_avg) / then_avg, 1)
    if abs(pct) < 10:
        return None  # ordinary wobble; saying so every week is noise
    return {"change_pct": pct, "from": _bytes(then_avg), "to": _bytes(now_avg)}


def overview() -> dict:
    jobs = evaluate(list_jobs())
    counts = {}
    for s in jobs:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {"jobs": jobs, "counts": counts, "statuses": list(STATUSES),
            "discovery_error": _discovery_error()}


def report_section() -> dict:
    """What the weekly digest wants: counts, and anything needing a human named.

    A top-level section rather than a per-connector slice — backups are not one
    system's business, the same reason `lab_issues` and `uptime_monitors` sit at
    the top level of the snapshot.
    """
    jobs = evaluate(list_jobs())
    attention = [
        {"name": j["name"], "status": j["status"], "kind": j.get("kind"),
         "last_ok": j.get("last_ok"), "detail": j.get("detail") or j.get("anomaly_detail")}
        for j in jobs if j["status"] in ("failed", "stale", "unprotected", "never", "anomaly")
    ]
    counts = {}
    for s in jobs:
        counts[s["status"]] = counts.get(s["status"], 0) + 1

    # Jobs whose size has moved materially over the week. Not the same question
    # as `anomaly`, which judges one run against a baseline: a backup shrinking
    # 15% a week never trips that and is exactly what a weekly digest is for.
    trends = {}
    for job in list_jobs():
        t = _size_trend(runs(job["id"]))
        if t:
            trends[job.get("name") or job["id"]] = t

    err = _discovery_error()
    return {
        "total": len(jobs),
        "by_status": counts,
        "needing_attention": attention,
        "size_trends": trends or None,
        # An outage is not an absence. Without this the digest cannot tell
        # "nothing is backed up" from "we could not look" (story 26).
        "discovery_error": err,
        "note": (
            "backup discovery is failing, so this list may be incomplete — it is "
            "NOT evidence that nothing needs backing up" if err else
            "no backup jobs are configured or discovered — this is not a "
            "clean bill of health" if not jobs else None),
    }


def start() -> None:
    sweeper.spawn("backups", sweep, SWEEP_INTERVAL,
                  system="backups", error="backup sweep error")
