// Operations: granular tables + actions per homelab category.
// Tabs: NETWORK (UniFi) / COMPUTE (Proxmox) / CONTAINERS (Docker) / HOME (HA)

import { api } from "../api.js";
import { el, fmtBytes, fmtPct, fmtUptime, debounce } from "../util.js";
import { meter, sparkRow, sparkline } from "../charts.js";
import { SYSTEMS, BY_ID } from "../meta.js";

const TABS = [
  { tab: "network",    label: "NETWORK",    sys: "unifi" },
  { tab: "compute",    label: "COMPUTE",    sys: "proxmox" },
  { tab: "containers", label: "CONTAINERS", sys: "docker" },
  { tab: "home",       label: "HOME",       sys: "homeassistant" },
  { tab: "uptime",     label: "UPTIME",     sys: null },  // service monitors — not tied to one system
  { tab: "reports",    label: "REPORTS",    sys: null },  // scheduled AI health reports
];

export async function renderOps(root, args, { toast }) {
  const active = TABS.find(t => t.tab === args[0])?.tab || "network";
  const overview = await api.overview().catch(() => ({ systems: {}, config: {} }));

  const tabs = el("div", { class: "tabs" });
  for (const t of TABS) {
    const st = overview.systems?.[t.sys];
    const dotColor = st?.ok === true ? "var(--good)" : st?.ok === false ? "var(--critical)" : "var(--ink-3)";
    tabs.append(el("a", { href: `#/ops/${t.tab}`, class: t.tab === active ? "active" : "" },
      el("span", { class: "tab-dot", style: `background:${dotColor}` }), t.label));
  }
  root.append(tabs);

  const body = el("div", {});
  root.append(body);

  const activeSys = TABS.find(t => t.tab === active).sys;
  const cfg = activeSys ? overview.config?.[activeSys] : null;
  if (activeSys && !cfg?.configured) {
    body.append(el("div", { class: "panel hero-empty" },
      el("h2", {}, "SYSTEM NOT LINKED"),
      el("p", {}, "Add the connection details on the Setup page first."),
      el("a", { href: "#/setup" }, el("button", { class: "btn" }, "▸ OPEN SETUP"))));
    return;
  }

  const renderers = { network, compute, containers, home, uptime, reports };
  await renderers[active](body, toast, overview);
}

// ------------------------------------------------------------ helpers

function toolbar(placeholder, onSearch, extra = []) {
  const input = el("input", { class: "search", type: "text", placeholder });
  input.addEventListener("input", debounce(() => onSearch(input.value.toLowerCase()), 150));
  return el("div", { class: "ops-toolbar" }, input, el("div", { class: "spacer" }), ...extra);
}

function tableWrap(headers, rows) {
  return el("div", { class: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {}, ...headers.map(h =>
        el("th", { class: h.startsWith(">") ? "num" : "" }, h.replace(/^>/, ""))))),
      el("tbody", {}, ...rows)));
}

function statePill(state, okStates = ["online", "running", "on", "ok"]) {
  const s = String(state || "?").toLowerCase();
  const cls = okStates.includes(s) ? "ok" : ["offline", "exited", "stopped", "unavailable", "error", "dead"].includes(s) ? "err" : "neutral";
  return el("span", { class: `pill ${cls}` }, s.toUpperCase());
}

// Two-click confirm for anything disruptive.
function actionBtn(label, run, { danger = false, confirm = true, accent = false, toast }) {
  const btn = el("button", { class: `btn btn-mini ${danger ? "btn-danger" : accent ? "" : "btn-ghost"}` }, label);
  let armed = false, timer = null;
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (confirm && !armed) {
      armed = true;
      btn.classList.add("confirming");
      const orig = btn.textContent;
      btn.textContent = "CONFIRM?";
      timer = setTimeout(() => { armed = false; btn.classList.remove("confirming"); btn.textContent = orig; }, 3000);
      return;
    }
    clearTimeout(timer);
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const res = await run();
      toast(res?.detail || "done", "ok", label);
      btn.textContent = "SENT ✓";
      setTimeout(() => location.reload(), 1400);
    } catch (err) {
      toast(String(err.message || err), "err", `${label} FAILED`);
      btn.disabled = false;
      btn.classList.remove("confirming");
      btn.textContent = label;
      armed = false;
    }
  });
  return btn;
}

// ------------------------------------------------------------ NETWORK

async function network(body, toast) {
  const [{ devices }, { clients }, insights, eventsRes, anomaliesRes] = await Promise.all([
    api.unifiDevices(), api.unifiClients(),
    api.unifiInsights().catch(e => ({ error: String(e.message || e) })),
    api.unifiEvents({ categories: ["SECURITY"] }).catch(e => ({ error: String(e.message || e) })),
    api.unifiAnomalies().catch(() => ({ anomalies: [] })),
  ]);

  // ---- gateway (UDM-SE) health
  const gwPanel = el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "GATEWAY HEALTH — UDM-SE"));
  const gw = insights.gateway;
  if (insights.error || !gw) {
    gwPanel.append(el("div", { class: "mono-dim" }, `⚠ ${insights.error || "no gateway device found"}`));
  } else {
    gwPanel.append(
      el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px" },
        el("span", { class: "pill neutral" }, `${gw.model} · fw ${gw.version}`),
        el("span", { class: "pill neutral" }, `up ${fmtUptime(gw.uptime)}`),
        ...(gw.temps || []).map(t => {
          const cls = t.value >= 75 ? "err" : t.value >= 62 ? "warn" : "ok";
          return el("span", { class: `pill ${cls}` }, `${t.name} ${t.value.toFixed(1)}°C`);
        })),
      meter("CPU", gw.cpu_pct, {}),
      meter("RAM", gw.mem_pct, {}));
  }

  // ---- ports with errors / drops
  const portPanel = el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "PORT ISSUES — ERRORS & DROPS"));
  const issues = insights.port_issues || [];
  if (!insights.error && !issues.length) {
    portPanel.append(el("div", { class: "pill ok" }, "● NO PORT ERRORS OR DROPS"));
  } else if (issues.length) {
    portPanel.append(
      el("div", { class: "mono-dim", style: "margin-bottom:8px" },
        "errors usually mean cabling/SFP/negotiation faults; modest drop counts can be benign"),
      el("div", { class: "table-wrap", style: "max-height:250px;overflow-y:auto" },
        el("table", {},
          el("thead", {}, el("tr", {},
            ...["DEVICE", "PORT", "LINK", ">RX ERR", ">TX ERR", ">DROPS"].map(h =>
              el("th", { class: h.startsWith(">") ? "num" : "" }, h.replace(/^>/, ""))))),
          el("tbody", {}, ...issues.map(p => {
            const errTotal = p.rx_errors + p.tx_errors;
            return el("tr", {},
              el("td", { class: "strong" }, p.device),
              el("td", {}, p.port),
              el("td", {}, p.up ? `${p.speed ?? "?"} Mbps` : el("span", { class: "pill neutral" }, "DOWN")),
              el("td", { class: "num", style: errTotal > 1000 ? "color:var(--critical);font-weight:600" : errTotal ? "color:var(--warning)" : "" }, p.rx_errors.toLocaleString()),
              el("td", { class: "num", style: p.tx_errors ? "color:var(--warning)" : "" }, p.tx_errors.toLocaleString()),
              el("td", { class: "num" }, p.drops.toLocaleString()));
          })))));
  }

  const updates = insights.updates || [];
  const devRows = () => devices.map(d => el("tr", { "data-k": `${d.name} ${d.ip} ${d.model}`.toLowerCase() },
    el("td", { class: "strong" }, d.name || "—"),
    el("td", {}, `${d.model || ""} ${d.type ? `(${d.type})` : ""}`),
    el("td", {}, d.ip || "—"),
    el("td", {}, statePill(d.state)),
    el("td", {}, d.upgradable
      ? el("span", { title: d.upgrade_to ? `→ ${d.upgrade_to}` : "" },
          actionBtn("⬆ UPDATE", () => api.unifiUpgrade(d.mac), { accent: true, toast }))
      : el("span", { class: "mono-dim" }, "current")),
    el("td", { class: "num" }, d.clients ?? "—"),
    el("td", { class: "num" }, d.cpu != null ? `${d.cpu}%` : "—"),
    el("td", { class: "num" }, fmtUptime(d.uptime)),
    el("td", {}, el("div", { class: "actions" },
      actionBtn("RESTART", () => api.unifiRestart(d.mac), { danger: true, toast })))));

  const cliRows = () => clients.map(c => el("tr", { "data-k": `${c.name} ${c.ip} ${c.essid || ""}`.toLowerCase() },
    el("td", { class: "strong" }, c.name || "—"),
    el("td", {}, c.ip || "—"),
    el("td", {}, c.wired ? "WIRED" : `WIFI · ${c.essid || "?"}`),
    el("td", {}, c.network || "—"),
    el("td", { class: "num" }, c.signal != null ? `${c.signal} dBm` : "—"),
    el("td", { class: "num" }, fmtUptime(c.uptime))));

  const devPanel = el("div", { class: "panel" },
    el("div", { class: "panel-title" },
      `UNIFI DEVICES — ${devices.length}`,
      updates.length ? el("span", { class: "pill warn", style: "margin-left:4px" },
        `${updates.length} FIRMWARE UPDATE${updates.length > 1 ? "S" : ""} AVAILABLE`) : ""),
    tableWrap(["NAME", "MODEL", "IP", "STATE", "FIRMWARE", ">CLIENTS", ">CPU", ">UPTIME", ""], devRows()));

  const cliPanel = el("div", { class: "panel" },
    el("div", { class: "panel-title" }, `CLIENTS — ${clients.length}`),
    tableWrap(["NAME", "IP", "CONNECTION", "NETWORK", ">SIGNAL", ">UPTIME"], cliRows()));

  body.append(
    toolbar("filter devices & clients…", q => filterRows(body, q)),
    el("div", { style: "display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:14px" },
      gwPanel, portPanel),
    el("div", { class: "section-gap" }),
    eventsPanel(eventsRes, anomaliesRes.anomalies || [], toast),
    el("div", { class: "section-gap" }),
    devPanel, el("div", { class: "section-gap" }), cliPanel);
}

