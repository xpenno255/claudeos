"""SMART disk health via the Proxmox API — Scrutiny-style evaluation.

Verified live on PVE (2026-07-16): /nodes/{node}/disks/list reports
health/wearout per disk; /disks/smart returns a structured `attributes`
table for ATA drives and raw smartctl `text` for NVMe. Disks are swept
every SWEEP_INTERVAL, evaluated against real-failure indicators
(reallocated/pending sectors for ATA; critical warning, spare, media
errors for NVMe), cached for the UI/report, and status *transitions*
alert through the notification layer.
"""

import re
import threading
import time

from . import notify, oplog, store
from .connectors import proxmox

SWEEP_INTERVAL = 6 * 3600
TEMP_WARN = 70  # °C — NVMe throttle/damage territory; ATA drives run cooler

_lock = threading.Lock()
_cache: dict = {"disks": [], "ts": None, "error": None}
_status: dict = {}  # "node:devpath" -> last status, for transition alerts

# smartctl NVMe health-log text → fields (values may contain thousands separators)
NVME_FIELDS = {
    "critical_warning": r"Critical Warning:\s+0x([0-9a-fA-F]+)",
    "temperature": r"Temperature:\s+([\d,]+)\s*Celsius",
    "available_spare": r"Available Spare:\s+(\d+)%",
    "spare_threshold": r"Available Spare Threshold:\s+(\d+)%",
    "percentage_used": r"Percentage Used:\s+(\d+)%",
    "media_errors": r"Media and Data Integrity Errors:\s+([\d,]+)",
    "unsafe_shutdowns": r"Unsafe Shutdowns:\s+([\d,]+)",
    "power_on_hours": r"Power On Hours:\s+([\d,]+)",
}

# ATA attributes whose raw value should be zero on a healthy drive
# (the Backblaze/Scrutiny failure-correlated set)
ATA_BAD_RAW = {
    5: "reallocated sectors",
    187: "reported uncorrectable errors",
    196: "reallocation events",
    197: "pending sectors",
    198: "offline uncorrectable sectors",
}


def _parse_nvme_text(text: str) -> dict:
    out = {}
    for key, pattern in NVME_FIELDS.items():
        m = re.search(pattern, text or "")
        if m:
            v = m.group(1).replace(",", "")
            out[key] = int(v, 16) if key == "critical_warning" else int(v)
    return out


def _evaluate(disk: dict, smart: dict) -> tuple[str, list, dict]:
    """Return (status ok|warn|fail, issues, detail) for one disk."""
    issues, detail = [], {}
    health = (disk.get("health") or smart.get("health") or "").upper()
    if health and health not in ("PASSED", "OK"):
        issues.append(f"self-assessment: {health}")

    if smart.get("type") == "text":  # NVMe
        d = _parse_nvme_text(smart.get("text", ""))
        detail = d
        if d.get("critical_warning"):
            issues.append(f"critical warning flag 0x{d['critical_warning']:02x}")
        if d.get("media_errors"):
            issues.append(f"{d['media_errors']} media/data integrity errors")
        if (spare := d.get("available_spare")) is not None and spare < max(d.get("spare_threshold", 10), 10):
            issues.append(f"available spare {spare}% below threshold")
        if (used := d.get("percentage_used")) is not None and used >= 90:
            issues.append(f"endurance {used}% used")
        if (t := d.get("temperature")) is not None and t >= TEMP_WARN:
            issues.append(f"temperature {t}°C")
    else:  # ATA attribute table
        attrs = smart.get("attributes") or []
        for a in attrs:
            aid = a.get("id")
            try:
                raw = int(str(a.get("raw", "0")).split()[0].replace(",", ""))
            except ValueError:
                continue
            if aid in ATA_BAD_RAW and raw > 0:
                issues.append(f"{ATA_BAD_RAW[aid]}: {raw}")
            if a.get("fail") not in (None, "-", False):
                issues.append(f"attribute {a.get('name', aid)} FAILING")
        temp = next((a for a in attrs if a.get("id") == 194), None)
        if temp:
            try:
                detail["temperature"] = int(str(temp.get("raw", "")).split()[0])
            except ValueError:
                pass

    hard = [i for i in issues if "self-assessment" in i or "critical warning" in i
            or "FAILING" in i or "below threshold" in i]
    status = "fail" if hard else "warn" if issues else "ok"
    return status, issues, detail


def sweep(alert: bool = True) -> list:
    """Probe every disk on every node; cache, and alert on transitions."""
    s = store.get_system("proxmox", reveal_secrets=True)
    if not s or not s.get("host"):
        with _lock:
            _cache.update(disks=[], ts=time.time(), error="Proxmox is not configured")
        return []

    disks = []
    try:
        for n in proxmox.nodes(s):
            node = n["node"]
            for d in proxmox.disk_list(s, node):
                dev = d.get("devpath")
                try:
                    smart = proxmox.disk_smart(s, node, dev)
                except Exception as e:  # noqa: BLE001 — one bad disk read shouldn't hide the rest
                    smart = {"error": str(e)}
                status, issues, detail = _evaluate(d, smart)
                disks.append({
                    "node": node,
                    "devpath": dev,
                    "model": d.get("model"),
                    "serial": d.get("serial"),
                    "type": d.get("type"),
                    "size": d.get("size"),
                    "health": d.get("health"),
                    "wearout": d.get("wearout"),  # PVE: 100 = unworn
                    "used_as": d.get("used"),
                    "status": status,
                    "issues": issues,
                    "detail": detail,
                })
    except Exception as e:  # noqa: BLE001
        with _lock:
            _cache.update(ts=time.time(), error=str(e))
        raise

    with _lock:
        _cache.update(disks=disks, ts=time.time(), error=None)

    for d in disks:
        key = f"{d['node']}:{d['devpath']}"
        prev = _status.get(key)
        _status[key] = d["status"]
        if not alert or prev is None or prev == d["status"]:
            continue  # first sight or unchanged — no alert
        label = f"{d['devpath']} ({d['model']}) on {d['node']}"
        if d["status"] != "ok":
            oplog.add("warn", "smart", f"disk {d['status']}: {label} — {'; '.join(d['issues'])}")
            notify.send(f"Disk {d['status'].upper()}: {d['devpath']} on {d['node']}",
                        f"{d['model']}: {'; '.join(d['issues'])}",
                        priority="urgent" if d["status"] == "fail" else "high",
                        tags=["floppy_disk"])
        else:
            oplog.add("info", "smart", f"disk recovered: {label}")
            notify.send(f"Disk recovered: {d['devpath']} on {d['node']}",
                        f"{d['model']}: SMART status back to ok",
                        priority="default", tags=["white_check_mark"])
    return disks


def get(max_age: int = SWEEP_INTERVAL) -> dict:
    """Cached sweep result, refreshing synchronously when stale."""
    with _lock:
        fresh = _cache["ts"] and time.time() - _cache["ts"] < max_age
        if fresh:
            return dict(_cache)
    sweep()
    with _lock:
        return dict(_cache)


def start() -> None:
    def loop():
        while True:
            try:
                sweep()
            except Exception as e:  # noqa: BLE001
                oplog.add("error", "smart", f"SMART sweep failed: {e}")
            time.sleep(SWEEP_INTERVAL)

    threading.Thread(target=loop, name="claudeos-smart", daemon=True).start()
