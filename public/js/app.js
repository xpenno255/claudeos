// ClaudeOS shell: hash router, rail status, clock, sync button, toasts.

import { api } from "./api.js";
import { el } from "./util.js";
import { SYSTEMS } from "./meta.js";
import { renderDashboard } from "./views/dashboard.js";
import { renderOps } from "./views/ops.js";
import { renderSetup } from "./views/setup.js";

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
  setup:     { title: "SETUP", render: renderSetup },
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

function renderRail(overview) {
  const rows = SYSTEMS.map(s => {
    const st = overview.systems?.[s.id] || {};
    const cls = st.ok === true ? "ok" : st.ok === false ? "err" : "off";
    return el("div", { class: "rail-sys" },
      el("span", { class: `led ${cls}` }),
      el("span", { class: "sys-name" }, s.label),
      el("span", { class: "mono-dim" }, st.ok === true ? "UP" : st.ok === false ? "DOWN" : "—"));
  });
  railSystems.replaceChildren(el("h6", {}, "SYSTEMS"), ...rows);
  const anyLive = Object.values(overview.systems || {}).some(s => s.ok === true);
  pollIndicator.classList.toggle("live", anyLive);
  pollIndicator.innerHTML = `<span class="dot"></span> ${anyLive ? "LINK ACTIVE" : "LINK IDLE"}`;
}

async function refreshRail() {
  try { renderRail(await api.overview()); } catch { /* server unreachable; keep last */ }
}

// ---------------------------------------------------------------- clock

function tickClock() {
  document.getElementById("clock").textContent =
    new Date().toLocaleTimeString([], { hour12: false });
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
