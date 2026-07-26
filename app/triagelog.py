"""What triage has already concluded, kept across restarts.

`labissues` posts a verdict to GitHub and marks the issue; the marker is what
stops a re-triage. But the marker is a *label*, and a label says only that
somebody looked — not what they found. Without this module the queue can show
`TRIAGED` and nothing more, and the app has to re-read GitHub comments to
recover a verdict it produced itself minutes earlier.

So: one record per issue, last write wins, in `data/triage.json`.

**Last write wins is a deliberate shape, not a shortcut.** This file answers
"what is this issue's current verdict" — the question the queue and the detail
card ask. It is explicitly *not* a spend ledger: re-triaging an issue overwrites
its record and the earlier run's cost goes with it. The daily budget ledger #36
needs is a different question ("what has triage cost today") and wants a
different shape, so it gets its own key in this file rather than trying to sum
these.

**GitHub remains the source of truth.** These records are a local convenience
that a `data/` wipe is allowed to destroy; the verdict itself is in the issue
comment, in the machine block, which is why that block exists. A missing record
therefore renders as "triaged, verdict not held locally" and never as untriaged.

The file's other half is the **daily ledger**: what triage has spent today. It
is a separate key because it answers a separate question, and the two shapes
disagree — `runs` is last-write-wins per issue and so forgets the cost of a run
it replaced, which is exactly what a budget must not do. The ops log cannot
serve as the ledger either: it stores message strings, is append-only without
rotation, and reloads only its tail.
"""

import json
import os
import threading
import time

from . import oplog
from .store import DATA_DIR

PATH = os.path.join(DATA_DIR, "triage.json")

# Records are small (a verdict block, a few hundred bytes) and one per issue, so
# this bound is generous by design — it exists to stop a long-lived install
# accumulating rows for issues closed years ago, not to ration anything.
KEEP = 500

# What one day of unattended triage may cost. A measured run is around $0.40 on
# ordinary material, so the soft limit buys roughly five before the sweep stops
# taking new work — enough for a bad morning in the lab, not enough to matter if
# something loops.
#
# Soft is the only one a healthy install ever meets, because no run starts above
# it. The two above exist for overshoot: a single run's worst case was measured
# at $3.15–$4.90, so one unlucky run started just under the soft limit can land
# well past it.
SOFT_USD = 2.00        # take no new work
HARD_USD = 4.00        # tell someone: a run overshot badly
STOP_USD = HARD_USD * 2  # disabled until the day resets

_lock = threading.Lock()

_EMPTY = {"runs": {}, "daily": {}}


def _load() -> dict:
    if not os.path.exists(PATH):
        return json.loads(json.dumps(_EMPTY))
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        # A truncated or hand-edited file must not take down the queue view.
        # Losing these records costs a re-read of GitHub, nothing more.
        oplog.add("warn", "labissues", f"triage records unreadable, starting fresh: {e}")
        return json.loads(json.dumps(_EMPTY))
    if not isinstance(d, dict):
        return json.loads(json.dumps(_EMPTY))
    if not isinstance(d.get("runs"), dict):
        d["runs"] = {}
    if not isinstance(d.get("daily"), dict):
        d["daily"] = {}
    return d


def _save(d: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, PATH)


def _prune(runs: dict) -> dict:
    if len(runs) <= KEEP:
        return runs
    newest = sorted(runs.items(), key=lambda kv: kv[1].get("ts") or 0, reverse=True)
    return dict(newest[:KEEP])


def record(number, run: dict) -> dict:
    """Remember one run's outcome, replacing any earlier one for that issue.

    `run` is what `labissues.triage()` returns. Stored whole: the queue needs a
    handful of fields today and the detail card (#37) needs the rest, and the
    difference between them is not worth a second shape.
    """
    entry = dict(run or {})
    entry["number"] = int(number)
    entry["ts"] = time.time()
    with _lock:
        d = _load()
        d["runs"][str(int(number))] = entry
        d["runs"] = _prune(d["runs"])
        _save(d)
    return entry


