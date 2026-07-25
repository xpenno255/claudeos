"""Tool catalog and dispatch for the agentic ops chat.

Implements §5–§7 and §10 of docs/spec-agentic-ops-chat.md:

  * curated tools only — no raw-API passthrough, no escape hatch
  * two tiers: precomputed lab-wide evidence first, per-connector queries second
  * every result wrapped in a StructuredToolResult-style envelope where
    `no_data` is distinct from `error` (an empty result must never read as
    "no problem")
  * hard output caps with explicit omission notices — never silent truncation
  * writes return `approval_required` INSTEAD of executing; the gate lives
    here in the dispatch layer, in code, never in the prompt

Adding a tool: append to READ_TOOLS / WRITE_TOOLS. Ordering is significant —
the schema list is part of the cached prompt prefix, so keep it stable.
"""

import time

from . import monitors, oplog, poller, registry, smart, store
from .connectors import docker, homeassistant, proxmox, synology, unifi

# ------------------------------------------------------------------ caps

MAX_LOG_TAIL = 100
MAX_ENTITY_ROWS = 200
MAX_EVENT_PAGE = 50
MAX_HISTORY_POINTS = 120
MAX_OPLOG_ENTRIES = 120
MAX_LIST_ROWS = 120          # generic ceiling for device/guest/container lists

# HA service domains the agent may touch. DEFAULT-DENY: anything absent is
# refused here, so a lock or garage cover added to HA later stays unavailable
# until someone deliberately edits this list.
HA_ALLOWED_DOMAINS = {
    "light", "switch", "fan", "input_boolean", "automation",
    "script", "scene", "media_player", "climate",
}

# Actions that carry a warning line in the confirmation card (spec §14).
_WARN = {
    ("proxmox_guest_action", "stop"):
        "Hard power-cut — the guest gets no chance to shut down cleanly and may corrupt its filesystem.",
    ("proxmox_guest_action", "reboot"):
        "The guest reboots immediately; anything running on it is interrupted.",
    ("proxmox_guest_action", "shutdown"):
        "The guest shuts down and stays off until started again.",
    ("docker_container_action", "stop"):
        "The container stays stopped until started again — anything depending on it will fail.",
    ("unifi_restart_device", None):
        "Every client connected to this AP/switch drops while it reboots (a few minutes).",
}


class ToolError(Exception):
    """A tool was called wrongly (bad name, bad params, refused domain)."""


# --------------------------------------------------------------- envelope

def envelope(status, *, data=None, error=None, invocation="", params=None,
             elapsed_ms=0, omitted=None, pending_id=None, warning=None):
    out = {
        "status": status,          # success | error | no_data | approval_required
        "data": data,
        "error": error,
        "invocation": invocation,
        "params": params or {},
        "elapsed_ms": elapsed_ms,
        "omitted": omitted,
    }
    if pending_id:
        out["pending_id"] = pending_id
    if warning:
        out["warning"] = warning
    return out


def _invocation(name: str, params: dict) -> str:
    if not params:
        return f"{name}()"
    bits = ", ".join(f'{k}={v!r}' for k, v in sorted(params.items()))
    return f"{name}({bits})"


def _cap(rows: list, limit: int, unit: str = "items"):
    """Truncate to `limit`, returning (rows, omission notice | None)."""
    if len(rows) <= limit:
        return rows, None
    return rows[:limit], (f"{len(rows) - limit} of {len(rows)} {unit} omitted — "
                          f"re-query with a narrower filter")


def _settings(system_id: str) -> dict:
    s = store.get_system(system_id, reveal_secrets=True)
    if not s or not s.get("host"):
        raise ToolError(f"{store.SYSTEM_LABELS.get(system_id, system_id)} is not "
                        f"configured in ClaudeOS — nothing to query")
    return s


# ------------------------------------------------- tier 1: lab-wide evidence

def _get_lab_overview():
    snap = poller.snapshot()
    if not snap:
        return None, None
    out = {}
    for sid, entry in snap.items():
        row = {"reachable": entry.get("ok"), "checked_ts": entry.get("ts")}
        if entry.get("ok"):
            row["summary"] = entry.get("data")
        else:
            row["error"] = entry.get("error")
        out[sid] = row
    return out, None