// ------------------------------------------------- NETWORK: events & IDS

const EVT_FILTERS = [
  { label: "THREATS & SECURITY", cats: ["SECURITY"] },
  { label: "CLIENT EVENTS", cats: ["CLIENT_DEVICES"] },
  { label: "ALL EVENTS", cats: null },
];

function eventsPanel(initial, anomalies, toast) {
  const panel = el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "EVENTS & THREATS — GATEWAY LOG"));
  if (initial.error) {
    panel.append(el("div", { class: "mono-dim" }, `⚠ ${initial.error}`));
    return panel;
  }

  let cats = EVT_FILTERS[0].cats;
  let page = 0;
  let seq = 0;  // request token — a filter switch invalidates in-flight loads
  const tbody = el("tbody", {});
  const countEl = el("span", { class: "mono-dim" }, "");

  // ---- filter chips — client anomalies is a tab like the rest
  const setActive = (b) => allChips.forEach(c => c.className = `btn btn-mini ${c === b ? "" : "btn-ghost"}`);
  const chips = EVT_FILTERS.map((f, i) => {
    const b = el("button", { class: `btn btn-mini ${i === 0 ? "" : "btn-ghost"}` }, f.label);
    b.addEventListener("click", () => {
      setActive(b);
      anomWrap.style.display = "none";
      eventsWrap.style.display = "";
      cats = f.cats;
      page = 0;
      load(true);
    });
    return b;
  });
  const anomChip = el("button", { class: "btn btn-mini btn-ghost" },
    `CLIENT ANOMALIES${anomalies.length ? ` (${anomalies.length})` : ""}`);
  anomChip.addEventListener("click", () => {
    setActive(anomChip);
    seq += 1;  // drop any in-flight event load
    eventsWrap.style.display = "none";
    anomWrap.style.display = "";
    countEl.textContent = `${anomalies.length} anomalous client${anomalies.length === 1 ? "" : "s"}`;
  });
  const allChips = [...chips, anomChip];

  const moreBtn = el("button", { class: "btn btn-mini btn-ghost" }, "LOAD OLDER ▾");
  moreBtn.addEventListener("click", () => { page += 1; load(false); });

  async function load(fresh) {
    const my = ++seq;
    moreBtn.disabled = true;
    if (fresh) {
      tbody.replaceChildren(el("tr", {},
        el("td", { colspan: "5", class: "mono-dim" }, "… loading events")));
      countEl.textContent = "";
    }
    try {
      const r = await api.unifiEvents({ categories: cats, page });
      if (my !== seq) return;  // a newer filter/page superseded this request
      if (fresh) tbody.replaceChildren();
      addRows(r.events);
      countEl.textContent = `${r.total ?? "?"} total · page ${page + 1}/${r.pages ?? "?"}`;
      moreBtn.style.display = r.pages && page + 1 >= r.pages ? "none" : "";
    } catch (e) {
      if (my !== seq) return;
      if (fresh) tbody.replaceChildren(el("tr", {},
        el("td", { colspan: "5", class: "mono-dim" }, `⚠ ${e.message}`)));
      toast(String(e.message || e), "err", "EVENTS");
    } finally {
      if (my === seq) moreBtn.disabled = false;
    }
  }

  function addRows(events) {
    for (const ev of events) {
      const sevCls = ev.severity === "HIGH" ? "err" : ev.severity === "MEDIUM" ? "warn" : "neutral";
      const triageBtn = el("button", { class: "btn btn-mini btn-ghost" }, "◈ TRIAGE");
      const row = el("tr", {},
        el("td", { class: "mono-dim", style: "white-space:nowrap" },
          new Date(ev.ts * 1000).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })),
        el("td", {}, el("span", { class: `pill ${sevCls}` }, ev.severity || "?")),
        el("td", { class: "mono-dim" }, (ev.subcategory || ev.category || "").replace(/^SECURITY_?/, "").replace(/_/g, " ").toLowerCase() || "—"),
        el("td", {}, ev.message || ev.title || ev.event),
        el("td", {}, triageBtn));
      tbody.append(row);

      triageBtn.addEventListener("click", async () => {
        triageBtn.disabled = true;
        const out = el("td", { colspan: "5" },
          el("div", { class: "ai-running" }, el("div", { class: "spinner" }),
            "Claude is triaging this event…"));
        const detail = el("tr", {}, out);
        row.after(detail);
        try {
          const r = await api.unifiTriage(ev.raw || ev);
          out.replaceChildren(triageCard(r));
          triageBtn.textContent = "✓ TRIAGED";
        } catch (e) {
          detail.remove();
          triageBtn.disabled = false;
          toast(String(e.message || e), "err", "TRIAGE FAILED");
        }
      });
    }
  }

  // ---- anomalies tab content (stat/anomalies — fetched once per render)
  const anomWrap = el("div", { style: "display:none" },
    anomalies.length
      ? el("div", { class: "table-wrap", style: "max-height:420px;overflow-y:auto" },
          el("table", {},
            el("thead", {}, el("tr", {}, ...["CLIENT", "ANOMALY", ">COUNT", "LAST SEEN"].map(h =>
              el("th", { class: h.startsWith(">") ? "num" : "" }, h.replace(/^>/, ""))))),
            el("tbody", {}, ...anomalies.map(a => el("tr", {},
              el("td", { class: "strong" }, a.client),
              el("td", {}, (a.anomaly || "").replace(/_/g, " ").toLowerCase()),
              el("td", { class: "num" }, a.count),
              el("td", { class: "mono-dim" }, a.last_ts
                ? new Date(a.last_ts * 1000).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })
                : "—"))))))
      : el("div", { class: "pill ok" }, "● NO CLIENT ANOMALIES DETECTED"));

  const eventsWrap = el("div", {},
    el("div", { class: "table-wrap", style: "max-height:420px;overflow-y:auto" },
      el("table", {},
        el("thead", {}, el("tr", {}, ...["TIME", "SEVERITY", "TYPE", "EVENT", ""].map(h => el("th", {}, h)))),
        tbody)),
    el("div", { style: "margin-top:8px" }, moreBtn));

  panel.append(
    el("div", { class: "ops-toolbar", style: "margin-bottom:8px" },
      ...allChips, el("div", { class: "spacer" }), countEl),
    eventsWrap,
    anomWrap);

  addRows(initial.events || []);
  countEl.textContent = `${initial.total ?? "?"} total · page 1/${initial.pages ?? "?"}`;
  if (initial.pages && initial.pages <= 1) moreBtn.style.display = "none";
  return panel;
}

function triageCard(r) {
  const lvl = r.threat_level || "?";
  const cls = ["high", "critical"].includes(lvl) ? "err" : lvl === "medium" ? "warn" : "ok";
  return el("div", { class: "finding", style: "margin:4px 0" },
    el("div", { class: "finding-head" },
      el("span", { class: `pill ${cls}` }, `THREAT: ${lvl.toUpperCase()}`),
      el("span", { class: `pill ${r.action_needed ? "warn" : "ok"}` },
        r.action_needed ? "ACTION NEEDED" : "NO ACTION NEEDED"),
      el("span", { class: "finding-title" }, r.summary || "")),
    el("div", { class: "finding-detail" }, r.explanation || ""),
    el("div", { class: "finding-fix" }, r.recommendation || ""),
    r._usage ? el("div", { class: "mono-dim", style: "margin-top:4px;font-size:10px" },
      `claude-opus-4-8 · ${r._usage.input_tokens} in / ${r._usage.output_tokens} out tokens`) : null);
}

// ------------------------------------------------------------ COMPUTE

