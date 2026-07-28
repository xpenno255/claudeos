"""UniFi Network connector (UDM/UDM-SE local API).

Auth: POST /api/auth/login with a local-account username/password. The
controller replies with a TOKEN cookie and a CSRF token header; both are
cached per-process and refreshed on 401.
Recommended: create a dedicated read/limited local admin on the UDM for this.
"""

import re
import threading
import time

from .. import httpclient
from ._report import soft

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


def metrics(summary: dict) -> dict:
    """Sparkline series. `tx_bytes-r`/`rx_bytes-r` are UniFi's instantaneous WAN
    rates, so they are recorded as-is rather than differenced."""
    return {
        "clients": summary.get("clients"),
        "latency_ms": summary.get("isp_latency_ms"),
        "wan_rx_bps": summary.get("rx_bytes_r"),
        "wan_tx_bps": summary.get("tx_bytes_r"),
    }


def report_slice(settings: dict) -> dict:
    """What the weekly digest wants from UniFi.

    Security events are capped at the ten most recent and quoted as messages
    only: the digest is prose about the week, and the full event objects are
    both large and already on the Ops page for anyone who wants them.

    Those messages are handed over with **resolved identities**, not as UniFi
    worded them. UniFi names a source by hostname when it has one and by bare IP
    when it does not, sometimes for the same device in consecutive events, and
    the snapshot carried no map between the two — so one NAS arrived as two
    offending hosts and the report attributed it to a third machine entirely
    (#59). Resolving here rather than in the prompt is the point: the join is the
    step that failed.
    """
    ins = soft(insights, settings)
    # Failing soft, not hard: an unreachable client list must not cost the report
    # its security events. An empty map degrades to "(ip unknown)", which is
    # true, and `event_identities` below says the resolution never ran.
    idents = soft(identities, settings)
    failed = "error" in idents  # `soft`'s shape; no MAC or IP is ever "error"
    resolved = {} if failed else idents
    sec = soft(events, settings, ["SECURITY"], 0, 10, resolved)
    anoms = soft(anomalies, settings)
    return {
        "summary": soft(summary, settings),
        "gateway": ins.get("gateway"),
        "port_issues": (ins.get("port_issues") or [])[:5],
        "firmware_updates": ins.get("updates"),
        "security_events_total": sec.get("total"),
        "recent_security_events": [e.get("message") for e in (sec.get("events") or [])[:8]],
        # Whether the identities above can be trusted, stated rather than
        # inferred — the same reason `reports.collect` states the alerting state
        # instead of leaving it to be deduced (#53).
        "event_identities": (idents if failed
                             else f"resolved against {len(resolved)} known addresses"),
        "client_anomalies": anoms[:10] if isinstance(anoms, list) else anoms,
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


def identities(settings: dict) -> dict:
    """MAC **and** IP → one canonical `name (ip)` per associated client.

    Two keys pointing at the same string is the whole point. UniFi templates an
    event's source either as `{SRC_CLIENT}`, whose `id` is the device's MAC, or
    as `{SRC_IP}`, whose `id` is a bare address — and nothing in the event says
    the two forms are the same machine. Keying on both means either phrasing
    resolves to the same identity.

    The join is on MAC, not on name: `{SRC_CLIENT}` carries a hostname with no
    address at all, so a name is the one thing that cannot be resolved against
    the event itself.
    """
    out = {}
    for c in clients(settings):
        name, ip, mac = c.get("name"), c.get("ip"), c.get("mac")
        label = f"{name} ({ip})" if name and ip else (name or ip)
        if not label:
            continue
        for key in (mac, ip):
            if key:
                out[key] = label
    return out


def _identity(p: dict, idents: dict) -> str | None:
    """One identity for one event parameter, however UniFi phrased it.

    The `(ip unknown)` case is deliberate and is the fix for #59. A named client
    that no longer appears in the associated-client list has no address here, and
    saying so out loud is the only rendering that cannot be mistaken for one — a
    bare `XpennoNas` beside an unrelated event mentioning `192.168.1.102` is
    exactly what got welded into a wrong `serious` finding about the wrong host.
    """
    ident = idents.get(p.get("id"))
    if ident:
        return ident
    name = p.get("name") or p.get("hostname")
    # Some parameters (the gateway's own DEVICE block) carry an address inline,
    # so they need no lookup to be canonical.
    if name and p.get("ip"):
        return f"{name} ({p['ip']})"
    # An external address arrives with id == name == the IP. It has no name to
    # be missing, so it must stay bare rather than gain "(ip unknown)".
    if name and name != p.get("id"):
        return f"{name} (ip unknown)"
    return name or p.get("id")


def _render_msg(raw: str, params: dict, idents: dict | None = None) -> str:
    """Fill a v2 system-log template ("{SRC_IP} blocked…") from its
    parameters map, preferring human names over ids.

    With `idents`, every device is rendered in one canonical form instead of
    whichever of its two names UniFi happened to use for that event. Without it
    the original behaviour is unchanged — the Ops page shows the event as the
    UniFi UI words it, and the raw row travels alongside for single-event triage.
    """
    def sub(m):
        p = (params or {}).get(m.group(1))
        if isinstance(p, dict):
            if idents is not None:
                return str(_identity(p, idents) or m.group(0))
            return str(p.get("name") or p.get("hostname") or p.get("id") or m.group(0))
        return str(p) if p is not None else m.group(0)
    return re.sub(r"\{([A-Z0-9_]+)\}", sub, raw or "")


def events(settings: dict, categories: list | None = None,
           page: int = 0, page_size: int = 50, idents: dict | None = None) -> dict:
    """Site events from the v2 system-log (the feed the UniFi UI uses).

    Verified live on UDM-SE fw 5.1.25 (2026-07-16): the v1 endpoints
    stat/event, list/alarm and stat/ips/event are gone; IDS/IPS blocks
    appear here as category SECURITY / subcategory
    SECURITY_INTRUSION_PREVENTION. The categories filter is server-side.

    `idents` is `identities()`, and passing it renders one identity per device
    (see `_render_msg`). Off by default so this stays the feed the UniFi UI
    shows; the weekly report opts in because it is the caller that has to
    *count* offending hosts, and counting one machine twice is what #59 was.
    """
    body = {"pageNumber": int(page), "pageSize": int(page_size)}
    if categories:
        body["categories"] = categories
    r = _call(settings, "POST",
              "/proxy/network/v2/api/site/default/system-log/all", json_body=body)
    out = []
    for x in r.get("data", []):
        out.append({
            "id": x.get("id"),
            "ts": (x.get("timestamp") or 0) / 1000,
            "category": x.get("category"),
            "subcategory": x.get("subcategory"),
            "event": x.get("event"),
            "severity": x.get("severity"),
            "status": x.get("status"),
            "title": _render_msg(x.get("title_raw"), x.get("parameters"), idents),
            "message": _render_msg(x.get("message_raw"), x.get("parameters"), idents),
            "raw": x,  # full row, fed to the AI triage
        })
    return {"events": out, "total": r.get("total_element_count"),
            "pages": r.get("total_page_count"), "page": r.get("page_number")}


def anomalies(settings: dict) -> list:
    """Client anomalies (stat/anomalies — still v1, verified working).
    Rows come as {anomaly, mac, timestamps}; enrich with client names."""
    rows = _call(settings, "GET",
                 "/proxy/network/api/s/default/stat/anomalies").get("data", [])
    names = {}
    try:
        names = {c["mac"]: c["name"] for c in clients(settings) if c.get("mac")}
    except Exception:  # noqa: BLE001 — names are a nicety, not a requirement
        pass
    out = []
    for a in rows:
        stamps = a.get("timestamps") or []
        out.append({
            "anomaly": a.get("anomaly"),
            "mac": a.get("mac"),
            "client": names.get(a.get("mac")) or a.get("mac"),
            "count": len(stamps),
            "last_ts": max(stamps) / 1000 if stamps else None,
        })
    out.sort(key=lambda x: -(x["last_ts"] or 0))
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
