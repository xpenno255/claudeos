"""Agentic ops chat — streaming, write approval, the conversation store.

Implements §2–§4, §8, §10, §12, §13 of docs/spec-agentic-ops-chat.md.

The tool-use loop itself lives in app/toolloop.py, shared with the headless
callers that drive the same core. What stays here is everything specific to a
human sitting in front of a browser: the SSE event stream, the write-approval
boundary, conversation persistence and per-turn cost accounting.

That approval boundary is what shapes this module: on `approval_required` the
loop's hook persists the messages list plus a single-use pending record, the
event reaches the browser, and the run suspends. A later POST to the approve
route resumes the loop from that persisted state with the tool_result filled in.
"""

import json
import os
import secrets
import threading
import time

from . import notify, oplog, toolloop, tools
from .store import DATA_DIR

# Re-exported for server.py and the Setup page: chat is unavailable without the
# SDK, exactly as the loop is.
HAS_SDK = toolloop.HAS_SDK

PATH = os.path.join(DATA_DIR, "chats.json")

MODEL = toolloop.MODEL
KEEP_CONVERSATIONS = 20
# Only for the UI's percentage gauge. Whether a turn is *full* is never decided
# here — toolloop.budget_reached() owns that, so the loop's mid-run stop and the
# turn-entry refusal cannot drift apart.
CONTEXT_BUDGET = toolloop.CONTEXT_BUDGET
APPROVAL_TTL = 30 * 60         # seconds

SYSTEM_PROMPT = """You are the ops assistant inside ClaudeOS, a homelab mission-control app. You answer questions about this specific homelab using the tools provided, and you can make changes when the user approves them.

The lab: a UniFi network (UDM-SE gateway, switches, access points), a Proxmox host running VMs and LXC containers, an Ubuntu VM running a Docker fleet with an NVIDIA GPU passed through, Home Assistant (HAOS) with a large Zigbee/ZHA mesh, and a Synology NAS.

Deliberately vague above: model numbers, hostnames, IPs, container names and capacities are NOT stated here because they change. Get every specific from a tool. If you name a piece of hardware, that name must have come from a tool result on this turn.

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


# -------------------------------------------------------------- the turn

def _drive(conv: dict, client, resume_results: list | None = None):
    """Run the shared loop over this conversation, yielding its events.

    The only thing layered on the loop here is the approval hook: it mints the
    single-use pending id, records it on the conversation so `_finish` persists
    it, and hands it back for the `approval_required` event the browser renders
    as a confirmation card. The loop stops right after that event; run_approval
    resumes from the persisted state.
    """
    messages = conv["messages"]
    if resume_results:
        messages.append({"role": "user", "content": resume_results})

    def approval(name, params, tool_use_id, env, other_results):
        pid = secrets.token_hex(8)
        env["pending_id"] = pid
        pending = {
            "id": pid, "tool": name, "params": params,
            "tool_use_id": tool_use_id, "expires": time.time() + APPROVAL_TTL,
            "warning": env.get("warning"),
            "other_results": other_results,   # sibling calls already resolved
        }
        oplog.add("info", "chat", f"approval requested: {env['invocation']}")
        conv["pending"] = pending
        return pending

    # Writes included: this is the one caller with a human to approve them.
    return toolloop.run(client, messages, schemas=tools.schemas(),
                        system=toolloop.system_blocks(SYSTEM_PROMPT),
                        approval=approval)


def _finish(conv: dict, payload: dict, *, suspended: bool):
    """Record turn accounting and persist. Returns the cost event payload.

    Runs on every ending the loop has, error paths included — a turn that failed
    part-way through still spent money, and dropping that would understate the
    conversation's cost.
    """
    usage = payload.get("usage")
    prompt_tokens = toolloop.prompt_tokens(usage) if usage else 0
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
        "context_full": toolloop.budget_reached(prompt_tokens),
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
    client = toolloop.new_client()
    conv = get_conversation(conversation_id) if conversation_id else _new_conversation(message)

    if conv.get("pending"):
        raise ValueError("resolve the pending action before sending another message")
    # Not just a courtesy: a turn the loop stopped on budget left the transcript
    # ending on an unpaired tool_use, so resuming it would be rejected by the
    # API. Same predicate as the loop's, so the two cannot drift apart.
    used = conv["turns"][-1]["prompt_tokens"] if conv.get("turns") else 0
    if toolloop.budget_reached(used):
        raise ValueError("this conversation is full — start a new one")

    _claim(conv["id"])
    try:
        yield "conversation", {"id": conv["id"], "title": conv["title"]}
        conv["messages"].append({"role": "user", "content": message})
        for event, payload in _drive(conv, client):
            if event in ("finished", "suspended"):
                yield "cost", _finish(conv, payload, suspended=event == "suspended")
                return
            yield event, payload
    finally:
        _release(conv["id"])


def run_approval(cid: str, pending_id: str, decision: str, guidance: str = ""):
    """Generator of (event, payload) resuming a suspended turn."""
    client = toolloop.new_client()
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
            result_block = toolloop.tool_result(pending["tool_use_id"], env)
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
        for event, payload in _drive(conv, client, resume_results=results):
            if event in ("finished", "suspended"):
                yield "cost", _finish(conv, payload, suspended=event == "suspended")
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
