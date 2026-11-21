// Small DOM + formatting helpers shared by all views.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function fmtBytes(n, perSec = false) {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 ? Math.round(n) : n.toFixed(1)} ${units[i]}${perSec ? "/s" : ""}`;
}

export function fmtUptime(sec) {
  if (!sec && sec !== 0) return "—";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function fmtPct(x, digits = 0) {
  if (x === null || x === undefined) return "—";
  return `${x.toFixed(digits)}%`;
}

export function timeAgo(ts) {
  const s = Math.max(0, (Date.now() / 1000) - ts);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function clockTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

// "We could not look" — distinct from "there is nothing here", which is the
// confusion #75 found in three views. Every empty state in this app is a claim
// about the lab; a failed fetch makes no claim at all, so it must not borrow
// one. `onRetry` defaults to re-rendering the current route, which also runs
// the view's cleanup.
export function rerender() {
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}

export function couldNotLoad(what, err, onRetry = rerender) {
  const retry = el("button", { class: "btn" }, "⟳ RETRY");
  retry.addEventListener("click", () => {
    retry.disabled = true;
    retry.textContent = "… RETRYING";
    onRetry();
  });
  const why = err ? String(err.message || err) : "";
  return el("div", { class: "panel load-fail", role: "alert" },
    el("div", { class: "glyph" }, "▲"),
    el("h2", {}, "COULD NOT LOAD"),
    el("p", {}, `${what} could not be read${why ? ` (${why})` : ""}. `
      + "This is not the same as there being nothing here — the state is unknown."),
    retry);
}

// A <label> tied to its control by id (#87). A label that only sits next to an
// input is a caption to a sighted reader and nothing at all to a screen reader,
// which then announces the placeholder — or, for a stored secret, "•••••".
let controlSeq = 0;
export function labelFor(control, ...children) {
  if (!control.id) control.id = `ctl-${++controlSeq}`;
  return el("label", { for: control.id }, ...children);
}

// For a click target that cannot become a real <button> without breaking its
// layout (a panel heading, a stack of meters): reachable by Tab, announced as a
// button, and operated by Enter or Space like one (#87). Its click handler is
// unchanged; the keys just call it. `expanded`, when given, is the initial
// aria-expanded — the handler keeps it current.
export function pressable(node, { expanded } = {}) {
  node.setAttribute("role", "button");
  node.tabIndex = 0;
  if (expanded !== undefined) node.setAttribute("aria-expanded", String(expanded));
  node.addEventListener("keydown", e => {
    if (e.target !== node || (e.key !== "Enter" && e.key !== " ")) return;
    e.preventDefault();
    node.click();
  });
  return node;
}

// Debounce for search boxes.
export function debounce(fn, ms = 200) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
