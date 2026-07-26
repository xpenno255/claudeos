// Lab Issues: the triage queue — open lab issues from the lab repo with their
// triage state, age and a link out to GitHub, and the control that triages one.
//
// Lab Issues is not a polled system (ADR-0001), so nothing colours a status dot
// for it: the health of the sweep itself has to be said here, in words. The one
// rule this view exists to keep is that a sweep failure never looks like an
// empty queue.
//
// The second rule, added with the trigger, is that no two of a row's states may
// blur into each other. Two pairs actually get confused and both are load
// bearing: **untriaged** ("nobody has looked") against **no fault found**
// ("looked, nothing wrong"), and **triage failed** ("the machinery broke")
// against **inconclusive** ("the machinery worked and could not tell"). Each of
// those four gets a different colour, a different glyph and a different word.

import { api } from "../api.js";
import { el, timeAgo, clockTime } from "../util.js";

const REFRESH_MS = 30000;          // the server sweeps every 60 s

// ---------------------------------------------------------------- the queue
//
// Module scope rather than view scope, and deliberately. A run takes minutes,
// and the person who started it is free to go and look at the dashboard while
// it happens; a queue that lived in the view closure would be silently emptied
// by that navigation while the runs it described carried on regardless.
//
// Runs are serialised here because the server refuses a second concurrent run
// outright — one lab, one investigation at a time. Queueing client-side is what
// turns that refusal into a waiting row instead of an error.

const pending = new Map();      // issue number -> "queued" | "running"
const unstarted = new Map();    // issue number -> why the request never began
let view = null;                // { paint, refresh } while mounted, else null
let draining = false;

function enqueue(number, toast) {
  if (pending.has(number)) return;
  unstarted.delete(number);
  pending.set(number, "queued");
  view?.paint();
  drain(toast);
}

async function drain(toast) {
  if (draining) return;
  draining = true;
  try {
    for (;;) {
      const next = [...pending].find(([, kind]) => kind === "queued");
      if (!next) return;
      const number = next[0];
      pending.set(number, "running");
      view?.paint();
      try {
        const run = await api.labTriage(number);
        const v = run?.verdict || {};
        toast(run?.ok ? `#${number}: ${verdictWord(v.verdict)} (${v.severity})`
                      : `#${number}: run failed`,
              run?.ok ? "ok" : "err", "TRIAGE");
      } catch (e) {
        // The request never started — no SDK, no credentials, no such issue, or
        // another run holds the slot. Nothing was spent and nothing was marked,
        // so the row goes back to untriaged carrying the reason, rather than to
        // a failed state that would imply an attempt was made.
        unstarted.set(number, String(e.message || e));
        toast(String(e.message || e), "err", "TRIAGE NOT STARTED");
      } finally {
        pending.delete(number);
      }
      // Refetch rather than repaint: the verdict this run produced lives in the
      // snapshot now, not in anything held here.
      await view?.refresh();
    }
  } finally {
    draining = false;
    view?.paint();
  }
}

// ---------------------------------------------------------------- the view

export async function renderLabIssues(root, _args, { toast }) {
  let snap = null;

  function paint() {
    if (snap) root.replaceChildren(...panels(snap, toast));
  }

  async function refresh() {
    try {
      snap = await api.labIssues();
    } catch (e) {
      // The snapshot itself did not arrive. Same rule as a failed sweep: the
      // issue state is unknown, so say so rather than showing nothing.
      snap = { issues: [], error: String(e.message || e), checked: null };
    }
    paint();
  }

  view = { paint, refresh };
  await refresh();
  const timer = setInterval(() => refresh().catch(() => {}), REFRESH_MS);
  return () => { clearInterval(timer); view = null; };
}

// ---------------------------------------------------------------- states

