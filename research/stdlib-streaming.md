# Research: streaming patterns on the stdlib chassis

**Ticket:** [#7](https://github.com/xpenno255/claudeos/issues/7)
**Question:** how should an agentic chat feature stream LLM tokens (and tool-call
progress events) to the browser, given ClaudeOS's exact stack — a
`http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler` backend
(`server.py`), a no-build ES-module frontend, and Docker with a plain
`8321:8321` port mapping?

All stdlib behaviour below was verified against the CPython source
(Python 3.14 locally; the container runs `python:3.13-slim` — the relevant code
is unchanged between those versions).

---

## 1. What this chassis actually gives us

Facts about `server.py` and the stdlib that constrain every option:

- **`ThreadingHTTPServer` = `ThreadingMixIn` + `HTTPServer`**; one thread per
  TCP connection, and it sets `daemon_threads = True` (verified:
  `http.server.ThreadingHTTPServer.daemon_threads` is `True`), so open streams
  never block interpreter exit.
  ([docs.python.org/3/library/http.server.html](https://docs.python.org/3/library/http.server.html),
  [CPython Lib/http/server.py](https://github.com/python/cpython/blob/main/Lib/http/server.py))
- **`protocol_version` defaults to `'HTTP/1.0'`** and `server.py`'s `Handler`
  does not override it, so every ClaudeOS response is HTTP/1.0: no keep-alive,
  the connection closes after each response, and **connection-close legally
  delimits a body of unknown length**. The docs: "If set to `'HTTP/1.1'`, the
  server will permit HTTP persistent connections; however, your server *must*
  then include an accurate `Content-Length` header … For backwards
  compatibility, the setting defaults to `'HTTP/1.0'`."
  ([docs.python.org/3/library/http.server.html](https://docs.python.org/3/library/http.server.html))
- **`wfile` is effectively unbuffered.** `StreamRequestHandler.wbufsize` is
  `0`, and `setup()` then makes `wfile` a `_SocketWriter` that writes straight
  to the socket (verified in
  [CPython Lib/socketserver.py](https://github.com/python/cpython/blob/main/Lib/socketserver.py)).
  So `self.wfile.write(...)` puts bytes on the wire immediately; an explicit
  `flush()` after each event is a harmless no-op, worth keeping only as
  documentation of intent. The only buffering on the response path is the
  *headers* buffer: `send_response()`/`send_header()` accumulate into
  `_headers_buffer` and nothing is sent until `end_headers()`.
- **`handle_one_request()` catches only `TimeoutError`.** Any
  `BrokenPipeError`/`ConnectionResetError` raised inside a `do_*` method
  propagates to `socketserver`'s `handle_error()` (traceback printed to
  stderr, that connection's thread exits, server unaffected). A streaming
  route must catch disconnects itself if we don't want a stack trace per
  closed tab. (Verified in CPython source, links above.)
- **`http.server` is not production-hardened** — the docs warn "`http.server`
  is not recommended for production. It only implements basic security
  checks." ClaudeOS has already accepted this trade-off for the whole app;
  streaming doesn't change it.
  ([docs.python.org/3/library/http.server.html](https://docs.python.org/3/library/http.server.html))
- **App-specific:** `Handler._dispatch()` wraps every route in
  `try/except Exception` and answers errors with `_send_json(...)`. For a
  streaming route this is actively harmful: after headers + partial body are
  sent, a fallback `_send_json` would try to emit a *second* response head
  onto the same socket (or write into a broken pipe). A streaming endpoint
  needs its own dispatch path that writes the socket itself and swallows
  disconnect errors.

### Threading implications

One thread is pinned per open stream for its entire life. For a homelab app
with a handful of concurrent chat tabs this is fine — threads cost ~8 MB of
*virtual* stack and the real constraint is that each stream thread also holds
whatever upstream LLM connection it proxies. Two real limits to note:

- Browsers cap SSE-style connections at ~6 per browser+domain over HTTP/1.1
  ("the limit is *per browser* and set to a very low number (6)" — a "Won't
  fix" in Chrome/Firefox), so a chat UI should hold at most one stream open at
  a time anyway.
  ([MDN EventSource](https://developer.mozilla.org/en-US/docs/Web/API/EventSource))
- A stream that never ends pins its thread forever; every stream must have a
  server-side termination condition (LLM turn finished, client disconnect
  detected via write failure, or an idle timeout).

---

## 2. The three transports, on this chassis

### 2a. Server-Sent Events (SSE)

The event stream format is defined in the WHATWG HTML spec
([html.spec.whatwg.org/multipage/server-sent-events.html](https://html.spec.whatwg.org/multipage/server-sent-events.html)):

- MIME type **must** be `text/event-stream`, encoded UTF-8.
- A stream is lines of `field: value`. Fields: `data:` (payload, may repeat —
  values join with `\n`), `event:` (names the event type), `id:` (sets the
  last-event-ID), `retry:` (reconnection delay in ms). A **blank line
  dispatches the event**; a line starting with `:` is a comment and is
  ignored (this is the heartbeat mechanism).
- Reconnection: "Clients will reconnect if the connection is closed", and on
  reconnect the browser sends a `Last-Event-ID` request header carrying the
  last `id:` it saw.
- Failure semantics: a response whose status is not 200 or whose
  `Content-Type` is not `text/event-stream` **fails the connection
  permanently** ("Once the user agent has failed the connection, it does not
  attempt to reconnect"); an HTTP 204 tells the client to stop reconnecting;
  plain network errors trigger reconnection.

Implementation in a `BaseHTTPRequestHandler` route is ~15 lines:

```python
def stream_chat(self):
    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream")
    self.send_header("Cache-Control", "no-store")
    self.send_header("X-Accel-Buffering", "no")   # future-proofing, see §4
    self.end_headers()                            # HTTP/1.0: no Content-Length
    try:
        for event, payload in llm_events():       # generator of (name, dict)
            self.wfile.write(
                f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"event: done\ndata: {}\n\n")
    except (BrokenPipeError, ConnectionResetError):
        pass                                      # tab closed — normal
    self.close_connection = True
```

Because the handler speaks HTTP/1.0, no `Content-Length` and no chunked
framing are needed — the close delimits the stream, which is exactly what SSE
clients expect ("reconnect on close" is handled at the application layer with
a terminal `done` event).

**Correctness notes for the HTTP/1.1 variant** (if `protocol_version` were
ever bumped for keep-alive): a streaming response with no `Content-Length`
must then either send `Transfer-Encoding: chunked` (hand-rolled — the stdlib
does not do it for you) or send `Connection: close` / set
`self.close_connection = True` so the client knows the body ends at close.
`send_header("Connection", "close")` sets `close_connection` automatically
(verified in CPython `send_header` source).

### 2b. Raw chunked transfer (NDJSON over `Transfer-Encoding: chunked`)

Chunked coding is defined in RFC 9112 §7.1
([datatracker.ietf.org/doc/html/rfc9112#section-7.1](https://datatracker.ietf.org/doc/html/rfc9112#section-7.1)):

```
chunk       = chunk-size [ chunk-ext ] CRLF chunk-data CRLF
chunk-size  = 1*HEXDIG
last-chunk  = 1*("0") [ chunk-ext ] CRLF   ; then trailer-section CRLF
```

So each write becomes `b"%x\r\n" % len(data) + data + b"\r\n"`, terminated by
`b"0\r\n\r\n"`. Hand-rolling this is only ~6 lines, **but**: RFC 9112 §6.1
says "A server MUST NOT send a response containing Transfer-Encoding unless
the corresponding request indicates HTTP/1.1" — and this server *responds*
with HTTP/1.0, so chunked is not even legal on the current chassis without
first switching `protocol_version` to HTTP/1.1. It buys keep-alive reuse of
the connection and nothing else; the client-side consumption code (fetch +
reader) is identical because the browser de-chunks transparently. Two
counterweights worth recording: RFC 9112 notes that close-delimited bodies
are ambiguous ("there is no way to distinguish a successfully completed,
close-delimited response message from a partially received message
interrupted by network failure") — which is why the recommendation below
uses an explicit application-level `done` event as the true end-of-stream
marker — and the WHATWG spec cautions that "HTTP chunking can have
unexpected negative effects on the reliability of this protocol, in
particular if the chunking is done by a different layer unaware of the
timing requirements." Verdict: extra framing + a protocol-version change
for no user-visible gain.

### 2c. Long-polling

RFC 6202 ([datatracker.ietf.org/doc/html/rfc6202](https://datatracker.ietf.org/doc/html/rfc6202))
defines long polling as holding each request open until an event exists, then
responding completely and having the client immediately re-request. Its §2.2
lists why this is the wrong shape for token streams: "Every long poll request
and long poll response is a complete HTTP message" (full header overhead per
batch of tokens), each cycle costs up to three network transits of latency,
and the server must still hold an open request per client — so it pins a
ThreadingHTTPServer thread *just like streaming does*, while adding per-token
latency and requiring a server-side event queue between polls. Strictly worse
here.

---

## 3. Client side without libraries

### EventSource

Built-in reconnection (with `Last-Event-ID`), built-in `event:`-type
dispatch, three-state `readyState`, `close()` to abort.
([MDN](https://developer.mozilla.org/en-US/docs/Web/API/EventSource), WHATWG
spec above.) But the connection is "a potential-CORS request" with the
default method — **GET only, no request body, no custom headers**. A chat
turn needs to POST a message history. Real apps square this in one of two
ways:

1. **POST + fetch-parsed SSE (the industry norm).** The LLM APIs themselves
   work this way: Anthropic's Messages API streams by setting
   `"stream": true` on the POST — "you can set `"stream": true` to
   incrementally stream the response using server-sent events (SSE)", with
   named events (`message_start`, `content_block_start`,
   `content_block_delta` carrying `text_delta` / `input_json_delta` /
   `thinking_delta`, `content_block_stop`, `message_delta`, `message_stop`,
   plus `ping` and `error`).
   ([platform.claude.com/docs/en/docs/build-with-claude/streaming](https://platform.claude.com/docs/en/docs/build-with-claude/streaming))
   No EventSource is involved; clients parse the SSE text off a fetch body.
   That event vocabulary is also a proven template for our tool-call progress
   events.
2. **POST the message, then GET an EventSource keyed by id.** Two requests,
   server must persist per-conversation event buffers so a reconnecting GET
   can replay from `Last-Event-ID`. More moving parts; only pays off if you
   want free reconnect-and-resume mid-generation.

### fetch + ReadableStream

`response.body` is a `ReadableStream`; `getReader()` locks it and
`await reader.read()` yields `{ done, value }` chunks
([MDN Using readable streams](https://developer.mozilla.org/en-US/docs/Web/API/Streams_API/Using_readable_streams)).
Decode with a persistent `TextDecoder` and `decode(value, { stream: true })`,
which "indicat[es] whether additional data will follow in subsequent calls"
and correctly handles multi-byte UTF-8 sequences split across chunks
([MDN TextDecoder.decode](https://developer.mozilla.org/en-US/docs/Web/API/TextDecoder/decode))
— this matters because emoji in LLM output *will* straddle chunk boundaries.
Abort with `AbortController` passed as `fetch(url, { signal })` — this is the
chat "stop generating" button for free. There is **no automatic
reconnection**; for chat that's the right default (you don't want the browser
silently re-POSTing a completed turn — an interrupted turn should surface an
error and let the user retry).

SSE parsing on top of the reader is ~20 lines: accumulate decoded text in a
buffer, split on `\n\n`, then split each block's lines on `event: ` / `data: `
prefixes, ignore `:`-comment lines.

---

## 4. Failure modes

- **Tab closes mid-stream.** The next `wfile.write()` in the handler thread
  raises `BrokenPipeError` (or `ConnectionResetError`). Unhandled, it reaches
  `socketserver.handle_error()` — server survives but prints a traceback per
  closed tab, and in the current `_dispatch` it would first detour through a
  doomed `_send_json` attempt. The streaming route must catch both exceptions
  and return quietly; the write failure is also the *only* disconnect signal
  the server gets, which is why heartbeats matter (an idle stream never
  notices the peer left).
- **Proxy buffering.** RFC 6202 §3: "There is no requirement for an
  intermediary to immediately forward a partial response," and an
  intermediary may buffer the whole response — this is the classic way SSE
  "doesn't work" behind a reverse proxy. nginx buffers upstream responses by
  default (`proxy_buffering on`), and honors a per-response opt-out: buffering
  "can also be enabled or disabled by passing `yes` or `no` in the
  `X-Accel-Buffering` response header field"
  ([nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering)).
  Sending `X-Accel-Buffering: no` unconditionally costs nothing and makes the
  endpoint survive a future nginx/Traefik front.
- **Docker's plain port mapping is not a buffering proxy.** Publishing a
  port "creates a firewall rule in the host, mapping a container port to a
  port on the Docker host" via NAT/PAT in the kernel (iptables/nftables),
  with the userland `docker-proxy` as a protocol-unaware TCP byte relay for
  loopback/hairpin cases — nothing on this path parses or stores an HTTP body
  ([docs.docker.com/engine/network/port-publishing/](https://docs.docker.com/engine/network/port-publishing/),
  [packet-filtering docs](https://docs.docker.com/engine/network/packet-filtering-firewalls/)).
  It forwards segments as they arrive and cannot hold an HTTP body; the
  current `8321:8321` deployment needs no mitigation.
- **Idle timeouts.** Proxies drop quiet connections — nginx's
  `proxy_read_timeout` defaults to 60s and "if the proxied server does not
  transmit anything within this time, the connection is closed" (nginx docs
  above). The WHATWG spec's mitigation, verbatim: "Legacy proxy servers are
  known to, in certain cases, drop HTTP connections after a short timeout. To
  protect against such proxy servers, authors can include a comment line (one
  starting with a ':' character) every 15 seconds or so."
  For chat this doubles as liveness during long tool calls: emit
  `: ping\n\n` (or a real `event: progress`) while a tool runs, both to keep
  intermediaries happy and to make the server's dead-peer detection prompt.
  (The old "2 KB padding for antivirus proxies" advice has no current primary
  source and targets long-dead clients — skip it.)
- **Compression middleware.** Gzipping a stream means the compressor buffers
  it; ClaudeOS has no compression anywhere (`grep` confirms no
  `gzip`/`Content-Encoding` in `server.py` or `app/`), so the only rule is:
  never add blanket gzip to the streaming route later.
- **Python-side buffering.** Non-issue on this chassis: `wfile` is the
  unbuffered `_SocketWriter` (see §1), and `PYTHONUNBUFFERED=1` in the
  Dockerfile already covers stdout logging.

---

## Recommendation

**POST + SSE-formatted body, consumed with fetch + ReadableStream.** One
endpoint, one connection per chat turn, no protocol changes:

- **Server:** a dedicated streaming route (bypassing `_dispatch`'s JSON
  wrapper) that reads the POSTed chat body, replies
  `200 text/event-stream` + `Cache-Control: no-store` +
  `X-Accel-Buffering: no`, writes spec-format SSE events
  (`event: token`, `event: tool_start`, `event: tool_result`,
  `event: done`, `: ping` heartbeats every ~15s during tool waits), catches
  `BrokenPipeError`/`ConnectionResetError` as a normal end, and lets the
  HTTP/1.0 connection-close delimit the stream. Keep `protocol_version` at
  its HTTP/1.0 default — it makes unbounded bodies legal without chunked
  framing.
- **Client:** `fetch("/api/chat/stream", { method: "POST", body, signal })`,
  read `response.body.getReader()`, decode with
  `TextDecoder.decode(chunk, { stream: true })`, parse SSE blocks on `\n\n`.
  `AbortController.abort()` is the stop button. No auto-reconnect: on error
  short of `event: done`, mark the turn failed and offer retry — correct
  semantics for chat, and it sidesteps EventSource's GET-only limit the same
  way Anthropic's and every mainstream LLM API's own streaming does.

Why not the alternatives: **EventSource** can't carry the POST body and its
auto-reconnect replays turns; **hand-rolled chunked** requires switching the
whole server to HTTP/1.1 (RFC 9112 §6.1) and adds framing for zero UX gain;
**long-polling** pins the same thread per client while adding per-batch header
overhead and latency (RFC 6202 §2.2). Threading cost of the recommendation:
one daemon thread per in-flight chat turn — with the browser's own 6-per-host
connection cap and a single-user homelab UI, that's single-digit threads worst
case.

## Sources

- WHATWG HTML spec, Server-sent events — https://html.spec.whatwg.org/multipage/server-sent-events.html
- MDN, `EventSource` — https://developer.mozilla.org/en-US/docs/Web/API/EventSource
- MDN, Using readable streams — https://developer.mozilla.org/en-US/docs/Web/API/Streams_API/Using_readable_streams
- MDN, `TextDecoder.decode()` — https://developer.mozilla.org/en-US/docs/Web/API/TextDecoder/decode
- Python docs, `http.server` — https://docs.python.org/3/library/http.server.html
- CPython source, `Lib/http/server.py` — https://github.com/python/cpython/blob/main/Lib/http/server.py
- CPython source, `Lib/socketserver.py` — https://github.com/python/cpython/blob/main/Lib/socketserver.py
- RFC 9112 (HTTP/1.1), §6.1 and §7.1 — https://datatracker.ietf.org/doc/html/rfc9112#section-7.1
- RFC 6202 (long polling / streaming best practices) — https://datatracker.ietf.org/doc/html/rfc6202
- nginx, `ngx_http_proxy_module` — https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering
- Anthropic, Streaming Messages — https://platform.claude.com/docs/en/docs/build-with-claude/streaming
- Docker Engine networking — https://docs.docker.com/engine/network/
- Docker Engine, port publishing — https://docs.docker.com/engine/network/port-publishing/
