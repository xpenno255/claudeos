// Lab Issues: the triage queue — open lab issues from the lab repo with their
// triage state, age and a link out to GitHub.
//
// Lab Issues is not a polled system (ADR-0001), so nothing colours a status dot
// for it: the health of the sweep itself has to be said here, in words. The one
// rule this view exists to keep is that a sweep failure never looks like an
// empty queue.

import { api } from "../api.js";
import { el, timeAgo, clockTime } from "../util.js";

const REFRESH_MS = 30000;          // the server sweeps every 60 s

export async function renderLabIssues(root) {
  async function draw() {
    let snap;
    try {
      snap = await api.labIssues();
    } catch (e) {
      // The snapshot itself did not arrive. Same rule as a failed sweep: the
      // issue state is unknown, so say so rather than showing nothing.
      snap = { issues: [], error: String(e.message || e), checked: null };
    }
    root.replaceChildren(...panels(snap));
  }

  await draw();
  const timer = setInterval(() => draw().catch(() => {}), REFRESH_MS);
  return () => clearInterval(timer);
}

// ---------------------------------------------------------------- states

function panels(snap) {
  const issues = snap.issues || [];

  // Order matters. A snapshot can carry an empty list AND an error at the same
  // time — an unconfigured install returns exactly that — and it is the unknown
  // state, not the empty one. Never fall through to "no issues" on a failure.
  if (snap.error) {
    const out = [unknownPanel(snap)];
    if (issues.length) out.push(el("div", { class: "section-gap" }), lastKnown(snap, issues));
    return out;
  }
  if (!snap.checked) return [pendingPanel()];
  if (!issues.length) return [emptyPanel(snap)];
  return [queue(snap, issues)];
}

// Alarming, critical-bordered, and explicit that this is not an empty queue.
function unknownPanel(snap) {
  const paused = pausedFor(snap);
  return el("div", { class: "panel" },
    el("div", { class: "panel-title" }, el("span", { class: "led err" }), "LAB ISSUE STATE UNKNOWN"),
    el("div", { class: "chat-terminal err" },
      el("div", { class: "chat-terminal-head" }, "✕ THE LAB REPO COULD NOT BE READ"),
      el("div", { style: "margin-top:8px" },
        "This is not an empty queue. ClaudeOS cannot see the lab repository, so how many open lab issues exist is ",
        el("b", {}, "unknown"),
        " — there may be several, and nothing is being triaged until this is fixed."),
      el("div", { class: "chat-error", style: "margin-top:12px;text-align:left" }, snap.error),
      paused ? el("div", { class: "mono-dim", style: "margin-top:10px" }, paused) : null,
      el("div", { class: "mono-dim", style: "margin-top:10px" },
        snap.checked
          ? `last successful read ${clockTime(snap.checked)} · ${timeAgo(snap.checked)}`
          : "no successful read yet"),
      el("a", { href: "#/setup" },
        el("button", { class: "btn btn-danger", style: "margin-top:14px" }, "▸ OPEN SETUP"))));
}

// The cache is deliberately not cleared by a failed sweep, so whatever it still
// holds is worth showing — clearly stamped as possibly out of date.
function lastKnown(snap, issues) {
  return el("div", { class: "panel" },
    el("div", { class: "panel-title" }, `LAST KNOWN QUEUE — ${issues.length} OPEN`),
    el("div", { class: "mono-dim", style: "margin-bottom:10px" },
      snap.changed
        ? `as read at ${clockTime(snap.changed)} (${timeAgo(snap.changed)}) — issues raised or closed since then are not here`
        : "may be out of date"),
    table(issues, snap.triage_label));
}

// Reassuring: the queue really is clear.
function emptyPanel(snap) {
  return el("div", { class: "panel accent hero-empty" },
    el("div", { class: "glyph" }, "◈"),
    el("h2", {}, "NO OPEN LAB ISSUES"),
    el("p", {}, "The lab repo is clear — nothing raised, nothing waiting for triage. ",
      "Open an issue there whenever something in the lab needs looking at and it will appear here."),
    el("div", { class: "mono-dim" }, sweepLine(snap)));
}