async function compute(body, toast) {
  const [{ nodes }, { guests }, storageRes, perfRes, disksRes] = await Promise.all([
    api.proxmoxNodes(), api.proxmoxGuests(),
    api.proxmoxStorage().catch(e => ({ error: String(e.message || e) })),
    api.proxmoxPerf().catch(e => ({ error: String(e.message || e) })),
    api.proxmoxDisks().catch(e => ({ error: String(e.message || e) })),
  ]);

  const nodePanel = el("div", { class: "panel" },
    el("div", { class: "panel-title" }, `NODES — ${nodes.length}`));
  for (const n of nodes) {
    nodePanel.append(
      el("div", { style: "display:flex;gap:10px;align-items:center;margin-top:6px" },
        statePill(n.status, ["online"]),
        el("b", { style: "letter-spacing:.1em" }, n.node),
        el("span", { class: "mono-dim" }, `up ${fmtUptime(n.uptime)}`)),
      meter("CPU", n.cpu != null ? n.cpu * 100 : null, { detail: `${n.maxcpu} cores` }),
      meter("MEM", n.maxmem ? 100 * n.mem / n.maxmem : null, { detail: `${fmtBytes(n.mem)} / ${fmtBytes(n.maxmem)}` }));
  }

  const guestRows = guests.map(g => {
    const running = g.status === "running";
    const memPct = running && g.maxmem ? 100 * g.mem / g.maxmem : null;
    const row = el("tr", { "data-k": `${g.name} ${g.vmid} ${g.type} ${g.node}`.toLowerCase(), style: "cursor:pointer", title: "click for extended stats & graphs" },
      el("td", { class: "num" }, g.vmid),
      el("td", { class: "strong" }, g.name || "—"),
      el("td", {}, el("span", { class: "pill neutral" }, g.type === "qemu" ? "VM" : "LXC")),
      el("td", {}, g.node),
      el("td", {}, statePill(g.status)),
      el("td", { class: "num" }, running && g.cpu != null ? `${(g.cpu * 100).toFixed(1)}%` : "—"),
      el("td", { class: "num", title: running ? `${fmtBytes(g.mem)} of ${fmtBytes(g.maxmem)} allocated` : "" },
        memPct != null ? `${memPct.toFixed(0)}%` : "—"),
      el("td", { class: "num" }, running ? fmtUptime(g.uptime) : "—"),
      el("td", {}, el("div", { class: "actions" },
        running
          ? [actionBtn("REBOOT", () => api.proxmoxAction(g.node, g.type, g.vmid, "reboot"), { danger: true, toast }),
             actionBtn("SHUTDOWN", () => api.proxmoxAction(g.node, g.type, g.vmid, "shutdown"), { danger: true, toast }),
             actionBtn("STOP", () => api.proxmoxAction(g.node, g.type, g.vmid, "stop"), { danger: true, toast })]
          : [actionBtn("START", () => api.proxmoxAction(g.node, g.type, g.vmid, "start"), { confirm: false, toast })])));
    row.addEventListener("click", () => toggleGuestDetail(row, g));
    return row;
  });

  // ---- datastores
  const storagePanel = el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "DATASTORES"));
  if (storageRes.error) {
    storagePanel.append(el("div", { class: "mono-dim" }, `⚠ ${storageRes.error}`));
  } else {
    for (const s of storageRes.storage || []) {
      storagePanel.append(
        el("div", { style: "display:flex;gap:10px;align-items:baseline;margin-top:8px" },
          statePill(s.status, ["available", "active"]),
          el("b", { style: "letter-spacing:.06em" }, s.storage),
          el("span", { class: "mono-dim" }, `${s.plugintype}${s.shared ? " · shared" : ""} · ${s.content}`)),
        meter("USED", s.pct, { detail: `${fmtBytes(s.used)} / ${fmtBytes(s.total)}` }));
    }
  }

  // ---- node performance (last hour, from PVE RRD)
  const perfPanel = el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "NODE PERFORMANCE — LAST HOUR"));
  if (perfRes.error) {
    perfPanel.append(el("div", { class: "mono-dim" }, `⚠ ${perfRes.error}`));
  } else {
    const grid = el("div", { style: "display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:0 26px" });
    for (const [node, s] of Object.entries(perfRes.perf || {})) {
      const col = el("div", {},
        el("div", { style: "letter-spacing:.1em;color:var(--ink);margin-top:6px" }, `▣ ${node}`),
        sparkRow("CPU", s.cpu_pct, { color: BY_ID.proxmox.hex, format: v => `${v.toFixed(1)}%` }),
        sparkRow("IO DELAY", s.iowait_pct, { color: "#c98500", format: v => `${v.toFixed(2)}%` }),
        sparkRow("LOAD", s.load, { color: "#9085e9", format: v => v.toFixed(2) }),
        sparkRow("NET IN", s.net_in_bps, { color: "#3987e5", format: v => fmtBytes(v, true) }),
        sparkRow("NET OUT", s.net_out_bps, { color: "#e66767", format: v => fmtBytes(v, true) }));
      grid.append(col);
    }
    perfPanel.append(grid);
  }

  body.append(
    toolbar("filter guests…", q => filterRows(body, q)),
    nodePanel,
    el("div", { class: "section-gap" }),
    el("div", { style: "display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:14px" }, storagePanel, perfPanel),
    el("div", { class: "section-gap" }),
    diskPanel(disksRes, toast),
    el("div", { class: "section-gap" }),
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, `VIRTUAL MACHINES & CONTAINERS — ${guests.length}`),
      tableWrap([">VMID", "NAME", "TYPE", "NODE", "STATE", ">CPU", ">MEM", ">UPTIME", ""], guestRows)));
}

// ---------------------------------------------- COMPUTE: guest detail row

function toggleGuestDetail(row, g) {
  if (row._detail) { row._detail.remove(); row._detail = null; return; }
  const td = el("td", { colspan: "9" },
    el("div", { class: "ai-running" }, el("div", { class: "spinner" }), "loading guest stats…"));
  const tr = el("tr", {}, td);
  row.after(tr);
  row._detail = tr;
  Promise.all([
    api.proxmoxGuestDetail(g.node, g.type, g.vmid),
    api.proxmoxGuestRrd(g.node, g.type, g.vmid),
  ]).then(([d, { rrd }]) => {
    if (row._detail === tr) td.replaceChildren(guestDetailView(g, d, rrd));
  }).catch(e => {
    if (row._detail === tr) td.replaceChildren(el("div", { class: "setup-result err" }, `✕ ${e.message}`));
  });
}

// The honest "does it need more RAM?" answer. Kernel PSI is ground truth:
// stalls mean real pressure; high occupancy alone is usually page cache.
function ramVerdict(d) {
  const some = d.pressure?.memorysome;
  const bal = d.balloon;
  const free = bal?.free ?? d.freemem;
  const total = bal?.total || d.maxmem;
  const freePct = free != null && total ? 100 * free / total : null;
  const cacheNote = freePct != null && freePct < 15
    ? ` Only ${fmtBytes(free)} is literally free inside the guest, but the rest of the "used" figure is mostly Linux page cache — reclaimed instantly when applications need it.`
    : freePct != null ? ` ${fmtBytes(free)} (${freePct.toFixed(0)}%) is free inside the guest.` : "";
  if (some != null) {
    if (some >= 5) return { cls: "err", head: "MEMORY PRESSURE — MORE RAM WOULD HELP",
      text: `Processes in this guest are stalled waiting for memory ${some.toFixed(1)}% of the time (kernel PSI). That is real pressure, not cache — allocate more RAM or move workloads off.` };
    if (some >= 0.5) return { cls: "warn", head: "MILD MEMORY PRESSURE",
      text: `Kernel PSI shows ${some.toFixed(2)}% of time stalled on memory — occasional pressure. Watch the pressure graph; sustained growth means it needs more RAM.${cacheNote}` };
    return { cls: "ok", head: "NO REAL MEMORY PRESSURE — RAM IS SUFFICIENT",
      text: `Kernel PSI shows ${some.toFixed(2)}% of time stalled on memory — effectively none. This guest does not need more RAM.${cacheNote}` };
  }
  if (bal?.free != null) {
    return freePct >= 20
      ? { cls: "ok", head: "LOOKS COMFORTABLE", text: `No PSI data, but ${freePct.toFixed(0)}% of guest memory is literally free.` }
      : { cls: "warn", head: "INCONCLUSIVE", text: `Free memory inside the guest is low (${fmtBytes(free)}), but that may just be page cache. Check inside with: free -m (look at "available").` };
  }
  return { cls: "warn", head: "LIMITED VISIBILITY",
    text: "No balloon/agent stats — the host-side figure includes guest disk cache, so it usually overstates real usage. Enable the QEMU guest agent + balloon device for the inside view." };
}

