"""Notification fan-out dispatcher.

Alerts (system down/recover today; uptime monitors, IDS events and AI
reports later) are pushed to every configured-and-enabled channel. Every
channel is a plain HTTP POST, so the stdlib http client is enough:

  ntfy      — JSON publish to the server root (topic name acts as secret)
  webhook   — generic JSON POST {title, message, priority, tags, ts}
  telegram  — Bot API sendMessage
  pushover  — api.pushover.net/1/messages.json
  hanotify  — Home Assistant notify.* service passthrough

Channel settings live in the encrypted store like any other system.
Priorities are "low" | "default" | "high" | "urgent"; tags are ntfy-style
emoji shortcodes and pass through to webhooks verbatim.
"""

import threading
import time

from . import oplog, store
from .connectors import homeassistant
from .httpclient import request

CHANNEL_IDS = ["ntfy", "webhook", "telegram", "pushover", "hanotify"]

# fields that must be present before a channel counts as configured
_REQUIRED = {
    "ntfy": ("topic",),
    "webhook": ("host",),
    "telegram": ("bot_token", "chat_id"),
    "pushover": ("token", "user_key"),
    "hanotify": ("service",),
}

NTFY_PRIORITY = {"low": 2, "default": 3, "high": 4, "urgent": 5}
# pushover 2 requires retry/expire params, so urgent caps at 1
PUSHOVER_PRIORITY = {"low": -1, "default": 0, "high": 1, "urgent": 1}

# identical titles are muted for this long so a flapping system can't
# flood every channel (the poller retries every 30s)
COOLDOWN_S = 300
_mute_lock = threading.Lock()
_last_sent: dict = {}  # title -> ts


def _label(cid: str) -> str:
    return store.SYSTEM_LABELS.get(cid, cid)


# ---------------------------------------------------------------- senders

def _send_ntfy(s, title, message, priority, tags):
    host = (s.get("host") or "https://ntfy.sh").strip().rstrip("/")
    if not host.startswith("http"):
        host = "https://" + host
    request("POST", host, json_body={
        "topic": s["topic"],
        "title": title,
        "message": message,
        "priority": NTFY_PRIORITY.get(priority, 3),
        "tags": tags or [],
    }, verify_tls=s.get("verify_tls", False))


def _send_webhook(s, title, message, priority, tags):
    request("POST", s["host"], json_body={
        "source": "claudeos",
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or [],
        "ts": time.time(),
    }, verify_tls=s.get("verify_tls", False))


def _send_telegram(s, title, message, priority, tags):
    request("POST", f"https://api.telegram.org/bot{s['bot_token']}/sendMessage",
            json_body={"chat_id": s["chat_id"], "text": f"{title}\n{message}"},
            verify_tls=True)


def _send_pushover(s, title, message, priority, tags):
    request("POST", "https://api.pushover.net/1/messages.json", json_body={
        "token": s["token"],
        "user": s["user_key"],
        "title": title,
        "message": message,
        "priority": PUSHOVER_PRIORITY.get(priority, 0),
    }, verify_tls=True)


def _send_hanotify(s, title, message, priority, tags):
    ha = store.get_system("homeassistant", reveal_secrets=True)
    if not ha or not ha.get("host") or not ha.get("token"):
        raise LookupError("HA Notify needs the Home Assistant connection configured first")
    service = s["service"].strip().removeprefix("notify.")
    homeassistant.call_service(ha, "notify", service,
                               data={"title": title, "message": message})


_SENDERS = {
    "ntfy": _send_ntfy,
    "webhook": _send_webhook,
    "telegram": _send_telegram,
    "pushover": _send_pushover,
    "hanotify": _send_hanotify,
}


# ------------------------------------------------------------- dispatcher

def _channel_settings(cid: str) -> dict | None:
    s = store.get_system(cid, reveal_secrets=True)
    if not s or any(not s.get(k) for k in _REQUIRED[cid]):
        return None
    return s


def channels(enabled_only: bool = True) -> list:
    """Ids of channels that are fully configured (and enabled)."""
    out = []
    for cid in CHANNEL_IDS:
        s = _channel_settings(cid)
        if s and (not enabled_only or s.get("enabled") is not False):
            out.append(cid)
    return out


def send(title: str, message: str, priority: str = "default",
         tags: list | None = None, background: bool = True) -> None:
    """Fan an alert out to every enabled channel. Never raises.

    Delivery runs on a daemon thread by default so callers (the poller,
    request handlers) never block on a slow push service.
    """
    now = time.time()
    with _mute_lock:
        if now - _last_sent.get(title, 0) < COOLDOWN_S:
            return
        _last_sent[title] = now
    if background:
        threading.Thread(target=_fan_out, args=(title, message, priority, tags),
                         name="claudeos-notify", daemon=True).start()
    else:
        _fan_out(title, message, priority, tags)


def _fan_out(title, message, priority, tags):
    sent, failed = [], []
    for cid in channels():
        try:
            _SENDERS[cid](_channel_settings(cid), title, message, priority, tags)
            sent.append(_label(cid))
        except Exception as e:  # noqa: BLE001 — one dead channel must not stop the rest
            failed.append(_label(cid))
            oplog.add("warn", "notify", f"{_label(cid)} delivery failed: {e}")
    if sent:
        oplog.add("info", "notify", f'alert "{title}" sent via {", ".join(sent)}')


def test_channel(cid: str) -> dict:
    """Send a real test notification through one channel (Setup page)."""
    if cid not in CHANNEL_IDS:
        raise LookupError(f"unknown notification channel: {cid}")
    s = _channel_settings(cid)
    if s is None:
        missing = ", ".join(k for k in _REQUIRED[cid] if not (store.get_system(cid) or {}).get(k))
        raise LookupError(f"{_label(cid)} is missing required settings: {missing}")
    _SENDERS[cid](s, "ClaudeOS test notification",
                  "If you can read this, the channel works.",
                  "default", ["white_check_mark"])
    return {"ok": True, "detail": f"test notification sent via {_label(cid)}"}
