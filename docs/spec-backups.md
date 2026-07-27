# Spec — Backups

Vocabulary follows `CONTEXT.md`. **Connector** means a polled lab system with up/down
semantics; **system** is the broader word for anything with a Setup card. This spec adds
one term — **backup job** — defined below and destined for the glossary.

Supersedes [#47](https://github.com/xpenno255/claudeos/issues/47), which proposed a `push`
type inside `app/monitors.py`. See *Why not `monitors.py`* under Implementation Decisions.

## Problem Statement

Nothing in ClaudeOS knows whether the lab's backups are happening.

The app watches things that are *up*: connectors answer or they don't, monitors respond or
they don't. A backup is neither. It is a scheduled job that runs for a few seconds in the
middle of the night and then isn't there any more. There is nothing to poll, and the
failure that matters is not a bad response — it is **the absence of a run**. Polling
cannot detect an absence.

So the current failure mode is silence. A cron job stops — an expired credential, a
permissions change, a container that didn't come back after a reboot, a typo in a path —
and every surface in ClaudeOS stays green, because every surface is measuring something
else. The host is up. The containers are running. The disks are healthy. Meanwhile
nothing has been backed up for weeks.

This is not hypothetical: a git auto-sync in this lab was dead for **five and a half
months** before anyone noticed, and during that window every dashboard was green and
every disk was healthy. The information that would have caught it — "this job last
succeeded in February" — existed nowhere.

There is a second, quieter failure underneath it. A job can run, exit zero, and produce
garbage: a dump truncated by a full disk, an archive of a directory that was empty
because a mount wasn't ready. "It ran" and "it produced a usable backup" are different
claims, and only the first is cheap to observe.

## Solution

A **Backups** tab under Ops, listing every backup job in the lab with when it last
succeeded, how big the result was, and whether that is normal for it.

Jobs arrive from two sources, behind one abstraction:

- **Heartbeat** — a scheduled job reports in when it finishes. One line at the end of an
  existing script: `curl -fsS -X POST <claudeos>/api/backups/<token>/ping -d '{"bytes":…}'`.
  ClaudeOS watches the clock and raises the alarm when a ping fails to arrive inside the
  job's grace period. **Absence of the signal is the signal.**
- **Proxmox** — vzdump task history read from the existing Proxmox connection. No
  configuration per VM: jobs are discovered, including the VMs that have *no* backup job
  at all, which is the one gap a per-job view can never show you.

Both land in the same table with the same status vocabulary, so "is everything backed up"
is one glance rather than a tour of three interfaces.

Where a job reports its size, ClaudeOS keeps the history and compares each run against
that job's own baseline. A dump that has been 3.4 MB every night for a month and comes in
at 40 KB is flagged as an anomaly even though the job succeeded — the "exit zero, garbage
out" case, caught by the only evidence available without reading the backup itself.

Status transitions notify through the existing channels. The weekly AI health report gains
a backups section, so a job drifting stale surfaces on its own.

ClaudeOS does not *perform* backups, and does not touch their contents. It observes.

## User Stories

1. As a homelab owner, I want one page showing every backup in the lab, so that "is
   everything backed up?" is a glance rather than an investigation.
2. As a homelab owner, I want to be told when a backup job stops running, so that I find
   out the next morning instead of five months later.
3. As a homelab owner, I want a job that has never run to look obviously different from a
   healthy one, so that I never mistake "nothing has happened yet" for "everything is fine".
4. As a homelab owner, I want each job to show when it last succeeded in plain relative
   time, so that I can judge staleness without doing date arithmetic.
5. As a homelab owner, I want to set how often each job is expected to run, so that a
   weekly job is not judged by a daily job's standard.
6. As a homelab owner, I want a grace period on top of the schedule, so that a run that
   starts twenty minutes late does not alert.
7. As a homelab owner, I want to add a heartbeat job by pasting one line into an existing
   script, so that instrumenting a backup does not mean rewriting it.
8. As a homelab owner, I want the ping URL to be unguessable, so that nobody can hold a
   job green from outside.
9. As a homelab owner, I want a job to be able to report that it *failed*, so that a known
   failure alerts immediately rather than waiting out the grace period.
10. As a homelab owner, I want the size of each backup recorded, so that I can see a job
    producing less than it used to.
11. As a homelab owner, I want a run that is wildly smaller than that job's normal to be
    flagged, so that a job that exits zero while producing nothing does not read as healthy.
12. As a homelab owner, I want size anomalies to be judged against each job's own history
    rather than a fixed threshold, so that a 3 MB database and a 50 GB archive are both
    judged sensibly.
13. As a homelab owner, I want no anomaly alerts until a job has enough history to have a
    baseline, so that a new job's first run is not reported as a problem.
14. As a homelab owner, I want my Proxmox VM backups listed without configuring each one,
    so that the list cannot drift out of date with reality.
15. As a homelab owner, I want VMs with **no** backup job to appear in the list, so that I
    can see what is unprotected — the gap no per-job view reveals.
16. As a homelab owner, I want a failed vzdump run to show its error, so that I can act
    without opening the Proxmox UI.
17. As a homelab owner, I want to mute a job I know about, so that a deliberately
    unprotected scratch VM does not sit permanently red.
18. As a homelab owner, I want a muted job to still be visible, so that muting hides the
    alert and not the fact.
19. As a homelab owner, I want backup history to survive a ClaudeOS restart, so that a
    container update does not erase what it knows about a 24-hour cycle.
20. As a homelab owner, I want to be notified when a job goes stale or fails, so that I
    act on it rather than finding it on a dashboard I happen to open.
21. As a homelab owner, I want a recovery notification when a job starts working again, so
    that I know an alert is resolved without going to look.
22. As a homelab owner, I want repeated alerts about the same broken job suppressed, so
    that a job that has been dead for a week does not notify me every hour.
23. As a homelab owner, I want size anomalies to notify at a lower urgency than a stale
    job, so that "worth a look" and "it stopped" stay distinguishable.
24. As a homelab owner, I want the weekly report to cover backup state, so that slow drift
    surfaces without me checking.
25. As a homelab owner, I want to see the size trend for a job, so that I can spot steady
    growth before a disk fills.
26. As a homelab owner, I want an unreachable Proxmox to look different from "no backups
    found", so that a broken connection is never read as a clean bill of health.

## Implementation Decisions

### Placement in the architecture

`app/backups.py`, a peer of `monitors.py`, `registry.py` and `labissues.py` — **not a
connector**. Per ADR-0001, `CONNECTORS` means *a polled lab system with up/down semantics*;
a backup job is not a lab system, and its states are not up/down. It owns its own store,
sweep loop and route set.

The precedent to follow is `app/smart.py`: a standalone module that *calls* the Proxmox
connector (`disk_list`, `disk_smart`) rather than becoming one, sweeps on its own cadence,
caches its own results, and contributes to the weekly report through `reports.py`. Backups
has the same shape with a second, non-Proxmox source alongside.

The sweeper is `sweeper.spawn("backups", …)`. The chassis is already generic and the
fifth-sweeper concern recorded in ADR-0001 was resolved by building it.

### Why not `monitors.py`

#47 proposed adding a `push` type to the uptime monitors. Rejecting that, for three
reasons:

**The semantics differ.** A monitor answers "is this responding *now*", with a response
time and a 24-hour uptime percentage. A backup job answers "did this produce a good copy
*recently*", with a size, a schedule and a baseline. Uptime % over a daily job is close to
meaningless, and response time does not exist.

**It would make a deep module shallow.** A sixth type whose `ok` means something
categorically different, that skips the probe path, ignores `CHECK_TIMEOUT`, and needs
per-type branching in the record path is the config-flag failure ADR-0001 argues against.

**It has a concrete bug waiting in it.** `monitors.list_monitors()` computes
`sum(p[2] for p in ok_points)` over history. Every `ok` point carries a float today
because `_probe()` always returns one. A push monitor recording `ok=True, ms=None` raises
`TypeError` in a loop covering *all* monitors, taking out `GET /api/monitors` entirely.
A separate module does not need this fixed, because it never creates the condition.

`monitors.py` is untouched by this work.

### The backup job

The domain term, for `CONTEXT.md`:

> **Backup job** — a scheduled process that should produce a recoverable copy of something,
> observed by its *outcome* rather than its reachability. Distinct from a **monitor**, which
> asks whether a service is responding now. A backup job has a cadence, a last-success time,
> and optionally a size history; it is never "up", only recently-successful or not.

Two kinds, one shape:

- **Configured** (`heartbeat`) — created by the user, editable, deletable, holds a token.
- **Discovered** (`proxmox`) — derived from the lab on each sweep. Cannot be created or
  deleted through the UI, only muted. A discovered job that disappears from the source
  disappears from the list.

Config and run history in `data/backups.json`, written with the tmp-and-`os.replace`
pattern already used by `monitors._save()`. **History is persisted, not in-memory** — this
is the difference between a 30-second poll, where losing state costs one tick, and a
26-hour window, where losing state means either a false alarm on boot or silently sailing
past a missed run. Keep the last 90 runs per job; that is a quarter of dailies and enough
for a baseline and a trend line.

### Status vocabulary

Closed set, owned by `app/backups.py`, in the spirit of `verdict.py`:

| Status | Means |
| --- | --- |
| `ok` | Succeeded within its grace period, size unremarkable |
| `stale` | No successful run inside the grace period |
| `failed` | The job explicitly reported failure |
| `anomaly` | Succeeded, but the size deviates from this job's baseline |
| `never` | Configured or discovered, but has never recorded a run |
| `unprotected` | Discovered: the resource exists and has no backup job at all |
| `muted` | Deliberately silenced; status still computed and shown |

`never` is **not** `ok`, and the UI must not render them alike — the same rule
`CONTEXT.md` already sets for untriaged versus `no_fault_found`. A job added but never
wired up is the most likely way this feature fails silently, so it gets its own state
rather than an empty cell.

`unprotected` exists because the alternative is a list that looks complete and isn't.

### Heartbeat ingest

`POST /api/backups/(?P<token>[0-9a-f]{32})/ping`, matching the existing route style
(`monitors` uses `(?P<mid>[0-9a-f]+)`). Body optional:

```json
{ "ok": true, "bytes": 3546657, "duration_s": 5.2, "detail": "..." }
```

Absent body means a bare success ping — the minimum viable integration is
`curl -fsS -X POST <url>` with no payload, and everything else is enrichment.

**POST rather than GET**, deliberately. A GET endpoint that mutates state gets pinged by
link previewers, crawlers and anything that unfurls a URL, and each of those would hold a
dead job green. `-X POST` is a trivial cost at the call site for removing a whole class of
false reassurance.

The route is **unauthenticated** — a cron job has no session — and the token is the only
credential. The app has no authentication anywhere today and is LAN-only, so this
introduces no new asymmetry, but note that `monitors.py`'s header claims its store holds
no secrets and `backups.json` will hold tokens. Say so in the module docstring rather than
inheriting a promise that is no longer true.

Token: `secrets.token_hex(16)`, generated on job creation, shown in the UI with a
copy-to-clipboard affordance and a regenerate action.

### Proxmox discovery

Read-only, through new functions on the Proxmox connector alongside `disk_list` /
`disk_smart`, called by `backups.py` — the SMART pattern exactly:

- vzdump task history: `/nodes/{node}/tasks` filtered to `vzdump`, giving per-VM last run,
  exit status and duration. Task lists are per-node, so iterate the nodes already returned
  by `nodes()`.
- unprotected guests: `/cluster/backup-info/not-backed-up`.

Both are **unverified against this cluster**. The roadmap's own caveat applies — Proxmox
endpoints in that research produced no verified claims. **Probe both live before building
against them**, and if `backup-info` is unavailable on this Proxmox version, derive the
unprotected set by differencing `guests()` against the VMIDs seen in vzdump history and
say in the code that it is a fallback.

Sweep every 30 minutes. vzdump runs are daily at most; polling harder buys nothing.

### Size anomaly detection

Median of the last N successful runs with a recorded size, flagged when the current run is
outside a ratio band of that median.

- Minimum **5** prior sized runs before anomaly detection engages at all. Below that,
  status is `ok` and the UI shows the baseline as still forming.
- Band: below 0.5× or above 3× the median. Asymmetric on purpose — a backup collapsing to
  a fraction of its size is the failure being hunted, while growth is usually just growth.
- Median, not mean: one 40 KB truncated run should not drag the baseline it is being
  judged against.

This is a heuristic and will occasionally be wrong. It is worth having anyway, because the
failure it targets — success exit code, useless artefact — is otherwise completely
invisible. Anomaly is a *lower* urgency than stale for exactly this reason.

### The surface

New tab in `public/js/views/ops.js` `TABS`, `{ tab: "backups", label: "BACKUPS", sys: null }`,
alongside `uptime` and `reports` which are likewise not tied to one system. New view
module `public/js/views/backups.js`, and a `BY_ID` entry with `tab: null` is **not**
needed — this is not a configured system with a Setup card.

Table columns: NAME, SOURCE, LAST SUCCESS, SIZE, TREND, STATUS, actions. Sorted worst
first, so the thing needing attention is at the top rather than alphabetically buried.

A sparkline of run sizes per job reuses the chart helpers already used for monitor
response times. Add / edit / delete for heartbeat jobs; mute only for discovered ones.

An empty list must read as "nothing configured yet", not as reassurance — same rule as the
lab issues queue.

### Notification and reporting

Alert on **transition**, never on state:

| Transition | Priority | Tag |
| --- | --- | --- |
| → `stale`, → `failed` | `high` | `rotating_light` |
| → `anomaly` | `default` | `warning` |
| → `ok` from any alerting state | `default` | `white_check_mark` |

A job already `stale` does not re-alert on the next sweep; that is what the existing
`alerted` latch pattern in `monitors._record()` is for, and the same shape applies here.

`reports.py` attaches a backups slice, as it already does for SMART and the registry check
— a non-connector contributing to the digest through the report module rather than through
the connector interface. Content: counts by status, anything stale or unprotected named
explicitly, and any job whose size trend has moved materially over the week.

## Testing Decisions

Per `CLAUDE.md`, the bar is failure modes that are *silent and expensive*, and a module
earns a seam only where it clears that bar. Three do here; the test file should say why.

**1. Status evaluation, with the clock injected.** The entire feature is a comparison
against wall-clock time, and every one of its failure modes is silent: a job wrongly `ok`
reports safety that does not exist. `evaluate(jobs, now)` takes `now` as an argument so
staleness, grace-period boundaries, and the `never` case are testable with no waiting and
no real time. Specifically pin: a job with no runs is `never` and not `ok`; a job one
second inside its grace period is `ok`; one second outside is `stale`.

**2. Persistence across restart.** The state that matters is the state that outlives the
process, and this is the concrete defect that sank the `monitors.py` approach. Write jobs
and history, reload from disk, assert last-success times and baselines survive intact.

**3. Anomaly baseline with insufficient history.** A new job alerting on its own first run
is the fastest way to make the feature untrusted and then ignored, which costs the alert
channel its meaning — the same reasoning as #41. Assert that fewer than 5 sized runs never
produces `anomaly`, and that a median baseline is not moved into uselessness by one
outlier.

No tests for the ingest route, the Proxmox parsing or the view. Consistent with the
existing bar.

## Out of Scope

**ClaudeOS performing backups.** It observes; it does not run, schedule, or trigger them,
and it never reads or writes backup contents. Consistent with
`.out-of-scope/unattended-lab-writes.md`.

**Restore testing.** "Backed up daily but never test-restored in 94 days" is a genuinely
valuable column and a natural follow-on, but it needs a way to record a restore test and
a decision about what counts as one. Deliberately deferred rather than half-built.

**Off-box dead-man's switch.** See below — a real gap, and not solvable from inside this
app.

**Auto-filing lab issues** when a backup goes stale. Covered by
`.out-of-scope/auto-filing-lab-issues.md`; this alerts and logs like everything else.

**Backup destination verification.** ClaudeOS does not connect to object storage or check
that the file the job claims to have written actually exists. The heartbeat is the job's
own word for it, enriched with a size the job reports. Verifying independently would mean
credentials to every backup destination, which is a much larger security surface for a
smaller increment of confidence than restore testing would buy.

## Further Notes

**The same-host blind spot, stated plainly.** ClaudeOS runs on the same Docker host as
several of the jobs it would watch. If that host dies, the jobs stop *and* the thing
meant to notice dies with them. No alert will be sent. This is a smoke alarm wired to the
house's own electricity, and it is not fixable from inside the app.

That is not an argument against building this. The failure it does catch is the far more
common one, and the one with a five-and-a-half-month precedent in this lab: **the host
stays perfectly healthy while a job underneath it quietly stops.** That is invisible today
and would be caught the next morning.

Closing the remaining gap needs a check that lives somewhere the host cannot take with it
— an external ping service, or a rule on the backup destination's own side. Worth doing;
not this repo's job. It should be recorded wherever the lab's recovery documentation lives,
so the limitation is known rather than assumed away.

**Grace period defaults.** For a daily job, 26 hours: long enough that a slow or slightly
delayed run does not alert, short enough that a missed run is caught the same day. Derive
the default as `schedule_hours × 1.1` and let it be overridden.