function guestDetailView(g, d, rrd) {
  const bal = d.balloon;
  const hostMem = d.memhost ?? d.mem;
  const insideUsed = bal?.total && bal.free != null ? bal.total - bal.free : null;
  const v = ramVerdict(d);

  const pills = el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px" },
    el("span", { class: "pill neutral" }, `${d.cpus ?? "?"} vCPU`),
    el("span", { class: "pill neutral" }, `up ${fmtUptime(d.uptime)}`),
    g.type === "qemu" ? el("span", { class: `pill ${d.agent ? "ok" : "neutral"}` },
      d.agent ? "GUEST AGENT ✓" : "NO GUEST AGENT") : null,
    bal?.swapped_out ? el("span", { class: "pill warn" }, `swapped out ${fmtBytes(bal.swapped_out)}`) : null);

  const memCol = el("div", {},
    meter("RAM — GUEST VIEW", d.maxmem ? 100 * d.mem / d.maxmem : null,
      { detail: `${fmtBytes(d.mem)} / ${fmtBytes(d.maxmem)}` }),
    hostMem !== d.mem ? meter("RAM — HOST VIEW (INCL. CACHE)", d.maxmem ? 100 * hostMem / d.maxmem : null,
      { detail: fmtBytes(hostMem) }) : null,
    insideUsed != null ? meter("INSIDE GUEST — NON-FREE", 100 * insideUsed / bal.total,
      { detail: `${fmtBytes(bal.free)} free` }) : null,
    g.type === "lxc" && d.maxswap ? meter("SWAP", 100 * (d.swap || 0) / d.maxswap,
      { detail: `${fmtBytes(d.swap)} / ${fmtBytes(d.maxswap)}` }) : null,
    g.type === "lxc" && d.maxdisk ? meter("ROOTFS", 100 * (d.disk || 0) / d.maxdisk,
      { detail: `${fmtBytes(d.disk)} / ${fmtBytes(d.maxdisk)}` }) : null,
    el("div", { style: "margin-top:10px" },
      el("span", { class: `pill ${v.cls}` }, v.head),
      el("div", { class: "mono-dim", style: "margin-top:6px;line-height:1.6" }, v.text)));

  const loadCol = el("div", {},
    sparkRow("CPU", rrd.cpu_pct, { color: BY_ID.proxmox.hex, format: x => `${x.toFixed(1)}%` }),
    sparkRow("RAM", rrd.mem_pct, { color: "#9085e9", format: x => `${x.toFixed(1)}%` }),
    sparkRow("MEM PRESSURE (PSI)", rrd.mem_pressure_pct, { color: "#e66767", format: x => `${x.toFixed(2)}%` }),
    sparkRow("IO PRESSURE (PSI)", rrd.io_pressure_pct, { color: "#c98500", format: x => `${x.toFixed(2)}%` }));

  const ioCol = el("div", {},
    sparkRow("DISK READ", rrd.disk_read_bps, { color: "#3987e5", format: x => fmtBytes(x, true) }),
    sparkRow("DISK WRITE", rrd.disk_write_bps, { color: "#56c8d8", format: x => fmtBytes(x, true) }),
    sparkRow("NET IN", rrd.net_in_bps, { color: "#30b48a", format: x => fmtBytes(x, true) }),
    sparkRow("NET OUT", rrd.net_out_bps, { color: "#ffb347", format: x => fmtBytes(x, true) }));

  return el("div", { style: "padding:8px 4px 12px" },
    pills,
    el("div", { style: "display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:0 26px" },
      memCol,
      el("div", {}, el("div", { class: "mono-dim", style: "margin:6px 0" }, "LAST HOUR"), loadCol),
      el("div", {}, el("div", { class: "mono-dim", style: "margin:6px 0" }, "I/O — LAST HOUR"), ioCol)));
}

// -------------------------------------------------- COMPUTE: disk health

function diskPanel(res, toast) {
  const panel = el("div", { class: "panel" });
  const title = el("div", { class: "panel-title" }, "DISK HEALTH — SMART");
  panel.append(title);
  if (res.error) {
    panel.append(el("div", { class: "mono-dim" }, `⚠ ${res.error}`));
    return panel;
  }
  const disks = res.disks || [];
  const checked = res.ts ? new Date(res.ts * 1000).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";

  const refreshBtn = el("button", { class: "btn btn-mini btn-ghost" }, "⟳ RE-CHECK");
  refreshBtn.addEventListener("click", async () => {
    refreshBtn.disabled = true;
    refreshBtn.textContent = "… CHECKING";
    try {
      await api.proxmoxDisksRefresh();
      location.reload();
    } catch (e) {
      toast(String(e.message || e), "err", "SMART");
      refreshBtn.disabled = false;
      refreshBtn.textContent = "⟳ RE-CHECK";
    }
  });
  title.append(
    el("span", { class: `pill ${disks.some(d => d.status === "fail") ? "err" : disks.some(d => d.status === "warn") ? "warn" : "ok"}` },
      disks.every(d => d.status === "ok") ? "● ALL HEALTHY" : disks.filter(d => d.status !== "ok").length + " NEED ATTENTION"),
    el("span", { class: "mono-dim", style: "margin-left:auto;letter-spacing:0" }, `checked ${checked}`),
    refreshBtn);

  const rows = disks.map(d => {
    const cls = d.status === "fail" ? "err" : d.status === "warn" ? "warn" : "ok";
    const temp = d.detail?.temperature;
    const wear = d.wearout != null ? `${100 - d.wearout}%` :
      d.detail?.percentage_used != null ? `${d.detail.percentage_used}%` : "—";
    const hours = d.detail?.power_on_hours;
    return el("tr", {},
      el("td", {}, el("span", { class: `pill ${cls}` }, d.status === "ok" ? "● OK" : d.status.toUpperCase())),
      el("td", { class: "strong" }, d.devpath),
      el("td", {}, `${d.model || "?"}`),
      el("td", {}, el("span", { class: "pill neutral" }, (d.type || "?").toUpperCase()), ` ${d.used_as || ""}`),
      el("td", { class: "num" }, fmtBytes(d.size)),
      el("td", { class: "num", style: temp >= 60 ? "color:var(--warning)" : "" }, temp != null ? `${temp}°C` : "—"),
      el("td", { class: "num" }, wear),
      el("td", { class: "num" }, hours != null ? `${Math.round(hours / 24)}d` : "—"),
      el("td", { class: d.issues?.length ? "" : "mono-dim" },
        d.issues?.length ? d.issues.join("; ") : "no issues"));
  });

  panel.append(
    el("div", { class: "mono-dim", style: "margin-bottom:8px" },
      "wear = endurance used (NVMe percentage-used / PVE wearout). Checked every 6 h; status changes alert via your notification channels."),
    tableWrap(["", "DEVICE", "MODEL", "TYPE", ">SIZE", ">TEMP", ">WEAR", ">POWER-ON", "ISSUES"], rows));
  return panel;
}

// ------------------------------------------------------------ CONTAINERS

