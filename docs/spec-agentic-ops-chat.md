# Spec: Agentic Ops Chat

**Status**: build-ready. Every design decision below is settled; nothing here
should require a fresh decision mid-build.

**Provenance**: assembled from wayfinder map
[#4](https://github.com/xpenno255/claudeos/issues/4) — three research findings
([#5](https://github.com/xpenno255/claudeos/issues/5),
[#6](https://github.com/xpenno255/claudeos/issues/6),
[#7](https://github.com/xpenno255/claudeos/issues/7)) and six decisions
([#8](https://github.com/xpenno255/claudeos/issues/8),
[#9](https://github.com/xpenno255/claudeos/issues/9),
[#10](https://github.com/xpenno255/claudeos/issues/10),
[#11](https://github.com/xpenno255/claudeos/issues/11),
[#13](https://github.com/xpenno255/claudeos/issues/13)). Primary sources live on
branches `research/agent-loop-api`, `research/prior-art-agentic-ops`,
`research/stdlib-streaming`, and `prototype/chat-ui`.

Numbers marked **(tunable)** are defaults chosen so the build never stalls, not
decisions to relitigate. Everything else is fixed.

---

## 1. What this is

A chat panel where Claude answers questions about the homelab — "why is plex
buffering?" — using curated tools over every connector, and can *make changes*
with a **mandatory confirmation step** before anything is written.

**In scope**: read tools across all five connectors; four families of write
actions behind confirmation; a streaming web chat panel; conversation history.

**Out of scope** (ruled out on the map, do not build):

- Proactive or scheduled chat — the agent never initiates a conversation.
- Multi-user and auth — inherits ClaudeOS's single-trusted-user, no-auth model.
- Non-web chat surfaces (Telegram etc.) — a separate effort once this proves out.
- Spill-to-disk for oversized tool output — caps and omission notices instead (§5).

## 2. Prerequisites

Chat **hard-requires the `anthropic` SDK**, i.e. the server must run as
`.venv/bin/python3 server.py`. Streaming tool use over raw urllib would mean
hand-rolling an SSE parser, a partial-JSON accumulator and thinking-block
passthrough — a mini-SDK. When the SDK is missing, every chat route returns a
clear error telling the user to run from `.venv`, and the UI shows that message
instead of a composer.

`app/ai.py`'s raw-urllib path stays exactly as-is for the existing one-shot
analyses (HA log analysis, UniFi event triage, weekly report). Those do **not**
fold into the chat loop: they are single-shot, need no tools, and suit Opus where
chat wants Sonnet's latency. Chat is purely additive.

## 3. Module layout

| Module | Responsibility |
|---|---|
| `app/chat.py` | the agent loop, conversation store, turn lifecycle, cost accounting |
| `app/tools.py` | tool catalog, JSON schemas, result envelope, dispatch, **approval gate** |
| `server.py` | chat routes, including the SSE streaming route that bypasses `_dispatch` |
| `public/js/views/chat.js` | the MISSION LOG UI |
| `app/ai.py` | untouched |

The approval gate lives in `app/tools.py`'s dispatch layer — **in code, never in
the prompt**. A system-prompt instruction to "please confirm first" is not a gate.

## 4. Model configuration

- Model `claude-sonnet-5`; adaptive thinking, effort `medium` **(tunable)**.
- `max_tokens` 8192 per turn **(tunable)**.
- `strict: true` on every tool schema (`additionalProperties: false`), so write-tool
  arguments rendered in the confirmation card are schema-guaranteed.
- **Prompt caching from day one**: one breakpoint after tools+system, a second on
  the conversation tail. The loop re-sends history every iteration, so caching
  matters more than model choice. Keep the system prompt frozen and tool ordering
  deterministic or the prefix won't match.
- Expected cost ≈ **$0.10–0.15 per conversation** cached, ~$0.45–0.55 uncached.
- **Known cache miss**: a turn suspended at an approval for longer than the cache
  TTL loses its prefix and the resumed turn re-charges full input price. That is
  the correct trade — do not keep the cache warm artificially.

## 5. Read tools

**Curated tools only.** No raw-API escape hatch, no 1:1 route passthrough. If a
question needs data no tool exposes, the agent says so and a tool gets added.
Each tool wraps an existing connector function or server-side cache, takes
minimal parameters, and returns the connectors' existing JSON shapes.

**Tier 1 — lab-wide precomputed evidence (the agent's first resort):**

| Tool | Source | Default cap **(tunable)** |
|---|---|---|
| `get_lab_overview` | `poller.snapshot()` — every system's summary, up/down, last error | — |
| `get_metric_history` | `poller.history()` for a (system, metric) | 120 points |
| `get_ops_log` | `oplog.recent()` | 120 entries |
| `get_uptime_monitors` | `monitors.list_monitors()` — 24h uptime %, status | — |

**Tier 2 — per-connector queries (second resort):**

- **unifi**: `unifi_devices`, `unifi_clients`, `unifi_events` (category, page; 50/page),
  `unifi_anomalies`, `unifi_insights`
- **proxmox**: `proxmox_nodes`, `proxmox_guests`, `proxmox_guest_detail` (incl. PSI + RRD),
  `proxmox_storage`, `proxmox_disk_health` (SMART cache)
- **docker**: `docker_containers`, `docker_container_logs` (tail 100),
  `docker_storage_report`, `docker_gpu_report`, `docker_image_updates`
- **homeassistant**: `ha_summary`, `ha_entities` (domain/search filter, 200 rows),
  `ha_error_log`, `ha_zha_devices`, `ha_updates`, `ha_system_info`
- **synology**: `synology_status`, `synology_storage`

Tool **descriptions are written for the model**, not for developers: say what the
tool answers, warn when output is large, and steer Tier 1 before Tier 2.

**Output budgets.** Every list/log tool has a default cap plus narrowing
parameters. Over-cap responses **must state the omission** — `"142 earlier lines
omitted — re-query with a larger tail or a narrower filter"`. Silent truncation is
forbidden; the model cannot reason about data it doesn't know is missing.

## 6. Write tools

Four families, each wrapping a connector function that already exists:

| Tool | Actions | Notes |
|---|---|---|
| `docker_container_action` | `start`, `stop`, `restart` | existing `CONTAINER_ACTIONS` |
| `proxmox_guest_action` | `start`, `shutdown`, `reboot`, `stop` | existing `VM_ACTIONS`; `stop` is a hard power-cut |
| `ha_call_service` | services in the domain allowlist below | wraps `call_service` |
| `unifi_restart_device` | reboot by MAC | — |

**HA domain allowlist** (default-deny, enforced in code):
`light`, `switch`, `fan`, `input_boolean`, `automation`, `script`, `scene`,
`media_player`, `climate`.

Everything else is rejected by the dispatch layer — including `water_heater`,
`siren`, `button` and `camera`. Default-deny is the point: if a `lock` or garage
`cover` appears in HA later it stays unavailable until someone deliberately edits
the allowlist. (Checked live 2026-07-18: 4803 entities, no `lock`, `cover`,
`alarm_control_panel` or `vacuum` domains exist today.)

**Never define as a tool** — the denylist is enforced by absence, so there is
nothing for the model to call:

- `docker.exec_run` — arbitrary command execution in a container
- `unifi.upgrade_device` — multi-minute outage, not cleanly reversible
- every non-allowlisted HA domain
- ClaudeOS's own configuration surface: credentials, Setup saves, system unlink
- monitor CRUD

## 7. Tool result envelope

Every tool — read or write — returns the same envelope (HolmesGPT's
`StructuredToolResult`, adapted):

```python
{
  "status": "success" | "error" | "no_data" | "approval_required",
  "data": ...,            # the connector's JSON, or None
  "error": str | None,    # human-readable, credentials redacted
  "invocation": str,      # e.g. 'docker_container_logs(name="plex", tail=100)'
  "params": {...},
  "elapsed_ms": int,
  "omitted": str | None,  # the omission notice, if any
}
```

`no_data` is a **distinct status** from `error`, and both are distinct from a
successful empty list. This distinction is load-bearing: it is what stops an empty
result from reading as "no problem" (§11).

Connector exceptions map to `error` with the message; the loop returns
`is_error: true` on that `tool_result` block so the model can react rather than
crash.

## 8. The agent loop

A **manual tool-use loop on the SDK** — `client.messages.stream(...)` per
iteration, accumulate with `get_final_message()`, continue while
`stop_reason == "tool_use"`. The SDK tool-runner is rejected: it is beta, and its
in-process iterator fights the fact that write-confirmation spans HTTP requests.
Suspend/resume is trivial when state is a serializable `messages` list.

Rules:

- **15 iterations maximum per turn (tunable)**, with **tools withheld on the final
  iteration** so a turn always ends in prose rather than an exception.
- **Duplicate-call guard**: an identical tool+params call returns an error telling
  the agent to vary its approach.
- All parallel `tool_result` blocks for one assistant turn go back in **one** user
  message.
- Handle `end_turn`, `max_tokens` and `refusal` explicitly; adaptive-thinking
  blocks are echoed back unchanged.

## 9. Streaming transport

`POST /api/chat/stream` — a dedicated route that **bypasses `_dispatch`'s JSON
wrapper**:

- Response: `200`, `Content-Type: text/event-stream`, `Cache-Control: no-store`,
  `X-Accel-Buffering: no`. Never gzip the stream.
- Keep `protocol_version` at its HTTP/1.0 default: connection-close legally
  delimits an unbounded body, so no Content-Length and no chunked framing. `wfile`
  is unbuffered, so writes hit the wire immediately.
- Event vocabulary: `token`, `tool_start`, `tool_result`, `approval_required`,
  `cost`, `done`, `error`, plus `: ping` heartbeats every **15s (tunable)** during
  long tool calls.
- Always end with an explicit `event: done` — close-delimited bodies are otherwise
  ambiguous.
- The route **must catch `BrokenPipeError`/`ConnectionResetError` itself** and treat
  them as a normal end-of-stream; `handle_one_request` only catches `TimeoutError`,
  so an uncaught disconnect prints a traceback per closed tab.

Client: `fetch(..., { method: "POST", signal })` +
`response.body.getReader()` + `TextDecoder.decode(chunk, { stream: true })`,
parsing SSE blocks on `\n\n`. `AbortController.abort()` is the stop button.
**No auto-reconnect** — a stream that dies before `done` marks the turn failed and
offers retry. EventSource is unusable here: GET-only, no body, and its
auto-reconnect would replay turns.

One daemon thread per in-flight turn; single-digit worst case on a single-user
homelab.

## 10. Approval flow

The core safety mechanism. `approval_required` is a **tool status**, not a prompt
convention:

1. The loop reaches a write tool. `app/tools.py` returns `approval_required`
   **without executing**, carrying a **single-use pending-action id**.
2. The loop **suspends**: persist the `messages` list plus the pending record, emit
   `approval_required` over SSE, end the stream cleanly.
3. `POST /api/chat/{id}/approve` resumes the turn (also an SSE stream, same event
   vocabulary). Body carries the pending id and the decision.
4. On resume the dispatch layer **re-validates the action against the allowlist** —
   never trusting the stored pending record alone — then:
   - **approve** → execute, real result becomes the `tool_result`
   - **deny** → `is_error` denial text as the `tool_result`
   - **deny-with-guidance** → the user's free text becomes the `tool_result`, so the
     agent redirects instead of dead-ending
5. The loop continues from the persisted messages.

Guards:

- **Expiry 30 minutes.** An unanswered pending approval is recorded as timed-out and
  **never executed**.
- **Single-use id** — an approval cannot be replayed.
- **Invalidated on process restart.** The conversation survives; the pending write is
  dropped and recorded as abandoned. The world may have changed while ClaudeOS was
  down, so a stale approval must never fire.
- **No "always allow" tier** of any kind. Every write confirms, every time.
- If the model emits several write calls in one turn, each becomes its own
  independently-approvable pending action.

**Audit**: `oplog` records request → approve/deny/timeout → result, reusing the
existing `oplog.add("action", …)` convention and tagged as chat-originated so the
Ops log distinguishes chat-driven changes from UI-driven ones.

## 11. System prompt: citation, grounding, coverage

Three rules, all prompt-side (the *gate* is code; this is about honesty of prose):

**Quote the evidence, never name the tool.** The prose must not say "based on the
tool output" or "per the docker_container_logs tool" — it reads robotic. Instead
claims carry their own evidence: quote the decisive line verbatim (`the log says
"hardware acceleration unavailable — no usable device"`) and state values as
fetched ("CPU is at 78%"). The expandable tool strips are the citation mechanism
and audit trail. This also keeps answers portable — they read correctly when copied
into an ntfy alert, a report, or an issue, where no strips exist.

**Verification pass before answering.** Trace each factual claim to a tool result
from this conversation. Anything not directly confirmed is rewritten into hedging
language — "possible cause", "might be", "likely" — and overconfident root-cause
claims are downgraded. **Never state a metric it did not fetch**: no invented
percentages, temperatures, versions or counts. General homelab knowledge is marked
as inference, not observation.

**Coverage honesty.**

- When a tool returns `error` or `no_data`, the **answer body** must state which area
  went unverified and why — "I couldn't reach the NAS, so anything library-related is
  unchecked". The red strip is the audit trail, not the disclosure.
- **An empty result is never reported as health.** "No errors in the 100 log lines I
  fetched" is correct; "everything is fine" is not.
- When a tool reports omissions, a conclusion drawn from the visible slice must say so.

## 12. Persistence

`data/chats.json`, **last 20 conversations (tunable)**, atomic `tmp` +
`os.replace` writes exactly like `monitors.py`. Oldest evicted past the limit.

```
{
  "id": "<hex>",
  "title": "<first user message, truncated>",
  "created": <ts>, "updated": <ts>,
  "messages": [ ... Anthropic wire format, including tool_use/tool_result blocks ... ],
  "turns": [ { "usage": {...}, "cost_usd": 0.031, "tools": 4, "ts": <ts> } ],
  "pending": { "id": "<single-use>", "tool": "...", "params": {...}, "expires": <ts> } | null
}
```

Messages persist in **Anthropic wire format** so a suspended turn resumes verbatim.
Transcripts live in the gitignored `data/` volume alongside the master key — no new
risk class, and they inherit the container's `/data` mount.

## 13. Concurrency and limits

- **One in-flight turn per conversation**, enforced server-side; a second turn on the
  same conversation is rejected with "a turn is already in progress". This prevents
  two loops interleaving tool calls on one `messages` list. Separate conversations
  stream concurrently.
- **Context: hard cap, no sliding window.** At **80%** of a **120K-token (tunable)**
  conversation budget, the agent completes its answer and the UI reports the
  conversation is full with a new-conversation action. A sliding window is rejected
  twice over: it silently forgets what's still on screen, and it invalidates the cache
  prefix every turn. Auto-compaction is rejected as cost, latency and a new failure
  mode mid-conversation.
- **Cost visibility, not a ceiling.** Per-turn cost is computed from `usage` and both
  shown in the UI and recorded in the chat entry. No monthly spend cap — the
  per-conversation guard plus visible per-turn cost is proportionate here.

## 14. UI — MISSION LOG

Chat is a **destination page**: a new top-level route (`#/chat`) alongside
Dashboard / Operations / Setup, with a nav entry. Reference implementation of the
layout: variant A on branch `prototype/chat-ui` (`public/js/views/protochat.js`,
commit `85d64ee`) — **rewrite it properly**, do not promote prototype code.

- **Single wide column**, ~880px, centred. The transcript reads top-to-bottom like an
  investigation log.
- **User turns** as amber command lines (`▸ WHY IS PLEX BUFFERING?`) in the display face.
- **Tool calls as full-width instrument strips**: system-coloured left edge (reuse the
  `--s-*` identity tokens), tool name, invocation, status pill, elapsed ms. Collapsed
  by default; expand **inline** to a scrollable mono body. Evidence stays in the flow —
  no separate pane, no numbered references.
- **Approval card in flow**, full column width: amber border, exact invocation, resolved
  target, the agent's reason, a `--serious` warning line for disruptive actions, three
  actions (approve / deny / deny-with-guidance, the last revealing a free-text input),
  and the expiry + single-use id.
- **Warning line required** for: proxmox `stop` (hard power-cut, may corrupt the guest),
  reboot/shutdown of a running guest, unifi device restart (clients on that device drop),
  and climate setpoint changes.
- **Composer pinned at the bottom** of the column; SEND becomes **■ STOP** while streaming.
  While an approval is **pending**, the composer is **disabled** with a hint to resolve the
  pending action first — a suspended turn is still in flight (§13), so accepting a new question
  there would either be dropped by the one-turn rule or race the resume. (Caught by visual
  review of the prototype, which left the composer live in that state.)
- **Cost readout** right-aligned immediately above the composer.
- **Terminal states render in the flow** where the next turn would be: "CONVERSATION FULL"
  with a new-conversation action, and "STREAM INTERRUPTED — no changes were made" with a
  retry action.
- Respect `prefers-reduced-motion` for the streaming cursor, as the rest of the app does.

## 15. Routes

| Method | Path | Notes |
|---|---|---|
| POST | `/api/chat/stream` | SSE; starts or continues a conversation |
| POST | `/api/chat/{id}/approve` | SSE; resumes a suspended turn with a decision |
| GET | `/api/chats` | conversation list (id, title, updated, turn count) |
| GET | `/api/chats/{id}` | full transcript |
| DELETE | `/api/chats/{id}` | forget a conversation |

The two SSE routes bypass the JSON dispatch wrapper; the rest go through it
normally and inherit the existing error→status mapping.

## 16. Acceptance checklist

- [ ] Running without `.venv` gives a clear "run with `.venv/bin/python3`" message, not a traceback.
- [ ] A read-only question streams tokens, shows one strip per tool call, and reports per-turn cost.
- [ ] Every tool over its cap says how much was omitted.
- [ ] An unreachable system (power off the NAS) yields an `error` strip **and** an explicit
      unverified-area caveat in the answer body.
- [ ] An empty-but-successful result is never described as healthy.
- [ ] A write attempt **never executes** before approval; the card shows the exact invocation.
- [ ] Approve executes and the loop continues in the same conversation.
- [ ] Deny-with-guidance redirects the agent using the typed reason.
- [ ] A pending approval left 30 minutes is recorded timed-out and cannot then be approved.
- [ ] Restarting the server abandons a pending approval; the conversation survives.
- [ ] An approval id cannot be replayed.
- [ ] A non-allowlisted HA domain (`water_heater`) is refused by the dispatch layer.
- [ ] `docker_container_logs` on a huge log does not blow the context budget.
- [ ] Closing the tab mid-stream leaves no traceback in the server log.
- [ ] A second turn on a busy conversation is rejected cleanly.
- [ ] Hitting the context cap surfaces "conversation full" rather than silently dropping turns.
- [ ] `data/chats.json` survives restart and holds at most 20 conversations.
- [ ] Every approved write appears in the Ops log tagged as chat-originated.

## 17. Related work

Not blockers, but adjacent: issue
[#1](https://github.com/xpenno255/claudeos/issues/1) documents the connector
interface contract and moves `metrics()` behind it. `app/tools.py` sits directly on
that seam, so landing #1 first would make the tool wrappers tidier.
