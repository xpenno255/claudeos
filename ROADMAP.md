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
2. **Uptime/service monitors** — per-service HTTP(S)/TCP/DNS/keyword checks
   with status history, response-time sparkline, alert on down/recover.
   Pattern: Uptime Kuma (~89k stars). Chassis: existing 30s poller +
   metric-history ring buffers. Monitor the ~38 containers' UIs, HA,
   Proxmox, UDM.
3. **UniFi events & IDS feed + Claude triage** — site events
   (`/stat/event`), alarms (`/list/alarm`), IDS/IPS events
   (`/stat/ips/event`), anomalies (`/stat/anomalies`) via the existing
   session (all under `/proxy/network` on UDM-SE). Live events panel +
   "Analyze with Claude" per alert. ⚠ community-documented API.
4. **Scheduled AI health report** — weekly cron feeds Claude the metric
   history, events, port errors, ZHA health, backup status → ranked digest
   delivered via ntfy/email. (Netdata ships this as a paid feature —
   pattern validated, no OSS reference.)
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
