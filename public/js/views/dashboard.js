// Dashboard: system tiles with sparklines, compute vitals, network
// health, container states and the live ops log.

import { api } from "../api.js";
import { el, fmtBytes, fmtPct, fmtUptime, clockTime } from "../util.js";
import { sparkline, meter } from "../charts.js";
import { SYSTEMS, BY_ID } from "../meta.js";

const REFRESH_MS = 15000;

export async function renderDashboard(root) {
  let timer = null;

  async function draw() {
    const [overview, history, log] = await Promise.all([
      api.overview(), api.history(), api.log(),
    ]);

    const anyConfigured = Object.values(overview.config || {}).some(c => c.configured);
    root.replaceChildren();

    if (!anyConfigured) {
      root.append(hero());
      return;
    }

    root.append(tiles(overview, history));

    const grid = el("div", { class: "dash-grid" });
    grid.append(
      computePanel(overview, "span-6"),
      networkPanel(overview, history, "span-6"),
      logPanel(log, "span-8"),
      statesPanel(overview, "span-4"),
    );
    root.append(grid);
  }

  await draw();
  timer = setInterval(() => draw().catch(() => {}), REFRESH_MS);
  return () => clearInterval(timer);
}

// ---------------------------------------------------------------- pieces

function hero() {
  return el("div", { class: "panel accent hero-empty" },
    el("div", { class: "glyph" }, "◈"),
    el("h2", { class: "blink-cursor" }, "NO SYSTEMS LINKED"),
    el("p", {}, "ClaudeOS has nothing to watch yet. Head to Setup and link your UniFi console, Proxmox host, Docker engine and Home Assistant — credentials are encrypted at rest and never leave this machine."),
    el("a", { href: "#/setup" }, el("button", { class: "btn" }, "▸ OPEN SETUP")));
}

function statusLed(st) {
  const cls = st?.ok === true ? "ok" : st?.ok === false ? "err" : "off";
  return el("span", { class: `led ${cls}` });
}

function tiles(overview, history) {
  const wrap = el("div", { class: "tiles" });
  for (const sys of SYSTEMS) {
    const cfg = overview.config?.[sys.id];
    const st = overview.systems?.[sys.id];
    const d = st?.data || {};
    let big = "—", sub = "", sparkPts = null, unit = "";

    if (sys.id === "unifi") {
      big = d.clients ?? "—"; unit = "clients";
      sub = st?.ok ? `WAN ${d.wan_status ?? "?"} · ${d.devices_online}/${d.devices_total} devices · ${d.isp_latency_ms ?? "—"}ms` : "";
      sparkPts = history?.unifi?.clients;
    } else if (sys.id === "proxmox") {
      big = st?.ok ? `${d.guests_running}/${d.guests_total}` : "—"; unit = "guests up";
      sub = st?.ok ? (d.perms_hint ? "⚠ token needs permissions — see vitals panel"
        : `CPU ${fmtPct((d.cpu_avg ?? 0) * 100)} · MEM ${d.mem_total ? fmtPct(100 * d.mem_used / d.mem_total) : "—"}`) : "";
      sparkPts = history?.proxmox?.cpu_pct;
    } else if (sys.id === "docker") {
      big = st?.ok ? `${d.containers_running}/${d.containers_total}` : "—"; unit = "running";
      const h = d.host;
      sub = st?.ok ? (h
        ? `HOST CPU ${fmtPct(h.cpu_pct ?? 0)} · RAM ${fmtPct(h.mem_pct ?? 0)}${(h.gpus?.length) ? ` · GPU ${fmtPct(h.gpus[0].util_pct ?? 0)}` : ""}`
        : `${d.containers_exited ?? 0} stopped`) : "";
      sparkPts = history?.docker?.host_cpu_pct?.length ? history.docker.host_cpu_pct : history?.docker?.running;
    } else if (sys.id === "homeassistant") {
      big = d.entities_total ?? "—"; unit = "entities";
      sub = st?.ok ? `${d.lights_on ?? 0} lights on · ${d.unavailable ?? 0} unavailable` : "";
      sparkPts = history?.homeassistant?.lights_on;
    } else if (sys.id === "synology") {
      big = d.vol_pct != null ? fmtPct(d.vol_pct) : "—"; unit = "volume used";
      sub = st?.ok ? (d.storage_error
        ? "⚠ storage stats need an administrators-group user"
        : `CPU ${fmtPct(d.cpu_pct ?? 0)} · RAM ${fmtPct(d.mem_pct ?? 0)}`
          + (d.temp_c != null ? ` · ${d.temp_c}°C` : "")
          + (d.disks_abnormal ? ` · ⚠ ${d.disks_abnormal} disk issue${d.disks_abnormal > 1 ? "s" : ""}` : "")) : "";
      sparkPts = history?.synology?.cpu_pct;
    }

    const configured = cfg?.configured;
    const tile = el("div", {
      class: `panel tile ${configured ? "" : "tile-unconfigured"}`,
      onclick: () => { location.hash = configured ? `#/ops/${sys.tab}` : "#/setup"; },
    },
      el("div", { class: "tile-head" }, statusLed(st), el("span", { class: "tile-name" }, sys.label),
        el("span", { class: "mono-dim" }, configured ? "" : "NOT LINKED")),
      el("div", { class: "tile-big" }, configured ? big : "···", " ",
        el("small", {}, configured ? unit : "configure in setup")),
      el("div", { class: "tile-sub" }, st?.ok === false ? `⚠ ${truncate(st.error, 60)}` : sub),
      el("div", { class: "tile-spark" }, sparkline(sparkPts, { color: sys.hex })));
    if (st?.ok === false) tile.querySelector(".tile-sub").style.color = "var(--critical)";
    wrap.append(tile);
  }
  return wrap;
}

