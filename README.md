# ◈ CLAUDEOS — Homelab Mission Control

An agentic control plane for your homelab: **report, monitor and carry out
tasks** across a UniFi network (UDM-SE), Proxmox VE, an Ubuntu Docker farm
and Home Assistant — from one retro-futuristic console.

Zero build step, zero npm. Python 3.10+ stdlib plus the `cryptography`
package (already present on most distros).

```bash
.venv/bin/python3 server.py                  # → http://127.0.0.1:8321
.venv/bin/python3 server.py --host 0.0.0.0   # expose on your LAN
```

The venv (created with `python3 -m venv --system-site-packages .venv` +
`.venv/bin/pip install anthropic`) provides the official Anthropic SDK for
the AI features — with automatic retries and typed errors. Plain
`python3 server.py` also works; the AI features then fall back to raw HTTP.

## Pages

| Page | What it does |
|---|---|
| **Dashboard** | Status tile per system with a one-hour sparkline, Proxmox node CPU/MEM/disk meters, WAN health & ISP latency, container states, HA summary, live ops log |
| **Operations → Network** | UniFi devices & clients, restart devices |
| **Operations → Compute** | VMs/LXCs (start/shutdown/reboot/stop), **datastores** with usage, **node performance** (CPU, IO delay, load, network — from PVE RRD) |
| **Operations → Containers** | Start/stop/restart, host vitals incl. **GPU utilisation & VRAM history**, on-demand **storage analysis** (largest images / writable layers / volumes, reclaimable space) |
| **Operations → Home** | HAOS internals (core CPU/RAM, host disk), **add-on states**, **ZHA mesh health** (LQI/RSSI/offline/weak links per device), all entities with toggles, plus two Claude-powered analyses (below) |
| **Setup** | Connection details per system, save-and-test, unlink |

## AI analyses (optional)

Add an Anthropic API key (console.anthropic.com) on Setup → Claude AI to enable,
on the Home tab:

- **AI Log Analysis** — sends the HA error log to `claude-opus-4-8`, which
  deduplicates, ranks and categorises issues and recommends concrete fixes.
- **AI Mesh Insights** — sends the ZHA device inventory (LQI/RSSI/availability)
  plus zigbee-related log lines to Claude for a graded mesh-health report with
  placement/repeater/battery recommendations.

Analyses run only when you click the button — nothing is sent automatically.
The key is encrypted at rest like every other secret.

### Extra enablement notes

- **HAOS internal stats & add-ons** need the long-lived token to come from an
  *administrator* HA user (supervisor endpoints reject non-admin tokens).
- **Docker storage analysis** needs `SYSTEM: 1` added to the socket-proxy
  container's environment.
- **Host folder scans** (du-style largest folders/files) appear when host
  paths are bind-mounted read-only under `/scan` in the claudeos service,
  e.g. `- /home/you:/scan/home:ro` — one SCAN button per mount.
- **ZHA health** uses the HA websocket API — works with the same token.

Disruptive actions require a second confirming click, and everything ClaudeOS
does is written to the **ops log** (`data/opslog.jsonl`) — the audit trail for
you or any agent driving the API.

## Running in Docker (recommended for the homelab)

The repo builds and publishes an image automatically: every push to `main`
triggers the GitHub Actions workflow (`.github/workflows/docker.yml`), which
pushes `ghcr.io/<owner>/claudeos:latest` (plus a commit-sha tag for
rollbacks) to GitHub Container Registry using the built-in `GITHUB_TOKEN` —
no secrets to configure.

On the docker host:

```bash
mkdir -p /opt/claudeos && cd /opt/claudeos
curl -fsSLO https://raw.githubusercontent.com/<owner>/claudeos/main/deploy/compose.yaml
docker compose up -d          # → http://<docker-host>:8321
```

All state — the master key, encrypted connection config, and ops log —
lives in `./data` next to the compose file and survives image updates.
To migrate an existing bare-metal install, copy its `data/` directory
there before first start (`chown -R 1000:1000 data` — the container runs
unprivileged as UID 1000). Update later with
`docker compose pull && docker compose up -d`.

Configuration env vars: `CLAUDEOS_DATA` (state dir, `/data` in the image),
`CLAUDEOS_HOST` (bind, `0.0.0.0` in the image), `CLAUDEOS_PORT` (8321), `TZ`.

If the GHCR package is private, log the docker host in first:
`docker login ghcr.io -u <owner>` with a PAT that has `read:packages`.