def _get_metric_history(system: str, metric: str):
    hist = poller.history().get(system) or {}
    if metric not in hist:
        available = sorted(hist.keys())
        raise ToolError(f"no metric {metric!r} for {system!r}; available: "
                        f"{', '.join(available) if available else 'none yet'}")
    points = hist[metric]
    if not points:
        return None, None
    kept, omitted = _cap(points[-MAX_HISTORY_POINTS:], MAX_HISTORY_POINTS, "points")
    return {"system": system, "metric": metric,
            "points": [{"ts": ts, "value": v} for ts, v in kept]}, omitted


def _get_ops_log(limit: int = MAX_OPLOG_ENTRIES, level: str | None = None):
    entries = oplog.recent(MAX_OPLOG_ENTRIES)
    if level:
        entries = [e for e in entries if e.get("level") == level]
    if not entries:
        return None, None
    kept, omitted = _cap(entries, min(limit, MAX_OPLOG_ENTRIES), "entries")
    return kept, omitted


def _get_uptime_monitors():
    mons = monitors.list_monitors()
    if not mons:
        return None, None
    return [{k: m.get(k) for k in
             ("name", "type", "target", "enabled", "ok", "ms", "error",
              "uptime_pct", "avg_ms", "since")} for m in mons], None


# ------------------------------------------------- tier 2: per-connector

def _unifi_devices():
    rows = unifi.devices(_settings("unifi"))
    if not rows:
        return None, None
    return _cap(rows, MAX_LIST_ROWS, "devices")


def _unifi_clients(search: str | None = None):
    rows = unifi.clients(_settings("unifi"))
    if search:
        q = search.lower()
        rows = [r for r in rows
                if q in str(r.get("name", "")).lower() or q in str(r.get("ip", "")).lower()]
    if not rows:
        return None, None
    return _cap(rows, MAX_LIST_ROWS, "clients")


def _unifi_events(category: str = "SECURITY", page: int = 0):
    res = unifi.events(_settings("unifi"), categories=[category], page=page,
                       page_size=MAX_EVENT_PAGE)
    events = res.get("events") or []
    if not events:
        return None, None
    return {"total": res.get("total"), "page": page,
            "events": [{k: e.get(k) for k in ("ts", "key", "event", "message",
                                              "subcategory", "severity")}
                       for e in events]}, None


def _unifi_anomalies():
    rows = unifi.anomalies(_settings("unifi"))
    if not rows:
        return None, None
    return _cap(rows, MAX_LIST_ROWS, "anomalies")


def _unifi_insights():
    return unifi.insights(_settings("unifi")) or None, None


def _proxmox_nodes():
    rows = proxmox.nodes(_settings("proxmox"))
    return (rows or None), None


def _proxmox_guests():
    rows = proxmox.guests(_settings("proxmox"))
    if not rows:
        return None, None
    return _cap(rows, MAX_LIST_ROWS, "guests")


def _proxmox_guest_detail(node: str, type: str, vmid: str):
    return proxmox.guest_detail(_settings("proxmox"), node, type, str(vmid)) or None, None


def _proxmox_storage():
    rows = proxmox.storage(_settings("proxmox"))
    return (rows or None), None


def _proxmox_disk_health():
    res = smart.get()
    disks = res.get("disks") or []
    if not disks:
        return None, res.get("error")
    return {"checked_ts": res.get("ts"), "disks": disks}, None


def _docker_containers(state: str | None = None):
    rows = docker.containers(_settings("docker"))
    if state:
        rows = [r for r in rows if r.get("state") == state]
    if not rows:
        return None, None
    return _cap(rows, MAX_LIST_ROWS, "containers")


