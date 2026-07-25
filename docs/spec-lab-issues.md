# Spec — Lab Issues

Synthesised from the [Lab Issues wayfinder map](https://github.com/xpenno255/claudeos/issues/14),
whose 11 closed tickets hold the reasoning behind every decision below. Where a decision has a
ticket, the ticket is the source of truth; this spec is the collapse of them into a buildable plan.

Vocabulary follows `CONTEXT.md`. **Connector** means a polled lab system with up/down semantics;
**lab issue** means a homelab problem raised as a GitHub issue in the dedicated lab repo, as
distinct from a ClaudeOS development issue; **read-only** always means *against the lab*.

## Problem Statement

When something misbehaves in the homelab — a Zigbee device that keeps failing firmware updates, a
container that restarts nightly, a VM that has gone slow — the diagnosis is a chore. The evidence is
scattered across UniFi, Proxmox, Docker, Home Assistant and the NAS, and gathering it means opening
five interfaces and holding the comparison in your head. So problems get noticed, mentally filed,
and then either forgotten or rediscovered weeks later.

There is nowhere to *put* a lab problem. ClaudeOS alerts when something breaks, and Ops Chat answers
questions while you are sitting in front of it, but neither gives a problem somewhere to live
between noticing it and solving it. Nothing works on a problem while you are not looking at it.

## Solution

Raise the problem as a GitHub issue in a dedicated private lab repo, and ClaudeOS investigates it
for you.

Within a minute of the issue appearing, ClaudeOS picks it up and runs an agentic investigation with
read-only access to every connector — the same tools Ops Chat uses, minus anything that can change
the lab. It gathers evidence, reasons across systems, and posts a verdict back onto the issue as a
comment: what it found, what it ruled out, what it could not see, and a precise remediation you can
carry out yourself.

A new **Lab Issues** page in ClaudeOS shows the queue and each issue's triage state, linking out to
GitHub for the conversation. Verdicts worth acting on notify through the existing channels; the rest
wait quietly. The weekly AI health report gains a section on the state of the backlog.

ClaudeOS never changes the lab as part of this. The remediation is text.

## User Stories

1. As a homelab owner, I want to raise a lab issue in GitHub from my phone, so that I can record a
   problem the moment I notice it rather than trying to remember it later.
2. As a homelab owner, I want ClaudeOS to pick up a newly raised lab issue without me telling it to,
   so that investigation starts while I am doing something else.
3. As a homelab owner, I want the investigation to read every connector, so that a problem spanning
   two systems is diagnosed as one problem rather than missed by both.
4. As a homelab owner, I want the verdict posted onto the issue itself, so that the diagnosis lives
   with the problem and I can find it again by searching GitHub.
5. As a homelab owner, I want the verdict written in plain prose, so that I can read it on my phone
   in the GitHub app without decoding a data structure.
6. As a homelab owner, I want to be told what the investigation *ruled out*, so that I do not waste
   an evening re-checking the obvious explanation it already eliminated.
7. As a homelab owner, I want to be told plainly when the investigation could not reach a
   conclusion, so that I do not act on a confident-sounding guess.
8. As a homelab owner, I want the verdict to distinguish "I looked and found nothing wrong" from "I
   could not tell", so that I know whether the problem is elsewhere or simply unexplained.
9. As a homelab owner, I want to see which tool results were empty or truncated, so that I know
   which parts of the conclusion rest on missing evidence.
10. As a homelab owner, I want evidence that belongs to a *different* device explicitly excluded and
    labelled as such, so that I do not draw the same wrong conclusion the investigation avoided.
11. As a homelab owner, I want the proposed remediation to be precise enough to copy and run, so
    that acting on it does not require a second round of research.
12. As a homelab owner, I want to be told when the honest next step is a diagnostic rather than a
    fix, so that I collect better evidence instead of applying a speculative change.
13. As a homelab owner, I want ClaudeOS never to change the lab on its own, so that nothing happens
    to my infrastructure while I am asleep.
14. As a homelab owner, I want to paste a proposed remediation into Ops Chat to execute it, so that
    I get confirm-gated execution when I choose it rather than automatically.
15. As a homelab owner, I want a Lab Issues page in ClaudeOS, so that I can see the whole queue
    without opening GitHub.
16. As a homelab owner, I want each row to show the issue's triage state at a glance, so that I can
    tell what has been looked at and what is still waiting.
17. As a homelab owner, I want an untriaged issue to look obviously different from one triaged with
    no fault found, so that I never mistake "not looked at" for "nothing wrong".
18. As a homelab owner, I want to see the verdict and severity in the list, so that I can prioritise
    without opening each issue.
19. As a homelab owner, I want to click through from a row to the full verdict, so that I can read
    the detail without leaving the app.
20. As a homelab owner, I want every row to link out to GitHub, so that I can reply, close, or
    continue the conversation where it belongs.
21. As a homelab owner, I want to trigger triage manually for one issue, so that I do not have to
    wait for the next sweep when I am watching.
22. As a homelab owner, I want an empty queue to look reassuring and an unreachable GitHub to look
    alarming, so that a broken connection is never mistaken for a clean bill of health.
23. As a homelab owner, I want to be told when my access token has expired, so that I find out from
    the app rather than from months of silence.
24. As a homelab owner, I want to be notified when a serious problem is diagnosed, so that I act on
    it rather than discovering it in a weekly digest.
25. As a homelab owner, I want *not* to be notified about routine verdicts, so that the notification
    channel keeps the meaning it has for lab-down and failing-hardware alerts.
26. As a homelab owner, I want to be notified if triage itself stops working, so that a silently
    stalled queue does not look like an empty one.
27. As a homelab owner, I want the weekly health report to cover the lab issue backlog, so that
    issues drifting untriaged surface on their own.
28. As a homelab owner, I want a hard ceiling on what one investigation can cost, so that a
    pathological run cannot produce a surprising bill.
29. As a homelab owner, I want a daily spend ceiling across all investigations, so that a mistake
    costs pounds rather than hundreds of pounds.
30. As a homelab owner, I want to see why nothing was triaged when the budget is exhausted, so that
    the silence is explained rather than mysterious.
31. As a homelab owner, I want investigations to run one at a time, so that a backlog of eight
    issues drains steadily instead of firing eight expensive runs at once.
32. As a homelab owner, I want an issue that has been triaged never to be triaged again by itself,
    so that I am not paying repeatedly for the same answer.
33. As a homelab owner, I want to re-trigger triage deliberately when I add new information, so that
    a stale verdict can be refreshed on demand.
34. As a homelab owner, I want a failed investigation to stop rather than retry forever, so that one
    error cannot become thousands of paid attempts.
35. As a homelab owner, I want to configure my GitHub connection on the Setup page like every other
    credential, so that there is one place I manage access.
36. As a homelab owner, I want my access token encrypted at rest and never sent to the browser, so
    that it is handled like every other secret in the app.
37. As a homelab owner, I want to test the GitHub connection from Setup before relying on it, so
    that I find a misconfigured token immediately.
38. As a homelab owner, I want the token scoped to the lab repo alone, so that ClaudeOS cannot reach
    the rest of my GitHub account.
39. As a homelab owner, I want lab issues kept out of the ClaudeOS development tracker, so that my
    laundry-room problems and my source-code tickets stay separate.
40. As a homelab owner, I want polling to be cheap enough to run every minute, so that triage feels
    responsive without exhausting the API budget.
41. As a homelab owner, I want the app to keep working when GitHub is briefly unreachable, so that a
    transient outage does not produce a false alarm about my lab.
42. As a developer, I want the agentic loop shared between Ops Chat and triage, so that a fix to
    iteration handling reaches both.
43. As a developer, I want the read-only guarantee enforced at the loop's interface, so that it is a
    property of the code rather than a convention.
44. As a developer, I want the sweep logic testable without touching GitHub or Anthropic, so that
    the risky parts can be verified cheaply.

## Implementation Decisions

### Placement in the architecture

**Lab Issues is not a connector.** It joins no connector registry, is not polled by the poller, has
no up/down semantics, and appears on no dashboard tile. It is a peer of the notification dispatcher
and the container-registry update checker: an external system ClaudeOS talks to, configured on
Setup, owning its own sweep and cache. Recorded as ADR-0001; do not revisit without re-opening it.

The consequence that motivated the decision: had it joined the connector registry, a transient
GitHub outage would have fired a high-priority "DOWN" alert indistinguishable from a Proxmox
failure, and the module would have had to fabricate a host field and return empty results from the
per-connector metrics and report hooks.

### New modules

**A lab issues module.** Owns the GitHub client, the sweep loop, the local cache, the triage gate,
the budget ledger, and orchestration of triage runs. Its public interface is a sweep entry point, a
cached read for the UI, and a single-issue triage entry point. It **accepts its GitHub caller and
its triage function as arguments** rather than reaching for them — this is a departure from the
prevailing style in the codebase and exists so the module is testable at its interface.

**A shared tool loop.** Extracted from the existing chat module: the iteration loop, the per-turn
duplicate-call guard, cost accumulation, tool dispatch and result rendering, and the echo of
thinking blocks. Parameterised by the tool schema list and by the shape of the final call. Ops Chat
layers streaming, write approval and conversation persistence on top; triage drives the same core
with writes withheld and a structured final call.

Two properties the current loop lacks and the extraction must add:

- The tool schema list must be a **parameter**. Today the loop always requests the full set
  including write tools; the read-only guarantee cannot presently be expressed.
- A **third ending mode**. Today tools are withheld on the final iteration so the turn ends in
  prose. Triage needs the final call to drop tools *and* request a structured response.

The context budget must be enforced **during** the loop. It is currently checked only at turn entry
against the previous turn's usage, so a single run can exceed it unchecked.

**A sweeper spawn helper.** The four existing background loops duplicate about ten lines each of
daemon-thread boilerplate differing only in callable, thread name, log tag and interval. Extract
that and only that, before the feature build. The cache-and-staleness chassis is **not** extracted:
only two of the four modules have it, and the parameter that would justify it is passed at none of
its call sites. See the correction recorded on the existing sweeper-duplication issue.

### Modified modules

- **Secret storage** gains an entry for the GitHub credential, encrypted like every other secret and
  never returned to the browser.
- **The chat module** loses its loop core to the shared module and keeps streaming, approval and
  persistence.
- **The HTTP server** gains routes for the lab issues list, a single issue's detail, and a manual
  triage trigger; and one more branch in the connection-test dispatch, which already handles four
  distinct kinds of configured system.
- **The report collector** gains a top-level lab issues section, alongside the uptime monitors,
  recent warnings and metric aggregates that already sit outside the per-connector blocks. It does
  **not** go through the proposed per-connector report-slice mechanism, which iterates connectors
  only.
- **The frontend** gains a fifth navigation entry and a self-contained view module.

### GitHub access

Read and comment on issues in one private repo, through the existing stdlib HTTP helper — no new
dependency. Two deviations from that helper's defaults are required: TLS verification must be **on**
(it defaults off, deliberately, because homelab gear runs self-signed certificates), and a 304
response must be treated as a **success** rather than an error, since the underlying library raises
on any non-2xx status.

Credential: a fine-grained personal access token scoped to the lab repo with issue read/write and
metadata read. Label writes need no broader permission. The token posts **as its human owner**,
which has two consequences that shape decisions below: author identity cannot distinguish ClaudeOS's
comments from the human's, and GitHub may not notify the issue author of ClaudeOS's comments at all.

Polling is ETag-conditional at roughly a 60-second cadence. A conditional request returning 304 does
not count against the primary rate limit, so an idle sweep is effectively free — about 2.4% of the
hourly budget at that cadence. The list endpoints return an ETag but **no** last-modified header, so
ETags are the only usable validator. Rate-limit and retry-after headers must be honoured, and
secondary rate limits are a separate mechanism from primary ones.

### Selection and idempotency

Every open issue in the lab repo is in scope. A `claudeos:triaged` label marks an issue as done.
Both facts come from one conditional request that returns numbers, bodies, labels and timestamps
together.

**A timestamp watermark must not be used.** Posting the triage comment bumps the issue's own updated
timestamp, which leaves no safe setting: anchored at fetch time the issue matches forever and
re-triages indefinitely; advanced to post time, any human comment arriving in that window is
silently and permanently skipped. Idempotency must be a predicate on the issue's content, never a
time comparison.

Removing the label by hand is the re-triage trigger. Accepted limitations: editing an issue body
after triage does not re-trigger, and neither do further comments.

### The verdict

A prose comment followed by a hidden HTML comment carrying the machine-readable payload. GitHub
renders the latter invisibly, so a human sees only prose, while the app parses it for the UI.

The following shape came out of a prototype run against a real lab issue and encodes decisions prose
would state less precisely. It is the decision-rich core, not a complete schema — field naming is an
implementation matter.

```
<!-- claudeos-triage
{ "v": 1,
  "verdict": "diagnosed" | "refuted" | "inconclusive" | "no_fault_found",
  "confidence": "low" | "medium" | "high",
  "severity": "critical" | "serious" | "warning" | "info",
  "refuted": ["<named hypothesis ruled out>", ...],
  "evidence": [ { "tool": "...",
                  "status": "success" | "no_data" | "truncated" | "excluded",
                  "note": "..." } ],
  "remediation": { "kind": "fix" | "diagnostic" | "none", "executable": false },
  "cost": { "input": 0, "output": 0, "usd": 0.0 } }
-->
```

Three properties of that shape are load-bearing, each forced by something the prototype hit against
real data:

- **Four verdict values, not two.** The real run's leading hypothesis was *refuted* — a genuinely
  useful result that a diagnosed/not-diagnosed pair would have reported as failure. Separately,
  "looked and found nothing wrong" must be distinguishable from "could not tell", because the UI has
  to distinguish both from "not yet triaged".
- **Status per finding, not per comment.** One run mixed successful, empty and truncated results. A
  single confidence figure at the top would have hidden that a key field came back empty.
- **Remediation carries a kind.** The honest output of the real run was a diagnostic — enable debug
  logging and retry — not a repair. A schema that assumes a fix will manufacture one.

An `excluded` evidence status exists so that evidence deliberately *not* used can be named with its
reason. In the prototype run, two warnings sat next to the relevant ones in the log and belonged to
different devices; saying so is more useful than omitting them.

### The triage run

Agentic, not one-shot. A single pre-fetched context is not viable: the Home Assistant slice alone
measures roughly 333,000 tokens — nearly three times the whole context budget — for one of four
configured systems. A targeted slice would fit, but choosing it requires knowing which system the
issue concerns, which is the first pass. The tool layer also filters on arguments the agent chooses
per issue.

The existing one-shot analysis of UniFi security events remains correct and unchanged: a security
event is a small payload of known shape. Arbitrary lab issues are not.

System prompt: split the existing chat prompt into a shared base and a caller-specific tail. The base
carries the lab description — deliberately vague on hardware, so every specific must come from a tool
result — the tool-tiering guidance, and the result-status semantics including the rule that an empty
result is not the same as a healthy one. The triage tail states the job and the output contract.

Model and reasoning effort follow the chat defaults absent a reason to differ.

Precondition: the Anthropic SDK is required. The one-shot analysis path has a stdlib fallback; the
agentic loop does not. Without the SDK, triage is unavailable, exactly as chat is today. This should
be a stated precondition surfaced in the UI, not a silent failure.

### Cost control

Three layers.

**Per run:** an iteration ceiling lower than chat's, a wall-clock cap, and a real mid-loop input
check. A typical run measures around $0.19 at current introductory rates; the unbounded worst case is
$3.15–$4.90. The ceiling should bound a run near $0.60.

**Concurrency:** strictly one triage at a time, following the existing report generator's pattern. A
backlog drains one issue per sweep. This also removes any race on the ledger.

**Per day:** a persisted ledger in the data directory. Skip and log at a soft limit; notify at a hard
limit; disable triage until reset at twice the hard limit. The operations log cannot serve as this
ledger — it stores message strings, is append-only without rotation, and reloads only its tail.

**Retry:** none. A failed run marks the issue and moves on; re-triage is the deliberate act of
removing the label. **The marker must be written on the failure path.** The existing weekly report
demonstrates the alternative: its last-run timestamp is written only after a successful generation,
so a failure leaves the schedule permanently due and the five-minute scheduler retries until the next
weekly slot — up to about 2,016 paid attempts from one failure. That is a live defect in shipped code
and should be filed separately; this feature must not reproduce its shape.

The loop must also account for spend on the error path. Today an API failure returns without the
finishing step, so accumulated cost is discarded.

### The surface

A fifth top-level navigation entry with a self-contained view module. The hash router already parses
a route argument, so an issue detail route comes free.

Not an Operations tab. That is cheaper today — three lines and a renderer — and more expensive every
month after: the operations view is already the largest frontend module at roughly 1,500 lines and a
full Lab Issues tab would add 250–400 more. Decisively, an operations tab's status dot is coloured
from a polled system's state, and Lab Issues has none, so "GitHub unreachable" could not surface
there without special-casing.

The index is a dense table: open/closed indicator, issue number and title, triage state, verdict,
severity, age, and a link out. Seven states must remain visually distinct — untriaged, queued,
running, failed, and the four verdict values. Reuse the existing status pill, LED and finding-border
vocabulary rather than inventing new classes. The detail route renders the verdict as a finding-style
card; a manual trigger follows the pattern of the existing report run-now control.

The empty state and the error state must not resemble each other. An unreachable GitHub or a rejected
token renders as an alarming, critical-bordered panel stating the issue state is **unknown, not
empty**. Never render an empty list on a fetch failure. Given the configured token expires in July
2027, this state is a certainty rather than a hypothetical. A budget-exhausted state must also be
visible, or a stalled queue looks identical to an idle one.

### Notification and reporting

**Notify only on a diagnosed verdict at critical or serious severity.** Every existing notification
caller alerts on a transition that needs hands, never on steady state, and high priority is reserved
for lab-down and failing hardware. A triage verdict should not outrank those.

The existing five-minute mute keys on the exact title string and will not help here, since titles
carry issue numbers and are never identical. The severity gate is the control.

A single failed run goes to the operations log only, which already reaches the weekly report through
its recent-warnings sweep. Only systemic failure — a revoked or expired token, or an exhausted budget
— notifies at high priority. Those are states where the feature has silently stopped working.

The weekly report gains counts by verdict, unresolved diagnosed issues, and issues untriaged beyond a
threshold. That last is a backlog signal with no notification equivalent and the most likely way to
notice triage has quietly stopped.

## Testing Decisions

**This feature introduces the repo's first tests.** There is no existing test suite, no runner, and
no prior art to follow — the Testing Decisions here establish the practice rather than extend it.
This is deliberately narrow: it is not a mandate to backfill tests across the codebase.

**Runner:** the Python standard library's unit test framework, discovered and run with the
interpreter directly. No new dependency, consistent with the project's stdlib-first constraint and
with the plain-system-python environment the container targets.

**One seam: the lab issues module's public interface.** Its GitHub caller and its triage function are
injected, so tests drive the real module against a fake GitHub returning canned payloads and ETags,
and a stub triage function returning canned verdicts. Nothing reaches the network or the Anthropic
API.

**What makes a good test here:** it asserts observable behaviour at that interface — what the module
returns, which comments and labels it would write, whether it would spend — and never inspects
private state or internal call ordering. A test that breaks when the module is reorganised without a
behaviour change is a bad test. Prefer table-driven cases over one test per branch.

The behaviours worth covering, in rough priority — these are the ones that fail silently and
expensively:

1. An issue carrying the triaged label is never triaged again.
2. Removing the label makes an issue eligible again.
3. Posting a comment, which bumps the issue's timestamp, does **not** cause re-triage. This is the
   feedback loop the design exists to avoid and is the single most important test.
4. A 304 response is treated as success and leaves the cache intact.
5. A failed triage run still marks the issue, so nothing retries in a loop.
6. A failed run's cost still counts against the daily budget.
7. Reaching the soft budget skips and logs; the hard budget notifies; twice the hard budget disables.
8. Only one triage runs at a time when a sweep finds several eligible issues.
9. The hidden verdict block round-trips: a written verdict parses back to the same values.
10. An unparseable or absent verdict block on a labelled issue degrades gracefully rather than
    crashing the sweep.
11. A rejected token produces the unknown state, not an empty list.
12. All four verdict values, and the untriaged state, are distinguishable in whatever the module
    hands the UI.

**Not tested at this seam**, and consciously so: the shared tool loop, the frontend view, and the
live GitHub API. The loop's read-only guarantee is a genuine safety property and testing it directly
was considered and deferred — if a second seam is ever added, that is the one.

## Out of Scope

- **Auto-fix and any unattended write against the lab.** A write-capable agent acting with no human
  present at the moment of action is a different safety class, and the existing approval boundary
  exists precisely to prevent it. Returns as a separate effort, if at all.
- **Comment-triggered fix**, where a command on the issue re-runs triage with write tools enabled.
  Same reason, plus unanswered questions about what a comment authorises, how stale a plan may be
  before executing it is unsafe, and partial failure.
- **Auto-filing issues from ClaudeOS's own alerts.** This would turn a triage inbox into an incident
  log, requiring flap deduplication, auto-close on recovery, severity thresholds and an
  alert-to-issue lifecycle — and partly duplicating the notification layer that already exists.
- **An Ops Chat tool for reading the lab issue queue.** Sits past this feature's edge. Also note the
  tool schema list has a single filter axis, so adding it would arm the triage agent as well; and the
  genuinely useful version — recognising that an issue resembles a previously diagnosed one — is
  issue correlation, a feature in its own right.
- **Backfilling tests for existing modules.** The test seam here covers the new module only.
- **Extracting the cache-and-staleness chassis** from the existing sweepers. Only the thread-spawn
  boilerplate is extracted; see the correction on the sweeper-duplication issue.

## Further Notes

**Sequencing.** The sweeper spawn helper and the tool loop extraction are both refactors of working
code and should land before the feature, separately and verifiably, rather than tangled with new
behaviour.

**Two pre-existing defects were found while planning this**, both unrelated to the feature and both
worth filing on their own:

1. The weekly report's retry storm described under Cost Control.
2. The chat context budget being checked only at turn entry, never during a turn.

**One assumption to verify cheaply during the build.** Because the token posts as its human owner,
GitHub may send the issue author no notification for ClaudeOS's comments — which would invert the
usual concern about duplicate notifications. One real comment settles it. The notification decision
holds either way, but it changes how much the severity gate is doing.

**Provisioning is already done.** The private lab repo exists, the scoped token is minted and expires
in July 2027, and a real seed issue is in place for testing against.

**Source material.** The research findings on the GitHub API live on a research branch; the verdict
shape prototype, including three drafted variants against real data, lives on a prototype branch.
Both are linked from their tickets on the map.
