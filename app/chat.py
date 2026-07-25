"""Agentic ops chat — the loop, the conversation store, cost accounting.

Implements §2–§4, §8, §10, §12, §13 of docs/spec-agentic-ops-chat.md.

A manual tool-use loop on the anthropic SDK (the tool runner can't suspend
across HTTP requests, which mandatory write-confirmation requires). Each turn
is driven as a generator of events; server.py serialises those to SSE.

The write-approval boundary is what shapes this module: on `approval_required`
the loop persists the messages list plus a single-use pending record, emits
`approval_required`, and returns. A later POST to the approve route resumes
from that persisted state with the tool_result filled in.
"""

import datetime
import json
import os
import secrets
import threading
import time

from . import notify, oplog, store, tools
from .store import DATA_DIR

try:
    import anthropic
    HAS_SDK = True
except ImportError:  # plain system python — chat is unavailable
    anthropic = None
    HAS_SDK = False

PATH = os.path.join(DATA_DIR, "chats.json")

MODEL = "claude-sonnet-5"
EFFORT = "medium"              # low | medium | high | xhigh | max
MAX_TOKENS = 16000             # caps thinking + text together
MAX_ITERATIONS = 15            # tools withheld on the final one
KEEP_CONVERSATIONS = 20
CONTEXT_BUDGET = 120_000       # input tokens; turn refused past CONTEXT_FULL_AT
CONTEXT_FULL_AT = 0.8
APPROVAL_TTL = 30 * 60         # seconds
API_TIMEOUT = 600

# Sonnet 5 list price is $3/$15 per MTok, with introductory $2/$10 running to
# 2026-08-31. Cache writes bill 1.25x input, reads 0.1x.
_INTRO_UNTIL = datetime.date(2026, 8, 31)
_PRICES = {"intro": (2.0, 10.0), "list": (3.0, 15.0)}

SYSTEM_PROMPT = """You are the ops assistant inside ClaudeOS, a homelab mission-control app. You answer questions about this specific homelab using the tools provided, and you can make changes when the user approves them.

The lab: a UniFi network (UDM-SE gateway, switches, access points), a Proxmox host running VMs and LXC containers, an Ubuntu VM running a Docker fleet with an RTX 4000 SFF Ada passed through, Home Assistant (HAOS) with a large Zigbee/ZHA mesh, and a Synology NAS.

## Using tools

Start with the tier-1 tools (get_lab_overview, get_metric_history, get_ops_log, get_uptime_monitors) — they are cheap, broad, and precomputed. Reach for a per-connector tool once you know where to look. If no tool exposes what you need, say so plainly rather than guessing; do not speculate about data you could not fetch.

Tool results carry a status. `success` has data. `no_data` means the query worked and found nothing — that is NOT the same as healthy, and never report it as such. `error` means the query failed, so that area is unverified. When a result reports omitted items or truncated output, any conclusion you draw from the visible part must say so.

## Grounding your answers

Before you answer, check each factual claim against a tool result from this conversation.

Quote the evidence rather than naming the tool. Write `the log says "hardware acceleration unavailable"`, not "the docker_container_logs tool shows...". State values as you fetched them — "CPU is at 78%". Never state a metric you did not actually fetch; no invented percentages, temperatures, versions or counts.

Anything you did not directly confirm is hedged: "possible cause", "might be", "likely". Distinguish what you observed from what you know generally — "your host reports X" versus "typically this means Y".

If a tool errored or returned nothing, say in your answer which area went unverified and why. "I couldn't reach the NAS, so anything library-related is unchecked." The user should never have to notice a failed tool call to learn that your answer has a hole in it.

## Making changes

Write tools return `approval_required` and do nothing until the user approves. Before calling one, say what you intend to do and why. After approval the result comes back as a normal tool result.

Never claim you have changed something you have not. If an approval is pending or denied, the change did not happen.

## Style

Lead with the answer, then the evidence. Be concise and concrete; skip preamble. Match length to the question — a status check gets a sentence or two, a diagnosis gets the reasoning. Plain prose, no headers for short answers."""


