"""Minimal Home Assistant WebSocket client (stdlib only).

Just enough RFC 6455 to authenticate and run one-shot commands like
zha/devices — client-masked text frames, fragmentation, 16/64-bit
lengths, ping/pong. Short-lived connection per call.
"""

import base64
import json
import os
import socket
import ssl
import struct
import urllib.parse


class HAWebSocketError(Exception):
    pass


def _connect(host_url: str, verify_tls: bool, timeout: int = 15):
    u = urllib.parse.urlparse(host_url)
    secure = u.scheme == "https"
    port = u.port or (443 if secure else 80)
    raw = socket.create_connection((u.hostname, port), timeout=timeout)
    if secure:
        ctx = ssl.create_default_context()
        if not verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        raw = ctx.wrap_socket(raw, server_hostname=u.hostname)
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET /api/websocket HTTP/1.1\r\n"
        f"Host: {u.hostname}:{port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    raw.sendall(handshake.encode())
    # read HTTP response headers
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = raw.recv(4096)
        if not chunk:
            raise HAWebSocketError("connection closed during websocket handshake")
        buf += chunk
    status_line = buf.split(b"\r\n", 1)[0].decode(errors="replace")
    if "101" not in status_line:
        raise HAWebSocketError(f"websocket handshake failed: {status_line}")
    return raw, buf.split(b"\r\n\r\n", 1)[1]  # socket + any leftover bytes


def _recv_exact(sock, n: int, leftover: bytearray) -> bytes:
    while len(leftover) < n:
        chunk = sock.recv(65536)
        if not chunk:
            raise HAWebSocketError("connection closed mid-frame")
        leftover.extend(chunk)
    out = bytes(leftover[:n])
    del leftover[:n]
    return out


def _send_text(sock, text: str) -> None:
    payload = text.encode()
    mask = os.urandom(4)
    header = bytearray([0x81])  # FIN + text
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _recv_message(sock, leftover: bytearray) -> str:
    """Receive one complete (possibly fragmented) text message."""
    parts = []
    while True:
        b1, b2 = _recv_exact(sock, 2, leftover)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", _recv_exact(sock, 2, leftover))
        elif length == 127:
            (length,) = struct.unpack(">Q", _recv_exact(sock, 8, leftover))
        payload = _recv_exact(sock, length, leftover)
        if opcode == 0x9:  # ping → pong
            mask = os.urandom(4)
            pong = bytearray([0x8A, 0x80 | min(len(payload), 125)]) + mask
            pong += bytes(b ^ mask[i % 4] for i, b in enumerate(payload[:125]))
            sock.sendall(bytes(pong))
            continue
        if opcode == 0x8:
            raise HAWebSocketError("server closed the websocket")
        if opcode in (0x1, 0x0):  # text / continuation
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")
        # ignore binary/pong frames


def command(settings: dict, cmd: dict, timeout: int = 30):
    """Authenticate and run one command; returns the result payload."""
    host = settings["host"].strip().rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    sock, extra = _connect(host, settings.get("verify_tls", False), timeout=timeout)
    leftover = bytearray(extra)
    sock.settimeout(timeout)
    try:
        first = json.loads(_recv_message(sock, leftover))
        if first.get("type") != "auth_required":
            raise HAWebSocketError(f"unexpected first message: {first.get('type')}")
        _send_text(sock, json.dumps({"type": "auth", "access_token": settings["token"]}))
        auth = json.loads(_recv_message(sock, leftover))
        if auth.get("type") != "auth_ok":
            raise HAWebSocketError("websocket auth failed — check the long-lived token")
        _send_text(sock, json.dumps({"id": 1, **cmd}))
        while True:
            msg = json.loads(_recv_message(sock, leftover))
            if msg.get("id") == 1 and msg.get("type") == "result":
                if not msg.get("success"):
                    err = (msg.get("error") or {}).get("message", "unknown error")
                    raise HAWebSocketError(f"command failed: {err}")
                return msg.get("result")
    finally:
        sock.close()