async function containers(body, toast, overview) {
  const [{ containers }, scanRootsRes, updatesRes] = await Promise.all([
    api.dockerContainers(), api.scanRoots().catch(() => ({ roots: [] })),
    api.dockerUpdates().catch(e => ({ images: [], error: String(e.message || e) })),
  ]);

  // image ref → update status ("update" | "current" | "local" | "error")
  const normRef = r => r && !r.includes("@sha256:") && !r.split("/").pop().includes(":") ? `${r}:latest` : r;
  const updByRef = Object.fromEntries((updatesRes.images || []).map(i => [i.ref, i]));
  const updates = (updatesRes.images || []).filter(i => i.status === "update");

  const h = overview?.systems?.docker?.data?.host;
  let hostPanel = null;
  if (h) {
    // single full-width stack: CPU, RAM, GPU, VRAM, then disks
    hostPanel = el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "DOCKER HOST VITALS — VIA GLANCES"),
      meter("CPU", h.cpu_pct, {}),
      meter("RAM", h.mem_pct, { detail: h.mem_total ? `${fmtBytes(h.mem_used)} / ${fmtBytes(h.mem_total)}` : "" }),
      ...(h.gpus || []).flatMap(g => [
        meter(`GPU ${g.name || ""}`.trim(), g.util_pct,
          { detail: g.temp != null ? `${g.temp}°C` : "" }),
        meter("GPU VRAM", g.mem_pct, {}),
      ]),
      ...(h.disks || []).map(d =>
        meter(`DISK ${d.mount}`, d.pct, { detail: `${fmtBytes(d.used)} / ${fmtBytes(d.total)}` })));
  } else if (overview?.systems?.docker?.data?.host_error) {
    hostPanel = el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "DOCKER HOST VITALS"),
      el("div", { class: "mono-dim", style: "color:var(--warning)" },
        `⚠ ${overview.systems.docker.data.host_error}`));
  }

  const makeRow = c => {
    const running = c.state === "running";
    const upd = updByRef[normRef(c.image)];
    return el("tr", { "data-k": `${c.name} ${c.image} ${c.compose_project || ""}`.toLowerCase() },
      el("td", { class: "strong" }, c.name),
      el("td", {}, c.compose_project || "—"),
      el("td", { class: "mono-dim" }, c.image,
        upd?.status === "update" ? el("span", { class: "pill warn", style: "margin-left:6px" }, "⬆ UPDATE") : "",
        upd?.status === "error" ? el("span", { class: "pill neutral", style: "margin-left:6px", title: upd.error || "" }, "?") : ""),
      el("td", {}, statePill(c.state)),
      el("td", {}, c.status || "—"),
      el("td", {}, (c.ports || []).join(", ") || "—"),
      el("td", {}, el("div", { class: "actions" },
        running
          ? [actionBtn("RESTART", () => api.dockerAction(c.id, "restart"), { danger: true, toast }),
             actionBtn("STOP", () => api.dockerAction(c.id, "stop"), { danger: true, toast })]
          : [actionBtn("START", () => api.dockerAction(c.id, "start"), { confirm: false, toast })])));
  };

  // ---- container view vs stack view (grouped by compose project)
  const FLEET_HEADERS = ["NAME", "PROJECT", "IMAGE", "STATE", "STATUS", "PORTS", ""];
  let viewMode = localStorage.getItem("claudeos-fleet-view") || "containers";
  const fleetBody = el("div", {});

  function stackView() {
    const groups = new Map();
    for (const c of containers) {
      const key = c.compose_project || "";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(c);
    }
    // named stacks alphabetical, loose containers last
    const keys = [...groups.keys()].sort((a, b) =>
      a === "" ? 1 : b === "" ? -1 : a.localeCompare(b));
    const trs = [];
    for (const key of keys) {
      const cs = groups.get(key);
      const runningN = cs.filter(c => c.state === "running").length;
      const updN = cs.filter(c => updByRef[normRef(c.image)]?.status === "update").length;
      const chev = el("span", { class: "mono-dim", style: "width:12px" }, "▸");
      const children = cs.map(c => { const r = makeRow(c); r.style.display = "none"; return r; });
      const head = el("tr", { class: "stack-head" },
        el("td", { colspan: "7" },
          el("div", { style: "display:flex;align-items:center;gap:10px" },
            chev,
            el("b", { style: "letter-spacing:.08em" }, key || "STANDALONE CONTAINERS"),
            el("span", { class: "mono-dim" }, `${cs.length} container${cs.length === 1 ? "" : "s"}`),
            el("span", { class: `pill ${runningN === cs.length ? "ok" : runningN ? "warn" : "neutral"}` },
              `${runningN}/${cs.length} RUNNING`),
            updN ? el("span", { class: "pill warn" }, `⬆ ${updN} UPDATE${updN > 1 ? "S" : ""}`) : "")));
      head.addEventListener("click", () => {
        const open = chev.textContent === "▾";
        chev.textContent = open ? "▸" : "▾";
        children.forEach(r => { r.style.display = open ? "none" : ""; });
      });
      trs.push(head, ...children);
    }
    return tableWrap(FLEET_HEADERS, trs);
  }

  const viewChips = [["containers", "CONTAINERS"], ["stacks", "STACKS"]].map(([mode, label]) => {
    const b = el("button", { class: "btn btn-mini btn-ghost" }, label);
    b.addEventListener("click", () => {
      viewMode = mode;
      localStorage.setItem("claudeos-fleet-view", mode);
      renderView();
    });
    return { mode, b };
  });

  function renderView() {
    for (const c of viewChips) c.b.className = `btn btn-mini ${c.mode === viewMode ? "" : "btn-ghost"}`;
    fleetBody.replaceChildren(viewMode === "stacks"
      ? stackView()
      : tableWrap(FLEET_HEADERS, containers.map(makeRow)));
  }
  renderView();

  // ---- storage analysis (on demand — docker system df can take a moment)
  const storageOut = el("div", {});
  const analyseBtn = el("button", { class: "btn" }, "▤ ANALYSE STORAGE");
  analyseBtn.addEventListener("click", async () => {
    analyseBtn.disabled = true;
    storageOut.replaceChildren(el("div", { class: "ai-running" },
      el("div", { class: "spinner" }), "measuring images, containers and volumes…"));
    try {
      const r = await api.dockerStorage();
      storageOut.replaceChildren(storageReport(r));
    } catch (e) {
      storageOut.replaceChildren(el("div", { class: "setup-result err" }, `✕ ${e.message}`));
    } finally {
      analyseBtn.disabled = false;
    }
  });

  // ---- host folder scans (per bind-mounted /scan/<name> root)
  const scanBtns = (scanRootsRes.roots || []).map(r => {
    const btn = el("button", { class: "btn" }, `⌂ SCAN /${r.name}`);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      storageOut.replaceChildren(el("div", { class: "ai-running" },
        el("div", { class: "spinner" }),
        `walking ${r.name} — large folders can take a minute or two…`));
      try {
        const res = await api.scanFolder(r.path);
        storageOut.replaceChildren(folderScanReport(r.name, res));
      } catch (e) {
        storageOut.replaceChildren(el("div", { class: "setup-result err" }, `✕ ${e.message}`));
      } finally {
        btn.disabled = false;
      }
    });
    return btn;
  });

  // ---- image update summary + on-demand re-check
  const checkBtn = el("button", { class: "btn" }, "⬆ CHECK UPDATES");
  checkBtn.addEventListener("click", async () => {
    checkBtn.disabled = true;
    checkBtn.textContent = "… CHECKING REGISTRIES";
    try {
      await api.dockerUpdatesRefresh();
      location.reload();
    } catch (e) {
      toast(String(e.message || e), "err", "UPDATE CHECK");
      checkBtn.disabled = false;
      checkBtn.textContent = "⬆ CHECK UPDATES";
    }
  });

  const updatePill = updatesRes.error
    ? el("span", { class: "pill neutral", title: updatesRes.error }, "UPDATE CHECK UNAVAILABLE")
    : updates.length
      ? el("span", { class: "pill warn", title: updates.map(u => u.ref).join("\n") },
          `⬆ ${updates.length} IMAGE UPDATE${updates.length > 1 ? "S" : ""} AVAILABLE`)
      : updatesRes.ts
        ? el("span", { class: "pill ok" }, "● IMAGES CURRENT")
        : el("span", { class: "pill neutral" }, "UPDATES NOT CHECKED YET");

  const running = containers.filter(c => c.state === "running").length;
  body.append(
    // clearing the filter re-renders the view so collapsed stacks reset cleanly;
    // typing reveals matching rows even inside collapsed stacks
    toolbar("filter containers…", q => q ? filterRows(body, q) : renderView(),
      [...scanBtns, analyseBtn, checkBtn]),
    ...(hostPanel ? [hostPanel, el("div", { class: "section-gap" })] : []),
    storageOut,
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, `DOCKER FLEET — ${running}/${containers.length} RUNNING`,
        ...viewChips.map(c => c.b), updatePill,
        updatesRes.ts ? el("span", { class: "mono-dim", style: "margin-left:auto;letter-spacing:0" },
          `registry check ${new Date(updatesRes.ts * 1000).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}`) : null),
      fleetBody));
}

function storageReport(r) {
  const t = r.totals, rec = r.reclaimable;
  const wrap = el("div", { class: "panel", style: "margin-bottom:14px" },
    el("div", { class: "panel-title" }, "DOCKER STORAGE ANALYSIS"),
    el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px" },
      el("span", { class: "pill neutral" }, `images ${fmtBytes(t.images)}`),
      el("span", { class: "pill neutral" }, `container layers ${fmtBytes(t.containers)}`),
      el("span", { class: "pill neutral" }, `volumes ${fmtBytes(t.volumes)}`),
      el("span", { class: "pill neutral" }, `build cache ${fmtBytes(t.build_cache)}`),
      el("span", { class: `pill ${(rec.unused_images + rec.unused_volumes + rec.build_cache) > 5e9 ? "warn" : "ok"}` },
        `♻ reclaimable ~${fmtBytes(rec.unused_images + rec.unused_volumes + rec.build_cache)} (docker system prune)`)));

  const cols = el("div", { style: "display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:0 24px" });
  const section = (title, items, render) => {
    const c = el("div", {}, el("div", { class: "mono-dim", style: "margin:6px 0" }, title));
    const max = Math.max(...items.map(i => i._size), 1);
    for (const i of items) c.append(barRow(render(i), i._size, max, i._dim));
    if (!items.length) c.append(el("div", { class: "mono-dim" }, "none"));
    return c;
  };
  cols.append(
    section("LARGEST IMAGES",
      r.images.map(i => ({ ...i, _size: i.size, _dim: !i.in_use })),
      i => `${i.tag}${i.in_use ? "" : "  (unused)"}`),
    section("CONTAINER WRITABLE LAYERS",
      r.containers.filter(c => c.rw_size > 0).map(c => ({ ...c, _size: c.rw_size })),
      c => c.name),
    section("VOLUMES",
      r.volumes.filter(v => v.size > 0).map(v => ({ ...v, _size: v.size, _dim: !v.in_use })),
      v => `${v.name.length > 28 ? v.name.slice(0, 28) + "…" : v.name}${v.in_use ? "" : "  (unused)"}`));
  wrap.append(cols);
  return wrap;
}