// Between server start and the first sweep the cache is empty with no error.
// That is not knowledge of an empty queue either.
function pendingPanel() {
  return el("div", { class: "panel hero-empty" },
    el("div", { class: "glyph" }, "◈"),
    el("h2", {}, "QUEUE NOT READ YET"),
    el("p", {}, "The lab repo has not been swept since the server started, so the queue is not known yet. ",
      "This fills in within a minute."),
    el("div", { class: "ai-running", style: "justify-content:center" },
      el("div", { class: "spinner" }), "waiting for the first sweep"));
}

function queue(snap, issues) {
  // The label name is the server's to own; the view is handed it, never
  // hard-codes it, so there is one source for the string and not two.
  const label = snap.triage_label;
  const untriaged = issues.filter(i => !isTriaged(i, label)).length;
  return el("div", {},
    el("div", { class: "ops-toolbar" },
      el("span", { class: "pill neutral" }, `${issues.length} OPEN`),
      untriaged
        ? el("span", { class: "pill neutral" }, `${untriaged} AWAITING TRIAGE`)
        : el("span", { class: "pill ok" }, "● ALL TRIAGED"),
      el("span", { class: "mono-dim" }, sweepLine(snap))),
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, `LAB ISSUE QUEUE — ${issues.length} OPEN`),
      table(issues, label)));
}

// ---------------------------------------------------------------- table

function table(issues, label) {
  const rows = [...issues]
    .sort((a, b) => stamp(b.created_at) - stamp(a.created_at))
    .map(i => issueRow(i, label));
  return el("div", { class: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, ""),
        el("th", { class: "num" }, "#"),
        el("th", {}, "TITLE"),
        el("th", {}, "TRIAGE"),
        el("th", { class: "num" }, "AGE"),
        el("th", {}, ""))),
      el("tbody", {}, ...rows)));
}

function issueRow(i, label) {
  const open = i.state !== "closed";
  const opened = stamp(i.created_at);
  return el("tr", {},
    el("td", {}, el("span", { class: `led ${open ? "ok" : "off"}`, title: open ? "open" : "closed" })),
    el("td", { class: "num mono-dim" }, `#${i.number ?? "?"}`),
    el("td", { class: "strong", title: preview(i.body) }, i.title || "(untitled)"),
    el("td", {}, triagePill(i, label)),
    el("td", { class: "num" }, opened ? timeAgo(opened) : "—"),
    el("td", {}, el("div", { class: "actions" },
      i.html_url
        ? el("a", { href: i.html_url, target: "_blank", rel: "noopener" },
            el("button", { class: "btn btn-mini btn-ghost" }, "GITHUB ↗"))
        : null)));
}

function isTriaged(issue, label) {
  return Boolean(label) && (issue.labels || []).includes(label);
}

// `claudeos:triaged` is the marker the triage run writes, so a row reads
// TRIAGED once one has completed against it. Verdict, confidence and severity
// are not in this snapshot and are not guessed at here — rendering them is #35.
function triagePill(issue, label) {
  return isTriaged(issue, label)
    ? el("span", { class: "pill ok" }, "✓ TRIAGED")
    : el("span", { class: "pill neutral" }, "○ UNTRIAGED");
}

// ---------------------------------------------------------------- bits

function sweepLine(snap) {
  return pausedFor(snap)
    || (snap.checked ? `last checked ${clockTime(snap.checked)} · ${timeAgo(snap.checked)}` : "not checked yet");
}

// A held-off sweep is waiting on purpose; without saying so it looks idle.
function pausedFor(snap) {
  const until = Number(snap.backoff_until) || 0;
  const secs = until - Date.now() / 1000;
  if (secs <= 0) return null;
  return `sweep paused until ${clockTime(until)} — resumes in ${Math.max(1, Math.ceil(secs / 60))}m, not idle`;
}

function stamp(iso) {
  const ms = Date.parse(iso || "");
  return Number.isNaN(ms) ? 0 : ms / 1000;
}

function preview(body) {
  const line = String(body || "").split("\n").map(s => s.trim()).find(Boolean) || "";
  return line.length > 160 ? `${line.slice(0, 159)}…` : line;
}