⚠ In a container the server binds 0.0.0.0 by design — anyone who can reach
port 8321 can drive your lab. Keep it on a trusted VLAN or put an
authenticating reverse proxy in front.

## Linking your systems

**UniFi (UDM-SE)** — create a dedicated *local* admin on the console
(Settings → Admins → Add New Admin, "Restrict to local access only").
Cloud/Ubiquiti-account credentials with MFA will not work. Host is just
`https://<udm-ip>`.

**Proxmox VE** — create an API token: Datacenter → Permissions → API Tokens
→ Add. Token ID looks like `root@pam!claudeos`; the secret is shown once.
If you leave "Privilege Separation" ticked, give the token its own
permissions (e.g. `PVEVMAdmin` on `/vms` to control guests, `PVEAuditor` on
`/` to read node stats). Host: `<pve-ip>:8006`.

**Docker (Ubuntu farm)** — expose the Engine API safely with a socket proxy
container rather than the raw daemon port:

```yaml
services:
  socket-proxy:
    image: tecnativa/docker-socket-proxy
    ports: ["2375:2375"]
    environment:
      CONTAINERS: 1        # list containers
      POST: 1              # allow actions
      ALLOW_START: 1
      ALLOW_STOP: 1
      ALLOW_RESTARTS: 1
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

Then point ClaudeOS at `http://<docker-host>:2375`.

**Docker host vitals (CPU / RAM / GPU / storage)** — the Engine API only
knows about containers, so ClaudeOS reads host metrics from an optional
[Glances](https://nicolargo.github.io/glances/) sidecar on the same VM:

```bash
docker run -d --name glances --restart unless-stopped \
  --pid host --network host \
  nicolargo/glances:latest-full glances -w
```

For NVIDIA GPU stats add `--gpus all` (requires the nvidia container
toolkit on the VM, which itself requires GPU passthrough from Proxmox).
Set the Glances URL (`http://<docker-host>:61208`) in Setup → Docker;
CPU, RAM, GPU utilisation/temperature and per-filesystem storage then
appear on the dashboard and the Containers operations tab.

**Home Assistant (HAOS)** — profile (bottom-left avatar) → Security →
Long-lived access tokens → Create. Host: `http://<ha-ip>:8123`.

## Security model

- Secrets (passwords/tokens) are encrypted at rest with **AES-256-GCM**.
  The 32-byte master key is generated on first run at `data/master.key`
  (mode `0600`) — it never leaves the machine, and secrets are never sent
  to the browser (the API only reports "stored").
- TLS verification is per-connection and defaults **off** because homelab
  gear ships self-signed certs; turn it on if you've installed real ones.
- The server binds to `127.0.0.1` by default and has **no login of its
  own** — if you expose it with `--host 0.0.0.0`, anyone on that network
  can drive your lab. Keep it on a trusted VLAN, or front it with a
  reverse proxy that adds auth (Authelia, Caddy basic-auth, Tailscale).
- `data/` holds the key, encrypted config and ops log — exclude it from
  any backup or repo you share (`data/` is in `.gitignore`).

## Driving it with an agent

Everything the UI does goes through a plain JSON API, so Claude Code (or a
scheduled agent) can operate the lab through the same audited path:

```
GET  /api/overview                                     # everything, one call
GET  /api/history                                      # sparkline ring buffers
GET  /api/log                                          # ops log
POST /api/poll                                         # poll all systems now
GET  /api/unifi/devices | /api/unifi/clients
POST /api/unifi/devices/{mac}/restart
GET  /api/proxmox/nodes | /api/proxmox/guests
POST /api/proxmox/guests/{node}/{qemu|lxc}/{vmid}/{start|shutdown|reboot|stop}
GET  /api/docker/containers
POST /api/docker/containers/{id}/{start|stop|restart}
GET  /api/ha/entities
POST /api/ha/service          {"domain","service","entity_id","data"}
```

Example: *"restart the plex container"* →
`curl -X POST localhost:8321/api/docker/containers/<id>/restart`.

## Layout

```
server.py               HTTP server, routing, static serving
app/
  store.py              encrypted config store (AES-256-GCM)
  httpclient.py         outbound HTTP w/ self-signed-TLS support
  poller.py             30s background poll + metric ring buffers
  oplog.py              audit log (memory + data/opslog.jsonl)
  connectors/           unifi · proxmox · docker · homeassistant
public/                 no-build frontend (ES modules)
data/                   master key, encrypted config, ops log (created at runtime)
```