_lock = threading.Lock()
_inflight: set = set()          # conversation ids with a turn running


# ------------------------------------------------------------------ store

def _load() -> list:
    if not os.path.exists(PATH):
        return []
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _save(convs: list) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(convs[-KEEP_CONVERSATIONS:], f, indent=2)
    os.replace(tmp, PATH)


def _get(cid: str) -> dict | None:
    return next((c for c in _load() if c["id"] == cid), None)


def _put(conv: dict) -> None:
    with _lock:
        convs = [c for c in _load() if c["id"] != conv["id"]]
        convs.append(conv)
        convs.sort(key=lambda c: c.get("updated", 0))
        _save(convs)


def list_conversations() -> list:
    return [{"id": c["id"], "title": c.get("title"), "created": c.get("created"),
             "updated": c.get("updated"), "turns": len(c.get("turns", [])),
             "pending": bool(c.get("pending")),
             "cost_usd": round(sum(t.get("cost_usd", 0) for t in c.get("turns", [])), 4)}
            for c in reversed(_load())]


def get_conversation(cid: str) -> dict:
    conv = _get(cid)
    if conv is None:
        raise LookupError(f"unknown conversation: {cid}")
    return conv


def delete_conversation(cid: str) -> None:
    with _lock:
        convs = _load()
        if not any(c["id"] == cid for c in convs):
            raise LookupError(f"unknown conversation: {cid}")
        _save([c for c in convs if c["id"] != cid])


def _new_conversation(first_message: str) -> dict:
    now = time.time()
    title = (first_message or "untitled").strip().replace("\n", " ")
    return {"id": secrets.token_hex(6), "title": title[:70],
            "created": now, "updated": now, "messages": [], "turns": [],
            "pending": None}


# --------------------------------------------------------------- pricing

def _rate() -> tuple:
    return _PRICES["intro" if datetime.date.today() <= _INTRO_UNTIL else "list"]