function folderScanReport(name, r) {
  const wrap = el("div", { class: "panel", style: "margin-bottom:14px" },
    el("div", { class: "panel-title" }, `HOST FOLDER SCAN — /${name}`),
    el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px" },
      el("span", { class: "pill neutral" }, `total ${fmtBytes(r.total)}`),
      el("span", { class: "pill neutral" }, `top ${r.dirs.length} folders shown (sizes include subfolders)`),
      r.skipped ? el("span", { class: "pill warn" }, `${r.skipped} unreadable entries skipped`) : null));

  const cols = el("div", { style: "display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:0 24px" });
  const dirCol = el("div", {}, el("div", { class: "mono-dim", style: "margin:6px 0" }, "LARGEST FOLDERS"));
  const maxDir = Math.max(...r.dirs.map(d => d.size), 1);
  for (const d of r.dirs) dirCol.append(barRow(d.path, d.size, maxDir));
  if (!r.dirs.length) dirCol.append(el("div", { class: "mono-dim" }, "none"));

  const fileCol = el("div", {}, el("div", { class: "mono-dim", style: "margin:6px 0" }, "LARGEST FILES"));
  const maxFile = Math.max(...r.files.map(f => f.size), 1);
  for (const f of r.files) fileCol.append(barRow(f.path, f.size, maxFile, true));
  if (!r.files.length) fileCol.append(el("div", { class: "mono-dim" }, "none"));

  cols.append(dirCol, fileCol);
  wrap.append(cols);
  return wrap;
}

function barRow(label, size, max, dim = false) {
  const row = el("div", { class: "meter" });
  const head = el("div", { class: "meter-label" },
    el("span", { style: dim ? "opacity:.55" : "" }, label),
    el("b", {}, fmtBytes(size)));
  const track = el("div", { class: "meter-track" },
    el("div", { class: "meter-fill", style: `width:${(100 * size / max).toFixed(1)}%;background:${dim ? "var(--ink-3)" : "var(--s-docker)"}` }));
  row.append(head, track);
  return row;
}

// ------------------------------------------------------------ HOME

async function home(body, toast) {
  const [{ entities }, sys, zha, updatesRes] = await Promise.all([
    api.haEntities(),
    api.haSystem().catch(e => ({ error: String(e.message || e) })),
    api.haZha().catch(e => ({ error: String(e.message || e) })),
    api.haUpdates().catch(e => ({ error: String(e.message || e) })),
  ]);

  body.append(systemPanel(sys));
  body.append(el("div", { class: "section-gap" }));
  body.append(updatesPanel(updatesRes));
  body.append(el("div", { class: "section-gap" }));
  body.append(zhaPanel(zha, toast));
  body.append(el("div", { class: "section-gap" }));
  body.append(aiLogPanel(toast));
  body.append(el("div", { class: "section-gap" }));
  const domains = [...new Set(entities.map(e => e.domain))].sort();

  let domainFilter = "";
  const select = el("select", { class: "search", style: "min-width:150px" },
    el("option", { value: "" }, "all domains"),
    ...domains.map(d => el("option", { value: d }, `${d} (${entities.filter(e => e.domain === d).length})`)));
  select.addEventListener("change", () => { domainFilter = select.value; apply(); });

  const input = el("input", { class: "search", type: "text", placeholder: "filter entities…" });
  let q = "";
  input.addEventListener("input", debounce(() => { q = input.value.toLowerCase(); apply(); }, 150));

  const rows = entities.map(e => {
    const row = el("tr", { "data-k": `${e.name} ${e.entity_id}`.toLowerCase(), "data-domain": e.domain },
      el("td", { class: "strong" }, e.name),
      el("td", { class: "mono-dim" }, e.entity_id),
      el("td", {}, statePill(e.state, ["on", "home", "ok"])),
      el("td", {}, e.unit ? `${e.state} ${e.unit}` : "—"),
      el("td", {}, el("div", { class: "actions" },
        e.toggleable
          ? actionBtn("TOGGLE", () => api.haService({ domain: "homeassistant", service: "toggle", entity_id: e.entity_id }), { confirm: false, toast })
          : null)));
    return row;
  });

  function apply() {
    for (const r of rows) {
      const matches = (!q || r.dataset.k.includes(q)) && (!domainFilter || r.dataset.domain === domainFilter);
      r.style.display = matches ? "" : "none";
    }
  }

  body.append(
    el("div", { class: "ops-toolbar" }, input, select, el("div", { class: "spacer" }),
      el("span", { class: "mono-dim" }, `${entities.length} entities`)),
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "HOME ASSISTANT ENTITIES"),
      tableWrap(["NAME", "ENTITY ID", "STATE", "VALUE", ""], rows)));
}

// ------------------------------------------------------------ HOME: panels

function systemPanel(sys) {
  const panel = el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "HAOS SYSTEM"));
  if (sys.error) {
    panel.append(el("div", { class: "mono-dim" }, `⚠ ${sys.error}`));
    return panel;
  }
  if (!sys.supervised) {
    panel.append(el("div", { class: "mono-dim" },
      "no supervisor detected (container/core install) — internal stats unavailable"));
    return panel;
  }
  if (!sys.core) {
    panel.append(el("div", { class: "pill warn", style: "white-space:normal;line-height:1.5;padding:8px 12px" },
      `⚠ ${sys.error || "supervisor stats unavailable"}`));
    return panel;
  }
  const cols = el("div", { style: "display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:0 24px" });
  const colA = el("div", {},
    meter("CORE CPU", sys.core.cpu_pct, {}),
    meter("CORE RAM", sys.core.mem_pct, {
      detail: sys.core.mem_total ? `${fmtBytes(sys.core.mem_used)} / ${fmtBytes(sys.core.mem_total)}` : "" }));
  if (sys.host?.disk_total) {
    // supervisor host info reports disk in GB
    colA.append(meter("HOST DISK", 100 * sys.host.disk_used / sys.host.disk_total,
      { detail: `${sys.host.disk_used} / ${sys.host.disk_total} GB` }));
  }
  const addons = sys.addons || [];
  const started = addons.filter(a => a.state === "started").length;
  const errored = addons.filter(a => !["started", "stopped"].includes(a.state)).length;
  const updates = addons.filter(a => a.update_available).length;
  const colB = el("div", {},
    el("div", { class: "mono-dim", style: "margin:6px 0" },
      `ADD-ONS — ${started} running / ${addons.length - started - errored} stopped${errored ? ` / ${errored} errored` : ""}${updates ? ` · ${updates} update(s) available` : ""}`),
    el("div", { class: "table-wrap", style: "max-height:220px;overflow-y:auto" },
      el("table", {},
        el("tbody", {}, ...addons.map(a => el("tr", {},
          el("td", { class: "strong" }, a.name),
          el("td", {}, statePill(a.state, ["started"])),
          el("td", { class: "mono-dim" }, a.version),
          el("td", {}, a.update_available ? el("span", { class: "pill warn" }, "UPDATE") : "")))))));
  cols.append(colA, colB);
  panel.append(cols);
  return panel;
}

function updatesPanel(res) {
  const panel = el("div", { class: "panel" });
  const title = el("div", { class: "panel-title" }, "UPDATES — CORE, OS, ADD-ONS & DEVICES");
  panel.append(title);
  if (res.error) {
    panel.append(el("div", { class: "mono-dim" }, `⚠ ${res.error}`));
    return panel;
  }
  const all = res.updates || [];
  const avail = all.filter(u => u.available);
  title.append(
    el("span", { class: `pill ${avail.length ? "warn" : "ok"}` },
      avail.length ? `${avail.length} UPDATE${avail.length > 1 ? "S" : ""} AVAILABLE` : "● ALL UP TO DATE"),
    el("span", { class: "mono-dim", style: "margin-left:auto;letter-spacing:0" },
      `${all.length} tracked update entities`));

  if (avail.length) {
    const rows = avail.map(u => el("tr", {},
      el("td", { class: "strong" },
        u.name,
        u.in_progress ? el("span", { class: "pill neutral", style: "margin-left:6px" }, "INSTALLING…") : "",
        u.skipped ? el("span", { class: "pill neutral", style: "margin-left:6px" }, "SKIPPED") : ""),
      el("td", { class: "mono-dim" }, u.installed || "?"),
      el("td", {}, "→ ", el("b", {}, u.latest || "?")),
      el("td", {}, u.release_url
        ? el("a", { href: u.release_url, target: "_blank", rel: "noopener", style: "color:var(--amber)" }, "release notes ↗")
        : el("span", { class: "mono-dim" }, "—"))));
    panel.append(
      el("div", { class: "mono-dim", style: "margin-bottom:8px" },
        "install from Home Assistant (Settings → Updates) — shown here so nothing slips by"),
      el("div", { class: "table-wrap", style: "max-height:300px;overflow-y:auto" },
        el("table", {},
          el("thead", {}, el("tr", {}, ...["COMPONENT", "INSTALLED", "LATEST", ""].map(h => el("th", {}, h)))),
          el("tbody", {}, ...rows))));
  }
  return panel;
}

function findingCard(f) {
  return el("div", { class: `finding ${f.severity}` },
    el("div", { class: "finding-head" },
      el("span", { class: `pill ${f.severity === "critical" ? "err" : f.severity === "info" ? "neutral" : "warn"}` },
        f.severity.toUpperCase()),
      f.category ? el("span", { class: "pill neutral" }, f.category) : null,
      f.affected && f.affected !== "general" ? el("span", { class: "mono-dim" }, f.affected) : null,
      el("span", { class: "finding-title" }, f.title)),
    el("div", { class: "finding-detail" }, f.detail),
    el("div", { class: "finding-fix" }, f.recommendation));
}

