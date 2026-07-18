#!/usr/bin/env python3
"""ClaudeOS — agentic homelab mission control.

Zero-dependency* HTTP server (stdlib http.server) exposing a JSON API over
the UniFi / Proxmox / Docker / Home Assistant connectors and serving the
frontend from public/.

    python3 server.py                 # http://127.0.0.1:8321
    python3 server.py --host 0.0.0.0  # expose on the LAN
    python3 server.py --port 9000

*needs the `cryptography` package for AES-GCM secret storage (already
present on most systems).
"""

import argparse
import json
import mimetypes
import os
import re
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import ai, monitors, notify, oplog, poller, registry, reports, scanner, smart, store
from app.connectors import CONNECTORS, docker, homeassistant, proxmox, synology, unifi
from app.httpclient import HttpError

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")

SYSTEM_LABELS = store.SYSTEM_LABELS


def _settings(system_id: str) -> dict:
    s = store.get_system(system_id, reveal_secrets=True)
    if not s or not s.get("host"):
        raise LookupError(f"{SYSTEM_LABELS.get(system_id, system_id)} is not configured — add it on the Setup page")
    return s


def _async_poll() -> None:
    threading.Thread(target=poller.poll_once, daemon=True).start()


# ---------------------------------------------------------------- routes

def route_overview(_m, _p, _b):
    return {"systems": poller.snapshot(), "config": store.public_summary(),
            "labels": SYSTEM_LABELS}


def route_history(_m, _p, _b):
    return poller.history()


def route_log(_m, _p, _b):
    return {"entries": oplog.recent(120)}


def route_systems_get(_m, _p, _b):
    return store.public_summary()


def route_system_save(_m, p, body):
    sid = p["id"]
    if sid not in store.SYSTEM_IDS:
        raise LookupError(f"unknown system: {sid}")
    store.save_system(sid, body or {})
    oplog.add("info", sid, "connection settings updated")
    _async_poll()
    return {"ok": True}


def route_system_delete(_m, p, _b):
    sid = p["id"]
    store.delete_system(sid)
    oplog.add("info", sid, "connection settings removed")
    _async_poll()
    return {"ok": True}


def route_system_test(_m, p, _b):
    sid = p["id"]
    if sid == "ai":
        s = store.get_system("ai", reveal_secrets=True)
        if not s or not s.get("api_key"):
            raise LookupError("AI is not configured — add your Anthropic API key first")
        result = ai.test(s)
    elif sid == "registries":
        result = registry.test_credentials()
    elif sid in notify.CHANNEL_IDS:
        result = notify.test_channel(sid)
    elif sid in CONNECTORS:
        result = CONNECTORS[sid].test(_settings(sid))
    else:
        raise LookupError(f"unknown system: {sid}")
    oplog.add("info", sid, f"connection test ok: {result.get('detail', '')}")
    return result


def route_poll_now(_m, _p, _b):
    poller.poll_once()
    return {"ok": True, "systems": poller.snapshot()}


def route_unifi_devices(_m, _p, _b):
    return {"devices": unifi.devices(_settings("unifi"))}


def route_unifi_clients(_m, _p, _b):
    return {"clients": unifi.clients(_settings("unifi"))}


def route_unifi_insights(_m, _p, _b):
    return unifi.insights(_settings("unifi"))


def route_unifi_events(_m, _p, body):
    body = body or {}
    return unifi.events(_settings("unifi"),
                        categories=body.get("categories"),
                        page=body.get("page", 0),
                        page_size=body.get("page_size", 50))


def route_unifi_anomalies(_m, _p, _b):
    return {"anomalies": unifi.anomalies(_settings("unifi"))}


def route_unifi_event_analyze(_m, _p, body):
    ev = (body or {}).get("event")
    if not ev:
        raise ValueError("event is required")
    snap = poller.snapshot().get("unifi", {}).get("data") or {}
    context = (f"UniFi UDM-SE gateway for a homelab; internal subnets are 192.168.x.x; "
               f"{snap.get('clients', '?')} clients online, WAN status {snap.get('wan_status', '?')}")
    result = ai.analyze_unifi_event(ev, context)
    oplog.add("action", "unifi",
              f"AI event triage: {ev.get('event') or ev.get('id')} → {result.get('threat_level')}")
    return result


def route_unifi_restart(_m, p, _b):
    res = unifi.restart_device(_settings("unifi"), p["mac"])
    oplog.add("action", "unifi", f"device restart requested: {p['mac']}")
    return res


def route_unifi_upgrade(_m, p, _b):
    res = unifi.upgrade_device(_settings("unifi"), p["mac"])
    oplog.add("action", "unifi", f"firmware upgrade requested: {p['mac']}")
    return res


