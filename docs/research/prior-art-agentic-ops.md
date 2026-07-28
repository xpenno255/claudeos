# Prior art: agentic ops tools — HolmesGPT, Netdata AI, k8sgpt

Research for the agentic ops chat ([map #4](https://github.com/xpenno255/claudeos/issues/4), ticket [#6](https://github.com/xpenno255/claudeos/issues/6)). ClaudeOS is adding a chat panel where Claude gets read+write tool access over the homelab connectors (UniFi, Proxmox, Docker, Home Assistant, Synology), with every write behind mandatory confirmation. This note studies how the proven agentic-ops tools shaped the same design decisions.

All claims below are from primary sources: project source code on GitHub and first-party docs, verified 2026-07-25. HolmesGPT claims were additionally verified against a local clone of `robusta-dev/holmesgpt` at `master`.

---

## 1. HolmesGPT (robusta-dev/holmesgpt, CNCF Sandbox)

The closest analogue: a real agentic tool-calling loop over ops data sources, in production via Robusta.

### 1.1 Toolsets: curated high-level tools, not raw passthrough

- Integrations ship as **toolsets** under [`holmes/plugins/toolsets/`](https://github.com/robusta-dev/holmesgpt/tree/master/holmes/plugins/toolsets) — one YAML file or Python package per system (`kubernetes.yaml`, `prometheus/`, `grafana/`, `datadog/`, `docker.yaml`, `bash/`, ~40 integrations per the [README](https://github.com/robusta-dev/holmesgpt/blob/master/README.md), some delivered as MCP servers). Default-enabled set is small: `kubernetes/core,kubernetes/logs,robusta,internet` ([`env_vars.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/common/env_vars.py)).
- Tools are **curated, named operations** with long LLM-facing descriptions, not API passthrough. Example: `kubernetes/core` ("Read access to cluster resources (excluding secrets and other sensitive data)") defines ~16 tools like `kubernetes_jq_query`, whose description teaches usage ("More memory-efficient than bash for large queries — uses Kubernetes API pagination … batches of 500, avoiding context window overflow", plural-kind gotchas, an example jq expression) and whose body is a bash `script` template ([`kubernetes.yaml`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/plugins/toolsets/kubernetes.yaml)).
- Declaration mechanics ([`holmes/core/tools.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/core/tools.py)): YAML tools are `name` + `description` + a Jinja2 `command`/`script`; undeclared `{{params}}` are auto-inferred into the JSON-Schema parameter list and shell-escaped with `shlex.quote`. Python toolsets subclass `Tool`/`Toolset` (Pydantic), with fields for `prerequisites`, `llm_instructions` (free text injected into the system prompt per enabled toolset), `transformers`, and `approval_required_tools`. Tools render to OpenAI function-calling JSON Schema (`get_openai_format()`).
- Users can add/override toolsets with plain YAML (`--custom-toolsets`, [custom-toolsets docs](https://github.com/robusta-dev/holmesgpt/blob/master/docs/data-sources/custom-toolsets.md)); templating distinguishes `{{ var }}` (LLM-inferred param) from `${VAR}` (env var hidden from the AI).
- There is **one generic escape hatch** — a `bash` tool — but it is allowlist-gated (§1.4), not free.

### 1.2 Tool output: structured envelope, not raw stdout

Every tool returns a [`StructuredToolResult`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/core/tools.py): `status` (`SUCCESS | ERROR | NO_DATA | APPROVAL_REQUIRED | FRONTEND_PAUSE`), `error`, `return_code`, `data`, `url`, `invocation` (the exact rendered command), `params`, `elapsed_seconds`. Exit code 0 with empty stdout maps to `NO_DATA` — the model is told explicitly there was nothing, rather than being shown an empty string. The LLM-facing message ([`models.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/core/models.py)) is a `tool` role message: metadata header, error line if any, "Params used for the tool call: …", then data stringified as compact JSON to save tokens.

### 1.3 Evidence: prompt-enforced verification + full tool-call records in the API

- The system prompt ([`generic_ask.jinja2`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/plugins/prompts/generic_ask.jinja2)) bans tool name-dropping in prose ("Do not say 'based on the tool output'") but enforces evidence discipline with a mandatory final-review phase: "Verify all claims backed by tool evidence", trace each claim to a specific tool output, flag unsupported statements, rewrite overconfident root-cause claims into hedging language ("possible cause", "might be") unless directly confirmed, quote relevant log lines verbatim, never guess values you cannot see (secrets).
- The **transcript is the citation mechanism**: `LLMResult.tool_calls` carries every `ToolCallResult`; the investigate API returns a `tool_calls` array alongside the analysis (`include_tool_calls`, `include_tool_call_results` — [FEATURES.md](https://github.com/robusta-dev/holmesgpt/blob/master/FEATURES.md)), which backs the Robusta UI's evidence display.
- CLI: live `Running tool #N <name>: <one-liner>` lines, `Finished … output length … - /show N to view contents`, plus `--show-tool-output` ([`main.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/main.py)).

### 1.4 Write gating: read-only core + prefix-approval bash + per-tool approval flags

- Built-in toolsets are **read-only by design**; mutation is a separate opt-in remediation MCP toolset. A code comment in [`safeguards.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/core/safeguards.py) notes the duplicate-call safeguard "is only reasonable … if Holmes is read only and does not mutate resources".
- The bash toolset ([`bash_toolset.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/plugins/toolsets/bash/bash_toolset.py), [`validation.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/plugins/toolsets/bash/validation.py), [`default_lists.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/plugins/toolsets/bash/common/default_lists.py)) validates commands per pipe/`&&` segment against prefix allow/deny lists using a real bash AST parse (`bashlex`). Defaults allow only read verbs (`kubectl get/describe/logs/top`, `jq`, `grep`, …); `sudo`/`su` are `HARDCODED_BLOCKS` that no config can override; deny beats allow.
- **Approval loop as a first-class tool status**: `Tool.invoke` calls `requires_approval()` *before* `_invoke`; unless `context.user_approved`, an unlisted command returns `status=APPROVAL_REQUIRED` with the reason and the exact invocation ([`tools.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/core/tools.py)). The CLI prompts; approved prefixes persist to `~/.holmes/bash_approved_prefixes.yaml` (an "always allow this prefix" memory). Any toolset can mark tools approval-required via `approval_required_tools` fnmatch patterns. Escape hatches are explicit and loudly named (`BASH_TOOL_UNSAFE_ALLOW_ALL`, `--bash-always-allow` "recommended only for sandboxed environments").

### 1.5 Context/token limits: spill-to-disk, compaction, per-tool budgets

Documented in [context-management.md](https://github.com/robusta-dev/holmesgpt/blob/master/docs/reference/context-management.md); three layers:

1. **Per-tool-result spill-to-disk** ([`tool_context_window_limiter.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/core/tools_utils/tool_context_window_limiter.py)): each tool result is token-counted; over budget (default min of 15% of the context window / 25,000 tokens — [`env_vars.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/common/env_vars.py)) it is written to disk and replaced with a pointer message + size-budgeted preview: "too large to return: X/Y tokens. Saved to <path>. Use `cat` … pipe into jq/grep". The bash allowlist auto-whitelists `cat/head/tail/wc/jq <storage_path>` so the model can page through spilled data itself.
2. **Conversation compaction** ([`truncation/compaction.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/core/truncation/compaction.py)): when history + reserved output exceeds the window, the LLM summarizes history down to system prompt + summary + last user message (on by default).
3. Output-token reservation and litellm-aware max-output resolution.

A per-tool `llm_summarize` transformer (fast-model summarization above a 1000-char threshold) exists but is marked **LEGACY, disabled by default** — it predates and lost to spill-to-disk ([`llm_summarize.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/core/transformers/llm_summarize.py)).

### 1.6 Loop guardrails

- `max_steps: int = 100` ([`config.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/config.py)); the loop withholds tools on the final iteration (`tools = None if i == max_steps`) to force a text answer instead of dying mid-investigation ([`tool_calling_llm.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/core/tool_calling_llm.py)).
- **Duplicate-call safeguard**: an identical tool+params repeat returns an ERROR telling the model to change parameters ([`safeguards.py`](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/core/safeguards.py)).
- Cost is **tracked** (per-iteration cost log, `--log-costs`, OTel token metrics) but there is no dollar cutoff in code; the hard budgets are max_steps and the context mechanisms.

---

## 2. Netdata AI / MCP

Netdata inverted the architecture: instead of running its own agent loop, every agent (v2.6.0+) **is an MCP server** at `:19999/mcp` that external LLM clients drive ([docs](https://learn.netdata.cloud/docs/netdata-ai/mcp/), [src/web/mcp](https://github.com/netdata/netdata/tree/master/src/web/mcp)). Separately, Netdata Cloud sells static AI report pipelines (Insights, alert troubleshooting) and chat ([Netdata AI](https://learn.netdata.cloud/docs/netdata-ai)).

### 2.1 Tool surface: 13 curated read-only tools

Registered in [`mcp-tools.c`](https://github.com/netdata/netdata/blob/master/src/web/mcp/mcp-tools.c):

- Discovery: `list_metrics`, `get_metrics_details`, `list_nodes`, `get_nodes_details`, `list_functions`
- Data: `query_metrics` (the one raw-ish query tool, wrapped with MCP-specific limits)
- Scoring: `find_correlated_metrics`, `find_anomalous_metrics`, `find_unstable_metrics` ([`mcp-tools-weights.c`](https://github.com/netdata/netdata/blob/master/src/web/mcp/mcp-tools-weights.c))
- Alerts: `list_raised_alerts`, `list_all_alerts`, `list_alert_transitions`
- Live/logs: `execute_function` (processes, network-connections, systemd-journal, …) with function metadata fetched dynamically from each function's `info` endpoint

Descriptions are verbose guidance ("Essential for…", warnings to filter/narrow to avoid timeouts) and steer the model to run discovery tools before querying. Every tool carries the MCP `readOnly: true` annotation; the [module README](https://github.com/netdata/netdata/blob/master/src/web/mcp/README.md) states dyncfg (write config) is deliberately **not exposed** — "AI assistants cannot read or modify Netdata settings". Sensitive functions (logs, live system data) additionally require an API key + claimed agent. One caveat found in code: `execute_function` does no MCP-side filtering — it relies on the backend permission model.

### 2.2 Evidence: anomaly rates as first-class data

- `query_metrics` returns per-point "timestamp, aggregated value, **anomaly rate**, and quality flags"; dedicated scoring tools exist purely for evidence-gathering (root cause via anomaly correlation). The substrate is the per-sample "anomaly bit" from on-device k-means models ([anomaly detection docs](https://learn.netdata.cloud/docs/netdata-ai/anomaly-detection)).
- [AI Insights](https://learn.netdata.cloud/docs/netdata-ai/insights) reports are an explicitly staged pipeline: collect metrics/anomaly scores/alerts → "compress them into a structured context (summaries, correlations, timelines)" → model synthesizes narrative — with an evidence section of charts, anomaly timelines and alert context. Grounding comes from structured pre-computation, not from letting the model roam.

### 2.3 Limits: hard cardinality caps + truncation notices

From [`mcp.h`](https://github.com/netdata/netdata/blob/master/src/web/mcp/mcp.h) and tool code: `query_metrics` max 1000 points/query, data cardinality default 10 (max 500); metadata cardinality default 50; alerts default 100; default window last hour; weights timeout 300s. On truncation "the response will indicate how many items were omitted", and responses carry cardinality info so the model knows what it's sampling. No token budgeting — context is managed entirely by capping and labeling at the source.

### 2.4 Writes

MCP surface: none. The one mutating AI feature ([Alerts Automation](https://learn.netdata.cloud/docs/netdata-ai/alerts-automation) — NL → alert config) lives behind the Cloud UI with a test-against-historical-data step and an explicit "Deploy it to your nodes" action, plus AI-credit billing as a de facto budget.

---

## 3. k8sgpt (CNCF)

The opposite end of the spectrum from HolmesGPT: **deterministic scanners first, LLM last and optional**.

### 3.1 Analyzer pattern

- Analyzers are plain Go code over client-go that emit `Result{Kind, Name, Error []Failure, Details, ParentObject}` / `Failure{Text, KubernetesDoc, Sensitive}` with zero LLM involvement ([`pkg/common/types.go`](https://github.com/k8sgpt-ai/k8sgpt/blob/main/pkg/common/types.go)); ~14 core analyzers (Pod, Deployment, Service, Node, …) run by default and ~16 more opt-in via filters ([`pkg/analyzer/analyzer.go`](https://github.com/k8sgpt-ai/k8sgpt/blob/main/pkg/analyzer/analyzer.go)), concurrently under a semaphore (`--max-concurrency` default 10).
- The AI step runs only with `--explain` ([`cmd/analyze/analyze.go`](https://github.com/k8sgpt-ai/k8sgpt/blob/main/cmd/analyze/analyze.go)).

### 3.2 What the LLM gets

- **Failure texts only**, joined into a tiny template — the default prompt asks to "Simplify the following Kubernetes error message … solution in … no more than 280 characters" ([`pkg/ai/prompts.go`](https://github.com/k8sgpt-ai/k8sgpt/blob/main/pkg/ai/prompts.go)). No manifests, no logs beyond what an analyzer put in `Text`.
- `--anonymize`: per-finding mask/unmask pairs — analyzers record `Sensitive{Unmasked, Masked}` at scan time (random same-length strings, [`util.MaskString`](https://github.com/k8sgpt-ai/k8sgpt/blob/main/pkg/util/util.go)); texts are masked before sending and de-masked in the displayed answer. Documented gaps: doesn't apply to events, and custom analyzers can't participate (`// TODO: Support sensitive data` in [`pkg/custom/client.go`](https://github.com/k8sgpt-ai/k8sgpt/blob/main/pkg/custom/client.go)).
- **Response cache** keyed on `sha256(provider + language + failure-text)` ([`pkg/analysis/analysis.go`](https://github.com/k8sgpt-ai/k8sgpt/blob/main/pkg/analysis/analysis.go), [`pkg/cache/cache.go`](https://github.com/k8sgpt-ai/k8sgpt/blob/main/pkg/cache/cache.go)) — same failure never pays for a second LLM call.

### 3.3 Interactivity

Not agentic in the CLI: `--interactive` is a REPL that stuffs the full analysis output as context into each follow-up completion ([`pkg/ai/interactive/interactive.go`](https://github.com/k8sgpt-ai/k8sgpt/blob/main/pkg/ai/interactive/interactive.go)) — no tools, no memory. The agentic story is inverted via `k8sgpt serve --mcp` ([`pkg/server/mcp.go`](https://github.com/k8sgpt-ai/k8sgpt/blob/main/pkg/server/mcp.go)), exposing `analyze`, `cluster-info`, `list-resources`, `get-resource`, `list-namespaces`, `list-events` as MCP tools for an external agent.

### 3.4 Remediation: alpha, off by default, similarity-gated

The CLI never mutates. The operator's auto-remediation ([AUTO_REMEDIATION.md](https://github.com/k8sgpt-ai/k8sgpt-operator/blob/main/AUTO_REMEDIATION.md)) is explicitly "Alpha … not ready for … production": opt-in CR flag defaulting to `false`, proposed fixes become `Mutation` CRs, and the controller **refuses to apply if the AI-proposed config's similarity to the original is below a threshold** (default 90 — "Risk threshold not met", phase `Aborted`) ([`mutation_controller.go`](https://github.com/k8sgpt-ai/k8sgpt-operator/blob/main/internal/controller/mutation/mutation_controller.go), [`k8sgpt_types.go`](https://github.com/k8sgpt-ai/k8sgpt-operator/blob/main/api/v1alpha1/k8sgpt_types.go)). Success is declared only when the originating finding stops re-appearing.

---

## 4. Cross-cutting: confirmation flows, loop budgets, transcripts

### 4.1 Claude Code's permission model (the reference design)

From the official docs ([permissions](https://code.claude.com/docs/en/permissions), [permission modes](https://code.claude.com/docs/en/permission-modes), [hooks](https://code.claude.com/docs/en/hooks), [Agent SDK permissions](https://code.claude.com/docs/en/agent-sdk/permissions)):

- **Tiered by risk**: read-only tools auto-run inside the working directories; Bash prompts except a built-in read-only command set; file modifications always prompt. **Asymmetric persistence**: "don't ask again" for a Bash command persists per-repo permanently; for file edits only until session end.
- **Rules**: `permissions.allow/ask/deny` with `Tool(specifier)` syntax; evaluation order **deny → ask → allow**, first match wins. Bash matching is shell-operator-aware (every subcommand split on `&&`/`|`/`;` must match; wrapper commands stripped; exec-wrappers like `find -exec` always prompt). Docs warn argument-constraining patterns are fragile and recommend denying the binary + gating at a different layer instead.
- **Programmatic gates below the rules**: PreToolUse hooks return `permissionDecision: allow|deny|ask|defer` and can rewrite tool input; deny from a hook overrides every mode including `bypassPermissions`. The SDK's `canUseTool` callback is the interactive fallback prompt, and can return `updatedInput` (approve-with-edits) and `updatedPermissions` (approve-and-remember). Full order: hooks → deny → ask → mode → allow → `canUseTool`.
- **Circuit breakers survive bypass**: even `bypassPermissions` keeps explicit `ask` rules and a hard-coded prompt on `rm -rf /` variants.
- Docs stress rules are "enforced by Claude Code, not by the model" — prompt text is not a security boundary.

### 4.2 Confirmation flows in comparable ops tools

- **GitHub Copilot CLI** ([docs](https://docs.github.com/en/copilot/concepts/agents/about-copilot-cli)): every risky action renders as the proposed command with exactly three options — Yes (once) / Yes for this tool for the session / No, and tell it what to do differently (deny-with-redirect). `--deny-tool` beats `--allow-all-tools`.
- **kubectl-ai** ([repo](https://github.com/GoogleCloudPlatform/kubectl-ai)): classifies commands and confirms only **resource-modifying** ones; `skipPermissions: false` by default; `maxIterations` default 20.
- **Warp** ([agent permissions](https://docs.warp.dev/agent-platform/capabilities/agent-profiles-permissions/)): per-category autonomy (Always ask / Agent decides / Always allow) + regex command allowlist (pure readers) and denylist (`rm`, `curl`, `wget`, `eval`) where **the denylist beats both the allowlist and "Agent decides"**.
- **k8sgpt-operator** (§3.4): machine gate rather than human gate — similarity threshold between current and proposed config as the apply condition.

### 4.3 Loop budgets

| System | Mechanism | Default |
|---|---|---|
| HolmesGPT | `max_steps`, tools withheld on final step | 100 ([config.py](https://github.com/robusta-dev/holmesgpt/blob/master/holmes/config.py)) |
| kubectl-ai | `maxIterations` | 20 ([README](https://github.com/GoogleCloudPlatform/kubectl-ai)) |
| OpenAI Agents SDK | `max_turns` → `MaxTurnsExceeded` | 10 ([docs](https://openai.github.io/openai-agents-python/running_agents/), [run_config.py](https://github.com/openai/openai-agents-python/blob/main/src/agents/run_config.py)) |
| LangGraph | `recursion_limit` → `GraphRecursionError` | 1000 since v1.0.6, was 25 ([docs](https://docs.langchain.com/oss/python/langgraph/graph-api)) |
| Claude Agent SDK | `maxTurns`, plus `maxBudgetUsd` (client-side cost stop) and token `taskBudget` | unlimited unless set ([TS SDK ref](https://code.claude.com/docs/en/agent-sdk/typescript)) |

Cost controls in the wild are mostly *accounting* (HolmesGPT `--log-costs`, Netdata AI credits) — only the Claude Agent SDK has a first-class dollar budget stop.

### 4.4 Transcript presentation

- HolmesGPT CLI: one live line per tool call (`Running tool #3 kubectl_describe: kubectl describe pod foo -n bar`) with output length + `/show N` to expand; the API returns the full `tool_calls` array for UIs.
- Claude Code ([interactive mode](https://code.claude.com/docs/en/interactive-mode)): condensed by default; `Ctrl+O` transcript viewer expands per-call detail; repeated MCP calls collapse to "Called slack 3 times".
- Copilot CLI: the approval prompt *is* the transcript for mutating actions — the user necessarily sees every write before it happens.

---

## Patterns to steal

1. **Curated tools per connector, not API passthrough.** Every serious system (HolmesGPT toolsets, Netdata's 13 MCP tools, k8sgpt's MCP tools) exposes a small set of named, high-level operations with rich descriptions that teach usage, warn about output size, and steer discovery-before-query. For ClaudeOS: a handful of tools per connector (`unifi_list_clients`, `proxmox_guest_status`, `docker_container_logs`, …) defined next to the existing `CONNECTORS` seam, each description written for the model.
2. **Structured tool-result envelope.** HolmesGPT's `StructuredToolResult` (`status: success/error/no_data/approval_required`, `error`, `invocation`, `params`, `elapsed`) is worth copying nearly verbatim: `NO_DATA` as a distinct status stops hallucination on empty results, and `APPROVAL_REQUIRED` as a *tool status* is the cleanest confirmation mechanic — the write tool returns "needs approval" into the loop, the UI prompts, and on approval the loop resumes with `user_approved` set. It composes perfectly with ClaudeOS's mandatory-confirmation decision and the oplog.
3. **Approval as deny→ask→allow tiers with asymmetric memory.** Read tools auto-run; writes always confirm; a denylist (never: e.g. deleting VMs, factory resets) beats everything. If "always allow" is ever offered, scope it narrowly (per exact action type) and expire it (Claude Code persists edits-approval only per session). Confirmation UI should show the exact invocation (Copilot CLI's proposed-command pattern) and offer deny-with-redirect ("No, and tell Claude what to do instead") — that third option is what keeps a denial from dead-ending the conversation.
4. **Spill-to-disk for oversized tool output** (HolmesGPT): token-count each tool result; over budget (theirs: min(15% of window, 25k tokens)), store the full output server-side and return a pointer + preview + follow-up tools (`grep`/`head`-style paging over the stored blob). Far better than silent truncation, and trivially maps onto ClaudeOS's existing store.
5. **Caps and truncation notices at the source** (Netdata): give list/query tools hard default limits (rows, points, time window = last hour) with the response stating "N items omitted" — the model can always re-query narrower. Cheap to implement in each connector tool.
6. **Deterministic pre-computation as evidence** (k8sgpt analyzers, Netdata anomaly bit): don't make the LLM page through raw data to find what code can find. ClaudeOS's poller/reports already compute states and events; expose *those* as tools (e.g. `get_current_alerts`, `get_recent_events`) so answers ground in cheap structured facts, with raw queries as the second resort.
7. **The transcript is the evidence.** HolmesGPT both instructs the model to verify claims against tool output (with hedging rules for the unconfirmed) and returns the full tool-call record to the client. Do both: a citation-discipline system prompt, plus collapsed-by-default expandable tool-call cards in the chat UI (one line per call: tool, human-readable invocation, status, elapsed, output size).
8. **Loop guardrails**: a max-steps cap (10–20 is plenty for a homelab; kubectl-ai ships 20), with HolmesGPT's trick of withholding tools on the last step so the run ends in an answer, not an exception; plus a duplicate-call guard (identical tool+params → error telling the model to vary). Keep the per-conversation token budget from charting; log per-turn cost like `--log-costs`.
9. **Enforce gates in code, not prompt.** Claude Code's docs say it outright: rules are enforced by the harness. ClaudeOS's confirmation must live in the tool-dispatch layer (server-side check that a write tool call carries a confirmed flag tied to a specific pending action id), never as an instruction to Claude.

## Mistakes to avoid

1. **Raw API/CLI passthrough as the primary surface.** Nobody ships "here's the Proxmox API, go wild". Even HolmesGPT's bash tool is AST-parsed and prefix-allowlisted with `sudo` unoverridably blocked. If ClaudeOS ever adds a generic escape hatch, gate it the same way; better, don't.
2. **Summarizing big tool outputs with a second LLM call.** HolmesGPT tried it (`llm_summarize` transformer) and marked it LEGACY in favor of spill-to-disk — it costs money, adds latency, and loses the details the investigation needed. Silent truncation is equally bad; always say what was cut.
3. **Prompt-only safety.** A "please confirm before writing" system-prompt line is not a gate. Every surveyed tool that gates writes does it in code (status codes, permission pipelines, CR opt-ins).
4. **Trusting backend auth as the only filter** (Netdata's `execute_function` passes through with no MCP-side filtering; k8sgpt's custom-analyzer gRPC dials plaintext with a TODO on sensitive data). ClaudeOS talks to connectors with admin credentials, so the tool layer itself must distinguish read from write — the connector API will happily do whatever it's asked.
5. **Fully autonomous remediation.** The one project that built it (k8sgpt-operator) still labels it alpha/not-production behind an off-by-default flag and a machine risk-check, with rollback a TODO. ClaudeOS's mandatory-confirmation decision is the industry-consistent choice; don't add an "auto mode" later without a denylist that survives it (Claude Code's bypass mode still hard-blocks `rm -rf /`).
6. **Unbounded loops with cost as an afterthought.** Defaults range 10–100 steps; every framework has *some* cap and raises/ends cleanly. Don't ship without max-steps + duplicate-call guard + the token budget, and surface cost per conversation rather than discovering it on the monthly bill.
7. **Letting an empty result read as absence of a problem.** HolmesGPT encodes `NO_DATA` distinctly and its prompt warns "running and reporting healthy does not mean it is without issues" / don't conclude absence from config alone. Encode empty-vs-error-vs-data in the envelope and the prompt.
8. **Anonymization gaps as a lesson in scope honesty** (k8sgpt masks failure texts but not events or custom analyzers). Single-user ClaudeOS doesn't need masking, but the general lesson holds: partial guarantees that look total are worse than none — document exactly what a gate covers.
