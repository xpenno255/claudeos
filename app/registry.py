"""Container image update detection — the What's Up Docker pattern.

For every image behind a container, compare the locally-pulled digest
(RepoDigests) against what the registry says the tag points to now (a
manifest HEAD — no image data is downloaded). Verified live 2026-07-17:

- /system/df supplies RepoTags + RepoDigests through a socket-proxy with
  SYSTEM:1 — /images/json needs the IMAGES permission, so it is avoided.
- The generic WWW-Authenticate bearer-token flow resolves docker.io,
  ghcr.io and lscr.io anonymously.

Optional Docker Hub / GHCR credentials (Setup → Container Registries)
are passed as Basic auth to the token endpoint — higher rate limits and
private repos. Locally-built and digest-pinned images have nothing to
compare and are reported as "local".
"""

import base64
import re
import threading
import time
import urllib.parse

from . import notify, oplog, store
from .connectors import docker
from .httpclient import HttpError, request

SWEEP_INTERVAL = 6 * 3600
ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])

_lock = threading.Lock()
_cache: dict = {"images": [], "ts": None, "error": None}
_alerted: set = set()  # refs already announced as having an update


def _parse_ref(ref: str):
    """'lscr.io/linuxserver/plex:latest' → (host, repo, tag); None for
    digest-pinned refs. Hosts must contain a dot (or be localhost) — a
    colon alone is a tag on a local image name, not a port."""
    if ref.startswith("sha256:") or "@sha256:" in ref:
        return None
    host, rest = "docker.io", ref
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or first.split(":")[0] == "localhost"):
        host, rest = first, ref.split("/", 1)[1]
    tag = "latest"
    if ":" in rest.rsplit("/", 1)[-1]:
        rest, tag = rest.rsplit(":", 1)
    if host == "docker.io" and "/" not in rest:
        rest = "library/" + rest
    return host, rest, tag


def _normalize(ref: str) -> str:
    """Container image string → the RepoTags form (explicit :latest)."""
    if "@sha256:" in ref or ref.startswith("sha256:"):
        return ref
    if ":" not in ref.rsplit("/", 1)[-1]:
        return ref + ":latest"
    return ref


def _creds_for(host: str):
    r = store.get_system("registries", reveal_secrets=True) or {}
    if host in ("docker.io", "registry-1.docker.io"):
        u, t = r.get("dockerhub_user"), r.get("dockerhub_token")
    elif host == "ghcr.io":
        u, t = r.get("ghcr_user"), r.get("ghcr_token")
    else:
        return None
    return (u, t) if u and t else None


def _remote_digest(host: str, repo: str, tag: str) -> str | None:
    """What the registry's tag points to right now (manifest HEAD)."""
    reg = "registry-1.docker.io" if host == "docker.io" else host
    url = f"https://{reg}/v2/{repo}/manifests/{tag}"
    try:
        _, hdrs = request("HEAD", url, headers={"Accept": ACCEPT},
                          verify_tls=True, timeout=15, return_headers=True)
        return hdrs.get("Docker-Content-Digest")
    except HttpError as e:
        if e.status != 401 or e.headers is None:
            raise
        # anonymous/authorized bearer-token dance per WWW-Authenticate
        parts = dict(re.findall(r'(\w+)="([^"]*)"', e.headers.get("WWW-Authenticate", "")))
        if not parts.get("realm"):
            raise
        q = {"service": parts.get("service", ""),
             "scope": parts.get("scope") or f"repository:{repo}:pull"}
        theaders = {}
        if (c := _creds_for(host)):
            theaders["Authorization"] = "Basic " + base64.b64encode(
                f"{c[0]}:{c[1]}".encode()).decode()
        tok = request("GET", parts["realm"] + "?" + urllib.parse.urlencode(q),
                      headers=theaders, verify_tls=True, timeout=15)
        token = (tok or {}).get("token") or (tok or {}).get("access_token")
        if not token:
            raise ConnectionError(f"{parts['realm']} returned no token")
        _, hdrs = request("HEAD", url,
                          headers={"Accept": ACCEPT, "Authorization": f"Bearer {token}"},
                          verify_tls=True, timeout=15, return_headers=True)
        return hdrs.get("Docker-Content-Digest")