def _docker_container_logs(name: str, tail: int = MAX_LOG_TAIL):
    tail = max(1, min(int(tail), MAX_LOG_TAIL))
    s = _settings("docker")
    conts = docker.containers(s)
    match = next((c for c in conts if c.get("name") == name), None)
    if match is None:
        names = ", ".join(sorted(c.get("name", "") for c in conts)[:40])
        raise ToolError(f"no container named {name!r}. Known containers: {names}")
    text = docker.container_logs(s, match["id"], tail=tail)
    if not (text or "").strip():
        return None, None
    lines = text.splitlines()
    return {"container": name, "lines": lines}, (
        f"showing the last {len(lines)} lines only — earlier output omitted; "
        f"re-query with a larger tail (max {MAX_LOG_TAIL})")


def _docker_storage_report():
    return docker.storage_report(_settings("docker")) or None, None


def _docker_gpu_report():
    return docker.gpu_report(_settings("docker")) or None, None


def _docker_image_updates():
    res = registry.get()
    imgs = res.get("images") or []
    if not imgs:
        return None, res.get("error")
    return {"checked_ts": res.get("ts"),
            "updates": [i for i in imgs if i.get("status") == "update"],
            "total_images": len(imgs)}, None


def _ha_summary():
    return homeassistant.summary(_settings("homeassistant")) or None, None


def _ha_entities(domain: str | None = None, search: str | None = None):
    rows = homeassistant.states(_settings("homeassistant"))
    if domain:
        rows = [r for r in rows if r.get("domain") == domain]
    if search:
        q = search.lower()
        rows = [r for r in rows
                if q in str(r.get("name", "")).lower() or q in str(r.get("entity_id", "")).lower()]
    if not rows:
        return None, None
    return _cap(rows, MAX_ENTITY_ROWS, "entities")


def _ha_error_log():
    text = homeassistant.error_log(_settings("homeassistant"))
    lines = (text or "").splitlines()
    if not lines:
        return None, None
    kept, omitted = _cap(lines[-MAX_LOG_TAIL:], MAX_LOG_TAIL, "lines")
    return {"lines": kept}, omitted or (
        f"showing the last {len(kept)} lines of the error log only"
        if len(lines) > len(kept) else None)


def _ha_zha_devices():
    rows = homeassistant.zha_devices(_settings("homeassistant"))
    if not rows:
        return None, None
    return _cap(rows, MAX_ENTITY_ROWS, "zigbee devices")


def _ha_updates():
    rows = homeassistant.updates(_settings("homeassistant"))
    avail = [u for u in rows if u.get("available")]
    if not rows:
        return None, None
    return {"available": avail, "total_update_entities": len(rows)}, None


def _ha_system_info():
    return homeassistant.system_info(_settings("homeassistant")) or None, None


def _synology_status():
    return synology.summary(_settings("synology")) or None, None


def _synology_storage():
    return synology.storage(_settings("synology")) or None, None


# ------------------------------------------------------------ write tools

def _docker_container_action(name: str, action: str):
    if action not in docker.CONTAINER_ACTIONS:
        raise ToolError(f"action must be one of {sorted(docker.CONTAINER_ACTIONS)}")
    s = _settings("docker")
    conts = docker.containers(s)
    match = next((c for c in conts if c.get("name") == name), None)
    if match is None:
        raise ToolError(f"no container named {name!r}")
    res = docker.container_action(s, match["id"], action)
    oplog.add("action", "docker", f"[chat] {action} → container {name}")
    return res, None


def _proxmox_guest_action(node: str, type: str, vmid: str, action: str):
    if action not in proxmox.VM_ACTIONS:
        raise ToolError(f"action must be one of {sorted(proxmox.VM_ACTIONS)}")
    res = proxmox.guest_action(_settings("proxmox"), node, type, str(vmid), action)
    oplog.add("action", "proxmox", f"[chat] {action} → {type}/{vmid} on {node}")
    return res, None


def _ha_call_service(domain: str, service: str, entity_id: str | None = None):
    # Re-validated here as well as at approval time — the dispatch layer never
    # trusts a stored pending record alone.
    if domain not in HA_ALLOWED_DOMAINS:
        raise ToolError(f"domain {domain!r} is not permitted from chat. Allowed: "
                        f"{', '.join(sorted(HA_ALLOWED_DOMAINS))}")
    res = homeassistant.call_service(_settings("homeassistant"), domain, service,
                                     entity_id=entity_id)
    oplog.add("action", "homeassistant",
              f"[chat] {domain}.{service} → {entity_id or '(no target)'}")
    return res, None