def route_proxmox_guests(_m, _p, _b):
    return {"guests": proxmox.guests(_settings("proxmox"))}


def route_proxmox_nodes(_m, _p, _b):
    return {"nodes": proxmox.nodes(_settings("proxmox"))}


def route_proxmox_action(_m, p, _b):
    res = proxmox.guest_action(_settings("proxmox"), p["node"], p["type"], p["vmid"], p["action"])
    oplog.add("action", "proxmox", f"{p['action']} → {p['type']}/{p['vmid']} on {p['node']}")
    _async_poll()
    return res


def route_docker_containers(_m, _p, _b):
    return {"containers": docker.containers(_settings("docker"))}


def route_docker_action(_m, p, _b):
    res = docker.container_action(_settings("docker"), p["cid"], p["action"])
    oplog.add("action", "docker", f"{p['action']} → container {p['cid']}")
    _async_poll()
    return res


def route_proxmox_storage(_m, _p, _b):
    return {"storage": proxmox.storage(_settings("proxmox"))}


def route_proxmox_perf(_m, _p, _b):
    return {"perf": proxmox.perf(_settings("proxmox"))}


def route_proxmox_guest_detail(_m, p, _b):
    return proxmox.guest_detail(_settings("proxmox"), p["node"], p["type"], p["vmid"])


def route_proxmox_guest_rrd(_m, p, _b):
    return {"rrd": proxmox.guest_rrd(_settings("proxmox"), p["node"], p["type"], p["vmid"])}


def route_proxmox_disks(_m, _p, _b):
    return smart.get()


def route_proxmox_disks_refresh(_m, _p, _b):
    disks = smart.sweep()
    oplog.add("info", "smart", f"manual SMART sweep: {len(disks)} disk(s) checked")
    return smart.get()


def route_docker_gpu(_m, _p, _b):
    return docker.gpu_report(_settings("docker"))


def route_docker_updates(_m, _p, _b):
    return registry.get()


def route_docker_updates_refresh(_m, _p, _b):
    imgs = registry.sweep()
    ups = sum(1 for i in imgs if i["status"] == "update")
    oplog.add("info", "registry", f"manual image update check: {ups} update(s) across {len(imgs)} image(s)")
    return registry.get()


def route_docker_storage(_m, _p, _b):
    report = docker.storage_report(_settings("docker"))
    oplog.add("info", "docker", "storage analysis run")
    return report


def route_scan_roots(_m, _p, _b):
    return {"roots": scanner.roots()}


def route_scan(_m, _p, body):
    path = (body or {}).get("path", "")
    if not path:
        raise ValueError("path is required")
    result = scanner.scan(path)
    oplog.add("info", "claudeos", f"host folder scan: {path} "
              f"({result['total'] / 1e9:.1f} GB)")
    return result


def route_monitors_list(_m, _p, _b):
    return {"monitors": monitors.list_monitors()}


def route_monitors_history(_m, _p, _b):
    return {"history": monitors.history()}


def route_monitor_create(_m, _p, body):
    mon = monitors.create(body or {})
    oplog.add("info", "monitor", f"monitor added: {mon['name']} ({mon['type']} {mon['target']})")
    threading.Thread(target=monitors.check_all, daemon=True).start()
    return {"ok": True, "monitor": mon}


def route_monitors_check(_m, _p, _b):
    monitors.check_all()
    return {"ok": True, "monitors": monitors.list_monitors()}


def route_monitor_update(_m, p, body):
    mon = monitors.update(p["mid"], body or {})
    oplog.add("info", "monitor", f"monitor updated: {mon['name']}")
    return {"ok": True, "monitor": mon}


def route_monitor_delete(_m, p, _b):
    monitors.delete(p["mid"])
    oplog.add("info", "monitor", f"monitor removed: {p['mid']}")
    return {"ok": True}


def route_reports_get(_m, _p, _b):
    return reports.get_state()


def route_reports_run(_m, _p, _b):
    return {"ok": True, "report": reports.generate("manual")}


def route_reports_config(_m, _p, body):
    cfg = reports.set_config(body or {})
    oplog.add("info", "reports",
              f"schedule updated: {'weekly' if cfg['enabled'] else 'disabled'} "
              f"day={cfg['day']} hour={cfg['hour']:02d}:00")
    return {"ok": True, "config": cfg}


def route_synology_storage(_m, _p, _b):
    return synology.storage(_settings("synology"))


def route_ha_system(_m, _p, _b):
    return homeassistant.system_info(_settings("homeassistant"))


def route_ha_zha(_m, _p, _b):
    return {"devices": homeassistant.zha_devices(_settings("homeassistant"))}


