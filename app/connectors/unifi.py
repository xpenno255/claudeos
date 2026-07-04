"""UniFi Network connector (UDM/UDM-SE local API).

Auth: POST /api/auth/login with a local-account username/password. The
controller replies with a TOKEN cookie and a CSRF token header; both are
cached per-process and refreshed on 401.
Recommended: create a dedicated read/limited local admin on the UDM for this.
"""

import threading
import time

from .. import httpclient

_session_lock = threading.Lock()
_session: dict = {}  # {cookie, csrf, host, ts}

SESSION_TTL = 45 * 60


def _base(settings: dict) -> str:
    host = settings["host"].strip().rstrip("/")
    if not host.startswith("http"):
        host = "https://" + host
    return host


def _login(settings: dict) -> dict:
    base = _base(settings)
    body, headers = httpclient.request(
        "POST",
        f"{base}/api/auth/login",
        json_body={"username": settings["username"], "password": settings["password"]},
        verify_tls=settings.get("verify_tls", False),
        return_headers=True,
    )
    cookies = headers.get_all("Set-Cookie") or []
    cookie = "; ".join(c.split(";", 1)[0] for c in cookies)
    csrf = headers.get("X-Csrf-Token") or headers.get("X-Updated-Csrf-Token") or ""
    if not cookie:
        raise ConnectionError("UniFi login returned no session cookie")
    return {"cookie": cookie, "csrf": csrf, "host": base, "ts": time.time()}


def _session_for(settings: dict, force: bool = False) -> dict:
    base = _base(settings)
    with _session_lock:
        s = dict(_session)
    if force or s.get("host") != base or time.time() - s.get("ts", 0) > SESSION_TTL:
        s = _login(settings)
        with _session_lock:
            _session.clear()
            _session.update(s)
    return s


def _call(settings: dict, method: str, path: str, json_body: dict | None = None):
    for attempt in (0, 1):
        s = _session_for(settings, force=attempt == 1)
        headers = {"Cookie": s["cookie"]}
        if s["csrf"]:
            headers["X-Csrf-Token"] = s["csrf"]
        try:
            return httpclient.request(
                method,
                s["host"] + path,
                headers=headers,
                json_body=json_body,
                verify_tls=settings.get("verify_tls", False),
            )
        except httpclient.HttpError as e:
            if e.status in (401, 403) and attempt == 0:
                continue
            raise
    raise ConnectionError("UniFi session could not be established")


def test(settings: dict) -> dict:
    data = _call(settings, "GET", "/proxy/network/api/s/default/stat/health")
    subsystems = [d.get("subsystem") for d in data.get("data", [])]
    return {"ok": True, "detail": f"health reported for: {', '.join(filter(None, subsystems))}"}


def summary(settings: dict) -> dict:
    health = _call(settings, "GET", "/proxy/network/api/s/default/stat/health").get("data", [])
    devices = _call(settings, "GET", "/proxy/network/api/s/default/stat/device").get("data", [])
    wan = next((h for h in health if h.get("subsystem") == "wan"), {})
    wlan = next((h for h in health if h.get("subsystem") == "wlan"), {})
    lan = next((h for h in health if h.get("subsystem") == "lan"), {})
    clients = (wlan.get("num_user") or 0) + (lan.get("num_user") or 0)
    return {
        "wan_status": wan.get("status", "unknown"),
        "wan_ip": wan.get("wan_ip"),
        "isp_latency_ms": wan.get("latency"),
        "clients": clients,
        "wifi_clients": wlan.get("num_user") or 0,
        "wired_clients": lan.get("num_user") or 0,
        "devices_total": len(devices),
        "devices_online": sum(1 for d in devices if d.get("state") == 1),
        "tx_bytes_r": wan.get("tx_bytes-r"),
        "rx_bytes_r": wan.get("rx_bytes-r"),
    }