def _unifi_restart_device(mac: str):
    res = unifi.restart_device(_settings("unifi"), mac)
    oplog.add("action", "unifi", f"[chat] device restart requested: {mac}")
    return res, None


# --------------------------------------------------------------- catalog

def _t(name, description, props, required, fn, *, writes=False):
    # `strict` is reserved for the write tools. The API caps strict tools at 20
    # per request and this catalog is larger than that, so spending the budget
    # where it matters: strict guarantees the arguments rendered in the
    # confirmation card are schema-valid. A malformed read call just comes back
    # as an error envelope, which the model can recover from.
    return {
        "name": name,
        "description": description,
        **({"strict": True} if writes else {}),
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
        "_fn": fn,
        "_writes": writes,
    }


_STR = {"type": "string"}


READ_TOOLS = [
    # ---- tier 1: precomputed lab-wide evidence (prefer these first)
    _t("get_lab_overview",
       "START HERE for almost any question. Returns the latest 30-second poll of "
       "every linked system (UniFi, Proxmox, Docker, Home Assistant, Synology): "
       "whether it is reachable, its summary metrics, and its last error. Cheap "
       "and broad — use it to orient before reaching for a per-connector tool.",
       {}, [], _get_lab_overview),
    _t("get_metric_history",
       "Recent time-series for one metric of one system, from the poller's ring "
       f"buffer (about the last hour, max {MAX_HISTORY_POINTS} points). Use it to "
       "tell 'has always been like this' from 'started recently'. If the metric "
       "name is wrong the error lists the valid ones.",
       {"system": {**_STR, "description": "unifi | proxmox | docker | homeassistant | synology"},
        "metric": {**_STR, "description": "e.g. cpu_pct, mem_pct, latency_ms, running, vol_pct"}},
       ["system", "metric"], _get_metric_history),
    _t("get_ops_log",
       "ClaudeOS's own audit log — recent actions and events across all systems, "
       "newest first. Good for 'what changed recently?' and for correlating a "
       "failure with a restart or config change.",
       {"limit": {"type": "integer", "description": f"max entries, 1-{MAX_OPLOG_ENTRIES}"},
        "level": {**_STR, "description": "optional filter: info | warn | error | action"}},
       [], _get_ops_log),
    _t("get_uptime_monitors",
       "Service monitors (HTTP/TCP/DNS/keyword) with current state, 24h uptime "
       "percentage and average response time. Use for 'is X up?' questions about "
       "services rather than hosts.",
       {}, [], _get_uptime_monitors),

    # ---- tier 2: per-connector queries
    _t("unifi_devices",
       "UniFi network hardware (gateway, switches, access points): state, model, "
       "IP, firmware, client count, uptime.",
       {}, [], _unifi_devices),
    _t("unifi_clients",
       "Devices currently connected to the network, wired and wireless, with "
       "signal strength and uptime. Optionally filter by name or IP substring.",
       {"search": {**_STR, "description": "optional name/IP substring filter"}},
       [], _unifi_clients),
    _t("unifi_events",
       f"Gateway system log, newest first, {MAX_EVENT_PAGE} per page. Category "
       "SECURITY covers IDS/IPS blocks and threats. Page through for older events.",
       {"category": {**_STR, "description": "SECURITY | UPDATE | CLIENT | ADMIN | CRITICAL"},
        "page": {"type": "integer", "description": "0-based page number"}},
       [], _unifi_events),
    _t("unifi_anomalies",
       "Clients the gateway flags as anomalous (poor signal, high retries, "
       "repeated disconnects). Useful for wifi complaints.",
       {}, [], _unifi_anomalies),
    _t("unifi_insights",
       "Gateway health detail: WAN status and throughput, per-port issues, and "
       "pending firmware updates.",
       {}, [], _unifi_insights),
    _t("proxmox_nodes",
       "Proxmox hosts with CPU, memory, root-disk usage and uptime.",
       {}, [], _proxmox_nodes),
    _t("proxmox_guests",
       "All VMs and LXC containers with state, CPU, memory and uptime. Note each "
       "guest's node, type (qemu|lxc) and vmid — other proxmox tools need them.",
       {}, [], _proxmox_guests),
    _t("proxmox_guest_detail",
       "Deep stats for ONE guest, including kernel pressure-stall (PSI) figures "
       "that distinguish real memory pressure from harmless page cache. Use this "
       "before concluding a guest needs more RAM.",
       {"node": _STR, "type": {**_STR, "description": "qemu | lxc"},
        "vmid": {**_STR, "description": "numeric guest id as a string"}},
       ["node", "type", "vmid"], _proxmox_guest_detail),
    _t("proxmox_storage",
       "Proxmox datastores: type, usage, shared flag and permitted content.",
       {}, [], _proxmox_storage),
    _t("proxmox_disk_health",
       "SMART health for every physical disk on every Proxmox node — status, "
       "temperature, wear and the specific failure indicators found. Cached and "
       "swept 6-hourly; use for any disk-failure or storage-reliability question.",
       {}, [], _proxmox_disk_health),
    _t("docker_containers",
       "Containers on the Docker host: state, image, ports, GPU passthrough. "
       "Optionally filter by state (running | exited | paused ...).",
       {"state": {**_STR, "description": "optional state filter"}},
       [], _docker_containers),
    _t("docker_container_logs",
       f"Recent log output for ONE container by name (last {MAX_LOG_TAIL} lines "
       "max). The response says when earlier lines were omitted — do not treat a "
       "clean tail as proof the container has never errored.",
       {"name": {**_STR, "description": "exact container name"},
        "tail": {"type": "integer", "description": f"lines to return, 1-{MAX_LOG_TAIL}"}},
       ["name"], _docker_container_logs),
    _t("docker_storage_report",
       "Disk usage breakdown for the Docker host: largest images, writable "
       "layers, volumes and reclaimable space.",
       {}, [], _docker_storage_report),
    _t("docker_gpu_report",
       "GPU state on the Docker host: utilisation, VRAM, temperature, which "
       "processes hold the GPU and which containers have passthrough. Use for "
       "transcoding and AI-workload questions.",
       {}, [], _docker_gpu_report),
    _t("docker_image_updates",
       "Which container images have newer versions in their registry, from the "
       "6-hourly digest sweep.",
       {}, [], _docker_image_updates),
    _t("ha_summary",
       "Home Assistant overview: version, entity count, unavailable entities, "
       "lights/switches on, automation count.",
       {}, [], _ha_summary),
    _t("ha_entities",
       f"Home Assistant entities with state (max {MAX_ENTITY_ROWS} rows). This "
       "instance has thousands of entities, so ALWAYS pass a domain and/or search "
       "filter — an unfiltered call is capped and mostly useless.",
       {"domain": {**_STR, "description": "e.g. light, switch, sensor, climate"},
        "search": {**_STR, "description": "name or entity_id substring"}},
       [], _ha_entities),
    _t("ha_error_log",
       f"Recent Home Assistant errors and warnings (last {MAX_LOG_TAIL} lines).",
       {}, [], _ha_error_log),
    _t("ha_zha_devices",
       "Zigbee (ZHA) devices with link quality (LQI), RSSI, availability, power "
       "source and role. Use for 'device keeps dropping off' questions.",
       {}, [], _ha_zha_devices),
    _t("ha_updates",
       "Available Home Assistant updates — core, OS, supervisor, add-ons and "
       "device firmware.",
       {}, [], _ha_updates),
    _t("ha_system_info",
       "HAOS internals: core CPU/RAM, host disk, and add-on states.",
       {}, [], _ha_system_info),
    _t("synology_status",
       "Synology NAS status: model, DSM version, uptime, temperature, CPU, RAM "
       "and overall volume usage.",
       {}, [], _synology_status),
    _t("synology_storage",
       "Synology volumes and physical disks with usage, temperature, status and "
       "SMART verdicts.",
       {}, [], _synology_storage),
]