def route_ha_updates(_m, _p, _b):
    return {"updates": homeassistant.updates(_settings("homeassistant"))}


def _zigbee_log_lines(settings: dict) -> list:
    log = homeassistant.error_log(settings)
    keywords = ("zha", "zigbee", "zigpy", "bellows", "deconz", "nwk", "ieee")
    return [ln for ln in log.splitlines() if any(k in ln.lower() for k in keywords)]


def _ha_context(settings: dict) -> str:
    try:
        s = homeassistant.summary(settings)
        return (f"Home Assistant {s.get('version')} at '{s.get('location')}', "
                f"{s.get('entities_total')} entities, {s.get('unavailable')} unavailable, "
                f"{s.get('automations')} automations")
    except Exception:  # noqa: BLE001
        return "Home Assistant (no summary available)"


def route_ha_analyze_logs(_m, _p, _b):
    settings = _settings("homeassistant")
    log = homeassistant.error_log(settings)
    if not log.strip():
        return {"summary": "The error log is empty — nothing to analyse. That's a good sign.",
                "issues": []}
    result = ai.analyze_ha_logs(log, _ha_context(settings))
    oplog.add("action", "homeassistant",
              f"AI log analysis: {len(result.get('issues', []))} issue(s) found")
    return result


def route_ha_zha_insights(_m, _p, _b):
    settings = _settings("homeassistant")
    devices = homeassistant.zha_devices(settings)
    result = ai.analyze_zha(devices, _zigbee_log_lines(settings), _ha_context(settings))
    oplog.add("action", "homeassistant",
              f"AI ZHA analysis: grade {result.get('grade')}, {len(result.get('findings', []))} finding(s)")
    return result


def route_ha_entities(_m, _p, _b):
    return {"entities": homeassistant.states(_settings("homeassistant"))}


def route_ha_service(_m, _p, body):
    body = body or {}
    domain, service = body.get("domain"), body.get("service")
    if not domain or not service:
        raise ValueError("domain and service are required")
    res = homeassistant.call_service(
        _settings("homeassistant"), domain, service,
        entity_id=body.get("entity_id"), data=body.get("data"))
    oplog.add("action", "homeassistant",
              f"{domain}.{service} → {body.get('entity_id') or '(no target)'}")
    return res


