"""Proxmox VE connector.

Auth: API token — create one in Datacenter → Permissions → API Tokens
(e.g. user root@pam, token id "claudeos"). Settings fields:
  host          e.g. 192.168.1.10:8006
  token_id      e.g. root@pam!claudeos
  token_secret  the UUID secret shown once at creation
"""

import re

from .. import httpclient

VM_ACTIONS = {"start", "stop", "shutdown", "reboot"}
TOKEN_ID_RE = re.compile(r"^[^@!\s]+@[^@!\s]+![^@!\s]+$")  # user@realm!tokenname


def _base(settings: dict) -> str:
    host = settings["host"].strip().rstrip("/")
    if not host.startswith("http"):
        host = "https://" + host
    if ":" not in host.split("//", 1)[1]:
        host += ":8006"
    return host


def _call(settings: dict, method: str, path: str, json_body: dict | None = None):
    token_id = (settings.get("token_id") or "").strip()
    secret = (settings.get("token_secret") or "").strip()
    if not TOKEN_ID_RE.match(token_id):
        raise ValueError(
            f"Proxmox token ID '{token_id}' is malformed — it must be the full "
            "user@realm!tokenname form (e.g. root@pam!claudeos), not just the token name")
    if not secret:
        raise ValueError("Proxmox token secret is missing — re-enter it on the Setup page")
    headers = {"Authorization": f"PVEAPIToken={token_id}={secret}"}
    try:
        return httpclient.request(
            method,
            _base(settings) + "/api2/json" + path,
            headers=headers,
            json_body=json_body,
            verify_tls=settings.get("verify_tls", False),
        )
    except httpclient.HttpError as e:
        if e.status == 401:
            raise ConnectionError(
                "Proxmox rejected the API token (401). Check that the token ID is the full "
                "user@realm!tokenname shown in Datacenter → Permissions → API Tokens, that the "
                "secret was pasted exactly (it is only shown once — regenerate if unsure), and "
                "that the token has no expiry set") from e
        if e.status == 403:
            raise ConnectionError(
                "Proxmox refused access (403) — the token authenticated but lacks permissions. "
                "Either untick 'Privilege Separation' on the token, or grant it a role "
                "(e.g. PVEAuditor on /, PVEVMAdmin on /vms) under Datacenter → Permissions") from e
        raise


PERMS_HINT = ("token authenticated but stats are hidden — grant it PVEAuditor on / "
              "(and PVEVMAdmin on /vms for actions) under Datacenter → Permissions → "
              "Add → API Token Permission")


def stats_hidden(nodes_data: list) -> bool:
    """True when every node lacks stat fields — the signature of a
    privilege-separated token with no Sys.Audit permission."""
    return bool(nodes_data) and all(n.get("maxcpu") is None and n.get("uptime") is None
                                    for n in nodes_data)


def test(settings: dict) -> dict:
    data = _call(settings, "GET", "/nodes").get("data", [])
    names = ", ".join(n.get("node", "?") for n in data)
    detail = f"connected — nodes: {names or 'none visible (check token permissions)'}"
    if stats_hidden(data):
        detail += f" — ⚠ {PERMS_HINT}"
    return {"ok": True, "detail": detail}


def nodes(settings: dict) -> list:
    data = _call(settings, "GET", "/nodes").get("data", [])
    out = []
    for n in data:
        out.append({
            "node": n.get("node"),
            "status": n.get("status"),
            "cpu": n.get("cpu"),                      # fraction 0..1
            "maxcpu": n.get("maxcpu"),
            "mem": n.get("mem"),
            "maxmem": n.get("maxmem"),
            "disk": n.get("disk"),
            "maxdisk": n.get("maxdisk"),
            "uptime": n.get("uptime"),
        })
    return out


def guests(settings: dict) -> list:
    data = _call(settings, "GET", "/cluster/resources?type=vm").get("data", [])
    out = []
    for g in data:
        out.append({
            "vmid": g.get("vmid"),
            "name": g.get("name"),
            "type": g.get("type"),                    # qemu | lxc
            "node": g.get("node"),
            "status": g.get("status"),                # running | stopped
            "cpu": g.get("cpu"),
            "maxcpu": g.get("maxcpu"),
            "mem": g.get("mem"),
            "maxmem": g.get("maxmem"),
            "uptime": g.get("uptime"),
        })
    out.sort(key=lambda g: (g["type"] or "", g["vmid"] or 0))
    return out


def summary(settings: dict) -> dict:
    ns = nodes(settings)
    gs = guests(settings)
    total_mem = sum(n["maxmem"] or 0 for n in ns)
    used_mem = sum(n["mem"] or 0 for n in ns)
    cpus = [n["cpu"] for n in ns if n["cpu"] is not None]
    hint = PERMS_HINT if all(n["maxcpu"] is None and n["uptime"] is None for n in ns) and ns else None
    return {
        "perms_hint": hint,
        "nodes_total": len(ns),
        "nodes_online": sum(1 for n in ns if n["status"] == "online"),
        "guests_total": len(gs),
        "guests_running": sum(1 for g in gs if g["status"] == "running"),
        "cpu_avg": sum(cpus) / len(cpus) if cpus else None,
        "mem_used": used_mem,
        "mem_total": total_mem,
        "nodes": ns,
    }