WRITE_TOOLS = [
    _t("docker_container_action",
       "Start, stop or restart a container by name. Requires the user's "
       "confirmation — the call returns approval_required and does nothing until "
       "they approve, so say what you intend and why before calling it.",
       {"name": {**_STR, "description": "exact container name"},
        "action": {**_STR, "description": "start | stop | restart"}},
       ["name", "action"], _docker_container_action, writes=True),
    _t("proxmox_guest_action",
       "Start, shutdown, reboot or stop a VM/LXC. 'stop' is a hard power-cut and "
       "'shutdown' is graceful — prefer shutdown. Requires the user's "
       "confirmation before anything happens.",
       {"node": _STR, "type": {**_STR, "description": "qemu | lxc"},
        "vmid": {**_STR, "description": "numeric guest id as a string"},
        "action": {**_STR, "description": "start | shutdown | reboot | stop"}},
       ["node", "type", "vmid", "action"], _proxmox_guest_action, writes=True),
    _t("ha_call_service",
       "Call a Home Assistant service — e.g. domain 'light', service 'turn_off'. "
       "Only these domains are permitted: " + ", ".join(sorted(HA_ALLOWED_DOMAINS)) +
       ". Anything else is refused. Requires the user's confirmation.",
       {"domain": {**_STR, "description": "one of the permitted domains"},
        "service": {**_STR, "description": "e.g. turn_on, turn_off, toggle, set_temperature"},
        "entity_id": {**_STR, "description": "target entity, or omit for none"}},
       ["domain", "service"], _ha_call_service, writes=True),
    _t("unifi_restart_device",
       "Reboot a UniFi access point or switch by MAC address. Every client on "
       "that device drops for a few minutes. Requires the user's confirmation.",
       {"mac": {**_STR, "description": "device MAC as shown by unifi_devices"}},
       ["mac"], _unifi_restart_device, writes=True),
]

