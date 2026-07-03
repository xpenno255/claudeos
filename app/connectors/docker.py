"""Docker Engine API connector, with optional Glances host vitals.

Talks to the Docker Engine HTTP API on your Ubuntu docker host. Expose it
safely with a socket proxy container (recommended), e.g.
tecnativa/docker-socket-proxy with CONTAINERS=1 POST=1, then set
  host: http://<docker-host>:2375

Host CPU/RAM/disk/GPU come from a Glances sidecar on the same VM
(optional `glances_url` setting, e.g. http://<docker-host>:61208):
  docker run -d --name glances --restart unless-stopped \
    --pid host --network host nicolargo/glances:latest-full glances -w
(add `--gpus all` for NVIDIA GPU stats)
"""

from .. import httpclient

CONTAINER_ACTIONS = {"start", "stop", "restart"}

# filesystems that are noise on a docker host
FS_SKIP_PREFIXES = ("/boot", "/snap", "/run", "/dev", "/sys", "/proc")


def _base(settings: dict) -> str:
    host = settings["host"].strip().rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    return host


def _call(settings: dict, method: str, path: str):
    return httpclient.request(
        method,
        _base(settings) + path,
        verify_tls=settings.get("verify_tls", False),
    )


def _glances(settings: dict, path: str):
    base = settings["glances_url"].strip().rstrip("/")
    if not base.startswith("http"):
        base = "http://" + base
    return httpclient.request("GET", base + path, verify_tls=settings.get("verify_tls", False))


def host_metrics(settings: dict) -> dict | None:
    """Host vitals from Glances (API v4, falling back to v3).
    Returns None when no glances_url is configured."""
    if not settings.get("glances_url"):
        return None
    last_err = None
    for v in ("4", "3"):
        try:
            quick = _glances(settings, f"/api/{v}/quicklook") or {}
            mem = _glances(settings, f"/api/{v}/mem") or {}
        except (httpclient.HttpError, ConnectionError) as e:
            last_err = e
            continue
        try:
            fs = _glances(settings, f"/api/{v}/fs") or []
        except Exception:  # noqa: BLE001 — plugin may be disabled
            fs = []
        try:
            gpus = _glances(settings, f"/api/{v}/gpu") or []
        except Exception:  # noqa: BLE001 — no GPU plugin / no GPU
            gpus = []
        # GPU/driver injection bind-mounts dozens of single files that glances
        # reports as filesystems. When the host root is mounted at /rootfs,
        # trust only /rootfs* entries (the host's real mounts); otherwise fall
        # back to prefix filtering plus dedupe on identical device stats.
        rootfs_seen = any(f.get("mnt_point") == "/rootfs" for f in fs)
        pool = []
        if rootfs_seen:
            for f in fs:
                mp = f.get("mnt_point") or ""
                if not mp.startswith("/rootfs") or "/var/lib/docker/" in mp:
                    continue
                label = "host /" if mp == "/rootfs" else mp[len("/rootfs"):]
                if label != "host /" and label.startswith(FS_SKIP_PREFIXES):
                    continue
                pool.append((label, f))
        else:
            seen = set()
            for f in fs:
                mp = f.get("mnt_point") or ""
                if not mp or mp.startswith(FS_SKIP_PREFIXES) or "docker/overlay" in mp:
                    continue
                key = (f.get("size"), f.get("used"))
                if key in seen:  # same underlying device → a file bind-mount
                    continue
                seen.add(key)
                pool.append((mp, f))
        disks = [
            {"mount": label, "used": f.get("used"),
             "total": f.get("size"), "pct": f.get("percent")}
            for label, f in pool
        ]
        disks.sort(key=lambda d: d["total"] or 0, reverse=True)
        return {
            "cpu_pct": quick.get("cpu"),
            "mem_pct": quick.get("mem") if quick.get("mem") is not None else mem.get("percent"),
            "mem_used": mem.get("used"),
            "mem_total": mem.get("total"),
            "load": quick.get("load"),
            "disks": disks[:4],
            "gpus": [
                {"name": g.get("name"), "util_pct": g.get("proc"),
                 "mem_pct": g.get("mem"), "temp": g.get("temperature")}
                for g in gpus
            ],
        }
    raise ConnectionError(f"Glances unreachable: {last_err}")


