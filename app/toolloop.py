"""The shared agentic tool-use loop — the core every agentic caller drives.

Extracted from app/chat.py so a second, unattended caller (headless lab-issue
triage) can drive the same loop without inheriting Ops Chat's SSE streaming,
write-approval suspend/resume or conversation persistence. This module owns
exactly the parts both callers need: the iteration ceiling, the per-turn
duplicate-call guard, cost and usage accounting, tool dispatch with
envelope→tool_result rendering, the thinking-block echo, and the context budget.

A manual loop on the anthropic SDK rather than the SDK's tool runner, because
the tool runner cannot suspend across HTTP requests — which is what Ops Chat's
mandatory write-confirmation requires.

Two things are deliberately parameters rather than module facts:

  * **The tool schema list.** A caller hands in the schemas it is willing to
    offer. `tools.schemas(include_writes=False)` therefore expresses a real
    read-only guarantee: a write tool the model was never shown cannot be
    called, and even if one somehow were, a run with no `approval` hook refuses
    it here rather than executing it.
  * **The system prompt blocks.** Callers differ in what they tell the model;
    the loop only cares that the blocks are stable enough to cache.

Everything the caller needs to observe comes back as `(event, payload)` pairs.
Ops Chat serialises them to SSE; a headless caller ignores the ones it does not
need. The two terminal events (`finished`, `suspended`) always carry the run's
accounting, including on the error path — spend that happened must be recorded.

A run ends in one of three ways. Tools are always withheld on the final call, so
the model cannot ask for something we would have to refuse. Without
`final_schema` that call answers in prose, which is what Ops Chat wants. With
one, it answers as schema-validated JSON instead — the third ending, added for
triage, where the caller needs data with prose as one field rather than prose it
would have to parse.
"""

import datetime
import json
import time

from . import store, tools

try:
    import anthropic
    HAS_SDK = True
except ImportError:  # plain system python — the agentic loop is unavailable
    anthropic = None
    HAS_SDK = False

MODEL = "claude-sonnet-5"
EFFORT = "medium"              # low | medium | high | xhigh | max
MAX_TOKENS = 16000             # caps thinking + text together
MAX_ITERATIONS = 15            # tools withheld on the final one
CONTEXT_BUDGET = 120_000       # input tokens one run may accumulate
CONTEXT_FULL_AT = 0.8          # fraction of the budget at which a run stops


def budget_reached(used: int, budget: int = CONTEXT_BUDGET) -> bool:
    """Has a run used up its context budget?

    The single place this threshold is decided. `run` stops on it mid-loop,
    and a caller that persists the transcript must refuse to resume on the
    same answer — a stopped run ends on an unpaired tool_use the API rejects.
    Two copies of this comparison would eventually disagree; one cannot.

    Stops short of the budget itself so there is still headroom for the tool
    results the next iteration would have appended.
    """
    return used >= budget * CONTEXT_FULL_AT
API_TIMEOUT = 600
MAX_TOOL_RESULT = 60_000       # characters of rendered envelope per tool_result


# ------------------------------------------------------------ system prompt

# The loop takes its system blocks as a parameter — this constant is not a
# default, it is the text that is *true for every caller*: what the lab is, how
# to work the tool tiers, and what a result status means. Each caller
# concatenates it with its own halves (see chat.SYSTEM_PROMPT and
# labissues.TRIAGE_PROMPT). Kept here rather than in either caller so neither
# has to import the other.
BASE_PROMPT = """The lab: a UniFi network (UDM-SE gateway, switches, access points), a Proxmox host running VMs and LXC containers, an Ubuntu VM running a Docker fleet with an NVIDIA GPU passed through, Home Assistant (HAOS) with a large Zigbee/ZHA mesh, and a Synology NAS.

Deliberately vague above: model numbers, hostnames, IPs, container names and capacities are NOT stated here because they change. Get every specific from a tool. If you name a piece of hardware, that name must have come from a tool result on this turn.

## Using tools

Start with the tier-1 tools (get_lab_overview, get_metric_history, get_ops_log, get_uptime_monitors) — they are cheap, broad, and precomputed. Reach for a per-connector tool once you know where to look. If no tool exposes what you need, say so plainly rather than guessing; do not speculate about data you could not fetch.

Tool results carry a status. `success` has data. `no_data` means the query worked and found nothing — that is NOT the same as healthy, and never report it as such. `error` means the query failed, so that area is unverified. When a result reports omitted items or truncated output, any conclusion you draw from the visible part must say so."""