def _cost(usage) -> float:
    inp, out = _rate()
    fresh = getattr(usage, "input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    otok = getattr(usage, "output_tokens", 0) or 0
    return ((fresh + cw * 1.25 + cr * 0.1) * inp + otok * out) / 1_000_000


def _prompt_tokens(usage) -> int:
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


def _client():
    require_sdk()
    s = store.get_system("ai", reveal_secrets=True)
    if not s or not s.get("api_key"):
        raise LookupError("chat needs an Anthropic API key — add it on the Setup page")
    return anthropic.Anthropic(api_key=s["api_key"], timeout=API_TIMEOUT)


def _system_blocks():
    # One cache breakpoint after tools+system (they render in that order), so
    # the stable prefix is reused every iteration and every turn.
    return [{"type": "text", "text": SYSTEM_PROMPT,
             "cache_control": {"type": "ephemeral"}}]


def _blocks(reply) -> list:
    """SDK content blocks → plain wire-format dicts.

    The conversation is persisted as JSON, and the SDK's block objects are not
    JSON-serialisable. Dumping to plain dicts keeps the messages list both
    storable and valid to send straight back — which matters for thinking
    blocks, which must be echoed back unchanged (their text is empty on Sonnet
    5, where `display` defaults to omitted, but the block itself is load-bearing).
    """
    out = []
    for b in reply.content:
        if hasattr(b, "model_dump"):
            out.append(b.model_dump(mode="json", exclude_none=True))
        else:
            out.append(b)
    return out


def _tool_result(tool_use_id: str, env: dict) -> dict:
    """Render an envelope as a tool_result block. Errors set is_error so the
    model can react rather than treating the failure as data."""
    payload = {k: v for k, v in env.items() if k != "params" and v is not None}
    block = {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": json.dumps(payload, default=str)[:60_000]}
    if env["status"] == "error":
        block["is_error"] = True
    return block


# -------------------------------------------------------------- the loop

def _iterate(client, conv: dict, resume_results: list | None = None):
    """Drive the tool loop, yielding (event, payload) pairs.

    Suspends by returning after an `approval_required` event; the conversation
    (already persisted with its pending record) resumes via run_approval.
    """
    messages = conv["messages"]
    if resume_results:
        messages.append({"role": "user", "content": resume_results})

    turn_cost, turn_tools, usage_last = 0.0, 0, None
    # Duplicate-call guard is per TURN, not per conversation: a follow-up
    # "check again now" must be able to re-run the same query.
    seen: set = set()

    for step in range(MAX_ITERATIONS):
        last_step = step == MAX_ITERATIONS - 1
        kwargs = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "system": _system_blocks(),
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": EFFORT},
        }
        # Tools are withheld on the final iteration so the turn ends in prose
        # rather than a tool call we would have to refuse.
        if not last_step:
            kwargs["tools"] = tools.schemas()

        try:
            with client.messages.stream(**kwargs) as stream:
                for event in stream:
                    if (event.type == "content_block_delta"
                            and getattr(event.delta, "type", None) == "text_delta"):
                        yield "token", {"text": event.delta.text}
                reply = stream.get_final_message()
        except Exception as e:  # noqa: BLE001 — surface API failures to the UI
            yield "error", {"message": f"{type(e).__name__}: {e}"}
            return

        usage_last = reply.usage
        turn_cost += _cost(reply.usage)
        messages.append({"role": "assistant", "content": _blocks(reply)})

        if reply.stop_reason == "refusal":
            yield "error", {"message": "Claude declined to answer this request."}
            break
        if reply.stop_reason != "tool_use":
            if reply.stop_reason == "max_tokens":
                yield "error", {"message": "the reply hit the token cap and was cut short"}
            break

        calls = [b for b in reply.content if b.type == "tool_use"]
        results, pending = [], None

        for call in calls:
            params = dict(call.input or {})
            key = (call.name, json.dumps(params, sort_keys=True))
            turn_tools += 1
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
                pid = secrets.token_hex(8)
                env["pending_id"] = pid
                pending = {
                    "id": pid, "tool": call.name, "params": params,
                    "tool_use_id": call.id, "expires": time.time() + APPROVAL_TTL,
                    "warning": env.get("warning"),
                    "other_results": results,   # sibling calls already resolved
                }
                oplog.add("info", "chat",
                          f"approval requested: {env['invocation']}")
                yield "tool_result", {"name": call.name, "envelope": env}
                yield "approval_required", {"pending": pending, "envelope": env}
                break

            results.append(_tool_result(call.id, env))
            yield "tool_result", {"name": call.name, "envelope": env}

        if pending:
            conv["pending"] = pending
            yield "_suspend", {"cost": turn_cost, "tools": turn_tools, "usage": usage_last}
            return

        messages.append({"role": "user", "content": results})

    yield "_done", {"cost": turn_cost, "tools": turn_tools, "usage": usage_last}


def _finish(conv: dict, payload: dict, *, suspended: bool):
    """Record turn accounting and persist. Returns the cost event payload."""
    usage = payload.get("usage")
    prompt_tokens = _prompt_tokens(usage) if usage else 0
    conv["turns"].append({
        "ts": time.time(), "cost_usd": round(payload.get("cost", 0.0), 6),
        "tools": payload.get("tools", 0), "prompt_tokens": prompt_tokens,
        "output_tokens": (getattr(usage, "output_tokens", 0) or 0) if usage else 0,
    })
    conv["updated"] = time.time()
    if not suspended:
        conv["pending"] = None
    _put(conv)
    return {
        "cost_usd": round(payload.get("cost", 0.0), 4),
        "tools": payload.get("tools", 0),
        "prompt_tokens": prompt_tokens,
        "context_pct": round(100 * prompt_tokens / CONTEXT_BUDGET, 1) if prompt_tokens else 0,
        "context_full": prompt_tokens >= CONTEXT_BUDGET * CONTEXT_FULL_AT,
        "total_usd": round(sum(t.get("cost_usd", 0) for t in conv["turns"]), 4),
    }


def _claim(cid: str) -> None:
    with _lock:
        if cid in _inflight:
            raise ValueError("a turn is already in progress for this conversation")
        _inflight.add(cid)


def _release(cid: str) -> None:
    with _lock:
        _inflight.discard(cid)


def run_turn(message: str, conversation_id: str | None = None):
    """Generator of (event, payload) for a new user message."""
    client = _client()
    conv = get_conversation(conversation_id) if conversation_id else _new_conversation(message)

    if conv.get("pending"):
        raise ValueError("resolve the pending action before sending another message")
    used = conv["turns"][-1]["prompt_tokens"] if conv.get("turns") else 0
    if used >= CONTEXT_BUDGET * CONTEXT_FULL_AT:
        raise ValueError("this conversation is full — start a new one")

    _claim(conv["id"])
    try:
        yield "conversation", {"id": conv["id"], "title": conv["title"]}
        conv["messages"].append({"role": "user", "content": message})
        for event, payload in _iterate(client, conv):
            if event in ("_done", "_suspend"):
                yield "cost", _finish(conv, payload, suspended=event == "_suspend")
                return
            yield event, payload
    finally:
        _release(conv["id"])


def run_approval(cid: str, pending_id: str, decision: str, guidance: str = ""):
    """Generator of (event, payload) resuming a suspended turn."""
    client = _client()
    conv = get_conversation(cid)
    pending = conv.get("pending")
    if not pending:
        raise LookupError("there is no action awaiting approval on this conversation")
    if pending["id"] != pending_id:
        raise ValueError("that approval is no longer valid")
    if time.time() > pending["expires"]:
        conv["pending"] = None
        _put(conv)
        raise ValueError("the approval expired — ask again and re-approve")

    label = tools._invocation(pending["tool"], pending["params"])
    _claim(cid)
    try:
        # Single-use: clear before doing anything, so an approval can't replay.
        conv["pending"] = None

        if decision == "approve":
            # Re-validated at execution time — never trust the stored record.
            env = tools.run(pending["tool"], pending["params"], approved=True)
            ok = env["status"] == "success"
            oplog.add("action" if ok else "warn", "chat",
                      f"approved: {label} → {env['status']}"
                      + (f" ({env['error']})" if env.get("error") else ""))
            if ok:
                notify.send("ClaudeOS chat made a change", label,
                            priority="default", tags=["wrench"])
            yield "tool_result", {"name": pending["tool"], "envelope": env}
            result_block = _tool_result(pending["tool_use_id"], env)
        else:
            text = (f"The user denied this action. Their guidance: {guidance}"
                    if guidance.strip() else
                    "The user denied this action. Ask what they would prefer.")
            oplog.add("info", "chat", f"denied: {label}"
                      + (f" — {guidance[:120]}" if guidance.strip() else ""))
            env = tools.envelope("error", error=text, invocation=label,
                                 params=pending["params"])
            yield "tool_result", {"name": pending["tool"], "envelope": env}
            result_block = {"type": "tool_result", "tool_use_id": pending["tool_use_id"],
                            "content": text, "is_error": True}

        results = list(pending.get("other_results") or []) + [result_block]
        for event, payload in _iterate(client, conv, resume_results=results):
            if event in ("_done", "_suspend"):
                yield "cost", _finish(conv, payload, suspended=event == "_suspend")
                return
            yield event, payload
    finally:
        _release(cid)


def expire_pending() -> None:
    """Drop stale pending approvals. Called on startup (a restart invalidates
    every pending write — the world may have changed while we were down) and
    periodically thereafter."""
    with _lock:
        convs = _load()
        changed = False
        for c in convs:
            p = c.get("pending")
            if p and (p.get("_boot") or time.time() > p.get("expires", 0)):
                oplog.add("info", "chat",
                          f"pending action abandoned: "
                          f"{p.get('tool')} {p.get('params')}")
                c["pending"] = None
                changed = True
        if changed:
            _save(convs)


def start() -> None:
    expire_pending()

    def loop():
        while True:
            time.sleep(300)
            try:
                expire_pending()
            except Exception as e:  # noqa: BLE001
                oplog.add("error", "chat", f"pending sweep failed: {e}")

    threading.Thread(target=loop, name="claudeos-chat", daemon=True).start()
