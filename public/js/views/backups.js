// Backups tab: did every backup in the lab actually produce something recently?
//
// Worst first, always. The point of the page is the row that needs attention,
// and sorting alphabetically buries it. The server already returns them in
// severity order (backups._SEVERITY) so the ordering is one decision, not two.

import { api } from "../api.js";
import { el, fmtBytes, timeAgo } from "../util.js";
import { sparkline } from "../charts.js";

// `never` deliberately reads as a warning, not a neutral blank. A job added and
// never wired up is the likeliest silent failure this tab exists to catch, and
// an empty cell would let it pass for fine.
const PILL = {
  ok: "ok", stale: "err", failed: "err", anomaly: "warn",
  never: "warn", unprotected: "warn", muted: "neutral",
};

const BLURB = {
  ok: "succeeded recently",
  stale: "no successful run inside its grace period",
  failed: "the job reported a failure",
  anomaly: "succeeded, but the size looks wrong",
  never: "configured but has never reported a run",
  unprotected: "this guest has no backup job at all",
  muted: "silenced — status still tracked",
};

export async function renderBackups(body, toast) {
  const data = await api.backups().catch(() => ({ jobs: [], counts: {} }));
  const jobs = data.jobs || [];
  const counts = data.counts || {};
  const bad = (counts.failed || 0) + (counts.stale || 0) + (counts.unprotected || 0);

  const form = jobForm(toast);
  form.style.display = jobs.length ? "none" : "";
  const addBtn = el("button", { class: "btn" }, "+ ADD BACKUP JOB");
  addBtn.addEventListener("click", () => {
    form.style.display = form.style.display === "none" ? "" : "none";
  });

  const sweepBtn = el("button", { class: "btn btn-ghost" }, "▸ RESCAN PROXMOX");
  sweepBtn.addEventListener("click", async () => {
    sweepBtn.disabled = true;
    sweepBtn.textContent = "… SCANNING";
    try {
      await api.backupSweep();
      location.reload();
    } catch (e) {
      toast(String(e.message || e), "err", "SCAN FAILED");
      sweepBtn.disabled = false;
      sweepBtn.textContent = "▸ RESCAN PROXMOX";
    }
  });

  body.append(
    el("div", { class: "ops-toolbar" },
      el("span", { class: `pill ${bad ? "err" : counts.never ? "warn" : jobs.length ? "ok" : "neutral"}` },
        bad ? `✕ ${bad} NEED ATTENTION`
          : jobs.length ? `● ${jobs.length} TRACKED` : "NOTHING TRACKED"),
      counts.anomaly ? el("span", { class: "pill warn" }, `${counts.anomaly} SIZE ANOMALY`) : null,
      counts.muted ? el("span", { class: "pill neutral" }, `${counts.muted} MUTED`) : null,
      el("span", { class: "mono-dim" },
        "checked every 30 min · a backup's failure is an absence, so it is watched by outcome, not reachability"),
      el("div", { class: "spacer" }),
      sweepBtn, addBtn),
    form,
    jobs.length
      ? el("div", { class: "panel" },
          el("div", { class: "panel-title" }, `BACKUP JOBS — ${jobs.length}`),
          el("div", { class: "table-wrap" },
            el("table", {},
              el("thead", {}, el("tr", {},
                el("th", {}, ""), el("th", {}, "NAME"), el("th", {}, "SOURCE"),
                el("th", {}, "LAST SUCCESS"), el("th", { class: "num" }, "SIZE"),
                el("th", {}, "TREND"), el("th", {}, "STATUS"), el("th", {}, ""))),
              el("tbody", {}, ...jobs.map(j => row(j, toast))))))
      // An empty list must read as "nothing is being watched", never as
      // reassurance — the same rule the lab issues queue follows.
      : el("div", { class: "panel hero-empty" },
          el("div", { class: "glyph" }, "⚠"),
          el("h2", {}, "NO BACKUPS TRACKED"),
          el("p", {},
            "This is not a clean bill of health — it means nothing is being watched. ",
            "Add a heartbeat job and paste one line into an existing backup script, ",
            "or link Proxmox on the Setup page to discover vzdump schedules automatically.")));
}