def devices(settings: dict) -> list:
    data = _call(settings, "GET", "/proxy/network/api/s/default/stat/device").get("data", [])
    out = []
    for d in data:
        out.append({
            "name": d.get("name") or d.get("model"),
            "model": d.get("model"),
            "type": d.get("type"),
            "mac": d.get("mac"),
            "ip": d.get("ip"),
            "state": "online" if d.get("state") == 1 else "offline",
            "uptime": d.get("uptime"),
            "cpu": (d.get("system-stats") or {}).get("cpu"),
            "mem": (d.get("system-stats") or {}).get("mem"),
            "clients": d.get("num_sta"),
            "version": d.get("version"),
            "upgradable": bool(d.get("upgradable")),
            "upgrade_to": d.get("upgrade_to_firmware"),
        })
    return out


def _num(x):
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


def insights(settings: dict) -> dict:
    """Gateway (UDM) health, ports with errors/drops, pending firmware
    updates — all from one stat/device call."""
    data = _call(settings, "GET", "/proxy/network/api/s/default/stat/device").get("data", [])
    gateway, port_issues, updates = None, [], []
    for d in data:
        name = d.get("name") or d.get("model")
        if gateway is None and d.get("type") in ("udm", "ugw", "uxg"):
            ss = d.get("system-stats") or {}
            gateway = {
                "name": name,
                "model": d.get("model"),
                "version": d.get("version"),
                "uptime": d.get("uptime"),
                "cpu_pct": _num(ss.get("cpu")),
                "mem_pct": _num(ss.get("mem")),
                "temps": [{"name": t.get("name"), "value": t.get("value")}
                          for t in (d.get("temperatures") or []) if t.get("value") is not None],
            }
        if d.get("upgradable"):
            updates.append({"name": name, "model": d.get("model"),
                            "version": d.get("version"),
                            "upgrade_to": d.get("upgrade_to_firmware")})
        for p in d.get("port_table") or []:
            errors = (p.get("rx_errors") or 0) + (p.get("tx_errors") or 0)
            drops = (p.get("rx_dropped") or 0) + (p.get("tx_dropped") or 0)
            if errors + drops > 0:
                port_issues.append({
                    "device": name,
                    "port": p.get("name") or f"Port {p.get('port_idx')}",
                    "up": bool(p.get("up")),
                    "speed": p.get("speed"),
                    "rx_errors": p.get("rx_errors") or 0,
                    "tx_errors": p.get("tx_errors") or 0,
                    "drops": drops,
                })
    port_issues.sort(key=lambda x: (-(x["rx_errors"] + x["tx_errors"]), -x["drops"]))
    return {"gateway": gateway, "port_issues": port_issues[:20], "updates": updates}


def clients(settings: dict) -> list:
    data = _call(settings, "GET", "/proxy/network/api/s/default/stat/sta").get("data", [])
    out = []
    for c in data:
        out.append({
            "name": c.get("name") or c.get("hostname") or c.get("oui") or c.get("mac"),
            "ip": c.get("ip"),
            "mac": c.get("mac"),
            "wired": c.get("is_wired", False),
            "network": c.get("network"),
            "essid": c.get("essid"),
            "signal": c.get("signal"),
            "uptime": c.get("uptime"),
        })
    out.sort(key=lambda c: (c["wired"], (c["name"] or "").lower()))
    return out


def restart_device(settings: dict, mac: str) -> dict:
    _call(settings, "POST", "/proxy/network/api/s/default/cmd/devmgr",
          json_body={"cmd": "restart", "mac": mac})
    return {"ok": True, "detail": f"restart sent to {mac}"}


def upgrade_device(settings: dict, mac: str) -> dict:
    """Tell a device to download and install its pending firmware update.
    The device reboots as part of the upgrade (a few minutes offline)."""
    _call(settings, "POST", "/proxy/network/api/s/default/cmd/devmgr",
          json_body={"cmd": "upgrade", "mac": mac})
    return {"ok": True, "detail": f"firmware upgrade started on {mac} — it will reboot and "
                                  "show offline for a few minutes"}