# Sent as a user turn when the model stops calling tools before the ceiling and
# a structured ending was asked for. Two assistant messages cannot sit next to
# each other, so the final call needs a turn to answer.
FINAL_PROMPT = ("Now give your result in the required structured format. "
                "Do not call any more tools.")

# Sonnet 5 list price is $3/$15 per MTok, with introductory $2/$10 running to
# 2026-08-31. Cache writes bill 1.25x input, reads 0.1x.
_INTRO_UNTIL = datetime.date(2026, 8, 31)
_PRICES = {"intro": (2.0, 10.0), "list": (3.0, 15.0)}


# --------------------------------------------------------------- pricing

def _rate() -> tuple:
    return _PRICES["intro" if datetime.date.today() <= _INTRO_UNTIL else "list"]


def cost(usage) -> float:
    inp, out = _rate()
    fresh = getattr(usage, "input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    otok = getattr(usage, "output_tokens", 0) or 0
    return ((fresh + cw * 1.25 + cr * 0.1) * inp + otok * out) / 1_000_000


def prompt_tokens(usage) -> int:
    """Total prompt size — the uncached remainder plus both cache tiers."""
    return ((getattr(usage, "input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0))


# ------------------------------------------------------------------ client

def require_sdk() -> None:
    if not HAS_SDK:
        raise LookupError(
            "chat needs the anthropic SDK — start ClaudeOS with .venv/bin/python3 "
            "server.py (the AI analysis features work either way)")


def new_client():
    require_sdk()
    s = store.get_system("ai", reveal_secrets=True)
    if not s or not s.get("api_key"):
        raise LookupError("chat needs an Anthropic API key — add it on the Setup page")
    return anthropic.Anthropic(api_key=s["api_key"], timeout=API_TIMEOUT)


# ----------------------------------------------------------- wire format

def system_blocks(prompt: str) -> list:
    # One cache breakpoint after tools+system (they render in that order), so
    # the stable prefix is reused every iteration and every run.
    return [{"type": "text", "text": prompt,
             "cache_control": {"type": "ephemeral"}}]


def _block_type(block) -> str | None:
    """The `type` of a content block, whichever shape it arrived in.

    `blocks()` returns plain dicts for anything with `model_dump` and the object
    itself otherwise, so both reach the transcript and either may be inspected.
    """
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def blocks(reply) -> list:
    """SDK content blocks → plain wire-format dicts.

    Callers persist the messages list as JSON, and the SDK's block objects are
    not JSON-serialisable. Dumping to plain dicts keeps the list both storable
    and valid to send straight back — which matters for thinking blocks, which
    must be echoed back unchanged (their text is empty on Sonnet 5, where
    `display` defaults to omitted, but the block itself is load-bearing).
    """
    out = []
    for b in reply.content:
        if hasattr(b, "model_dump"):
            out.append(b.model_dump(mode="json", exclude_none=True))
        else:
            out.append(b)
    return out


def tool_result(tool_use_id: str, env: dict) -> dict:
    """Render an envelope as a tool_result block. Errors set is_error so the
    model can react rather than treating the failure as data."""
    payload = {k: v for k, v in env.items() if k != "params" and v is not None}
    block = {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": json.dumps(payload, default=str)[:MAX_TOOL_RESULT]}
    if env["status"] == "error":
        block["is_error"] = True
    return block


# -------------------------------------------------------------- the loop

def run(client, messages: list, *, schemas: list, system: list,
        approval=None, model: str = MODEL, max_tokens: int = MAX_TOKENS,
        effort: str = EFFORT, max_iterations: int = MAX_ITERATIONS,
        context_budget: int = CONTEXT_BUDGET, final_schema: dict | None = None,
        max_seconds: float | None = None):
    """Drive the tool loop over `messages`, yielding (event, payload) pairs.

    `messages` is appended to in place, so the caller keeps the transcript it
    can persist or resume from. `schemas` is the exact tool list offered to the
    model. `approval` is an optional hook, called as
    `approval(name, params, tool_use_id, envelope, other_results) -> pending`
    when a tool returns `approval_required`; it may stamp the envelope (Ops Chat
    adds a single-use `pending_id`) and returns an opaque record the caller
    needs to resume later. Without the hook a write tool cannot run at all —
    the loop hands the model an error instead, which is what makes a read-only
    schema list an actual guarantee rather than a convention.

    `final_schema` switches the ending mode. Given one, the last call drops
    tools *and* asks for a response validated against that JSON schema, and the
    parsed object comes back on the terminal event as `structured`. The final
    call happens whether the ceiling was reached or the model simply stopped
    asking for tools — the latter is the common case, and it is not an ending in
    this mode, because a caller that asked for data must not be handed prose.
    Without a schema the run ends in prose and `structured` is None, which is
    Ops Chat's behaviour and is unchanged.

    Events: `token`, `tool_start`, `tool_result`, `approval_required`, `error`,
    and exactly one terminal `finished` or `suspended` carrying
    {cost, tools, usage, error, structured}. `suspended` means the approval hook
    fired and the run stopped mid-turn; the caller resumes by calling run again
    with the tool_result blocks appended to `messages`.
    """
    started = time.monotonic()
    run_cost, run_tools, usage_last, failure = 0.0, 0, None, None
    run_output = 0   # summed: usage_last holds only the final reply's count
    structured, ending = None, False
    # Duplicate-call guard is per RUN, not per conversation: a follow-up
    # "check again now" must be able to re-run the same query.
    seen: set = set()

    for step in range(max_iterations):
        last_step = ending or step == max_iterations - 1
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
        # Tools are withheld on the final iteration so the run ends in prose
        # rather than a tool call we would have to refuse.
        if not last_step:
            kwargs["tools"] = schemas
        elif final_schema is not None:
            kwargs["output_config"]["format"] = {"type": "json_schema",
                                                 "schema": final_schema}

        try:
            with client.messages.stream(**kwargs) as stream:
                for event in stream:
                    if (event.type == "content_block_delta"
                            and getattr(event.delta, "type", None) == "text_delta"):
                        yield "token", {"text": event.delta.text}
                reply = stream.get_final_message()
        except Exception as e:  # noqa: BLE001 — surface API failures to the caller
            # break, not return: whatever this run already spent still has to
            # be reported, or the cost is silently discarded.
            failure = f"{type(e).__name__}: {e}"
            yield "error", {"message": failure}
            break

        usage_last = reply.usage
        run_cost += cost(reply.usage)
        run_output += getattr(reply.usage, "output_tokens", 0) or 0
        wire = blocks(reply)
        if reply.stop_reason != "tool_use":
            # A reply can be cut short *mid tool call*: stop_reason is max_tokens
            # while the content still carries tool_use blocks the loop will never
            # dispatch. Left in the transcript those are unpaired, and the API
            # rejects any later request carrying them ("tool_use ids were found
            # without tool_result blocks"). Nothing is lost by dropping them —
            # they were never run, so they hold no evidence.
            wire = [b for b in wire if _block_type(b) != "tool_use"]
        if wire:
            messages.append({"role": "assistant", "content": wire})
        elif ending or last_step:
            break   # nothing left to say and nowhere left to say it

        if reply.stop_reason == "refusal":
            failure = "Claude declined to answer this request."
            yield "error", {"message": failure}
            break
        if reply.stop_reason != "tool_use":
            truncated = reply.stop_reason == "max_tokens"
            if truncated and (final_schema is None or ending):
                # Nothing left to try: either the caller wanted prose, or this
                # WAS the structured call and it still came back cut short.
                failure = "the reply hit the token cap and was cut short"
                yield "error", {"message": failure}
            if final_schema is not None and failure is None:
                if last_step:
                    structured, failure = _structured(reply)
                    if failure:
                        yield "error", {"message": failure}
                else:
                    # The model has finished gathering evidence before the
                    # ceiling — the ordinary way a run ends. In this mode that
                    # is not the ending: one more call, without tools, turns
                    # what it concluded into the data the caller asked for.
                    #
                    # A truncated reply still gets this call. Its prose was cut
                    # short, but the evidence is already in the transcript and
                    # the structured call is cheap; dying here would throw away
                    # a whole run's tool results over a clipped summary.
                    ending = True
                    messages.append({"role": "user", "content": FINAL_PROMPT})
                    continue
            break

        # Enforced here, before dispatching another round of tool calls, so the
        # run that would blow the budget stops instead of completing and being
        # billed. Every response's prompt carries the whole transcript, so the
        # latest prompt size IS the accumulated input.
        #
        # Breaking here leaves `messages` ending on an assistant turn whose
        # tool_use blocks have no matching tool_result — a transcript the API
        # will reject if it is ever sent back. That is safe only because a
        # caller persisting it must refuse to resume it, which chat.run_turn
        # does by asking budget_reached() the same question with the same
        # threshold. Keep those two in lockstep or resuming crashes.
        # Wall-clock, same placement and same reasoning as the budget check: a
        # slow or hanging connector can burn an unattended run's time without
        # ever tripping an iteration or token ceiling. Checked between rounds,
        # so an in-flight call is never abandoned mid-request.
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            failure = (f"stopped before the next tool call: this run has used its "
                       f"{max_seconds:g}s time budget")
            yield "error", {"message": failure}
            break

        used = prompt_tokens(reply.usage)
        if budget_reached(used, context_budget):
            failure = (f"stopped before the next tool call: this run has used its "
                       f"context budget ({used:,} of {context_budget:,} input tokens)")
            yield "error", {"message": failure}
            break

        calls = [b for b in reply.content if b.type == "tool_use"]
        results, pending = [], None

        for call in calls:
            params = dict(call.input or {})
            key = (call.name, json.dumps(params, sort_keys=True))
            run_tools += 1
            yield "tool_start", {"name": call.name, "invocation": f"{call.name}(…)"}

            if key in seen:
                env = tools.envelope(
                    "error", invocation=f"{call.name}(…)", params=params,
                    error="you already ran this exact call on this turn — vary the "
                          "parameters or use the result you already have")
            else:
                seen.add(key)
                env = tools.run(call.name, params)

            if env["status"] == "approval_required":
                if approval is None:
                    # No approval channel: the write cannot happen. Report it as
                    # an error result so the model reacts instead of assuming it
                    # succeeded, and keep going read-only.
                    # Deliberately does not say *what* the write touches: the
                    # lab and the tracker are distinct boundaries (CONTEXT.md,
                    # "read-only"), and this module is shared by callers that
                    # mean different ones.
                    env = tools.envelope(
                        "error", invocation=env["invocation"], params=params,
                        error="this tool makes a change and needs a human to "
                              "approve it; this run has no approval channel, "
                              "so it did not run")
                else:
                    pending = approval(call.name, params, call.id, env, results)
                    yield "tool_result", {"name": call.name, "envelope": env}
                    yield "approval_required", {"name": call.name, "envelope": env,
                                                "pending": pending}
                    break

            results.append(tool_result(call.id, env))
            yield "tool_result", {"name": call.name, "envelope": env}

        if pending:
            yield "suspended", {"cost": run_cost, "tools": run_tools,
                                "usage": usage_last, "output_tokens": run_output,
                                "error": failure,
                                "structured": structured}
            return

        messages.append({"role": "user", "content": results})

    yield "finished", {"cost": run_cost, "tools": run_tools, "usage": usage_last,
                       "output_tokens": run_output,
                       "error": failure, "structured": structured}


def _structured(reply) -> tuple:
    """The final schema-validated reply, as (parsed, failure).

    The format constraint means the text blocks hold JSON, but a run can still
    end here with nothing usable — a refusal, or a reply cut short. Report that
    as a failure rather than raising, so the caller still sees what was spent.
    """
    text = "".join(b.text for b in reply.content if b.type == "text")
    if not text.strip():
        return None, f"the run returned no result (stop_reason: {reply.stop_reason})"
    try:
        parsed = json.loads(text)
    except ValueError as e:
        return None, f"the final result was not valid JSON: {e}"
    if not isinstance(parsed, dict):
        return None, f"the final result was a {type(parsed).__name__}, expected an object"
    return parsed, None
