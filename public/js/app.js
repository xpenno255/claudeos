// ClaudeOS shell: hash router, rail status, clock, sync button, toasts.

import { api } from "./api.js";
import { el, timeAgo } from "./util.js";
import { SYSTEMS } from "./meta.js";
import { renderDashboard } from "./views/dashboard.js";
import { renderOps } from "./views/ops.js";
import { renderSetup } from "./views/setup.js";
import { renderChat } from "./views/chat.js";
import { renderLabIssues } from "./views/labissues.js";

const viewRoot = document.getElementById("view");
const crumb = document.getElementById("crumb");
const nav = document.getElementById("nav");
const railSystems = document.getElementById("rail-systems");
const pollIndicator = document.getElementById("poll-indicator");

let cleanup = null;

// ---------------------------------------------------------------- toasts

export function toast(msg, kind = "info", title = "CLAUDEOS") {
  const box = document.getElementById("toasts");
  const t = el("div", { class: `toast ${kind}` }, el("small", {}, title), msg);
  box.append(t);
  setTimeout(() => { t.classList.add("leaving"); setTimeout(() => t.remove(), 350); }, 4200);
}

// ---------------------------------------------------------------- router

const ROUTES = {
  dashboard: { title: "DASHBOARD", render: renderDashboard },
  ops:       { title: "OPERATIONS", render: renderOps },
  chat:      { title: "OPS CHAT", render: renderChat },
  setup:     { title: "SETUP", render: renderSetup },
  labissues: { title: "LAB ISSUES", render: renderLabIssues },
};

function currentRoute() {
  const hash = location.hash.replace(/^#\/?/, "") || "dashboard";
  const [name, ...rest] = hash.split("/");
  return { name: ROUTES[name] ? name : "dashboard", args: rest };
}

async function navigate() {
  const { name, args } = currentRoute();
  if (typeof cleanup === "function") { cleanup(); cleanup = null; }
  for (const a of nav.querySelectorAll("a")) {
    a.classList.toggle("active", a.dataset.route === name);
  }
  crumb.innerHTML = `/// <b>${ROUTES[name].title}</b>${args[0] ? ` / ${args[0].toUpperCase()}` : ""}`;
  viewRoot.replaceChildren();
  viewRoot.style.animation = "none";
  void viewRoot.offsetHeight; // restart the view-in animation
  viewRoot.style.animation = "";
  try {
    cleanup = await ROUTES[name].render(viewRoot, args, { toast });
  } catch (e) {
    viewRoot.replaceChildren(
      el("div", { class: "panel" },
        el("div", { class: "panel-title" }, "RENDER FAULT"),
        el("div", { class: "mono-dim" }, String(e))));
  }
}

window.addEventListener("hashchange", navigate);

// ---------------------------------------------------------------- rail

// `attention` is null when that check failed, and then the rail says only what
// it can see — up or down — rather than implying the systems were checked.
function renderRail(overview, attention) {
  const flagged = new Set((attention?.items || [])
    .filter(i => i.severity !== "info").map(i => i.system));
  const rows = SYSTEMS.map(s => {
    const st = overview.systems?.[s.id] || {};
    // UP used to mean only that the API answered (#75); WARN is answering
    // with something on the dashboard's attention strip.
    const warn = st.ok === true && flagged.has(s.id);
    const cls = st.scheduled_off ? "sleep" : warn ? "warn" : st.ok === true ? "ok" : st.ok === false ? "err" : "off";
    const word = st.scheduled_off ? "ASLEEP" : warn ? "WARN" : st.ok === true ? "UP" : st.ok === false ? "DOWN" : "—";
    return el("a", { class: "rail-sys", href: `#/ops/${s.tab}`, title: `open ${s.label} operations` },
      el("span", { class: `led ${cls}` }),
      el("span", { class: "sys-name" }, s.label),
      el("span", { class: "mono-dim" }, word));
  });
  railSystems.replaceChildren(el("h6", {}, "SYSTEMS"), ...rows);
  const anyLive = Object.values(overview.systems || {}).some(s => s.ok === true);
  pollIndicator.classList.toggle("live", anyLive);
  pollIndicator.innerHTML = `<span class="dot"></span> ${anyLive ? "LINK ACTIVE" : "LINK IDLE"}`;
}

// Two misses before the rail stops vouching for its last render — the same
// threshold as the dashboard. Until then a blip just ages the data.
const STALE_AFTER = 2;
let railOkAt = null;
let railMisses = 0;

function setStale(stale) {
  document.body.classList.toggle("server-stale", stale);
  document.getElementById("stale-flag").hidden = !stale;
  if (stale) {
    pollIndicator.classList.remove("live");
    pollIndicator.innerHTML = `<span class="dot"></span> ▲ STALE — SERVER UNREACHABLE`;
  }
}

async function refreshRail() {
  try {
    const [overview, attention] = await Promise.all([
      api.overview(), api.attention().catch(() => null)]);
    renderRail(overview, attention);
    railOkAt = Date.now();
    railMisses = 0;
    setStale(false);
  } catch {
    railMisses += 1;
    if (railMisses >= STALE_AFTER) setStale(true);
  }
  tickAge();
}

function tickAge() {
  document.getElementById("data-age").textContent = railOkAt === null
    ? (railMisses ? "NO DATA — SERVER UNREACHABLE" : "")
    : `DATA ${timeAgo(railOkAt / 1000).toUpperCase()}`;
}

// ---------------------------------------------------------------- clock

function tickClock() {
  document.getElementById("clock").textContent =
    new Date().toLocaleTimeString([], { hour12: false });
  tickAge();
}
setInterval(tickClock, 1000);
tickClock();

// ---------------------------------------------------------------- sync

document.getElementById("refresh-btn").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = "⟳ SYNCING…";
  try {
    await api.pollNow();
    toast("all systems polled", "ok", "SYNC");
    await refreshRail();
    await navigate(); // re-render current view with fresh data
  } catch (err) {
    toast(String(err.message || err), "err", "SYNC FAILED");
  } finally {
    btn.disabled = false;
    btn.textContent = "⟳ SYNC";
  }
});

// ---------------------------------------------------------------- boot

refreshRail();
setInterval(refreshRail, 15000);
navigate();