function aiButton(label, run, out, toast) {
  const btn = el("button", { class: "btn" }, label);
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    out.replaceChildren(el("div", { class: "ai-running" }, el("div", { class: "spinner" }),
      "Claude is analysing — this can take a minute or two…"));
    try {
      const r = await run();
      out.replaceChildren(...renderAnalysis(r));
      toast("analysis complete", "ok", "CLAUDE");
    } catch (e) {
      out.replaceChildren(el("div", { class: "setup-result err" }, `✕ ${e.message}`));
      toast(String(e.message || e), "err", "ANALYSIS FAILED");
    } finally {
      btn.disabled = false;
    }
  });
  return btn;
}

function renderAnalysis(r) {
  const parts = [];
  const head = el("div", { style: "display:flex;gap:14px;align-items:center;margin-bottom:12px" });
  if (r.grade) head.append(el("span", { class: `grade-badge grade-${r.grade}` }, r.grade));
  head.append(el("div", { class: "ai-summary", style: "flex:1;margin:0" }, r.summary || ""));
  parts.push(head);
  const items = r.issues || r.findings || [];
  const order = { critical: 0, serious: 1, warning: 2, info: 3 };
  items.sort((a, b) => (order[a.severity] ?? 9) - (order[b.severity] ?? 9));
  if (!items.length) parts.push(el("div", { class: "pill ok" }, "● NO ISSUES FOUND"));
  for (const f of items) parts.push(findingCard(f));
  if (r._usage) parts.push(el("div", { class: "mono-dim", style: "margin-top:6px" },
    `claude-opus-4-8 · ${r._usage.input_tokens ?? "?"} in / ${r._usage.output_tokens ?? "?"} out tokens`));
  return parts;
}

function aiLogPanel(toast) {
  const out = el("div", {});
  const panel = el("div", { class: "panel accent" },
    el("div", { class: "panel-title" }, "AI LOG ANALYSIS — POWERED BY CLAUDE"),
    el("div", { class: "mono-dim", style: "margin-bottom:10px" },
      "Sends the HA error log to Claude, which deduplicates, ranks and categorises the issues and recommends concrete fixes. Needs the Anthropic API key from Setup."),
    aiButton("◈ ANALYSE LOGS", () => api.haAnalyzeLogs(), out, toast),
    el("div", { class: "section-gap" }),
    out);
  return panel;
}