function panels(snap, toast) {
  const issues = snap.issues || [];

  // Order matters. A snapshot can carry an empty list AND an error at the same
  // time — an unconfigured install returns exactly that — and it is the unknown
  // state, not the empty one. Never fall through to "no issues" on a failure.
  if (snap.error) {
    const out = [unknownPanel(snap)];
    if (issues.length) out.push(el("div", { class: "section-gap" }), lastKnown(snap, issues, toast));
    return out;
  }
  if (!snap.checked) return [pendingPanel()];
  if (!issues.length) return [emptyPanel(snap)];
  return [queue(snap, issues, toast)];
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
function lastKnown(snap, issues, toast) {
  return el("div", { class: "panel" },
    el("div", { class: "panel-title" }, `LAST KNOWN QUEUE — ${issues.length} OPEN`),
    el("div", { class: "mono-dim", style: "margin-bottom:10px" },
      snap.changed
        ? `as read at ${clockTime(snap.changed)} (${timeAgo(snap.changed)}) — issues raised or closed since then are not here`
        : "may be out of date"),
    table(snap, issues, toast));
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

function queue(snap, issues, toast) {
  const states = issues.map(i => state(i, snap).kind);
  const count = (kind) => states.filter(k => k === kind).length;
  const waiting = count("queued");
  return el("div", {},
    el("div", { class: "ops-toolbar" },
      el("span", { class: "pill neutral" }, `${issues.length} OPEN`),
      // "ALL TRIAGED" is only true when nothing is untriaged AND nothing is
      // still going: claiming it over a running row reports a verdict that has
      // not been reached yet.
      count("untriaged")
        ? el("span", { class: "pill neutral" }, `${count("untriaged")} AWAITING TRIAGE`)
        : (count("running") || waiting
            ? null
            : el("span", { class: "pill ok" }, "● ALL TRIAGED")),
      count("running")
        ? el("span", { class: "pill warn" },
            `1 RUNNING${waiting ? ` · ${waiting} QUEUED` : ""}`)
        : null,
      el("span", { class: "mono-dim" }, sweepLine(snap))),
    snap.triage_available === false ? sdkNotice() : null,
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, `LAB ISSUE QUEUE — ${issues.length} OPEN`),
      table(snap, issues, toast),
      el("div", { class: "mono-dim", style: "margin-top:10px" },
        `A triage run is read-only against the lab: it gathers evidence and posts a verdict `
        + `to the issue, then marks it ${snap.triage_label}. It never changes anything. `
        + `Remove that label on GitHub to make an issue eligible again.`)));
}

// Triage needs the Anthropic SDK, exactly as chat does. Stated up front, next
// to the controls it disables, rather than discovered as a failed request.
function sdkNotice() {
  return el("div", { class: "panel", style: "border-left:3px solid var(--warning)" },
    el("div", { class: "panel-title" }, el("span", { class: "led warn" }), "TRIAGE UNAVAILABLE"),
    el("div", { class: "mono-dim" },
      "The anthropic SDK is not installed in the interpreter running ClaudeOS, so no triage "
      + "can be started. The sweep above is unaffected — the queue is real and current. "
      + "Start the server with .venv/bin/python3 server.py to enable triage."));
}

// ---------------------------------------------------------------- table

