// Operations: granular tables + actions per homelab category.
// Tabs: NETWORK (UniFi) / COMPUTE (Proxmox) / CONTAINERS (Docker) / HOME (HA)

import { api } from "../api.js";
import { el, fmtBytes, fmtPct, fmtUptime, debounce } from "../util.js";
import { meter, sparkRow } from "../charts.js";
import { SYSTEMS, BY_ID } from "../meta.js";

const TABS = [
  { tab: "network",    label: "NETWORK",    sys: "unifi" },
  { tab: "compute",    label: "COMPUTE",    sys: "proxmox" },
  { tab: "containers", label: "CONTAINERS", sys: "docker" },
  { tab: "home",       label: "HOME",       sys: "homeassistant" },
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

  const cfg = overview.config?.[TABS.find(t => t.tab === active).sys];
  if (!cfg?.configured) {
    body.append(el("div", { class: "panel hero-empty" },
      el("h2", {}, "SYSTEM NOT LINKED"),
      el("p", {}, "Add the connection details on the Setup page first."),
      el("a", { href: "#/setup" }, el("button", { class: "btn" }, "▸ OPEN SETUP"))));
    return;
  }

  const renderers = { network, compute, containers, home };
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
function actionBtn(label, run, { danger = false, confirm = true, toast }) {
  const btn = el("button", { class: `btn btn-mini ${danger ? "btn-danger" : "btn-ghost"}` }, label);
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
  const [{ devices }, { clients }] = await Promise.all([api.unifiDevices(), api.unifiClients()]);

  const devRows = () => devices.map(d => el("tr", { "data-k": `${d.name} ${d.ip} ${d.model}`.toLowerCase() },
    el("td", { class: "strong" }, d.name || "—"),
    el("td", {}, `${d.model || ""} ${d.type ? `(${d.type})` : ""}`),
    el("td", {}, d.ip || "—"),
    el("td", {}, statePill(d.state)),
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
    el("div", { class: "panel-title" }, `UNIFI DEVICES — ${devices.length}`),
    tableWrap(["NAME", "MODEL", "IP", "STATE", ">CLIENTS", ">CPU", ">UPTIME", ""], devRows()));

  const cliPanel = el("div", { class: "panel" },
    el("div", { class: "panel-title" }, `CLIENTS — ${clients.length}`),
    tableWrap(["NAME", "IP", "CONNECTION", "NETWORK", ">SIGNAL", ">UPTIME"], cliRows()));

  body.append(
    toolbar("filter devices & clients…", q => filterRows(body, q)),
    devPanel, el("div", { class: "section-gap" }), cliPanel);
}

// ------------------------------------------------------------ COMPUTE

async function compute(body, toast) {
  const [{ nodes }, { guests }, storageRes, perfRes] = await Promise.all([
    api.proxmoxNodes(), api.proxmoxGuests(),
    api.proxmoxStorage().catch(e => ({ error: String(e.message || e) })),
    api.proxmoxPerf().catch(e => ({ error: String(e.message || e) })),
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
    return el("tr", { "data-k": `${g.name} ${g.vmid} ${g.type} ${g.node}`.toLowerCase() },
      el("td", { class: "num" }, g.vmid),
      el("td", { class: "strong" }, g.name || "—"),
      el("td", {}, el("span", { class: "pill neutral" }, g.type === "qemu" ? "VM" : "LXC")),
      el("td", {}, g.node),
      el("td", {}, statePill(g.status)),
      el("td", { class: "num" }, running && g.cpu != null ? `${(g.cpu * 100).toFixed(1)}%` : "—"),
      el("td", { class: "num" }, running ? fmtBytes(g.mem) : "—"),
      el("td", { class: "num" }, running ? fmtUptime(g.uptime) : "—"),
      el("td", {}, el("div", { class: "actions" },
        running
          ? [actionBtn("REBOOT", () => api.proxmoxAction(g.node, g.type, g.vmid, "reboot"), { danger: true, toast }),
             actionBtn("SHUTDOWN", () => api.proxmoxAction(g.node, g.type, g.vmid, "shutdown"), { danger: true, toast }),
             actionBtn("STOP", () => api.proxmoxAction(g.node, g.type, g.vmid, "stop"), { danger: true, toast })]
          : [actionBtn("START", () => api.proxmoxAction(g.node, g.type, g.vmid, "start"), { confirm: false, toast })])));
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
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, `VIRTUAL MACHINES & CONTAINERS — ${guests.length}`),
      tableWrap([">VMID", "NAME", "TYPE", "NODE", "STATE", ">CPU", ">MEM", ">UPTIME", ""], guestRows)));
}

// ------------------------------------------------------------ CONTAINERS

async function containers(body, toast, overview) {
  const { containers } = await api.dockerContainers();

  const h = overview?.systems?.docker?.data?.host;
  let hostPanel = null;
  if (h) {
    hostPanel = el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "DOCKER HOST VITALS — VIA GLANCES"));
    const cols = el("div", { style: "display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:0 24px" });
    const colA = el("div", {}), colB = el("div", {});
    colA.append(
      meter("CPU", h.cpu_pct, {}),
      meter("RAM", h.mem_pct, { detail: h.mem_total ? `${fmtBytes(h.mem_used)} / ${fmtBytes(h.mem_total)}` : "" }),
      ...(h.gpus || []).flatMap(g => [
        meter(`GPU ${g.name || ""}`.trim(), g.util_pct,
          { detail: g.temp != null ? `${g.temp}°C` : "" }),
        meter("GPU VRAM", g.mem_pct, {}),
      ]));
    colB.append(...(h.disks || []).map(d =>
      meter(`DISK ${d.mount}`, d.pct, { detail: `${fmtBytes(d.used)} / ${fmtBytes(d.total)}` })));
    cols.append(colA, colB);
    hostPanel.append(cols);
  } else if (overview?.systems?.docker?.data?.host_error) {
    hostPanel = el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "DOCKER HOST VITALS"),
      el("div", { class: "mono-dim", style: "color:var(--warning)" },
        `⚠ ${overview.systems.docker.data.host_error}`));
  }

  const rows = containers.map(c => {
    const running = c.state === "running";
    return el("tr", { "data-k": `${c.name} ${c.image} ${c.compose_project || ""}`.toLowerCase() },
      el("td", { class: "strong" }, c.name),
      el("td", {}, c.compose_project || "—"),
      el("td", { class: "mono-dim" }, c.image),
      el("td", {}, statePill(c.state)),
      el("td", {}, c.status || "—"),
      el("td", {}, (c.ports || []).join(", ") || "—"),
      el("td", {}, el("div", { class: "actions" },
        running
          ? [actionBtn("RESTART", () => api.dockerAction(c.id, "restart"), { danger: true, toast }),
             actionBtn("STOP", () => api.dockerAction(c.id, "stop"), { danger: true, toast })]
          : [actionBtn("START", () => api.dockerAction(c.id, "start"), { confirm: false, toast })])));
  });

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

  const running = containers.filter(c => c.state === "running").length;
  body.append(
    toolbar("filter containers…", q => filterRows(body, q), [analyseBtn]),
    ...(hostPanel ? [hostPanel, el("div", { class: "section-gap" })] : []),
    storageOut,
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, `DOCKER FLEET — ${running}/${containers.length} RUNNING`),
      tableWrap(["NAME", "PROJECT", "IMAGE", "STATE", "STATUS", "PORTS", ""], rows)));
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

function barRow(label, size, max, dim = false) {
  const row = el("div", { class: "meter" });
  row.innerHTML = `
    <div class="meter-label"><span style="${dim ? "opacity:.55" : ""}">${label}</span><b>${fmtBytes(size)}</b></div>
    <div class="meter-track"><div class="meter-fill" style="width:${(100 * size / max).toFixed(1)}%;background:${dim ? "var(--ink-3)" : "var(--s-docker)"}"></div></div>`;
  return row;
}

// ------------------------------------------------------------ HOME

async function home(body, toast) {
  const [{ entities }, sys, zha] = await Promise.all([
    api.haEntities(),
    api.haSystem().catch(e => ({ error: String(e.message || e) })),
    api.haZha().catch(e => ({ error: String(e.message || e) })),
  ]);

  body.append(systemPanel(sys));
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

// ------------------------------------------------------------ shared

function filterRows(scope, q) {
  for (const r of scope.querySelectorAll("tr[data-k]")) {
    r.style.display = !q || r.dataset.k.includes(q) ? "" : "none";
  }
}
