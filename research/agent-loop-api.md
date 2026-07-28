# Research: agent-loop options on the Anthropic API

Ticket: [#5](https://github.com/xpenno255/claudeos/issues/5) · Part of map issue [#4](https://github.com/xpenno255/claudeos/issues/4)
Date: 2026-07-25

## Context (what we're building on)

- ClaudeOS is a **Python 3.10+ stdlib HTTP server**. The `anthropic` SDK is available only via `.venv`
  (`anthropic 0.116.0` on Python 3.14.4 — verified locally; `client.beta.messages.tool_runner`,
  `@beta_tool`, and `messages.parse` are all present).
- `app/ai.py` has dual paths: SDK when importable, raw-urllib fallback (`app/httpclient.py`) otherwise.
  Both paths do **one-shot, non-streaming** `messages` calls with `thinking: {"type": "adaptive"}` and
  `output_config.format` (json_schema) on model `claude-opus-4-8`.
- `app/httpclient.py` buffers the entire response body before parsing — it **cannot consume SSE**.
- Standing decisions from #4 that constrain the loop: **every write action needs a mandatory user
  confirmation**, chat is strictly user-initiated, single trusted user.

---

## 1. SDK tool runner vs manual tool-use loop

### Tool runner (`client.beta.messages.tool_runner`)

- Present in the `.venv` SDK (0.116.0). Define tools as plain functions with the `@beta_tool`
  decorator (schema auto-generated from the signature + docstring), pass them to
  `client.beta.messages.tool_runner(...)`, iterate: each iteration yields the assistant
  `BetaMessage` *before* tools run; the runner executes the tool functions, feeds `tool_result`s
  back, and stops when Claude stops calling tools (or at `max_iterations`).
- **Hooks** cover most "I need control" cases without a manual loop: inspect pending `tool_use`
  blocks in the yielded message, override with `set_messages_params()` / `append_messages()`,
  intercept results via `generate_tool_call_response()`, per-turn retries, `stream=True` support.
- **Maturity**: still under the `beta` namespace (beta in all 7 SDKs), though semantics are
  consistent across SDKs. Known sharp edge in 0.116.0: it does not auto-resume `pause_turn`
  (only relevant for server-side tools, which ClaudeOS won't use).
- **What you give up**: the runner owns the loop and the conversation-history bookkeeping
  in-process. That matters for us — see the confirmation-gate point in the Recommendation.

### Manual loop over the Messages API

Wire protocol (identical whether called via SDK or raw HTTP):

1. Send `tools=[{name, description, input_schema, strict?}]` + `messages`.
2. Response with `stop_reason: "tool_use"` contains one **or several parallel** `tool_use` blocks
   (`{type, id, name, input}`); parallel calls are on by default
   (`tool_choice: {"type": "auto", "disable_parallel_tool_use": true}` to force one at a time).
3. Append the assistant's **full `content`** to `messages`, execute tools, and return **all**
   `tool_result` blocks (`{type: "tool_result", tool_use_id, content, is_error?}`) in a **single**
   user message. Failed tools get `is_error: true`, never dropped.
4. Repeat until `stop_reason == "end_turn"` (also handle `max_tokens` and `refusal`).
5. `tool_choice` options: `auto` (default) / `any` / `{type: "tool", name}` / `none`.

The manual loop is GA, has no beta dependency, and — critically — the conversation state is just a
serializable `messages` list you own, so the loop can be **suspended and resumed across HTTP
requests**.

## 2. Streaming with tool calls

Yes — `stream: true` works with `tools`; text streams token-by-token and tool calls arrive as
structured deltas in the same response. Event sequence per response:

```
message_start
  content_block_start   (index N, type: thinking | text | tool_use)
  content_block_delta   (thinking_delta | text_delta | input_json_delta {partial_json})
  content_block_stop
  ... (blocks repeat; thinking, text, and tool_use blocks can all appear in one response,
       each with its own start/delta/stop keyed by index)
message_delta           (stop_reason e.g. "tool_use", usage)
message_stop
```

- **Tool inputs stream as string fragments** (`input_json_delta.partial_json`), not parsed objects.
  A correct client must buffer fragments per block `index` and `json.loads` the concatenation at
  `content_block_stop`.
- The Python SDK's `client.messages.stream(...)` helper does all accumulation:
  `stream.text_stream` for live text, `stream.get_final_message()` for the assembled message —
  ideal for "stream text to the UI, then check `stop_reason == "tool_use"` and continue the loop".
- **Adaptive thinking interaction**: with `thinking: {"type": "adaptive"}` the model interleaves
  thinking between tool calls automatically. Thinking blocks must be passed back **unchanged** when
  you continue the conversation with `tool_result`s (the SDK helper preserves them; a hand-rolled
  client must too, including signature fields).

## 3. Structured outputs alongside tools

- `output_config: {"format": {"type": "json_schema", ...}}` (the shape `app/ai.py` already uses)
  **can be combined with `tools` in the same request**, and does not conflict with `tool_choice`.
  Structured outputs are GA — no beta header. (The old top-level `output_format` is deprecated;
  the repo is already on the correct `output_config.format`.)
- Separately, **`strict: true` on a tool definition** (top-level field, schema needs
  `additionalProperties: false` + `required`) guarantees `tool_use.input` validates against the
  schema — worth using on every ClaudeOS connector tool, especially write tools whose args feed
  the confirmation UI.
- For the chat feature itself, the final answer is prose, so `output_config.format` isn't needed
  in the loop; it stays as-is for the existing one-shot analyses (log/ZHA/event/report).

## 4. Model choice + per-conversation cost

Current lineup (Claude API, mid-2026, per MTok in/out):

| Model | ID | Price in/out | Notes |
|---|---|---|---|
| Claude Fable 5 | `claude-fable-5` | $10 / $50 | Overkill tier for homelab chat |
| Claude Opus 5 | `claude-opus-5` | $5 / $25 | Current Opus; same price as Opus 4.8 |
| Claude Opus 4.8 | `claude-opus-4-8` | $5 / $25 | What `app/ai.py` uses today |
| **Claude Sonnet 5** | `claude-sonnet-5` | $3 / $15 (**intro $2 / $10 through 2026-08-31**) | Near-Opus agentic quality; 1M ctx |
| Claude Sonnet 4.6 | `claude-sonnet-4-6` | $3 / $15 | Previous Sonnet |
| Claude Haiku 4.5 | `claude-haiku-4-5` | $1 / $5 | 200K ctx; weakest multi-step reasoning |

**Cost model for a homelab Q&A conversation** (assumptions: ~4–5K tokens of system prompt +
~12 connector tool schemas, ~10 tool calls → ~11 API round-trips, tool results are ~1K-token JSON
summaries, history re-sent every round): cumulative input ≈ 110–150K tokens, output ≈ 5–8K tokens.

| Model | Uncached | With prompt caching (bulk of prefix at 0.1×) |
|---|---|---|
| Opus 5 / 4.8 | ≈ $0.75–0.95 | ≈ $0.15–0.25 |
| Sonnet 5 | ≈ $0.45–0.55 (intro ≈ $0.30–0.37) | ≈ $0.08–0.15 |
| Haiku 4.5 | ≈ $0.15–0.20 | ≈ $0.03–0.05 |

**Prompt caching is the dominant lever**, not model choice: an agent loop re-sends the whole
conversation on every tool iteration, so cache the static prefix (tools render first, then system —
one `cache_control: {"type": "ephemeral"}` breakpoint on the last system block caches both;
writes cost 1.25×, reads 0.1×; min cacheable prefix 1024 tokens on Sonnet 5/Opus 4.8, 512 on
Opus 5) and put a second breakpoint on the conversation tail. Keep the system prompt byte-stable
(no timestamps) and the tool list deterministic, or the cache silently never hits.

**Pick**: `claude-sonnet-5` with `thinking: {"type": "adaptive"}` and `output_config.effort`
`"medium"` (drop to `"low"` for snappier chat) as the chat default — near-Opus tool-use quality at
~40–60% of Opus cost, ~$0.10–0.15 per conversation cached. Keep Opus for the existing deep
one-shot analyses. Haiku 4.5 is a fine budget fallback for simple lookups but risks shallow
multi-step diagnosis ("why is X slow?" chains).

## 5. Is the raw-urllib fallback still viable once tools + streaming are involved?

**Non-streaming tools**: technically yes — a manual loop is just repeated POSTs, which
`httpclient.request` can do. But you'd re-implement what the SDK gives free: retries with backoff
on 429/529/5xx (httpclient has none), typed errors, thinking-block passthrough rules, parallel
tool-call handling, and every future wire change.

**Streaming**: no. `httpclient.py` reads the entire body before parsing — it cannot consume SSE.
Supporting streamed tool use over raw urllib means hand-writing an SSE parser, per-index
`input_json_delta` accumulation, thinking/signature block handling, and mid-stream error recovery
in stdlib: a small unmaintained SDK clone, for a fallback path that only exists in case someone
runs the server with system Python.

**Conclusion**: **chat should require the SDK (`.venv`) outright.** When `HAS_SDK` is false, the
chat endpoint returns a clear error ("run the server with `.venv/bin/python3` to use chat"). The
raw path stays exactly where it is today — the one-shot `ask_json` analyses — so nothing regresses.

---

## Recommendation

1. **Build the chat loop as a manual tool-use loop on the SDK**, using
   `client.messages.stream(...)` per iteration (stream text to the browser over SSE from our
   stdlib server, `get_final_message()`, continue while `stop_reason == "tool_use"`).
   - **Why not the tool runner**: ClaudeOS's mandatory write-confirmation spans HTTP requests —
     the loop must *stop*, persist the `messages` list, return "pending confirmation" to the
     browser, and resume in a later request when the user clicks confirm. That suspend/resume is
     natural when state is a plain messages list, and awkward inside the runner's in-process
     iterator. The runner is also still beta, and its main win (auto-generated schemas + loop
     plumbing) is small for ~a dozen hand-written connector tools. Revisit if it goes GA and chat
     ever becomes single-request.
2. **Chat requires the venv SDK; raw urllib stays for the legacy one-shot analyses only** (§5).
3. **Model**: `claude-sonnet-5`, adaptive thinking, effort `medium` (tunable), `max_tokens` sized
   for streaming; keep `claude-opus-4-8`/Opus 5 for weekly report / log analysis. Expected cost
   ≈ **$0.10–0.15 per conversation** with caching, worst case ≈ $0.55 uncached.
4. **Prompt caching from day one**: breakpoint after tools+system, second breakpoint on the
   conversation tail; frozen system prompt, deterministic tool ordering.
5. **`strict: true` on every connector tool schema** (`additionalProperties: false`), so write-tool
   arguments shown in the confirmation dialog are guaranteed schema-valid.
6. Loop hygiene: return all parallel `tool_result`s in one user message; `is_error: true` for
   failed connector calls; handle `end_turn` / `max_tokens` / `refusal`; cap iterations
   (~15) as the per-conversation budget guard.

## Sources

- Tool use overview (wire protocol, tool_choice, parallel calls): <https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview>
- Tool runner (beta helper, `@beta_tool`, hooks): <https://platform.claude.com/docs/en/build-with-claude/tool-use/tool-runner> and <https://github.com/anthropics/anthropic-sdk-python>
- Streaming (event types, `input_json_delta`, accumulation): <https://platform.claude.com/docs/en/build-with-claude/streaming>
- Structured outputs (`output_config.format`, `strict` tools, GA status): <https://platform.claude.com/docs/en/build-with-claude/structured-outputs>
- Adaptive thinking (interleaving with tools, block passthrough): <https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking>
- Models & pricing: <https://platform.claude.com/docs/en/about-claude/models/overview> and <https://platform.claude.com/docs/en/pricing>
- Prompt caching (multipliers, prefix rules, minimums): <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>
- Stop reasons (`tool_use`, `pause_turn`, `refusal`): <https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons>
- Managed Agents (considered, rejected for self-hosted homelab loop): <https://platform.claude.com/docs/en/managed-agents/overview>
- Local verification: `.venv` `anthropic==0.116.0` (`tool_runner`, `beta_tool`, `parse` present); `app/httpclient.py` (no SSE support).