def get(number) -> dict | None:
    """The whole stored record for one issue, or None."""
    with _lock:
        return _load()["runs"].get(str(int(number)))


def _summary(entry: dict) -> dict:
    """The fields a queue row needs — no evidence, no remediation, no prose.

    The full record can run to several kilobytes of evidence notes, and the
    queue polls every 30 seconds. What a row renders is a verdict, a severity
    and a count, so that is what crosses the wire.
    """
    block = entry.get("verdict") if isinstance(entry.get("verdict"), dict) else {}
    return {
        "number": entry.get("number"),
        "ok": bool(entry.get("ok")),
        "error": entry.get("error"),
        "verdict": block.get("verdict"),
        "confidence": block.get("confidence"),
        "severity": block.get("severity"),
        # A count, not the list: the row shows how many hypotheses were ruled
        # out, the detail card names them.
        "refuted": len(block.get("refuted") or []),
        "usd": (block.get("cost") or {}).get("usd"),
        "comment_url": entry.get("comment_url"),
        # A run whose verdict posted but whose label did not is a re-triage
        # waiting to happen, so it must not look like a clean finish.
        "labelled": entry.get("labelled", True),
        "ts": entry.get("ts"),
    }


def summaries() -> dict:
    """Every stored record, compacted, keyed by issue number as a string."""
    with _lock:
        runs = _load()["runs"]
    return {k: _summary(v) for k, v in runs.items() if isinstance(v, dict)}


# ------------------------------------------------------------- daily ledger

def _day(ts=None) -> str:
    """The local calendar day. Local, not UTC: "today's spend" is a claim about
    the owner's day, and a UTC rollover at 01:00 BST would be a surprise."""
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def _fresh(daily: dict, day: str) -> dict:
    """Today's page of the ledger, blank if the stored one belongs to a past day.

    Rolling over by comparison rather than by a scheduled reset means "until the
    day resets" needs nothing to be running at midnight to come true.
    """
    if isinstance(daily, dict) and daily.get("date") == day:
        return daily
    return {"date": day, "usd": 0.0, "runs": 0, "logged": False, "notified": False}


def _state(usd: float) -> str:
    if usd >= STOP_USD:
        return "stopped"
    if usd >= HARD_USD:
        return "hard"
    if usd >= SOFT_USD:
        return "soft"
    return "ok"


def _view(daily: dict) -> dict:
    usd = round(float(daily.get("usd") or 0.0), 6)
    return {"date": daily.get("date"), "usd": usd, "runs": int(daily.get("runs") or 0),
            "state": _state(usd), "soft": SOFT_USD, "hard": HARD_USD, "stop": STOP_USD}


def ledger(*, ts=None) -> dict:
    """What triage has spent today, and which band that puts it in."""
    day = _day(ts)
    with _lock:
        return _view(_fresh(_load()["daily"], day))


def spend(usd, *, ts=None) -> dict:
    """Add one run's cost to today's page. Returns the ledger as it now stands.

    Called for every run including the ones that died: a failed run has already
    been billed for every token it spent, and a ledger that counts only
    successes under-reports exactly the runs most likely to be repeated.
    """
    day = _day(ts)
    with _lock:
        d = _load()
        daily = _fresh(d["daily"], day)
        daily["usd"] = round(float(daily.get("usd") or 0.0) + float(usd or 0.0), 6)
        daily["runs"] = int(daily.get("runs") or 0) + 1
        d["daily"] = daily
        _save(d)
        return _view(daily)


def mark(flag: str, *, ts=None) -> bool:
    """Claim a once-a-day event. True the first time today, False after.

    The sweep runs every minute, so "log when the budget stops us" without this
    is 1,440 copies of one sentence a day, which is how an ops log stops being
    read. Claiming and writing happen under the same lock, so two threads cannot
    both win.
    """
    day = _day(ts)
    with _lock:
        d = _load()
        daily = _fresh(d["daily"], day)
        if daily.get(flag):
            d["daily"] = daily
            _save(d)
            return False
        daily[flag] = True
        d["daily"] = daily
        _save(d)
        return True