function table(snap, issues, toast) {
  const rows = [...issues]
    .sort((a, b) => stamp(b.created_at) - stamp(a.created_at))
    .map(i => issueRow(i, snap, toast));
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

function issueRow(issue, snap, toast) {
  const open = issue.state !== "closed";
  const opened = stamp(issue.created_at);
  const st = state(issue, snap);
  return el("tr", {},
    el("td", {}, el("span", { class: `led ${open ? "ok" : "off"}`, title: open ? "open" : "closed" })),
    el("td", { class: "num mono-dim" }, `#${issue.number ?? "?"}`),
    el("td", { class: "strong", title: preview(issue.body) }, issue.title || "(untitled)"),
    el("td", {}, triageCell(st)),
    el("td", { class: "num" }, opened ? timeAgo(opened) : "—"),
    el("td", {}, el("div", { class: "actions" },
      st.kind === "untriaged" ? triggerButton(issue, snap, toast) : null,
      issue.html_url
        ? el("a", { href: issue.html_url, target: "_blank", rel: "noopener" },
            el("button", { class: "btn btn-mini btn-ghost" }, "GITHUB ↗"))
        : null)));
}

function triggerButton(issue, snap, toast) {
  const off = snap.triage_available === false;
  const btn = el("button", {
    class: "btn btn-mini",
    disabled: off ? "" : null,
    title: off ? "triage needs the anthropic SDK — see the notice above"
               : "gather evidence and post a verdict onto this issue",
  }, "◈ TRIAGE");
  if (!off) {
    btn.addEventListener("click", () => {
      btn.disabled = true;              // this click, before the repaint lands
      enqueue(issue.number, toast);
    });
  }
  return btn;
}

// ---------------------------------------------------------------- row state
//
// One function decides which of the row states applies, so the pill, the
// trigger and the "awaiting triage" count can never disagree about a row.

function state(issue, snap) {
  const n = issue.number;
  const here = pending.get(n);
  // The server reports the run it is executing, so a second browser tab shows
  // the row as running rather than offering a trigger that would be refused.
  const elsewhere = snap.triage_running && snap.triage_running.number === n;
  if (here === "running" || elsewhere) return { kind: "running" };
  if (here === "queued") return { kind: "queued" };

  const rec = (snap.triage || {})[String(n)];
  if (rec && !rec.ok) return { kind: "failed", rec };
  if (rec) return { kind: "verdict", rec };
  if (isTriaged(issue, snap.triage_label)) return { kind: "elsewhere" };
  return { kind: "untriaged", note: unstarted.get(n) };
}

function isTriaged(issue, label) {
  return Boolean(label) && (issue.labels || []).includes(label);
}

const VERDICTS = {
  diagnosed:      { glyph: "◈", text: "DIAGNOSED" },
  refuted:        { glyph: "⊘", text: "REFUTED" },
  inconclusive:   { glyph: "?", text: "INCONCLUSIVE" },
  no_fault_found: { glyph: "✓", text: "NO FAULT FOUND" },
};

function verdictWord(verdict) {
  return VERDICTS[verdict]?.text.toLowerCase() || "unknown verdict";
}

// `td .actions` right-aligns, which is right for the buttons column and wrong
// for a status cell — these read left to right with the title beside them.
function cell(...kids) {
  return el("div", { style: "display:flex;gap:6px;align-items:center;flex-wrap:wrap" }, ...kids);
}

// Severity drives the colour, following the mapping the AI findings list
// already uses, so a critical lab issue reads the same here as it does there.
//
// With one change: that list greys out `info`, and grey is the colour of
// "nobody has looked". No verdict may wear it, or an `info`-severity answer
// becomes indistinguishable from an unanswered row at a glance. So a verdict is
// green when it found nothing wrong, red when critical, and amber otherwise —
// with the severity itself spelled out in words beside the pill, which is where
// the finer distinction belongs anyway.
function verdictClass(verdict, severity) {
  if (verdict === "no_fault_found") return "ok";
  return severity === "critical" ? "err" : "warn";
}

function triageCell(st) {
  if (st.kind === "running") {
    return el("div", { class: "ai-running", style: "padding:0" },
      el("div", { class: "spinner" }), "TRIAGING…");
  }
  if (st.kind === "queued") {
    return cell(
      el("span", { class: "pill warn" }, "… QUEUED"),
      el("span", { class: "mono-dim" }, "waiting for the run ahead"));
  }
  if (st.kind === "failed") {
    // The machinery broke. Distinct from `inconclusive`, where it worked and
    // could not tell — different colour, different glyph, different words.
    return cell(
      el("span", { class: "pill err", title: st.rec.error || "" }, "✕ TRIAGE FAILED"),
      el("span", { class: "mono-dim", title: st.rec.error || "" }, firstLine(st.rec.error)),
      unmarked(st.rec));
  }
  if (st.kind === "verdict") {
    const v = VERDICTS[st.rec.verdict] || { glyph: "•", text: String(st.rec.verdict || "?") };
    const cls = verdictClass(st.rec.verdict, st.rec.severity);
    return cell(
      el("span", { class: `pill ${cls}` }, `${v.glyph} ${v.text}`),
      el("span", { class: "mono-dim" }, st.rec.severity || "—"),
      // A refuted verdict is a useful result, and the count is the size of it:
      // "ruled nothing out" and "ruled three things out" are different answers.
      (st.rec.verdict === "refuted" || st.rec.refuted)
        ? el("span", { class: "mono-dim" }, `· ${st.rec.refuted || 0} ruled out`)
        : null,
      st.rec.confidence ? el("span", { class: "mono-dim" }, `· ${st.rec.confidence} confidence`) : null,
      unmarked(st.rec));
  }
  if (st.kind === "elsewhere") {
    // Labelled on GitHub, but no local record — a data/ wipe, or a run from
    // another install. Triaged, verdict unknown to this app; #37 fetches it.
    return cell(
      el("span", { class: "pill neutral" }, "✓ TRIAGED"),
      el("span", { class: "mono-dim" }, "verdict not held locally"));
  }
  // Untriaged: dim, no verdict, and an em-dash where a severity would be.
  // Nobody has looked. This must not resemble no-fault-found's green pill.
  return cell(
    el("span", { class: "pill neutral" }, "○ UNTRIAGED"),
    el("span", { class: "mono-dim" }, "—"),
    st.note ? el("span", { class: "mono-dim", style: "color:var(--critical)", title: st.note },
                 `not started: ${firstLine(st.note)}`) : null);
}

// A verdict posted without its label is a re-triage waiting to happen — it must
// not read as a clean finish.
function unmarked(rec) {
  return rec.labelled === false
    ? el("span", { class: "pill err", title: "the triaged label could not be applied, so this "
                                            + "issue is still eligible for triage" }, "! NOT MARKED")
    : null;
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

function firstLine(text) {
  const line = String(text || "").split("\n").map(s => s.trim()).find(Boolean) || "";
  return line.length > 90 ? `${line.slice(0, 89)}…` : line;
}

function preview(body) {
  const line = String(body || "").split("\n").map(s => s.trim()).find(Boolean) || "";
  return line.length > 160 ? `${line.slice(0, 159)}…` : line;
}
