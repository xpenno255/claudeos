# ClaudeOS Roadmap

Feature candidates from a deep-research pass (2026-07-16, 22 sources, claims
adversarially verified). Ordered by agreed priority. Items marked ⚠ carry
caveats noted at the bottom.

## Agreed build order (top 5)

1. ✅ **Notification layer** *(shipped 2026-07-16)* — fan-out dispatcher
   (`app/notify.py`) with five channels: ntfy (JSON publish, topic = secret),
   generic webhook, Telegram bot API, Pushover, HA notify passthrough.
   Configured on Setup page, secrets encrypted as usual; per-channel
   enable/pause + SAVE + TEST sends a real test notification. Poller fires
   down (True→False) / recover alerts; identical titles muted for 5 min.
2. ✅ **Uptime/service monitors** *(shipped 2026-07-16)* — HTTP(S)/TCP/DNS/
   keyword checks (`app/monitors.py`), concurrent 30s sweeps, ~24h ring-
   buffer history, Ops → UPTIME tab (add/pause/remove, response sparklines,
   24h uptime %). Alerts via the notification layer after 2 consecutive
   failures, recover alert on first success.
3. ✅ **UniFi events & IDS feed + Claude triage** *(shipped 2026-07-16)* —
   live probe found v1 `stat/event` / `list/alarm` / `stat/ips/event` GONE
   on UDM-SE fw 5.1.25; the working feed is
   `POST /proxy/network/v2/api/site/default/system-log/all`
   (`{pageNumber, pageSize, categories:["SECURITY"]}` — server-side filter,
   IPS blocks are subcategory SECURITY_INTRUSION_PREVENTION); v1
   `stat/anomalies` still works. EVENTS & THREATS panel on Ops → NETWORK
   with per-event "◈ TRIAGE" (Claude judges real risk vs alert severity).
4. ✅ **Scheduled AI health report** *(shipped 2026-07-16)* —
   `app/reports.py` collects a compact lab snapshot (gateway, security
   events, Proxmox nodes/storage, Docker fleet, HA/ZHA, monitors, week's
   warnings, metric aggregates) → Claude digests to grade + highlights +
   ranked findings. Weekly scheduler (day/hour, stdlib loop), summary
   delivered via notification channels, last 12 reports kept in
   data/reports.json, Ops → REPORTS tab (schedule config + run-now +
   report history). Email deferred — deliverable via any notify channel.
5. ✅ **SMART disk health** *(shipped 2026-07-16)* — the Proxmox API probe
   succeeded with the existing token (no agent needed): `disks/list` gives
   health/wearout, `disks/smart` gives ATA attribute tables or raw NVMe
   text. `app/smart.py` parses both, evaluates Scrutiny-style
   (realloc/pending sectors, critical warning, spare, media errors,
   endurance, temp), sweeps 6-hourly, alerts on status transitions, feeds
   the AI health report, and renders a DISK HEALTH panel on Ops → COMPUTE.

## Backlog (validated, unordered)

- **WAN speed-test tracker** — trigger/poll gateway speed test via
  `cmd/devmgr` (`speedtest`, `speedtest-status`) — same endpoint pattern as
  restart/upgrade. History chart + below-plan alert. Quick win.
- ✅ **Docker image update detection** *(shipped 2026-07-17)* —
  `app/registry.py`: local digests from `/system/df` (works behind a
  socket-proxy with SYSTEM:1 — `/images/json` needs IMAGES and is
  avoided), registry digests via manifest HEAD + generic WWW-Authenticate
  token flow (docker.io/ghcr/lscr verified live). Optional Docker Hub /
  GHCR PATs on Setup → Container Registries (rate limits, private repos).
  6-hourly sweeps, UPDATE pills on the Containers tab, notify on new
  updates, feeds the weekly AI report.
- ✅ **Proxmox backup monitoring** *(shipped 2026-07-27, #50)* — became the
  wider **Backups tab**: `app/backups.py` tracks backup *outcomes* rather than
  reachability, with heartbeat jobs (a `curl` line in any script) alongside
  discovered Proxmox schedules. Live probing corrected two assumptions in this
  entry: vzdump tasks are job-level, not per-VM (`id` is always `""`), so
  `/cluster/backup` — not the task feed — is the source of job identity;
  `/cluster/backup-info/not-backed-up` does work and needed no fallback.
  The first sweep found 25 consecutive nightly failures nobody knew about.
- **HA push updates** — persistent WebSocket `subscribe_events`
  (`state_changed`) using existing hws.py → real-time dashboard, alerts on
  device_offline/automation failure. Replaces polling.
- **Anomaly detection** — stdlib z-score/EWMA baselines over the poller's
  ring buffers; Claude summarizes flagged anomalies (Netdata's on-by-default
  ML validates the pattern; expect warm-up period, no seasonality).
- **AI alert triage + NL alert rules** — one-click root-cause hypothesis on
  any alert; "describe an alert in English" → rule. (Netdata pattern.)
- ✅ **Agentic ops chat (flagship)** *(shipped 2026-07-25, `5de0438`)* — Ops
  Chat: `app/chat.py` over the shared `app/toolloop.py`, read-only tool access
  across the connectors, writes confirm-gated. The same loop was later reused
  for lab-issue triage, which is what made #50-era triage cheap to build.

## Caveats / open questions from the research

- Per-app **DPI endpoints refuted** (`/stat/sitedpi`, `/stat/stadpi` failed
  verification 1–2) — live-test against the UDM-SE before planning a
  traffic-by-app feature. Official docs confirm only aggregate traffic
  insights; cloud Site Manager API (read-only, X-API-Key) has ISP metrics.
- UniFi local endpoints are community-documented; schemas may shift with
  firmware. Always prefix `/proxy/network` on UDM-SE.
- Proxmox/Docker/HA-energy areas produced no verified claims — treat those
  backlog items as general knowledge until probed.
- GitHub stars/versions checked 2026-07-16.