ALL_TOOLS = READ_TOOLS + WRITE_TOOLS
BY_NAME = {t["name"]: t for t in ALL_TOOLS}


def schemas(include_writes: bool = True) -> list:
    """Anthropic tool definitions, deterministically ordered so the cached
    prompt prefix stays byte-identical between turns."""
    src = ALL_TOOLS if include_writes else READ_TOOLS
    return [{k: v for k, v in t.items() if not k.startswith("_")} for t in src]


def is_write(name: str) -> bool:
    t = BY_NAME.get(name)
    return bool(t and t["_writes"])


def warning_for(name: str, params: dict) -> str | None:
    return _WARN.get((name, params.get("action"))) or _WARN.get((name, None))


# -------------------------------------------------------------- dispatch

def run(name: str, params: dict, *, approved: bool = False) -> dict:
    """Execute one tool call and return an envelope.

    Write tools return `approval_required` unless `approved=True`, which only
    app/chat.py sets after a single-use pending id has been redeemed. The
    allowlist is re-checked inside the write handlers themselves, so an
    approval cannot smuggle a domain past the gate.
    """
    t = BY_NAME.get(name)
    inv = _invocation(name, params)
    if t is None:
        return envelope("error", error=f"no such tool: {name}", invocation=inv, params=params)

    if t["_writes"] and not approved:
        return envelope("approval_required", invocation=inv, params=params,
                        warning=warning_for(name, params))

    t0 = time.monotonic()
    try:
        data, omitted = t["_fn"](**params)
    except ToolError as e:
        return envelope("error", error=str(e), invocation=inv, params=params,
                        elapsed_ms=int((time.monotonic() - t0) * 1000))
    except TypeError as e:
        return envelope("error", error=f"bad parameters: {e}", invocation=inv,
                        params=params, elapsed_ms=int((time.monotonic() - t0) * 1000))
    except Exception as e:  # noqa: BLE001 — a failing tool must not kill the turn
        return envelope("error", error=f"{type(e).__name__}: {e}", invocation=inv,
                        params=params, elapsed_ms=int((time.monotonic() - t0) * 1000))

    ms = int((time.monotonic() - t0) * 1000)
    if data is None or data == [] or data == {}:
        return envelope("no_data", invocation=inv, params=params, elapsed_ms=ms,
                        omitted=omitted,
                        error=None)
    return envelope("success", data=data, invocation=inv, params=params,
                    elapsed_ms=ms, omitted=omitted)
