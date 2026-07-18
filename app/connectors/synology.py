"""Synology DSM connector.

Auth: username/password of a dedicated DSM user (Control Panel → User &
Group). Give it only what it needs — no admin required for read-only
stats, but the Storage Manager figures need membership of the
"administrators" group on most DSM 7 boxes. 2-factor auth is NOT
supported by the API session login — use a dedicated account without it.

Settings: host (e.g. 192.168.1.50:5000 or https://…:5001), username,
password. Sessions (sid) are cached per host+user and refreshed
automatically when DSM expires them.
"""

import re
import threading
import urllib.parse

from .. import httpclient

# DSM API error codes worth translating for humans.
_ERRORS = {
    100: "unknown error",
    101: "invalid parameter",
    102: "API does not exist",
    103: "method does not exist",
    104: "API version not supported",
    105: "no permission — the DSM user needs access to this app",
    106: "session timed out",
    107: "session interrupted (logged in elsewhere)",
    119: "invalid session id",
    400: "invalid username or password",
    401: "account disabled",
    402: "permission denied",
    403: "2-step verification required — use a dedicated DSM account without 2FA",
    404: "2-step verification code failed",
}
_RETRY_CODES = {105, 106, 107, 119}  # stale session — re-login and retry once

_lock = threading.Lock()
_sids: dict = {}  # (base, username) -> sid


def _base(settings: dict) -> str:
    host = settings["host"].strip().rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    return host


def _get(settings: dict, path: str, params: dict):
    """DSM's API is query-string based, so credentials ride in the URL —
    scrub them from anything that can bubble up to the oplog/browser."""
    url = _base(settings) + "/webapi/" + path + "?" + urllib.parse.urlencode(params)
    try:
        resp = httpclient.request("GET", url, verify_tls=settings.get("verify_tls", False),
                                  timeout=10)
    except (ConnectionError, httpclient.HttpError) as e:
        raise ConnectionError(_redact(str(e))) from None
    if not isinstance(resp, dict):
        raise ConnectionError(f"unexpected non-JSON reply from DSM at {path}")
    return resp


def _redact(msg: str) -> str:
    return re.sub(r"(passwd|_sid|account)=[^&\s:]*", r"\1=•••", msg)


def _err(resp: dict) -> tuple:
    code = (resp.get("error") or {}).get("code")
    return code, _ERRORS.get(code, f"DSM error {code}")


def _login(settings: dict) -> str:
    resp = _get(settings, "auth.cgi", {
        "api": "SYNO.API.Auth", "version": 3, "method": "login",
        "account": settings["username"], "passwd": settings["password"],
        "session": "ClaudeOS", "format": "sid",
    })
    if not resp.get("success"):
        _, msg = _err(resp)
        raise ConnectionError(f"DSM login failed: {msg}")
    sid = (resp.get("data") or {}).get("sid")
    if not sid:
        raise ConnectionError("DSM login returned no session id")
    with _lock:
        _sids[(_base(settings), settings["username"])] = sid
    return sid


def _call(settings: dict, api: str, method: str, version: int = 1,
          params: dict | None = None, _retried: bool = False) -> dict:
    key = (_base(settings), settings["username"])
    with _lock:
        sid = _sids.get(key)
    if not sid:
        sid = _login(settings)
    q = {"api": api, "version": version, "method": method, "_sid": sid,
         **(params or {})}
    resp = _get(settings, "entry.cgi", q)
    if resp.get("success"):
        return resp.get("data") or {}
    code, msg = _err(resp)
    if code in _RETRY_CODES and not _retried:
        with _lock:
            _sids.pop(key, None)
        return _call(settings, api, method, version, params, _retried=True)
    raise ConnectionError(f"DSM {api}.{method} failed: {msg}")


# ---------------------------------------------------------------- parsing

def _uptime_seconds(up: str) -> int | None:
    """SYNO.Core.System reports up_time as 'H:MM:SS' with unbounded hours."""
    try:
        h, m, s = (int(x) for x in str(up).split(":"))
        return h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _cpu_pct(util: dict) -> float | None:
    cpu = util.get("cpu") or {}
    loads = [cpu.get(k) for k in ("user_load", "system_load", "other_load")]
    if all(v is None for v in loads):
        return None
    return min(100.0, float(sum(v for v in loads if v is not None)))


# ---------------------------------------------------------------- public

def test(settings: dict) -> dict:
    info = _call(settings, "SYNO.Core.System", "info")
    up = _uptime_seconds(info.get("up_time"))
    days = f", up {up // 86400}d" if up is not None else ""
    return {"ok": True,
            "detail": f"{info.get('model', 'Synology')} — {info.get('firmware_ver', 'DSM ?')}{days}"}


def summary(settings: dict) -> dict:
    info = _call(settings, "SYNO.Core.System", "info")
    util = _call(settings, "SYNO.Core.System.Utilization", "get")

    out = {
        "model": info.get("model"),
        "dsm_version": info.get("firmware_ver"),
        "uptime_s": _uptime_seconds(info.get("up_time")),
        "temp_c": _int(info.get("sys_temp")),
        "temp_warning": bool(info.get("temperature_warning")),
        "cpu_pct": _cpu_pct(util),
        "mem_pct": (util.get("memory") or {}).get("real_usage"),
        "volumes": [],
        "disks_total": 0,
        "disks_abnormal": 0,
        "vol_used": None,
        "vol_total": None,
        "vol_pct": None,
    }

    # Storage Manager needs more privileges than the system stats — degrade
    # to a hint rather than failing the whole poll.
    try:
        st = storage(settings)
    except ConnectionError as e:
        out["storage_error"] = str(e)
        return out

    out["volumes"] = st["volumes"]
    used = sum(v["used"] for v in st["volumes"] if v["used"] is not None)
    total = sum(v["total"] for v in st["volumes"] if v["total"] is not None)
    if total:
        out.update(vol_used=used, vol_total=total,
                   vol_pct=round(100.0 * used / total, 1))
    out["disks_total"] = len(st["disks"])
    out["disks_abnormal"] = sum(1 for d in st["disks"] if d["status"] != "normal")
    return out


def storage(settings: dict) -> dict:
    """Volumes + physical disks from Storage Manager (load_info)."""
    data = _call(settings, "SYNO.Storage.CGI.Storage", "load_info")

    volumes = []
    for v in data.get("volumes") or []:
        size = v.get("size") or {}
        used, total = _int(size.get("used")), _int(size.get("total"))
        volumes.append({
            "id": v.get("id"),
            "name": (v.get("vol_desc") or v.get("id") or "").strip() or v.get("id"),
            "status": v.get("status"),
            "fs": v.get("fs_type"),
            "used": used,
            "total": total,
            "pct": round(100.0 * used / total, 1) if used is not None and total else None,
        })

    disks = []
    for d in data.get("disks") or []:
        disks.append({
            "id": d.get("id"),
            "name": d.get("name") or d.get("id"),
            "model": (d.get("model") or "").strip(),
            "serial": d.get("serial"),
            "size": _int(d.get("size_total")),
            "temp_c": _int(d.get("temp")),
            "status": d.get("status"),
            "smart": d.get("smart_status"),
        })
    disks.sort(key=lambda d: str(d["id"]))
    return {"volumes": volumes, "disks": disks}
