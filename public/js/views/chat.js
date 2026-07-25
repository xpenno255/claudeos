// Agentic ops chat — MISSION LOG layout (spec §14).
//
// One wide column: user turns as amber command lines, tool calls as full-width
// instrument strips that expand inline, the approval card in flow, composer
// pinned at the bottom with SEND becoming STOP while streaming.
//
// Streaming is POST + SSE parsed with fetch/ReadableStream (EventSource can't
// carry a body and its auto-reconnect would replay turns).

import { api } from "../api.js";
import { el, clockTime } from "../util.js";
import { BY_ID } from "../meta.js";

const SYS_OF = {
  unifi: "unifi", proxmox: "proxmox", docker: "docker", ha: "homeassistant",
  synology: "synology", get: null,
};

function toolColor(name) {
  const prefix = name.split("_")[0];
  const sys = SYS_OF[prefix];
  return sys ? BY_ID[sys].hex : "var(--cyan)";
}

const STATUS_CLASS = {
  success: "ok", error: "err", no_data: "warn", approval_required: "warn",
};

export async function renderChat(root, args, { toast }) {
  const state = {
    conversationId: args[0] || null,
    streaming: false,
    pending: null,
    abort: null,
    full: false,
  };

  const meta = await api.chats().catch(() => ({ conversations: [], available: false }));

  if (!meta.available) {
    root.append(el("div", { class: "panel accent hero-empty" },
      el("div", { class: "glyph" }, "◈"),
      el("h2", {}, "CHAT NEEDS THE SDK"),
      el("p", {}, "The agentic chat requires the anthropic SDK. Start ClaudeOS with ",
        el("code", {}, ".venv/bin/python3 server.py"),
        " — the other AI features work either way.")));
    return;
  }

  // ---- layout: history rail + transcript column
  const log = el("div", { class: "chat-log" });
  const composerWrap = el("div", { class: "chat-composer-wrap" });
  const column = el("div", { class: "chat-col" }, log, composerWrap);

  const histBox = el("div", { class: "panel chat-hist" });
  root.append(el("div", { class: "chat-shell" }, column, histBox));

  function renderHistory(list) {
    histBox.replaceChildren(
      el("div", { class: "panel-title" }, "CONVERSATIONS"),
      el("button", { class: "btn btn-mini", style: "width:100%;margin-bottom:10px",
        onclick: () => { location.hash = "#/chat"; location.reload(); } }, "＋ NEW"),
      ...(list.length ? list.map(c => el("a", {
        class: `chat-hist-row ${c.id === state.conversationId ? "on" : ""}`,
        href: `#/chat/${c.id}`,
        onclick: () => setTimeout(() => location.reload(), 0),
      },
        el("div", { class: "strong" }, c.title || "untitled"),
        el("div", { class: "mono-dim", style: "font-size:10px" },
          `${clockTime(c.updated)} · ${c.turns} turn${c.turns === 1 ? "" : "s"} · $${c.cost_usd}`,
          c.pending ? el("span", { style: "color:var(--amber)" }, " · ⚠ PENDING") : null)))
        : [el("div", { class: "mono-dim", style: "font-size:11px" }, "no conversations yet")]));
  }
  renderHistory(meta.conversations || []);

  // ---- composer
  const input = el("textarea", { class: "chat-input", rows: "1",
    placeholder: "ask about your lab…" });
  const sendBtn = el("button", { class: "btn btn-mini" }, "SEND ▸");
  const costLine = el("div", { class: "chat-meta" });
  const composer = el("div", { class: "px-composer chat-composer" }, input, sendBtn);
  composerWrap.append(costLine, composer);

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  });
  sendBtn.addEventListener("click", () => {
    if (state.streaming) { state.abort?.abort(); return; }
    submit();
  });

  function setComposer() {
    if (state.full) {
      input.disabled = true; sendBtn.disabled = true;
      input.placeholder = "conversation full — start a new one";
      return;
    }
    if (state.pending) {
      input.disabled = true; sendBtn.disabled = true;
      input.placeholder = "resolve the pending action above first";
      sendBtn.className = "btn btn-mini"; sendBtn.textContent = "SEND ▸";
      return;
    }
    input.disabled = state.streaming;
    sendBtn.disabled = false;
    sendBtn.className = state.streaming ? "btn btn-mini btn-danger" : "btn btn-mini";
    sendBtn.textContent = state.streaming ? "■ STOP" : "SEND ▸";
    input.placeholder = state.streaming ? "" : "ask about your lab…";
  }

  function scroll() { log.scrollTop = log.scrollHeight; }

  // ---- transcript pieces
  function userLine(text) {
    log.append(el("div", { class: "chat-user" }, el("span", {}, "▸"),
      el("b", {}, text.toUpperCase())));
  }

  function strip(name, envelope) {
    const st = envelope?.status || "running";
    const caret = el("span", { style: "color:var(--ink-3)" }, "▸");
    const body = el("pre", { class: "chat-strip-body" });
    body.style.display = "none";
    if (envelope) {
      const shown = { ...envelope };
      delete shown.data;
      body.textContent =
        (envelope.omitted ? `⚠ ${envelope.omitted}\n\n` : "") +
        (envelope.error ? `${envelope.error}\n\n` : "") +
        JSON.stringify(envelope.data ?? shown, null, 2);
    }
    const head = el("div", { class: "chat-strip-head", onclick: () => {
      const open = body.style.display === "none";
      body.style.display = open ? "block" : "none";
      caret.textContent = open ? "▾" : "▸";
    } },
      caret,
      el("span", { class: "chat-strip-name" }, name),
      el("span", { class: "chat-strip-inv" }, envelope?.invocation || ""),
      el("span", { class: `pill ${STATUS_CLASS[st] || "neutral"}` }, st.toUpperCase()),
      el("span", { style: "color:var(--ink-3)" },
        envelope?.elapsed_ms != null ? `${envelope.elapsed_ms}ms` : ""));
    const wrap = el("div", { class: "chat-strip", style: `--sysc:${toolColor(name)}` },
      head, body);
    log.append(wrap);
    return { wrap, head, body, caret };
  }

  function approvalCard(pending, envelope) {
    const guidance = el("input", { class: "chat-guidance", type: "text",
      placeholder: "…tell Claude what to do instead" });
    guidance.style.display = "none";

    const act = (decision) => async () => {
      card.querySelectorAll("button").forEach(b => { b.disabled = true; });
      await runStream(`/api/chat/${state.conversationId}/approve`, {
        pending_id: pending.id, decision,
        guidance: decision === "deny_guided" ? guidance.value : "",
      }, decision === "approve" ? "APPROVED" : "DENIED");
    };

    const buttons = el("div", { class: "chat-appr-acts" },
      el("button", { class: "btn btn-mini", onclick: act("approve") }, "✓ APPROVE"),
      el("button", { class: "btn btn-mini btn-danger", onclick: act("deny") }, "✕ DENY"),
      el("button", { class: "btn btn-mini btn-ghost", onclick: () => {
        guidance.style.display = "block"; guidance.focus();
      } }, "✕ DENY WITH GUIDANCE"));

    guidance.addEventListener("keydown", (e) => {
      if (e.key === "Enter") act("deny_guided")();
    });

    const expiresAt = pending.expires * 1000;
    const expiry = el("div", { class: "chat-appr-target", style: "margin-top:9px" });
    const tick = setInterval(() => {
      const left = Math.max(0, Math.floor((expiresAt - Date.now()) / 1000));
      expiry.textContent = `expires in ${String(Math.floor(left / 60)).padStart(2, "0")}:`
        + `${String(left % 60).padStart(2, "0")}  ·  single-use id ${pending.id.slice(0, 6)}`;
      if (left <= 0) clearInterval(tick);
    }, 1000);

    const card = el("div", { class: "chat-appr" },
      el("div", { class: "chat-appr-tag" }, "⚠ APPROVAL REQUIRED",
        el("span", { class: "pill warn" }, "APPROVAL_REQUIRED")),
      el("div", { class: "chat-appr-inv" }, envelope.invocation),
      pending.warning
        ? el("div", { class: "chat-warn" }, el("span", {}, "⚠"), el("span", {}, pending.warning))
        : null,
      buttons, guidance, expiry);
    log.append(card);
    return card;
  }

  function terminal(kind, message, actionLabel, onAction) {
    const box = el("div", { class: `chat-terminal ${kind}` },
      el("div", { class: "chat-terminal-head" },
        kind === "err" ? "✕ STREAM INTERRUPTED" : "◈ CONVERSATION FULL"),
      el("div", { style: "margin-top:8px" }, message),
      actionLabel
        ? el("button", { class: "btn btn-mini", style: "margin-top:12px", onclick: onAction },
            actionLabel)
        : null);
    log.append(box);
    scroll();
  }

  // ---- SSE stream driver
  async function runStream(url, body, echoLabel) {
    state.streaming = true;
    state.pending = null;
    setComposer();

    const ctrl = new AbortController();
    state.abort = ctrl;

    let prose = null;                 // current assistant text block
    const strips = new Map();         // tool name -> strip handle awaiting result
    let sawDone = false;

    if (echoLabel) {
      log.append(el("div", { class: "chat-decision" }, `▸ ${echoLabel}`));
      scroll();
    }

    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      if (!resp.ok || !resp.body) throw new Error(`stream failed: ${resp.status}`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });

        let split;
        while ((split = buf.indexOf("\n\n")) !== -1) {
          const frame = buf.slice(0, split);
          buf = buf.slice(split + 2);
          if (frame.startsWith(":")) continue;           // heartbeat comment

          let event = "message", data = "{}";
          for (const line of frame.split("\n")) {
            if (line.startsWith("event: ")) event = line.slice(7).trim();
            else if (line.startsWith("data: ")) data = line.slice(6);
          }
          let payload = {};
          try { payload = JSON.parse(data); } catch { /* keep {} */ }

          if (event === "conversation") {
            state.conversationId = payload.id;
            history.replaceState(null, "", `#/chat/${payload.id}`);
          } else if (event === "token") {
            if (!prose) { prose = el("div", { class: "chat-prose" }); log.append(prose); }
            prose.textContent += payload.text;
            scroll();
          } else if (event === "tool_start") {
            prose = null;
            strips.set(payload.name, strip(payload.name, null));
            scroll();
          } else if (event === "tool_result") {
            const handle = strips.get(payload.name);
            if (handle) { handle.wrap.remove(); strips.delete(payload.name); }
            strip(payload.name, payload.envelope);
            scroll();
          } else if (event === "approval_required") {
            prose = null;
            state.pending = payload.pending;
            approvalCard(payload.pending, payload.envelope);
            setComposer();
            scroll();
          } else if (event === "cost") {
            costLine.replaceChildren(
              el("span", {}, `${payload.tools} tool${payload.tools === 1 ? "" : "s"}`),
              el("span", {}, `${(payload.prompt_tokens / 1000).toFixed(1)}k ctx · ${payload.context_pct}%`),
              el("span", { style: "color:var(--amber)" }, `$${payload.cost_usd} this turn`),
              el("span", { class: "mono-dim" }, `$${payload.total_usd} total`));
            if (payload.context_full) {
              state.full = true;
              terminal("full", "This conversation has reached its context budget. "
                + "Start a new one to keep going — nothing here is lost.",
                "▸ NEW CONVERSATION", () => { location.hash = "#/chat"; location.reload(); });
            }
          } else if (event === "error") {
            prose = null;
            log.append(el("div", { class: "chat-error" }, `✕ ${payload.message}`));
            scroll();
          } else if (event === "done") {
            sawDone = true;
          }
        }
      }
      if (!sawDone && !ctrl.signal.aborted) {
        terminal("err", "The connection dropped before the answer finished. "
          + "No changes were made without your approval.", "⟳ RETRY", () => location.reload());
      }
    } catch (e) {
      if (ctrl.signal.aborted) {
        log.append(el("div", { class: "mono-dim", style: "margin:8px 0" }, "— stopped —"));
      } else {
        terminal("err", String(e.message || e), "⟳ RETRY", () => location.reload());
      }
    } finally {
      state.streaming = false;
      state.abort = null;
      setComposer();
      api.chats().then(m => renderHistory(m.conversations || [])).catch(() => {});
    }
  }

  async function submit() {
    const text = input.value.trim();
    if (!text || state.streaming || state.pending || state.full) return;
    input.value = "";
    input.style.height = "auto";
    userLine(text);
    scroll();
    await runStream("/api/chat/stream", {
      message: text, conversation_id: state.conversationId,
    }, null);
  }

  // ---- replay an existing conversation
  if (state.conversationId) {
    try {
      const conv = await api.chat(state.conversationId);
      for (const msg of conv.messages || []) {
        if (msg.role === "user") {
          if (typeof msg.content === "string") userLine(msg.content);
        } else if (Array.isArray(msg.content)) {
          for (const block of msg.content) {
            if (block.type === "text" && block.text?.trim()) {
              log.append(el("div", { class: "chat-prose" }, block.text));
            } else if (block.type === "tool_use") {
              strip(block.name, { status: "success", invocation: `${block.name}(…)`,
                data: block.input });
            }
          }
        }
      }
      const last = (conv.turns || []).at(-1);
      if (last) {
        const n = (conv.turns || []).length;
        costLine.replaceChildren(
          el("span", { class: "mono-dim" },
            `${n} turn${n === 1 ? "" : "s"} · $`
            + (conv.turns.reduce((a, t) => a + (t.cost_usd || 0), 0)).toFixed(4) + " total"));
        if (last.prompt_tokens >= 96000) state.full = true;
      }
      if (conv.pending) {
        state.pending = conv.pending;
        approvalCard(conv.pending, {
          invocation: `${conv.pending.tool}(…)`, status: "approval_required" });
      }
      scroll();
    } catch (e) {
      toast(String(e.message || e), "err", "CHAT");
    }
  } else {
    log.append(el("div", { class: "chat-hint" },
      el("div", { class: "glyph" }, "◈"),
      el("h3", {}, "ASK ABOUT YOUR LAB"),
      el("p", {}, "Claude can read every linked system and, with your approval, "
        + "restart containers and guests, call Home Assistant services, or reboot "
        + "UniFi devices. Every change asks first."),
      el("div", { class: "chat-examples" },
        ...["why is plex buffering?", "is anything unhealthy right now?",
            "which disks are wearing out?", "what changed in the last hour?"]
          .map(q => el("button", { class: "btn btn-mini btn-ghost", onclick: () => {
            input.value = q; submit();
          } }, q)))));
  }

  setComposer();
  return () => state.abort?.abort();
}
