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

export async function renderLabIssues(root, args, { toast }) {
  // `#/labissues/<n>` is the detail card. The hash router already splits the
  // argument off, so the route costs nothing but this branch.
  if (args && args[0]) return renderVerdict(root, args[0]);
  return renderQueue(root, toast);
}

async function renderQueue(root, toast) {
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
      budgetPill(snap.budget),
      el("span", { class: "mono-dim" }, sweepLine(snap))),
    snap.triage_available === false ? sdkNotice() : null,
    budgetNotice(snap.budget, count("untriaged")),
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, `LAB ISSUE QUEUE — ${issues.length} OPEN`),
      table(snap, issues, toast),
      el("div", { class: "mono-dim", style: "margin-top:10px" },
        `A triage run is read-only against the lab: it gathers evidence and posts a verdict `
        + `to the issue, then marks it ${snap.triage_label}. It never changes anything. `
        + `Remove that label on GitHub to make an issue eligible again.`)));
}

// ---------------------------------------------------------------- the budget

const MONEY = (n) => `$${(Number(n) || 0).toFixed(2)}`;

// Always shown, because "how much has triage spent today" is a question the
// owner of the API key gets to see the answer to without waiting for it to go
// wrong.
function budgetPill(b) {
  if (!b) return null;
  const cls = { ok: "neutral", soft: "warn", hard: "err", stopped: "err" }[b.state] || "neutral";
  return el("span", { class: `pill ${cls}`, title: `soft ${MONEY(b.soft)} · hard `
    + `${MONEY(b.hard)} · stop ${MONEY(b.stop)} — resets at midnight` },
    `${MONEY(b.usd)} TODAY${b.runs ? ` · ${b.runs} RUN${b.runs === 1 ? "" : "S"}` : ""}`);
}

// A queue stalled on budget and an idle one look identical from the rows alone
// — both just sit there. Only this says which.
//
// One branch per band, because the three are different facts: past soft is a
// pause, past hard is a pause somebody has been told about, past twice hard is
// off for the day. Collapsing them means naming the wrong limit on screen.
const BANDS = {
  soft:    { led: "warn", edge: "warning",  head: "AUTOMATIC TRIAGE PAUSED — DAILY BUDGET",
             limit: (b) => `${MONEY(b.soft)} soft limit`, said: "" },
  hard:    { led: "err",  edge: "critical", head: "AUTOMATIC TRIAGE PAUSED — PAST THE HARD LIMIT",
             limit: (b) => `${MONEY(b.hard)} hard limit`,
             said: " A notification has gone out." },
  stopped: { led: "err",  edge: "critical", head: "AUTOMATIC TRIAGE STOPPED — DAILY BUDGET",
             limit: (b) => `${MONEY(b.stop)} stop limit`,
             said: " That is past the hard limit as well, so a notification has gone out." },
};

function budgetNotice(b, waiting) {
  const band = b && BANDS[b.state];
  if (!band) return null;
  return el("div", { class: "panel", style: `border-left:3px solid var(--${band.edge})` },
    el("div", { class: "panel-title" }, el("span", { class: `led ${band.led}` }), band.head),
    el("div", { class: "mono-dim" },
      `${MONEY(b.usd)} spent today across ${b.runs} run${b.runs === 1 ? "" : "s"}, past the `,
      el("b", {}, band.limit(b)),
      `. No issue will be triaged automatically until the day resets at midnight`,
      waiting ? `, and ${waiting} ${waiting === 1 ? "is" : "are"} waiting.` : ".",
      band.said),
    el("div", { class: "mono-dim", style: "margin-top:8px" },
      "The queue below is current — this is a spending pause, not a fault. Every run counts "
      + "towards it, including ones you start yourself, but triggering one by hand is never "
      + "blocked: the budget exists because nobody is watching the automatic ones."));
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
    el("td", { class: "strong", title: preview(issue.body) }, titleCell(issue, st)),
    el("td", {}, triageCell(st)),
    el("td", { class: "num" }, opened ? timeAgo(opened) : "—"),
    el("td", {}, el("div", { class: "actions" },
      st.kind === "untriaged" ? triggerButton(issue, snap, toast) : null,
      HAS_VERDICT.has(st.kind)
        ? el("a", { href: `#/labissues/${issue.number}` },
            el("button", { class: "btn btn-mini btn-ghost" }, "VERDICT ▸"))
        : null,
      issue.html_url
        ? el("a", { href: issue.html_url, target: "_blank", rel: "noopener" },
            el("button", { class: "btn btn-mini btn-ghost" }, "GITHUB ↗"))
        : null)));
}

// The states where a verdict exists to open — including `elsewhere`, where the
// record is missing locally and the detail route reads it back from GitHub, and
// `failed`, where what there is to read is why the run died.
const HAS_VERDICT = new Set(["verdict", "failed", "elsewhere"]);

