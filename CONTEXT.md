# ClaudeOS — domain context

The glossary for this repo. Terms are added lazily, as they actually get resolved —
absence from this file means the term has not needed pinning down yet, not that it
is undefined.

Architecture and the JSON API surface live in `README.md`; agent conventions in
`CLAUDE.md` and `docs/agents/`. Decisions live in `docs/adr/`.

## Glossary

### Connector

**A polled lab system with up/down semantics.** The five members of `CONNECTORS`
(`app/connectors/__init__.py`): UniFi, Proxmox, Docker, Home Assistant, Synology.

A connector is a piece of the homelab whose reachability is itself meaningful — when
it stops answering, something is wrong with the lab. `poller.poll_once()` sweeps every
connector on a 30s interval, records ok/error plus sparkline metrics, and alerts on a
`True→False` transition.

**The interface is written down**, in the `app/connectors/__init__.py` docstring:
`test`, `summary`, `metrics` and `report_slice`, the settings shape, and the exception
taxonomy the server maps to HTTP status. It is the contract a new connector satisfies
and the only thing the poller, the server and the weekly report know about any of them.

Each connector curates its own contribution to the digest, so what is *interesting*
about a system is decided by that system's adapter and nowhere else. Where the digest
wants something another module owns — the SMART cache, the registry check —
`reports.py` attaches it, so a connector never reaches into app state.

**Not every external system ClaudeOS talks to is a connector.** `ai`, `registries`, the
five notification channels, and `labissues` are all configured on Setup and hold
encrypted credentials, but none is polled and none has up/down semantics. Encrypted
storage comes from `store.SECRET_FIELDS`, the Setup card from `FORMS` in
`public/js/views/setup.js`, and the TEST button from the if/elif chain in
`server.route_system_test` — all independent of connector membership.

_Avoid_: "integration", "system", or "adapter" as a synonym. **System** is the broader
word (anything with a `SECRET_FIELDS` entry and a Setup card); a connector is the subset
that is polled. See ADR-0001.

### Lab issue

**A homelab problem raised by a human as a GitHub issue in the dedicated private lab
repo** (`xpenno255/homelab`), for ClaudeOS to triage.

Deliberately distinct from a **ClaudeOS issue** — a development ticket in
`xpenno255/claudeos` about the app's source code. They live in separate repos precisely
so the two never share a number space or a queue. A lab issue is about the lab's state;
a ClaudeOS issue is about this codebase.

ClaudeOS never opens a lab issue; it reads, triages, and comments.

### Triage run

**One agentic, read-only investigation of a single lab issue.** It drives the
shared tool loop with `tools.schemas(include_writes=False)` and no approval
channel, gathers evidence across connectors, and ends in a **verdict**.

A triage run never changes the lab. Its output is text, including any
remediation it proposes — a human decides whether to act, and Ops Chat is where
they would execute it, because that is the caller with confirm-gated write
tools. Runs are unattended: nobody is watching while one happens.

### Verdict

**What a triage run concluded**, in a closed vocabulary owned by `app/verdict.py`:

| `verdict` | Means |
| --- | --- |
| `diagnosed` | Cause identified |
| `refuted` | Leading hypothesis ruled out, cause still open |
| `inconclusive` | Could not tell |
| `no_fault_found` | Looked, found nothing wrong |

`refuted` is a **useful result, not a failure** — eliminating a hypothesis saves
the owner from checking it. `no_fault_found` and `inconclusive` are distinct, and
both are distinct from *untriaged*: the UI must never conflate "nobody has looked"
with "looked, nothing wrong".

Evidence carries a status **per finding**, not per verdict — `success`,
`no_data`, `truncated`, `excluded`. `no_data` is not health (see **read-only**'s
neighbour rule in the tool-result semantics); `excluded` names evidence
deliberately not used, with the reason, so a human reading the same source does
not draw the conclusion the run rejected.

Remediation carries a `kind`: `fix`, `diagnostic`, or `none`. A schema that
assumes a fix will manufacture one.

### Machine block

**The machine-readable half of a triage comment** — a JSON payload inside an HTML
comment, which GitHub renders invisibly. One comment therefore serves two readers:
a human sees prose, the app parses fields.

Its presence is also how ClaudeOS could recognise its own comments, which matters
because a fine-grained token posts **as its human owner** — author identity cannot
distinguish them. `BLOCK_VERSION` is its compatibility clock.

_Avoid_: "the JSON", "the payload". The block is a named thing with a version.

### Triage record

**The local copy of what a triage run concluded**, one per lab issue, in
`data/triage.json` (`app/triagelog.py`). Last write wins: it answers *what is
this issue's current verdict*, so a re-triage replaces the earlier answer.

**GitHub is the source of truth, not this.** The verdict a human reads is the
comment; the record exists so the queue can show a verdict without re-reading
the tracker. A `data/` wipe is allowed to destroy it, and a lab issue carrying
the triaged label with no record renders as *triaged, verdict not held locally*
— never as untriaged, which would offer a trigger and spend again.