ROUTES = [
    ("GET",    r"^/api/overview$",                                        route_overview),
    ("GET",    r"^/api/history$",                                         route_history),
    ("GET",    r"^/api/log$",                                             route_log),
    ("GET",    r"^/api/systems$",                                         route_systems_get),
    ("POST",   r"^/api/systems/(?P<id>[a-z]+)$",                          route_system_save),
    ("DELETE", r"^/api/systems/(?P<id>[a-z]+)$",                          route_system_delete),
    ("POST",   r"^/api/systems/(?P<id>[a-z]+)/test$",                     route_system_test),
    ("POST",   r"^/api/poll$",                                            route_poll_now),
    ("GET",    r"^/api/unifi/devices$",                                   route_unifi_devices),
    ("GET",    r"^/api/unifi/clients$",                                   route_unifi_clients),
    ("GET",    r"^/api/unifi/insights$",                                  route_unifi_insights),
    ("POST",   r"^/api/unifi/events$",                                    route_unifi_events),
    ("GET",    r"^/api/unifi/anomalies$",                                 route_unifi_anomalies),
    ("POST",   r"^/api/unifi/events/analyze$",                            route_unifi_event_analyze),
    ("POST",   r"^/api/unifi/devices/(?P<mac>[0-9a-fA-F:]+)/restart$",    route_unifi_restart),
    ("POST",   r"^/api/unifi/devices/(?P<mac>[0-9a-fA-F:]+)/upgrade$",    route_unifi_upgrade),
    ("GET",    r"^/api/proxmox/guests$",                                  route_proxmox_guests),
    ("GET",    r"^/api/proxmox/nodes$",                                   route_proxmox_nodes),
    ("GET",    r"^/api/proxmox/storage$",                                 route_proxmox_storage),
    ("GET",    r"^/api/proxmox/perf$",                                    route_proxmox_perf),
    ("GET",    r"^/api/proxmox/disks$",                                   route_proxmox_disks),
    ("POST",   r"^/api/proxmox/disks/refresh$",                           route_proxmox_disks_refresh),
    ("GET",    r"^/api/docker/storage$",                                  route_docker_storage),
    ("GET",    r"^/api/docker/gpu$",                                      route_docker_gpu),
    ("GET",    r"^/api/docker/updates$",                                  route_docker_updates),
    ("POST",   r"^/api/docker/updates/refresh$",                          route_docker_updates_refresh),
    ("GET",    r"^/api/storage/roots$",                                   route_scan_roots),
    ("POST",   r"^/api/storage/scan$",                                    route_scan),
    ("GET",    r"^/api/reports$",                                         route_reports_get),
    ("POST",   r"^/api/reports/run$",                                     route_reports_run),
    ("POST",   r"^/api/reports/config$",                                  route_reports_config),
    ("GET",    r"^/api/monitors$",                                        route_monitors_list),
    ("GET",    r"^/api/monitors/history$",                                route_monitors_history),
    ("POST",   r"^/api/monitors$",                                        route_monitor_create),
    ("POST",   r"^/api/monitors/check$",                                  route_monitors_check),
    ("POST",   r"^/api/monitors/(?P<mid>[0-9a-f]+)$",                     route_monitor_update),
    ("DELETE", r"^/api/monitors/(?P<mid>[0-9a-f]+)$",                     route_monitor_delete),
    ("GET",    r"^/api/synology/storage$",                                route_synology_storage),
    ("GET",    r"^/api/ha/system$",                                       route_ha_system),
    ("GET",    r"^/api/ha/zha$",                                          route_ha_zha),
    ("GET",    r"^/api/ha/updates$",                                      route_ha_updates),
    ("POST",   r"^/api/ha/analyze-logs$",                                 route_ha_analyze_logs),
    ("POST",   r"^/api/ha/zha-insights$",                                 route_ha_zha_insights),
    ("GET",    r"^/api/proxmox/guests/(?P<node>[\w.-]+)/(?P<type>qemu|lxc)/(?P<vmid>\d+)/detail$", route_proxmox_guest_detail),
    ("GET",    r"^/api/proxmox/guests/(?P<node>[\w.-]+)/(?P<type>qemu|lxc)/(?P<vmid>\d+)/rrd$", route_proxmox_guest_rrd),
    ("POST",   r"^/api/proxmox/guests/(?P<node>[\w.-]+)/(?P<type>qemu|lxc)/(?P<vmid>\d+)/(?P<action>\w+)$", route_proxmox_action),
    ("GET",    r"^/api/docker/containers$",                               route_docker_containers),
    ("POST",   r"^/api/docker/containers/(?P<cid>[0-9a-fA-F]+)/(?P<action>\w+)$", route_docker_action),
    ("GET",    r"^/api/ha/entities$",                                     route_ha_entities),
    ("POST",   r"^/api/ha/service$",                                      route_ha_service),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeOS/1.0"

    def log_message(self, fmt, *args):  # quiet the default per-request noise
        pass

    # -------------------------------------------------------- responses
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # revalidate every load so UI updates are never held back by cache
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -------------------------------------------------------- dispatch
    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _dispatch(self, method):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/"):
            for m, pattern, fn in ROUTES:
                if m != method:
                    continue
                match = re.match(pattern, path)
                if match:
                    try:
                        return self._send_json(fn(method, match.groupdict(), self._read_body()))
                    except LookupError as e:
                        return self._send_json({"error": str(e)}, 404)
                    except ValueError as e:
                        return self._send_json({"error": str(e)}, 400)
                    except HttpError as e:
                        return self._send_json(
                            {"error": f"upstream error: {e} — {e.body[:300]}"}, 502)
                    except ConnectionError as e:
                        return self._send_json({"error": str(e)}, 502)
                    except Exception as e:  # noqa: BLE001
                        traceback.print_exc()
                        return self._send_json({"error": f"internal error: {e}"}, 500)
            return self._send_json({"error": "not found"}, 404)

        # static frontend
        if method != "GET":
            return self._send_json({"error": "not found"}, 404)
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(PUBLIC, rel))
        if not full.startswith(PUBLIC):
            return self._send_json({"error": "forbidden"}, 403)
        if not os.path.isfile(full):
            full = os.path.join(PUBLIC, "index.html")  # SPA fallback
        return self._send_file(full)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


def main():
    ap = argparse.ArgumentParser(description="ClaudeOS homelab mission control")
    ap.add_argument("--host", default=os.environ.get("CLAUDEOS_HOST", "127.0.0.1"),
                    help="bind address (use 0.0.0.0 to expose on your LAN)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CLAUDEOS_PORT", "8321")))
    args = ap.parse_args()

    poller.start()
    monitors.start()
    reports.start()
    smart.start()
    registry.start()
    oplog.add("info", "claudeos", f"server started on {args.host}:{args.port}")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\n  ┌─ CLAUDEOS ── homelab mission control")
    print(f"  │  http://{args.host}:{args.port}")
    print(f"  └─ ctrl-c to stop\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