function computePanel(overview, span) {
  const panel = el("div", { class: `panel ${span}` },
    el("div", { class: "panel-title" }, "COMPUTE VITALS — PROXMOX NODES"));
  const st = overview.systems?.proxmox;
  if (!st?.ok) {
    panel.append(el("div", { class: "mono-dim" }, st?.error || "not configured"));
    return panel;
  }
  if (st.data.perms_hint) {
    panel.append(el("div", { class: "pill warn", style: "white-space:normal;line-height:1.5;padding:8px 12px;margin-bottom:10px" },
      `⚠ ${st.data.perms_hint}`));
  }
  for (const n of st.data.nodes || []) {
    panel.append(
      el("div", { class: "meter-label", style: "margin-top:8px" },
        el("span", { class: "strong", style: "color:var(--ink);letter-spacing:.1em" }, `▣ ${n.node}`),
        el("span", { class: "mono-dim" }, `up ${fmtUptime(n.uptime)}`)),
      meter("CPU", n.cpu != null ? n.cpu * 100 : null, { detail: `${n.maxcpu ?? "?"} cores` }),
      meter("MEMORY", n.maxmem ? (100 * n.mem / n.maxmem) : null, { detail: `${fmtBytes(n.mem)} / ${fmtBytes(n.maxmem)}` }),
      meter("DISK (root)", n.maxdisk ? (100 * n.disk / n.maxdisk) : null, { detail: fmtBytes(n.maxdisk) }));
  }
  return panel;
}

function networkPanel(overview, history, span) {
  const panel = el("div", { class: `panel ${span}` },
    el("div", { class: "panel-title" }, "NETWORK HEALTH — UNIFI"));
  const st = overview.systems?.unifi;
  if (!st?.ok) {
    panel.append(el("div", { class: "mono-dim" }, st?.error || "not configured"));
    return panel;
  }
  const d = st.data;
  const wanOk = d.wan_status === "ok";
  panel.append(
    el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px" },
      el("span", { class: `pill ${wanOk ? "ok" : "err"}` }, `${wanOk ? "●" : "✕"} WAN ${(d.wan_status || "?").toUpperCase()}`),
      d.wan_ip ? el("span", { class: "pill neutral" }, `IP ${d.wan_ip}`) : null,
      el("span", { class: "pill neutral" }, `↓ ${fmtBytes(d.rx_bytes_r, true)}`),
      el("span", { class: "pill neutral" }, `↑ ${fmtBytes(d.tx_bytes_r, true)}`),
      el("span", { class: "pill neutral" }, `${d.wifi_clients} wifi / ${d.wired_clients} wired`)),
    el("div", { class: "mono-dim", style: "margin-bottom:4px" }, "ISP LATENCY (ms) — last hour"),
    el("div", { class: "tile-spark", style: "height:56px" },
      Object.assign(sparkline(history?.unifi?.latency_ms, { color: BY_ID.unifi.hex, height: 56 }), { style: "" })));
  return panel;
}