_Avoid_ treating it as a spend ledger. Last-write-wins loses the cost of a
replaced run by design; the daily budget ledger is a different question and gets
its own key in the same file.

### Daily budget

**What triage has spent in one local calendar day**, against three thresholds
owned by `app/triagelog.py`: `SOFT_USD` ($2), `HARD_USD` ($4), `STOP_USD` ($8).
The band is reported as a state — `ok`, `soft`, `hard`, `stopped`.

| Band | What happens |
| --- | --- |
| `soft` | No new automatic run starts. Logged once a day, not once a pass. |
| `hard` | Also notifies at `high`: the sweep is blocked and issues are waiting. |
| `stopped` | Same, past twice the hard limit — the "disabled until reset" band. |

Soft is the only band a healthy install meets, because no automatic run starts
above it; the two above exist for **overshoot**, since one run started just under
soft can cost $3–5 by itself. Reset is by comparing the stored date to today's,
so "until the day resets" needs nothing running at midnight to come true.

**Every run is charged; only the unattended sweep is gated.** A hand-triggered
run counts towards the day — the money is the money — but is never blocked,
because a person clicking the button is deciding to spend. The bands exist for
the runs nobody is watching. This is why the `hard` notification says only that
the sweep is blocked, and never that a run overshot: the spend may have been
somebody's deliberate afternoon.

Failed runs count. The tokens were billed; a ledger that counted only successes
would under-report exactly the runs most likely to be repeated.

The gate itself is the `claudeos:triaged` label — but read from the sweep's
*cached* copy, so two things guard it (`labissues.eligible`): a run that could
not be marked (`labelled: false`) is never retried automatically, and a label
absent from a copy older than the run that would have applied it is not
believed. Both failures cost a second paid investigation of the same issue.

### Notification volume

**Which lab-issue events are worth interrupting somebody for, and how loudly.**
Two bands, and the quieter one is the verdict:

| Event | Priority |
| --- | --- |
| A `diagnosed` verdict at `critical` or `serious` | `default` |
| Any other verdict, at any severity | *silent* |
| One failed run | *silent* — ops log only, which the weekly report sweeps |
| A dead credential, or the daily budget past `hard` | `high` |

**A verdict is deliberately quieter than a dead credential**, which reads
backwards until you see why: the owner filed the issue themselves, so they know
the thing is broken and what is new is only the cause. `high` is reserved across
this whole app for lab-down and failing hardware, and a triage verdict must not
arrive at that volume. What *does* page is the pair of states nobody would
otherwise discover — where the feature has silently stopped working.

**Severity is the gate, and it is the only one.** `notify.send` mutes an
identical title for five minutes, but these titles carry the issue number, so no
two are ever equal and the mute can never collapse them.

A dead credential alerts **once per transition, not once per pass** — the sweep
runs every 60s and none of these states heals, so per-pass would be 1,440 pushes
a day. A working sweep re-arms it. Transient failures — a timeout, a 5xx, a rate
limit — are not this, per ADR-0001: GitHub being briefly unreachable is not a lab
incident. Each stop is explained as itself (`labissues._stopped_because`), because
sending someone to re-mint a working token when the repo was renamed is worse than
saying nothing.

### Nowhere to go

**An alert raised while no notification channel is configured** — not a delivery
failure, because nothing was attempted and nothing can be retried. The third
outcome of a fan-out beside sent and failed (`notify.alerting_gap`).

**Configuring no channel is a valid choice, so it is not warned about; losing an
alert is not, so it is.** The distinction is what the app says and when: an
install that never notifies is left alone, and the count and the dashboard banner
appear only once something has actually been discarded. This is the same rule as
everywhere else here — interrupt for a feature that has silently stopped working,
not for one that was never asked to start.

Each drop is an ops-log line, which is the audit record; the count exists because
the ops log is where somebody looks *after* they suspect something, and an
install that has never notified is not a state anybody thinks to suspect. Drops
are forgotten once an alert gets through, since they then describe a
configuration that no longer exists.

### Untriaged too long

**An open lab issue still carrying no verdict 24 hours after it was raised**
(`labissues.STALE_UNTRIAGED_HOURS`). Automatic triage picks an issue up within a
minute, so this means triage itself has quietly stopped.

**It has no notification, by design** — it is a backlog signal, not a transition
— which makes the weekly report the only place it surfaces, and the most likely
way anyone notices. A fourth state beside the three in **verdict**: not a
verdict, and not the same as *untriaged* generally.

_Distinguish_ from **`triaged_verdict_unknown`**, which counts issues that *were*
triaged but whose verdict this install no longer holds (a `data/` wipe). Those are
not untriaged and not problem-free; their verdicts are on GitHub. Both exist so a
digest can never read a queue it cannot account for as a queue with nothing in it.

### Read-only

In the context of triage, **read-only always means against the lab** —
`tools.schemas(include_writes=False)`, no `WRITE_TOOLS`. It does not mean ClaudeOS makes
no writes at all: posting a triage comment and applying labels are writes *against the
tracker*. These two boundaries are distinct and conflating them causes confusion.