function row(j, toast) {
  const status = j.status;
  const detail = j.detail || j.anomaly_detail || BLURB[status] || "";

  const actions = el("div", { style: "display:flex;gap:6px;justify-content:flex-end" });
  const muteBtn = el("button", { class: "btn btn-ghost" }, j.muted ? "UNMUTE" : "MUTE");
  muteBtn.addEventListener("click", async () => {
    try {
      await api.backupUpdate(j.id, { muted: !j.muted });
      location.reload();
    } catch (e) { toast(String(e.message || e), "err", "FAILED"); }
  });
  actions.append(muteBtn);

  if (j.kind === "heartbeat") {
    // Regenerate rather than reveal: the list payload deliberately carries no
    // token, so the only way to see one again is to mint a new one — which
    // also means a leaked token is one click from being dead.
    const tokBtn = el("button", { class: "btn btn-ghost" }, "NEW TOKEN");
    tokBtn.addEventListener("click", async () => {
      if (!confirm(`Regenerate the token for "${j.name}"? The old ping URL stops working.`)) return;
      try {
        const { job } = await api.backupToken(j.id);
        const url = `${location.origin}/api/backups/${job.token}/ping`;
        const cell = tr.querySelector(".token-out");
        cell.replaceChildren(el("pre", { class: "ping-snippet" }, `curl -fsS -X POST ${url}`));
      } catch (e) { toast(String(e.message || e), "err", "FAILED"); }
    });
    actions.append(tokBtn);

    const delBtn = el("button", { class: "btn btn-ghost" }, "DELETE");
    delBtn.addEventListener("click", async () => {
      if (!confirm(`Delete backup job "${j.name}"? Its history goes too.`)) return;
      try {
        await api.backupDelete(j.id);
        location.reload();
      } catch (e) { toast(String(e.message || e), "err", "FAILED"); }
    });
    actions.append(delBtn);
  }

  // sparkline() reads p[1], so sizes have to be [x, y] pairs — a bare list of
  // numbers yields NaN coordinates and draws nothing at all
  const sizes = (j.sizes || [])
    .filter(s => s !== null && s !== undefined)
    .map((s, i) => [i, s]);
  const tr = el("tr", {},
    el("td", {}, el("span", { class: `led ${status === "ok" ? "ok" : status === "muted" ? "off" : PILL[status] === "err" ? "err" : "warn"}` })),
    el("td", {},
      el("div", { class: "strong" }, j.name),
      el("div", { class: "mono-dim" }, detail)),
    el("td", {}, el("span", { class: "pill neutral" }, j.kind === "heartbeat" ? "HEARTBEAT" : j.kind === "unprotected" ? "PROXMOX" : "PROXMOX")),
    el("td", {}, j.last_ok ? timeAgo(j.last_ok) : el("span", { class: "mono-dim" }, "never")),
    el("td", { class: "num" }, j.last_size != null ? fmtBytes(j.last_size) : "—"),
    el("td", {}, sizes.length > 1
      ? el("div", { style: "width:120px;height:26px" },
          sparkline(sizes, { width: 120, height: 26, color: "var(--cyan)" }))
      : el("span", { class: "mono-dim" }, j.baseline_ready === false && sizes.length
        ? "baseline forming" : "—")),
    el("td", {},
      el("span", { class: `pill ${PILL[status] || "neutral"}` }, status.toUpperCase()),
      j.muted && j.real_status && j.real_status !== "muted"
        // muting hides the alert, not the fact
        ? el("span", { class: "mono-dim", style: "margin-left:6px" }, `(${j.real_status})`)
        : null),
    el("td", {}, actions,
      // filled in by NEW TOKEN; empty until then so the row stays compact
      el("div", { class: "token-out" })));

  return tr;
}

function jobForm(toast) {
  const name = el("input", { placeholder: "nightly postgres dump" });
  const hours = el("input", { placeholder: "24", type: "number", min: "1" });
  const grace = el("input", { placeholder: "auto (schedule × 1.1)", type: "number", min: "1" });
  const out = el("div", { class: "mono-dim", style: "margin-top:10px" });

  const save = el("button", { class: "btn" }, "CREATE");
  save.addEventListener("click", async () => {
    if (!name.value.trim()) { toast("give the job a name", "err", "MISSING NAME"); return; }
    try {
      const { job } = await api.backupCreate({
        name: name.value.trim(),
        schedule_hours: hours.value || 24,
        grace_hours: grace.value || null,
      });
      // The token is shown once here and then lives in the row's regenerate
      // action; it is a bearer credential, so it is never logged.
      const url = `${location.origin}/api/backups/${job.token}/ping`;
      out.replaceChildren(
        el("div", { class: "strong" }, "Add this to the end of your backup script:"),
        el("pre", { class: "ping-snippet" }, `curl -fsS -X POST ${url}`),
        el("div", {}, "…or report size and outcome:"),
        el("pre", { class: "ping-snippet" },
          `curl -fsS -X POST -H 'Content-Type: application/json' \\\n`
          + `  -d '{"ok":true,"bytes":'"$(stat -c%s backup.tar.gz)"'}' \\\n  ${url}`),
        el("div", { class: "mono-dim" },
          "POST, not GET — a URL that anything can fetch would be pinged by link "
          + "previewers and hold a dead job green. Reload the page to see the job."));
    } catch (e) { toast(String(e.message || e), "err", "CREATE FAILED"); }
  });

  return el("div", { class: "panel accent", style: "margin-bottom:14px" },
    el("div", { class: "panel-title" }, "NEW HEARTBEAT JOB"),
    el("div", { style: "display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:0 18px" },
      el("div", { class: "field" }, el("label", {}, "NAME"), name),
      el("div", { class: "field" }, el("label", {}, "EXPECTED EVERY (HOURS)"), hours),
      el("div", { class: "field" }, el("label", {}, "GRACE (HOURS)"), grace,
        el("div", { class: "hint" }, "blank derives it from the schedule"))),
    el("div", { class: "setup-actions" }, save),
    out);
}
