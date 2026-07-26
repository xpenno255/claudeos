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

_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(PATH):
        return {"runs": {}}
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        # A truncated or hand-edited file must not take down the queue view.
        # Losing these records costs a re-read of GitHub, nothing more.
        oplog.add("warn", "labissues", f"triage records unreadable, starting fresh: {e}")
        return {"runs": {}}
    if not isinstance(d, dict):
        return {"runs": {}}
    if not isinstance(d.get("runs"), dict):
        d["runs"] = {}
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