function zhaPanel(zha, toast) {
  const panel = el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "ZHA HEALTH — ZIGBEE MESH"));
  if (zha.error) {
    panel.append(el("div", { class: "mono-dim" }, `⚠ ${zha.error}`));
    return panel;
  }
  const devices = zha.devices || [];
  const offline = devices.filter(d => d.available === false);
  const routers = devices.filter(d => d.device_type === "Router");
  const endDevices = devices.filter(d => d.device_type === "EndDevice");
  const weak = devices.filter(d => d.available !== false && d.lqi != null && d.lqi < 80);

  panel.append(el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px" },
    el("span", { class: "pill neutral" }, `${devices.length} devices`),
    el("span", { class: "pill neutral" }, `${routers.length} routers / ${endDevices.length} end devices`),
    el("span", { class: `pill ${offline.length ? "err" : "ok"}` }, `${offline.length} offline`),
    el("span", { class: `pill ${weak.length ? "warn" : "ok"}` }, `${weak.length} weak links (LQI<80)`)));

  const out = el("div", {});
  panel.append(
    aiButton("◈ AI MESH INSIGHTS", () => api.haZhaInsights(), out, toast),
    el("div", { class: "section-gap" }), out);

  const rows = devices.map(d => {
    const lqiColor = d.lqi == null ? "" : d.lqi < 80 ? "color:var(--critical)" : d.lqi < 130 ? "color:var(--warning)" : "color:var(--good)";
    return el("tr", { "data-k": `${d.name} ${d.model} ${d.ieee}`.toLowerCase() },
      el("td", { class: "strong" }, d.name || "—"),
      el("td", { class: "mono-dim" }, `${d.manufacturer || ""} ${d.model || ""}`),
      el("td", {}, el("span", { class: "pill neutral" }, d.device_type || "?")),
      el("td", {}, statePill(d.available === false ? "offline" : "online")),
      el("td", { class: "num", style: lqiColor }, d.lqi ?? "—"),
      el("td", { class: "num" }, d.rssi != null ? `${d.rssi} dBm` : "—"),
      el("td", { class: "mono-dim" }, d.last_seen ? new Date(d.last_seen).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—"));
  });
  panel.append(
    el("div", { class: "table-wrap", style: "max-height:340px;overflow-y:auto" },
      el("table", {},
        el("thead", {}, el("tr", {},
          ...["DEVICE", "MODEL", "TYPE", "STATE", ">LQI", ">RSSI", "LAST SEEN"].map(h =>
            el("th", { class: h.startsWith(">") ? "num" : "" }, h.replace(/^>/, ""))))),
        el("tbody", {}, ...rows))));
  return panel;
}

// ------------------------------------------------------------ UPTIME

const MONITOR_TYPES = [
  { value: "http",    label: "HTTP(S)",       placeholder: "https://192.168.1.30:9000",  hint: "up when the response status is < 400" },
  { value: "keyword", label: "HTTP + KEYWORD", placeholder: "http://192.168.1.20:8123",  hint: "up when the page loads AND contains the keyword" },
  { value: "tcp",     label: "TCP PORT",      placeholder: "192.168.1.10:22",            hint: "up when the port accepts a connection" },
  { value: "dns",     label: "DNS",           placeholder: "unifi.local",                hint: "up when the hostname resolves" },
];

async function uptime(body, toast) {
  const [{ monitors }, { history }] = await Promise.all([
    api.monitors(), api.monitorsHistory().catch(() => ({ history: {} })),
  ]);

  const up = monitors.filter(m => m.ok === true).length;
  const down = monitors.filter(m => m.ok === false).length;
  const paused = monitors.filter(m => m.enabled === false).length;

  // ---- add-monitor form (collapsed until needed)
  const form = monitorForm(toast);
  const addBtn = el("button", { class: "btn" }, "+ ADD MONITOR");
  addBtn.addEventListener("click", () => {
    form.style.display = form.style.display === "none" ? "" : "none";
  });
  form.style.display = monitors.length ? "none" : "";

  const checkBtn = el("button", { class: "btn btn-ghost" }, "▸ CHECK ALL NOW");
  checkBtn.addEventListener("click", async () => {
    checkBtn.disabled = true;
    checkBtn.textContent = "… CHECKING";
    try {
      await api.monitorsCheck();
      location.reload();
    } catch (e) {
      toast(String(e.message || e), "err", "CHECK FAILED");
      checkBtn.disabled = false;
      checkBtn.textContent = "▸ CHECK ALL NOW";
    }
  });

  const rows = monitors.map(m => monitorRow(m, history[m.id] || [], toast));

  body.append(
    el("div", { class: "ops-toolbar" },
      el("span", { class: `pill ${down ? "err" : up ? "ok" : "neutral"}` },
        down ? `✕ ${down} DOWN` : up ? `● ALL ${up} UP` : "NO MONITORS"),
      paused ? el("span", { class: "pill neutral" }, `${paused} PAUSED`) : null,
      el("span", { class: "mono-dim" }, "checked every 30 s · alerts after 2 consecutive failures"),
      el("div", { class: "spacer" }),
      monitors.length ? checkBtn : null, addBtn),
    form,
    monitors.length
      ? el("div", { class: "panel" },
          el("div", { class: "panel-title" }, `SERVICE MONITORS — ${monitors.length}`),
          tableWrap(["", "NAME", "TYPE", "TARGET", "RESPONSE — LAST HOUR", ">NOW", ">24H UPTIME", "FOR", ""], rows))
      : el("div", { class: "panel hero-empty" },
          el("h2", {}, "NO MONITORS YET"),
          el("p", {}, "Watch any HTTP endpoint, TCP port or hostname — container UIs, Proxmox, the UDM, HA. ",
            "Down/recover alerts go to the notification channels configured on the Setup page.")));
}

function monitorRow(m, points, toast) {
  const pill =
    m.enabled === false ? el("span", { class: "pill neutral" }, "PAUSED")
    : m.ok === true ? el("span", { class: "pill ok" }, "● UP")
    : m.ok === false ? el("span", { class: "pill err", title: m.error || "" }, "✕ DOWN")
    : el("span", { class: "pill neutral" }, "PENDING");

  // response-time sparkline over the last hour (successful checks only)
  const hourAgo = Date.now() / 1000 - 3600;
  const spark = sparkline(
    points.filter(p => p[0] > hourAgo && p[1] && p[2] != null).map(p => [p[0], p[2]]),
    { color: m.ok === false ? "var(--critical)" : "#30b48a", width: 150, height: 26 });
  spark.style.width = "150px";
  spark.style.height = "26px";

  const uptimeColor = m.uptime_pct == null ? ""
    : m.uptime_pct < 95 ? "color:var(--critical);font-weight:600"
    : m.uptime_pct < 99.5 ? "color:var(--warning)" : "color:var(--good)";

  const pauseBtn = actionBtn(m.enabled === false ? "RESUME" : "PAUSE",
    () => api.monitorUpdate(m.id, { enabled: m.enabled === false }),
    { confirm: false, toast });

  return el("tr", { "data-k": `${m.name} ${m.type} ${m.target}`.toLowerCase() },
    el("td", {}, pill),
    el("td", { class: "strong", title: m.error || "" }, m.name),
    el("td", {}, el("span", { class: "pill neutral" }, m.type.toUpperCase())),
    el("td", { class: "mono-dim" }, m.target),
    el("td", {}, spark),
    el("td", { class: "num" }, m.ms != null ? `${m.ms.toFixed(0)} ms` : m.error ? "—" : "…"),
    el("td", { class: "num", style: uptimeColor }, m.uptime_pct != null ? `${m.uptime_pct}%` : "—"),
    el("td", { class: "num" }, m.since && m.ok != null ? fmtUptime(Date.now() / 1000 - m.since) : "—"),
    el("td", {}, el("div", { class: "actions" },
      pauseBtn,
      actionBtn("REMOVE", () => api.monitorDelete(m.id), { danger: true, toast }))));
}

function monitorForm(toast) {
  const name = el("input", { type: "text", placeholder: "Jellyfin" });
  const target = el("input", { type: "text", placeholder: MONITOR_TYPES[0].placeholder });
  const keyword = el("input", { type: "text", placeholder: "healthy" });
  const tls = el("input", { type: "checkbox" });
  const hint = el("div", { class: "hint" }, MONITOR_TYPES[0].hint);

  const type = el("select", { class: "search", style: "min-width:170px" },
    ...MONITOR_TYPES.map(t => el("option", { value: t.value }, t.label)));
  const keywordField = el("div", { class: "field", style: "display:none" },
    el("label", {}, "KEYWORD"), keyword);
  const tlsField = el("label", { class: "check" }, tls, "verify TLS certificate (off for self-signed)");
  type.addEventListener("change", () => {
    const t = MONITOR_TYPES.find(x => x.value === type.value);
    target.placeholder = t.placeholder;
    hint.textContent = t.hint;
    keywordField.style.display = type.value === "keyword" ? "" : "none";
    tlsField.style.display = ["http", "keyword"].includes(type.value) ? "" : "none";
  });

  const result = el("div", { class: "setup-result" });
  const saveBtn = el("button", { class: "btn" }, "▸ START MONITORING");
  saveBtn.addEventListener("click", async () => {
    try {
      await api.monitorCreate({
        name: name.value.trim(), type: type.value, target: target.value.trim(),
        keyword: keyword.value.trim(), verify_tls: tls.checked,
      });
      toast(`monitoring ${name.value.trim()}`, "ok", "MONITOR ADDED");
      setTimeout(() => location.reload(), 700);
    } catch (e) {
      result.className = "setup-result err";
      result.textContent = `✕ ${e.message}`;
    }
  });

  return el("div", { class: "panel accent", style: "margin-bottom:14px" },
    el("div", { class: "panel-title" }, "ADD MONITOR"),
    el("div", { style: "display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:0 18px" },
      el("div", { class: "field" }, el("label", {}, "NAME"), name),
      el("div", { class: "field" }, el("label", {}, "CHECK TYPE"), type),
      el("div", { class: "field" }, el("label", {}, "TARGET"), target, hint),
      keywordField),
    tlsField,
    el("div", { class: "setup-actions" }, saveBtn),
    result);
}

// ------------------------------------------------------------ REPORTS

const DAY_NAMES = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"];

async function reports(body, toast) {
  const state = await api.reports();
  const cfg = state.config || {};

  // ---- schedule panel
  const enabled = el("input", { type: "checkbox" });
  enabled.checked = cfg.enabled === true;
  const day = el("select", { class: "search" },
    ...DAY_NAMES.map((d, i) => el("option", { value: String(i) }, d)));
  day.value = String(cfg.day ?? 0);
  const hour = el("select", { class: "search" },
    ...Array.from({ length: 24 }, (_, h) => el("option", { value: String(h) }, `${String(h).padStart(2, "0")}:00`)));
  hour.value = String(cfg.hour ?? 8);

  const saveBtn = el("button", { class: "btn" }, "SAVE SCHEDULE");
  saveBtn.addEventListener("click", async () => {
    try {
      await api.reportConfig({ enabled: enabled.checked, day: +day.value, hour: +hour.value });
      toast(enabled.checked ? `weekly report: ${DAY_NAMES[+day.value]} ${hour.value.padStart(2, "0")}:00` : "schedule disabled", "ok", "REPORTS");
    } catch (e) {
      toast(String(e.message || e), "err", "REPORTS");
    }
  });

  const out = el("div", {});
  const runBtn = el("button", { class: "btn" }, "◈ RUN REPORT NOW");
  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true;
    out.replaceChildren(el("div", { class: "ai-running" }, el("div", { class: "spinner" }),
      "Collecting the lab snapshot and asking Claude — this can take a few minutes…"));
    try {
      const { report } = await api.reportRun();
      out.replaceChildren();
      toast(`report generated — grade ${report.grade}`, "ok", "REPORTS");
      list.prepend(reportPanel(report, true), el("div", { class: "section-gap" }));
    } catch (e) {
      out.replaceChildren(el("div", { class: "setup-result err" }, `✕ ${e.message}`));
    } finally {
      runBtn.disabled = false;
    }
  });

  const schedPanel = el("div", { class: "panel accent" },
    el("div", { class: "panel-title" }, "AI HEALTH REPORT — POWERED BY CLAUDE"),
    el("div", { class: "mono-dim", style: "margin-bottom:10px" },
      "A weekly snapshot of the whole lab — gateway health, security events, Proxmox, Docker, HA/ZHA, ",
      "uptime monitors, warnings — digested by Claude into a graded report with ranked findings. ",
      "The summary is delivered through your notification channels; full reports live here (last 12 kept)."),
    el("div", { style: "display:flex;gap:14px;align-items:center;flex-wrap:wrap" },
      el("label", { class: "check", style: "margin:0" }, enabled, "run weekly on"),
      day, el("span", { class: "mono-dim" }, "at"), hour,
      saveBtn, el("div", { class: "spacer" }), runBtn),
    el("div", { class: "section-gap" }),
    out);

  const list = el("div", {});
  const past = state.reports || [];
  if (!past.length) {
    list.append(el("div", { class: "panel hero-empty" },
      el("h2", {}, "NO REPORTS YET"),
      el("p", {}, "Run one now, or enable the weekly schedule above.")));
  } else {
    past.forEach((r, i) => list.append(reportPanel(r, i === 0), el("div", { class: "section-gap" })));
  }

  if (state.running) {
    out.replaceChildren(el("div", { class: "ai-running" }, el("div", { class: "spinner" }),
      "a report is being generated right now — refresh in a minute or two"));
  }

  body.append(schedPanel, el("div", { class: "section-gap" }), list);
}

function reportPanel(r, expanded) {
  const when = new Date(r.ts * 1000).toLocaleString([], { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  const content = el("div", { style: expanded ? "" : "display:none" }, ...reportBody(r));
  const head = el("div", { class: "panel-title", style: "cursor:pointer" },
    el("span", { class: `grade-badge grade-${r.grade}`, style: "font-size:12px;padding:2px 8px;margin-right:8px" }, r.grade),
    `HEALTH REPORT — ${when.toUpperCase()}`,
    el("span", { class: "pill neutral", style: "margin-left:8px" }, (r.trigger || "manual").toUpperCase()),
    el("span", { class: "mono-dim", style: "margin-left:auto" }, expanded ? "▾" : "▸"));
  head.addEventListener("click", () => {
    const hidden = content.style.display === "none";
    content.style.display = hidden ? "" : "none";
    head.lastChild.textContent = hidden ? "▾" : "▸";
  });
  return el("div", { class: "panel" }, head, content);
}

function reportBody(r) {
  const parts = [];
  parts.push(el("div", { class: "ai-summary", style: "margin:4px 0 12px" }, r.summary || ""));
  if (r.highlights?.length) {
    parts.push(el("div", { style: "display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px" },
      ...r.highlights.map(h => el("span", { class: "pill ok", style: "white-space:normal;line-height:1.5" }, `✓ ${h}`))));
  }
  const order = { critical: 0, serious: 1, warning: 2, info: 3 };
  const items = [...(r.findings || [])].sort((a, b) => (order[a.severity] ?? 9) - (order[b.severity] ?? 9));
  if (!items.length) parts.push(el("div", { class: "pill ok" }, "● NO ISSUES FOUND"));
  for (const f of items) parts.push(findingCard(f));
  if (r._usage) parts.push(el("div", { class: "mono-dim", style: "margin-top:6px" },
    `claude-opus-4-8 · ${r._usage.input_tokens ?? "?"} in / ${r._usage.output_tokens ?? "?"} out tokens`));
  return parts;
}

// ------------------------------------------------------------ shared

function filterRows(scope, q) {
  for (const r of scope.querySelectorAll("tr[data-k]")) {
    r.style.display = !q || r.dataset.k.includes(q) ? "" : "none";
  }
}