function titleCell(issue, st) {
  const title = issue.title || "(untitled)";
  return HAS_VERDICT.has(st.kind)
    ? el("a", { href: `#/labissues/${issue.number}`, class: "strong" }, title)
    : title;
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

// A severity on its own — not a verdict — where grey is fine because the word
// beside it says what it is. This is the AI findings list's mapping.
function severityClass(sev) {
  return sev === "critical" ? "err" : sev === "info" ? "neutral" : "warn";
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

// ------------------------------------------------------------ the whole verdict
//
// What the row cannot say. Three things here are load-bearing and each is a way
// a verdict stays honest: evidence that came back empty or truncated is shown
// rather than quietly dropped, evidence deliberately excluded is shown *with
// its reason* — because a human reading the same log will otherwise reach the
// conclusion the run rejected — and a diagnostic never renders as a fix.

async function renderVerdict(root, arg) {
  // The queue's repaint hooks belong to the queue; nothing here is live.
  view = null;
  const number = Number(arg);
  root.replaceChildren(el("div", { class: "ai-running" },
    el("div", { class: "spinner" }), `reading the verdict on lab issue #${number}…`));
  try {
    const got = await api.labVerdict(number);
    root.replaceChildren(...verdictPanels(number, got));
  } catch (e) {
    root.replaceChildren(backLink(),
      el("div", { class: "panel" },
        el("div", { class: "panel-title" }, el("span", { class: "led err" }),
          `LAB ISSUE #${number} — VERDICT UNAVAILABLE`),
        el("div", { class: "chat-error", style: "text-align:left" }, String(e.message || e))));
  }
  return () => {};
}

function backButton() {
  return el("a", { href: "#/labissues" },
    el("button", { class: "btn btn-mini btn-ghost" }, "◂ BACK TO THE QUEUE"));
}

function backLink() {
  return el("div", { class: "ops-toolbar" }, backButton());
}

function verdictPanels(number, got) {
  const v = got.verdict;
  if (!v) {
    // Labelled but nothing readable anywhere: an older ClaudeOS, a truncated
    // write, or a label somebody applied by hand. Say which, do not invent one.
    return [backLink(), el("div", { class: "panel hero-empty" },
      el("div", { class: "glyph" }, "◈"),
      el("h2", {}, "NO VERDICT ON THIS ISSUE"),
      el("p", {}, `Lab issue #${number} carries no triage verdict — not here, and not in its `,
        "comments on GitHub. It may have been marked by hand, or triaged by a version of ",
        "ClaudeOS older than the current machine-block format."),
      backButton())];
  }

  const failed = Boolean(v.error);
  const word = VERDICTS[v.verdict] || { glyph: "•", text: String(v.verdict || "?") };
  const sev = SEVERITIES.has(v.severity) ? v.severity : "info";

  // The left border is the lab issue's severity — except on the failure path,
  // where there is no assessed severity to show and the card is about the run.
  return [backLink(), el("div", { class: `finding ${failed ? "critical" : sev}` },
    el("div", { class: "finding-head" },
      el("span", { class: `pill ${failed ? "err" : verdictClass(v.verdict, v.severity)}` },
        failed ? "✕ TRIAGE FAILED" : `${word.glyph} ${word.text}`),
      // Not on the failure path. A run that died never assessed severity or
      // confidence — the block carries the cautious defaults, and rendering
      // them as pills claims a judgement nothing made.
      failed ? null : el("span", { class: `pill ${severityClass(sev)}` }, sev.toUpperCase()),
      failed ? null : el("span", { class: "pill neutral" },
        `${v.confidence || "?"} confidence`.toUpperCase()),
      el("span", { class: "finding-title" }, got.title || `LAB ISSUE #${number}`)),
    el("div", { class: "mono-dim", style: "margin-bottom:10px" }, provenance(got, v)),

    failed ? el("div", { class: "chat-error", style: "text-align:left;margin-bottom:12px" },
                `The run did not finish: ${v.error}`) : null,

    got.summary
      ? el("div", { class: "ai-summary prose" }, got.summary)
      : el("div", { class: "mono-dim", style: "margin-bottom:14px" },
          "(this verdict carries no prose)"),

    ruledOut(v.refuted),
    evidencePanel(v.evidence),
    remediation(v.remediation),
    machineBlock(v),

    el("div", { class: "ops-toolbar", style: "margin-top:14px" },
      got.issue_url
        ? el("a", { href: got.issue_url, target: "_blank", rel: "noopener" },
            el("button", { class: "btn btn-mini" }, "OPEN THE ISSUE ON GITHUB ↗"))
        : null,
      got.comment_url
        ? el("a", { href: got.comment_url, target: "_blank", rel: "noopener" },
            el("button", { class: "btn btn-mini btn-ghost" }, "THE TRIAGE COMMENT ↗"))
        : null,
      backButton()))];
}

const SEVERITIES = new Set(["critical", "serious", "warning", "info"]);

// "We ran this" and "we read this back out of the issue" are different claims.
function provenance(got, v) {
  const cost = (v.cost || {}).usd;
  const when = got.ts ? `${clockTime(got.ts)} · ${timeAgo(got.ts)}` : null;
  const bits = [
    got.source === "github"
      ? "read back from the issue's own comments — this install did not run it"
      : "run by this install",
    when, cost ? `$${Number(cost).toFixed(4)}` : null,
    got.labelled === false ? "the triaged label was NOT applied — this issue is still eligible" : null,
  ];
  return bits.filter(Boolean).join(" · ");
}

// A refuted hypothesis is a result, not a gap: naming them saves the owner from
// checking the same thing again.
function ruledOut(list) {
  if (!list || !list.length) return null;
  return el("div", { style: "margin-bottom:14px" },
    el("div", { class: "mono-dim", style: "margin-bottom:6px" },
      `RULED OUT — ${list.length} HYPOTHES${list.length === 1 ? "IS" : "ES"}`),
    ...list.map(h => el("div", { class: "finding-detail", style: "margin-bottom:4px" }, `⊘ ${h}`)));
}

// status -> how it reads. `no_data` is NOT health: the query worked and found
// nothing, and reporting that as green is the specific mistake the tool-result
// semantics in the base prompt exist to prevent.
const STATUS = {
  success:   { cls: "ok",      text: "✓ SUCCESS",   note: "data came back" },
  no_data:   { cls: "warn",    text: "⊘ NO DATA",   note: "the query worked and found nothing — not the same as healthy" },
  truncated: { cls: "warn",    text: "… TRUNCATED", note: "only part of the result was seen" },
  excluded:  { cls: "neutral", text: "− EXCLUDED",  note: "looked at and deliberately not relied on" },
};

function evidencePanel(evidence) {
  const items = evidence || [];
  if (!items.length) {
    return el("div", { class: "mono-dim", style: "margin-bottom:14px" },
      "NO EVIDENCE RECORDED — the verdict rests on nothing this run wrote down");
  }
  const rows = items.map(e => {
    const s = STATUS[e.status] || { cls: "neutral", text: String(e.status || "?"), note: "" };
    return el("tr", {},
      el("td", {}, el("span", { class: `pill ${s.cls}`, title: s.note }, s.text)),
      el("td", { class: "mono-dim" }, e.tool || "?"),
      el("td", {}, e.note || ""));
  });
  const counts = items.reduce((acc, e) => ({ ...acc, [e.status]: (acc[e.status] || 0) + 1 }), {});
  const shape = Object.entries(counts).map(([k, n]) => `${n} ${k}`).join(" · ");
  return el("div", { style: "margin-bottom:14px" },
    el("div", { class: "mono-dim", style: "margin-bottom:6px" },
      `EVIDENCE — ${items.length} FINDING${items.length === 1 ? "" : "S"} · ${shape}`),
    el("div", { class: "table-wrap" },
      el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "STATUS"), el("th", {}, "TOOL"), el("th", {}, "WHAT IT SHOWED"))),
        el("tbody", {}, ...rows))));
}

