# Auto-Filing Lab Issues From ClaudeOS Alerts

ClaudeOS does not open lab issues. When a connector goes down or a monitor fails,
that becomes a notification and an ops-log line — not a GitHub issue. Humans raise
lab issues; ClaudeOS reads, triages and comments on them.

## Why this is out of scope

**It changes what the lab repo is.** The queue is a **triage inbox**: a human
noticed something, described it in their own words, and wants an investigation.
Auto-filing would turn it into an **incident log** — a machine-generated record of
every transition, most of which need no investigation and several of which will
have resolved themselves before anyone looks. Those are different products with
different lifecycles, and the triage budget is sized for the first one.

**It pulls in a queue of hard problems that the current design never has to
solve:**

- **Flap dedup** — a connector that bounces five times in an hour is one problem,
  not five issues. Getting this wrong is expensive twice over, because each issue
  is also a paid triage run.
- **Auto-close on recovery** — if the app opened it, the app presumably closes it,
  which means deciding what "recovered" means and what happens to a triage run
  already in flight against an issue that is about to close.
- **Severity thresholds** — which alerts deserve an issue at all. Get it loose and
  the queue is noise; get it tight and the feature does nothing.
- **The alert↔issue lifecycle** — the mapping between a live alert's state and an
  issue's state, kept consistent in both directions.

**It partly duplicates a layer that already exists.** Notifications already tell
the owner when something needs hands, on a documented volume ladder (see
`CONTEXT.md` → **notification volume**), and the weekly report already sweeps
recent warnings and errors into the digest. An auto-filed issue would be a third
telling of the same event.

**The human-written description is load-bearing, not incidental.** A triage run
works from what the owner wrote — the symptom as they experienced it, what they
already tried, which device they mean by "the utility plug". A generated title like
`homeassistant: poll failed` gives the run nothing to test a hypothesis against,
and the honest verdict would usually be `inconclusive`.

## If this is reconsidered

The strongest version is probably narrower than "auto-file alerts": a **suggested
issue** — the app drafts the title and body from an alert and offers it, and a
human presses the button. That keeps a person in the loop, keeps the description
human-owned, and sidesteps dedup and auto-close entirely.

## Prior requests

- Settled during the Lab Issues wayfinder map (#14) and recorded in the spec (#26)
  under **Out of scope**. No separate ticket was filed.