def test(settings: dict) -> dict:
    info = _call(settings, "GET", "/version")
    ver = info.get("Version", "?") if isinstance(info, dict) else "?"
    detail = f"Docker Engine {ver}"
    if settings.get("glances_url"):
        try:
            h = host_metrics(settings)
            gpu_note = f", {len(h['gpus'])} GPU" if h["gpus"] else ", no GPU visible"
            detail += f" · Glances OK (CPU {h['cpu_pct']}%, RAM {h['mem_pct']}%{gpu_note})"
        except Exception as e:  # noqa: BLE001
            detail += f" · ⚠ Glances: {e}"
    return {"ok": True, "detail": detail}


def containers(settings: dict) -> list:
    data = _call(settings, "GET", "/containers/json?all=1") or []
    out = []
    for c in data:
        name = (c.get("Names") or ["/?"])[0].lstrip("/")
        out.append({
            "id": c.get("Id", "")[:12],
            "name": name,
            "image": c.get("Image"),
            "state": c.get("State"),          # running | exited | paused ...
            "status": c.get("Status"),        # human string "Up 3 days"
            "created": c.get("Created"),
            "compose_project": (c.get("Labels") or {}).get("com.docker.compose.project"),
            "ports": [
                f"{p.get('PublicPort')}→{p.get('PrivatePort')}/{p.get('Type')}"
                for p in (c.get("Ports") or []) if p.get("PublicPort")
            ],
        })
    out.sort(key=lambda c: ((c["compose_project"] or "~"), c["name"]))
    return out


def summary(settings: dict) -> dict:
    cs = containers(settings)
    states = {}
    for c in cs:
        states[c["state"]] = states.get(c["state"], 0) + 1
    host, host_error = None, None
    try:
        host = host_metrics(settings)
    except Exception as e:  # noqa: BLE001 — containers still count with vitals down
        host_error = str(e)
    return {
        "containers_total": len(cs),
        "containers_running": states.get("running", 0),
        "containers_exited": states.get("exited", 0),
        "states": states,
        "host": host,
        "host_error": host_error,
    }


def storage_report(settings: dict) -> dict:
    """docker system df equivalent: what's eating the docker disk.
    Needs SYSTEM: 1 on the socket-proxy for /system/df."""
    try:
        df = _call(settings, "GET", "/system/df")
    except httpclient.HttpError as e:
        if e.status == 403:
            raise ConnectionError(
                "the socket-proxy blocked /system/df — add SYSTEM: 1 to its environment "
                "and recreate it to enable storage analysis") from e
        raise

    images = []
    for i in df.get("Images") or []:
        size = i.get("Size") or 0
        shared = i.get("SharedSize") or 0
        images.append({
            "tag": (i.get("RepoTags") or ["<untagged>"])[0],
            "size": size,
            "unique": size - max(shared, 0),
            "in_use": (i.get("Containers") or 0) > 0,
        })
    images.sort(key=lambda x: -x["size"])

    containers = []
    for c in df.get("Containers") or []:
        containers.append({
            "name": (c.get("Names") or ["/?"])[0].lstrip("/"),
            "rw_size": c.get("SizeRw") or 0,      # container's own writable layer
            "state": c.get("State"),
        })
    containers.sort(key=lambda x: -x["rw_size"])

    volumes = []
    for v in df.get("Volumes") or []:
        usage = v.get("UsageData") or {}
        volumes.append({
            "name": v.get("Name", "?"),
            "size": usage.get("Size") or 0,
            "in_use": (usage.get("RefCount") or 0) > 0,
        })
    volumes.sort(key=lambda x: -x["size"])

    build_cache = sum((b.get("Size") or 0) for b in df.get("BuildCache") or [])
    reclaimable_images = sum(i["size"] for i in images if not i["in_use"])
    return {
        "totals": {
            "images": sum(i["unique"] for i in images) or df.get("LayersSize") or 0,
            "containers": sum(c["rw_size"] for c in containers),
            "volumes": sum(v["size"] for v in volumes),
            "build_cache": build_cache,
        },
        "reclaimable": {
            "unused_images": reclaimable_images,
            "unused_volumes": sum(v["size"] for v in volumes if not v["in_use"]),
            "build_cache": build_cache,
        },
        "images": images[:12],
        "containers": containers[:12],
        "volumes": volumes[:12],
    }


def container_action(settings: dict, container_id: str, action: str) -> dict:
    if action not in CONTAINER_ACTIONS:
        raise ValueError(f"unsupported action: {action}")
    _call(settings, "POST", f"/containers/{container_id}/{action}")
    return {"ok": True, "detail": f"{action} sent to {container_id}"}