function remediation(rem) {
  const kind = rem && ["fix", "diagnostic", "none"].includes(rem.kind) ? rem.kind : "none";
  const text = (rem && rem.text) || "";
  return el("div", { style: "margin-bottom:4px" },
    el("div", { class: `remedy ${kind}` },
      text || (kind === "none" ? "nothing to do — the run found no action worth taking" : "(no text)")),
    el("div", { class: "mono-dim", style: "margin-top:6px" },
      "ClaudeOS never runs this. A person reads it and decides."));
}

// The weekly report's collapsible-panel pattern, used for the one thing that
// should start hidden — the fields, for anyone checking what the row was built
// from. Nothing the ticket requires is behind it.
function machineBlock(v) {
  const content = el("pre", {
    class: "mono-dim",
    style: "display:none;white-space:pre-wrap;font-size:11px;margin:8px 0 0",
  }, JSON.stringify(v, null, 1));
  const head = el("div", { class: "mono-dim", style: "cursor:pointer;margin-top:12px" },
    "▸ THE MACHINE BLOCK");
  head.addEventListener("click", () => {
    const hidden = content.style.display === "none";
    content.style.display = hidden ? "" : "none";
    head.textContent = hidden ? "▾ THE MACHINE BLOCK" : "▸ THE MACHINE BLOCK";
  });
  return el("div", {}, head, content);
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
