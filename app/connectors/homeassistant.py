"""Home Assistant connector.

Auth: long-lived access token (HA → your profile → Security → Long-lived
access tokens). Settings: host (e.g. http://192.168.1.20:8123), token.
"""

import json

from .. import httpclient

# Domains surfaced in the Operations page with quick toggles.
TOGGLE_DOMAINS = {"light", "switch", "fan", "input_boolean", "automation", "script"}


def _base(settings: dict) -> str:
    host = settings["host"].strip().rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    return host


def _call(settings: dict, method: str, path: str, json_body: dict | None = None):
    headers = {"Authorization": f"Bearer {settings['token']}"}
    return httpclient.request(
        method,
        _base(settings) + path,
        headers=headers,
        json_body=json_body,
        verify_tls=settings.get("verify_tls", False),
    )


def test(settings: dict) -> dict:
    cfg = _call(settings, "GET", "/api/config")
    return {"ok": True, "detail": f"{cfg.get('location_name', 'HA')} — version {cfg.get('version', '?')}"}


def states(settings: dict) -> list:
    data = _call(settings, "GET", "/api/states") or []
    out = []
    for s in data:
        eid = s.get("entity_id", "")
        domain = eid.split(".", 1)[0]
        attrs = s.get("attributes") or {}
        out.append({
            "entity_id": eid,
            "domain": domain,
            "name": attrs.get("friendly_name", eid),
            "state": s.get("state"),
            "unit": attrs.get("unit_of_measurement"),
            "device_class": attrs.get("device_class"),
            "last_changed": s.get("last_changed"),
            "toggleable": domain in TOGGLE_DOMAINS,
        })
    return out


def summary(settings: dict) -> dict:
    cfg = _call(settings, "GET", "/api/config")
    ss = states(settings)
    domains = {}
    for s in ss:
        domains[s["domain"]] = domains.get(s["domain"], 0) + 1
    unavailable = sum(1 for s in ss if s["state"] in ("unavailable", "unknown"))
    lights_on = sum(1 for s in ss if s["domain"] == "light" and s["state"] == "on")
    switches_on = sum(1 for s in ss if s["domain"] == "switch" and s["state"] == "on")
    return {
        "location": cfg.get("location_name"),
        "version": cfg.get("version"),
        "entities_total": len(ss),
        "unavailable": unavailable,
        "lights_on": lights_on,
        "switches_on": switches_on,
        "automations": domains.get("automation", 0),
        "domains": domains,
    }


def system_info(settings: dict) -> dict:
    """HAOS internals via the supervisor proxy: core CPU/RAM, host disk,
    and add-on states. Needs an admin user's long-lived token."""
    out = {"supervised": True, "core": None, "host": None, "addons": []}
    try:
        core = _call(settings, "GET", "/api/hassio/core/stats").get("data", {})
        out["core"] = {
            "cpu_pct": core.get("cpu_percent"),
            "mem_pct": core.get("memory_percent"),
            "mem_used": core.get("memory_usage"),
            "mem_total": core.get("memory_limit"),
        }
    except (httpclient.HttpError, ConnectionError) as e:
        status = getattr(e, "status", None)
        if status == 404:
            out["supervised"] = False  # container/core install, no supervisor
            return out
        if status in (401, 403):
            out["error"] = ("supervisor endpoints refused the token — create the long-lived "
                            "token from an *administrator* user account")
            return out
        raise
    try:
        host = _call(settings, "GET", "/api/hassio/host/info").get("data", {})
        out["host"] = {
            "disk_used": host.get("disk_used"),
            "disk_total": host.get("disk_total"),
            "disk_free": host.get("disk_free"),
            "operating_system": host.get("operating_system"),
        }
    except (httpclient.HttpError, ConnectionError):
        pass
    try:
        addons = _call(settings, "GET", "/api/hassio/addons").get("data", {}).get("addons", [])
        out["addons"] = [{
            "name": a.get("name"),
            "slug": a.get("slug"),
            "state": a.get("state"),                # started | stopped | error | unknown
            "version": a.get("version"),
            "update_available": a.get("update_available", False),
        } for a in addons]
        out["addons"].sort(key=lambda a: (a["state"] != "error", a["state"] != "stopped",
                                          (a["name"] or "").lower()))
    except (httpclient.HttpError, ConnectionError):
        pass
    return out


def error_log(settings: dict) -> str:
    """Recent errors/warnings as text. Prefers the REST /api/error_log
    (full log file); newer HA installs 404 that endpoint, so fall back to
    the system_log/list websocket command — deduplicated entries with
    counts, which suit AI analysis even better."""
    try:
        log = _call(settings, "GET", "/api/error_log")
        return log if isinstance(log, str) else json.dumps(log)
    except httpclient.HttpError as e:
        if e.status != 404:
            raise
    from .. import hws
    entries = hws.command(settings, {"type": "system_log/list"}) or []
    lines = []
    for en in entries:
        msgs = en.get("message")
        msg = " | ".join(str(m) for m in msgs) if isinstance(msgs, list) else str(msgs)
        ts = en.get("timestamp")
        when = ""
        if ts:
            try:
                from datetime import datetime, timezone
                when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(" @ %Y-%m-%d %H:%M UTC")
            except (OSError, OverflowError, ValueError):
                pass
        lines.append(f"[{en.get('level', '?')}] {en.get('name', '?')} "
                     f"(x{en.get('count', 1)}{when}): {msg}")
    return "\n".join(lines)


def zha_devices(settings: dict) -> list:
    """ZHA device inventory via the websocket API."""
    from .. import hws
    try:
        devices = hws.command(settings, {"type": "zha/devices"}) or []
    except hws.HAWebSocketError as e:
        if "Unknown command" in str(e) or "unknown_command" in str(e):
            raise LookupError("ZHA integration not detected on this Home Assistant") from e
        raise ConnectionError(str(e)) from e
    out = []
    for d in devices:
        out.append({
            "name": d.get("user_given_name") or d.get("name"),
            "model": d.get("model"),
            "manufacturer": d.get("manufacturer"),
            "ieee": d.get("ieee"),
            "nwk": d.get("nwk"),
            "lqi": d.get("lqi"),
            "rssi": d.get("rssi"),
            "last_seen": d.get("last_seen"),
            "available": d.get("available"),
            "power_source": d.get("power_source"),
            "device_type": d.get("device_type"),    # Coordinator | Router | EndDevice
            "quirk": d.get("quirk_class"),
        })
    out.sort(key=lambda x: (x["available"] is not False, x["lqi"] if x["lqi"] is not None else 999))
    return out


def call_service(settings: dict, domain: str, service: str, entity_id: str | None = None,
                 data: dict | None = None) -> dict:
    body = dict(data or {})
    if entity_id:
        body["entity_id"] = entity_id
    _call(settings, "POST", f"/api/services/{domain}/{service}", json_body=body)
    target = entity_id or "(no target)"
    return {"ok": True, "detail": f"{domain}.{service} called on {target}"}