def storage(settings: dict) -> list:
    data = _call(settings, "GET", "/cluster/resources?type=storage").get("data", [])
    out = []
    for s in data:
        out.append({
            "storage": s.get("storage"),
            "node": s.get("node"),
            "plugintype": s.get("plugintype"),
            "content": s.get("content"),
            "status": s.get("status"),
            "shared": bool(s.get("shared")),
            "used": s.get("disk"),
            "total": s.get("maxdisk"),
            "pct": round(100 * s["disk"] / s["maxdisk"], 1) if s.get("maxdisk") else None,
        })
    out.sort(key=lambda s: -(s["total"] or 0))
    return out


def perf(settings: dict, timeframe: str = "hour") -> dict:
    """Per-node RRD performance series: CPU %, IO delay %, load, net."""
    if timeframe not in ("hour", "day", "week"):
        raise ValueError("timeframe must be hour, day or week")
    out = {}
    for n in nodes(settings):
        name = n["node"]
        data = _call(settings, "GET",
                     f"/nodes/{name}/rrddata?timeframe={timeframe}&cf=AVERAGE").get("data", [])
        series = {"cpu_pct": [], "iowait_pct": [], "load": [], "net_in_bps": [], "net_out_bps": []}
        for p in data:
            t = p.get("time")
            if t is None:
                continue
            if p.get("cpu") is not None:
                series["cpu_pct"].append([t, round(p["cpu"] * 100, 2)])
            if p.get("iowait") is not None:
                series["iowait_pct"].append([t, round(p["iowait"] * 100, 2)])
            if p.get("loadavg") is not None:
                series["load"].append([t, round(p["loadavg"], 2)])
            if p.get("netin") is not None:
                series["net_in_bps"].append([t, p["netin"]])
            if p.get("netout") is not None:
                series["net_out_bps"].append([t, p["netout"]])
        out[name] = series
    return out


def guest_action(settings: dict, node: str, gtype: str, vmid: str, action: str) -> dict:
    if action not in VM_ACTIONS:
        raise ValueError(f"unsupported action: {action}")
    if gtype not in ("qemu", "lxc"):
        raise ValueError(f"unsupported guest type: {gtype}")
    data = _call(settings, "POST", f"/nodes/{node}/{gtype}/{vmid}/status/{action}")
    return {"ok": True, "detail": f"{action} task {data.get('data', '')} started for {gtype}/{vmid}"}


def disk_list(settings: dict, node: str) -> list:
    """Physical disks on a node (health/wearout come from PVE's smartctl)."""
    return _call(settings, "GET", f"/nodes/{node}/disks/list").get("data", [])


def disk_smart(settings: dict, node: str, devpath: str) -> dict:
    """SMART detail for one disk: structured `attributes` for ATA drives,
    raw smartctl `text` for NVMe (verified on PVE 8, 2026-07-16)."""
    import urllib.parse
    q = urllib.parse.quote(devpath, safe="")
    return _call(settings, "GET", f"/nodes/{node}/disks/smart?disk={q}").get("data", {})


def guest_detail(settings: dict, node: str, gtype: str, vmid: str) -> dict:
    """Extended live stats for one guest (status/current), including the
    balloon inside-guest memory view and PSI pressure counters (verified
    on this PVE 2026-07-17: pressure* are percent-of-time-stalled)."""
    st = _call(settings, "GET", f"/nodes/{node}/{gtype}/{vmid}/status/current").get("data", {})
    out = {k: st.get(k) for k in (
        "name", "status", "qmpstatus", "uptime", "cpus", "cpu",
        "mem", "maxmem", "memhost", "freemem", "disk", "maxdisk",
        "diskread", "diskwrite", "netin", "netout", "swap", "maxswap", "agent")}
    out["pressure"] = {k[len("pressure"):]: st.get(k)
                       for k in st if k.startswith("pressure")}
    bi = st.get("ballooninfo") or {}
    if bi:
        out["balloon"] = {"total": bi.get("total_mem"), "free": bi.get("free_mem"),
                          "actual": bi.get("actual"), "max": bi.get("max_mem"),
                          "swapped_out": bi.get("mem_swapped_out")}
    return out


def guest_rrd(settings: dict, node: str, gtype: str, vmid: str,
              timeframe: str = "hour") -> dict:
    """Per-guest RRD series for the detail graphs."""
    if timeframe not in ("hour", "day", "week"):
        raise ValueError("timeframe must be hour, day or week")
    data = _call(settings, "GET",
                 f"/nodes/{node}/{gtype}/{vmid}/rrddata?timeframe={timeframe}&cf=AVERAGE"
                 ).get("data", [])
    series = {"cpu_pct": [], "mem_pct": [], "mem_pressure_pct": [], "io_pressure_pct": [],
              "disk_read_bps": [], "disk_write_bps": [], "net_in_bps": [], "net_out_bps": []}
    for p in data:
        t = p.get("time")
        if t is None:
            continue
        if p.get("cpu") is not None:
            series["cpu_pct"].append([t, round(p["cpu"] * 100, 2)])
        if p.get("mem") is not None and p.get("maxmem"):
            series["mem_pct"].append([t, round(100 * p["mem"] / p["maxmem"], 1)])
        if p.get("pressurememorysome") is not None:
            series["mem_pressure_pct"].append([t, round(p["pressurememorysome"], 2)])
        if p.get("pressureiosome") is not None:
            series["io_pressure_pct"].append([t, round(p["pressureiosome"], 2)])
        if p.get("diskread") is not None:
            series["disk_read_bps"].append([t, p["diskread"]])
        if p.get("diskwrite") is not None:
            series["disk_write_bps"].append([t, p["diskwrite"]])
        if p.get("netin") is not None:
            series["net_in_bps"].append([t, p["netin"]])
        if p.get("netout") is not None:
            series["net_out_bps"].append([t, p["netout"]])
    return series
