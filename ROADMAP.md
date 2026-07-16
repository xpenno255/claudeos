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
5. **SMART disk health** — Scrutiny-style attribute tracking with
   real-failure-rate thresholds. First step: probe Proxmox
   `/api2/json/nodes/{node}/disks/smart` with the existing token to see if
   attributes suffice; else tiny agent/cron on the PVE host.

## Backlog (validated, unordered)

- **WAN speed-test tracker** — trigger/poll gateway speed test via
  `cmd/devmgr` (`speedtest`, `speedtest-status`) — same endpoint pattern as
  restart/upgrade. History chart + below-plan alert. Quick win.
- **Docker image update detection** — What's Up Docker/watchtower pattern
  (watchtower discontinued → hot demand): compare running digests vs
  registry digests, show UPDATE pills like UniFi firmware. ⚠ unverified
  claims, general knowledge only.
- **Proxmox backup monitoring** — vzdump task success/failure feed +
  `/cluster/backup-info/not-backed-up` (VMs with no backup job).
- **HA push updates** — persistent WebSocket `subscribe_events`
  (`state_changed`) using existing hws.py → real-time dashboard, alerts on
  device_offline/automation failure. Replaces polling.
- **Anomaly detection** — stdlib z-score/EWMA baselines over the poller's
  ring buffers; Claude summarizes flagged anomalies (Netdata's on-by-default
  ML validates the pattern; expect warm-up period, no seasonality).
- **AI alert triage + NL alert rules** — one-click root-cause hypothesis on
  any alert; "describe an alert in English" → rule. (Netdata pattern.)
- **Agentic ops chat (flagship)** — chat panel where Claude gets read-only
  tool access to all connectors (UniFi/Proxmox/Docker/HA), answers
  "why is X slow?" with evidence; write actions confirm-gated. Architecture
  proven by HolmesGPT (CNCF Sandbox, first-class Anthropic support).

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