function logPanel(log, span) {
  const feed = el("div", { class: "feed" });
  const entries = log.entries || [];
  if (!entries.length) feed.append(el("div", { class: "mono-dim" }, "no events yet"));
  for (const e of entries) {
    feed.append(el("div", { class: `feed-row ${e.level}` },
      el("span", { class: "feed-ts" }, clockTime(e.ts)),
      el("span", { class: "feed-sys" }, e.system),
      el("span", { class: "feed-msg" }, e.message)));
  }
  return el("div", { class: `panel ${span}` },
    el("div", { class: "panel-title" }, "OPS LOG — LATEST EVENTS"), feed);
}

function statesPanel(overview, span) {
  const panel = el("div", { class: `panel ${span}` },
    el("div", { class: "panel-title" }, "FLEET & HOME"));
  const dk = overview.systems?.docker;
  const ha = overview.systems?.homeassistant;

  panel.append(el("div", { class: "mono-dim", style: "margin-bottom:6px" }, "CONTAINER STATES"));
  if (dk?.ok) {
    const states = dk.data.states || {};
    const total = dk.data.containers_total || 1;
    for (const [state, count] of Object.entries(states).sort((a, b) => b[1] - a[1])) {
      const color = state === "running" ? "var(--good)" : state === "exited" ? "var(--ink-3)" : "var(--warning)";
      panel.append(meterRow(state, count, total, color));
    }
    const h = dk.data.host;
    if (h) {
      panel.append(el("div", { class: "mono-dim", style: "margin:14px 0 2px" }, "DOCKER HOST VITALS"));
      panel.append(meter("CPU", h.cpu_pct, {}));
      panel.append(meter("RAM", h.mem_pct, { detail: h.mem_total ? `${fmtBytes(h.mem_used)} / ${fmtBytes(h.mem_total)}` : "" }));
      for (const g of h.gpus || []) {
        panel.append(meter(`GPU ${g.name || ""}`.trim(), g.util_pct, { detail: g.temp != null ? `${g.temp}°C · vram ${fmtPct(g.mem_pct ?? 0)}` : "" }));
      }
      for (const disk of h.disks || []) {
        panel.append(meter(`DISK ${disk.mount}`, disk.pct, { detail: `${fmtBytes(disk.used)} / ${fmtBytes(disk.total)}` }));
      }
    } else if (dk.data.host_error) {
      panel.append(el("div", { class: "mono-dim", style: "margin-top:10px;color:var(--warning)" }, `⚠ host vitals: ${dk.data.host_error}`));
    }
  } else {
    panel.append(el("div", { class: "mono-dim" }, dk?.error || "not configured"));
  }

  panel.append(el("div", { class: "mono-dim", style: "margin:14px 0 6px" }, "HOME ASSISTANT"));
  if (ha?.ok) {
    const d = ha.data;
    panel.append(
      el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" },
        el("span", { class: "pill neutral" }, `${d.location || "home"} · v${d.version}`),
        el("span", { class: "pill neutral" }, `${d.automations} automations`),
        el("span", { class: `pill ${d.unavailable ? "warn" : "ok"}` }, `${d.unavailable} unavailable`),
        el("span", { class: "pill neutral" }, `${d.lights_on} lights on`)));
  } else {
    panel.append(el("div", { class: "mono-dim" }, ha?.error || "not configured"));
  }
  return panel;
}

function meterRow(label, count, total, color) {
  const row = el("div", { class: "meter" });
  row.innerHTML = `
    <div class="meter-label"><span>${label}</span><b>${count}</b></div>
    <div class="meter-track"><div class="meter-fill" style="width:${(100 * count / total).toFixed(1)}%;background:${color}"></div></div>`;
  return row;
}

function truncate(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