def _local_digests(settings: dict) -> dict:
    """tag ref → pulled digest, from /system/df image summaries."""
    df = docker._call(settings, "GET", "/system/df")
    out = {}
    for img in df.get("Images") or []:
        digests = img.get("RepoDigests") or []
        for tag in img.get("RepoTags") or []:
            repo = tag.rsplit(":", 1)[0]
            for d in digests:
                if d.split("@", 1)[0] == repo:
                    out[tag] = d.split("@", 1)[1]
                    break
    return out


def sweep(alert: bool = True) -> list:
    s = store.get_system("docker", reveal_secrets=True)
    if not s or not s.get("host"):
        with _lock:
            _cache.update(images=[], ts=time.time(), error="Docker is not configured")
        return []

    try:
        local = _local_digests(s)
        conts = docker.containers(s)
    except Exception as e:  # noqa: BLE001
        with _lock:
            _cache.update(ts=time.time(), error=str(e))
        raise

    by_ref: dict = {}
    for c in conts:
        ref = _normalize(c.get("image") or "")
        by_ref.setdefault(ref, []).append(c.get("name"))

    images = []
    for ref, names in sorted(by_ref.items()):
        parsed = _parse_ref(ref)
        entry = {"ref": ref, "containers": names, "status": "local",
                 "local": local.get(ref), "remote": None, "error": None}
        if parsed and entry["local"]:
            host, repo, tag = parsed
            try:
                entry["remote"] = _remote_digest(host, repo, tag)
                entry["status"] = ("update" if entry["remote"] and entry["remote"] != entry["local"]
                                   else "current")
            except Exception as e:  # noqa: BLE001 — one registry down ≠ sweep failed
                entry["status"] = "error"
                entry["error"] = str(e)[:160]
        images.append(entry)

    with _lock:
        _cache.update(images=images, ts=time.time(), error=None)

    updates = [i for i in images if i["status"] == "update"]
    fresh = [i["ref"] for i in updates if i["ref"] not in _alerted]
    _alerted.intersection_update({i["ref"] for i in updates})  # forget applied ones
    if alert and fresh:
        _alerted.update(fresh)
        oplog.add("info", "registry", f"image updates available: {', '.join(fresh)}")
        notify.send(
            f"{len(fresh)} container image update{'s' if len(fresh) > 1 else ''} available",
            ", ".join(fresh)[:800], priority="default", tags=["package"])
    return images


def test_credentials() -> dict:
    """Setup-page test: fetch a public manifest per registry — with stored
    creds attached the token endpoint rejects bad credentials outright."""
    bits = []
    for label, host, repo in (("Docker Hub", "docker.io", "library/hello-world"),
                              ("GHCR", "ghcr.io", "home-assistant/home-assistant")):
        mode = "with key" if _creds_for(host) else "anonymous"
        try:
            _remote_digest(host, repo, "latest")
            bits.append(f"{label} ok ({mode})")
        except Exception as e:  # noqa: BLE001
            bits.append(f"{label} FAILED {mode}: {str(e)[:80]}")
    if any("FAILED" in b for b in bits):
        raise ConnectionError("; ".join(bits))
    return {"ok": True, "detail": "; ".join(bits)}


def get(max_age: int = SWEEP_INTERVAL) -> dict:
    with _lock:
        if _cache["ts"] and time.time() - _cache["ts"] < max_age:
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
                oplog.add("error", "registry", f"image update sweep failed: {e}")
            time.sleep(SWEEP_INTERVAL)

    threading.Thread(target=loop, name="claudeos-registry", daemon=True).start()
