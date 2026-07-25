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
"""

import datetime
import json

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
API_TIMEOUT = 600
MAX_TOOL_RESULT = 60_000       # characters of rendered envelope per tool_result

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
        context_budget: int = CONTEXT_BUDGET):
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

    Events: `token`, `tool_start`, `tool_result`, `approval_required`, `error`,
    and exactly one terminal `finished` or `suspended` carrying
    {cost, tools, usage, error}. `suspended` means the approval hook fired and
    the run stopped mid-turn; the caller resumes by calling run again with the
    tool_result blocks appended to `messages`.
    """
    run_cost, run_tools, usage_last, failure = 0.0, 0, None, None
    # Duplicate-call guard is per RUN, not per conversation: a follow-up
    # "check again now" must be able to re-run the same query.
    seen: set = set()
    # Stop while there is still headroom for the tool results the next
    # iteration would append, rather than at the budget itself.
    stop_at = context_budget * CONTEXT_FULL_AT

    for step in range(max_iterations):
        last_step = step == max_iterations - 1
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
        messages.append({"role": "assistant", "content": blocks(reply)})

        if reply.stop_reason == "refusal":
            failure = "Claude declined to answer this request."
            yield "error", {"message": failure}
            break
        if reply.stop_reason != "tool_use":
            if reply.stop_reason == "max_tokens":
                failure = "the reply hit the token cap and was cut short"
                yield "error", {"message": failure}
            break

        # Enforced here, before dispatching another round of tool calls, so the
        # run that would blow the budget stops instead of completing and being
        # billed. Every response's prompt carries the whole transcript, so the
        # latest prompt size IS the accumulated input.
        used = prompt_tokens(reply.usage)
        if used >= stop_at:
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
                    env = tools.envelope(
                        "error", invocation=env["invocation"], params=params,
                        error="this tool changes the lab and needs a human to "
                              "approve it; this run is read-only, so it did not run")
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
                                "usage": usage_last, "error": failure}
            return

        messages.append({"role": "user", "content": results})

    yield "finished", {"cost": run_cost, "tools": run_tools, "usage": usage_last,
                       "error": failure}
