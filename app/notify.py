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

An install with no channel configured cannot deliver anything, which is a
third outcome beside sent and failed and is recorded as one — see **the
zero-channel gap** below.
"""

import json
import os
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


def state() -> dict:
    """Where alerting stands, for anything that needs to describe it.

    Exists because the weekly report was *inferring* this from stale ops-log
    lines and got it wrong (#53): it announced "no notification channel
    configured" while Telegram was delivering. A caller should be told the
    state, not left to deduce it from the wreckage.

    `paused` is separate from `live` on purpose — a channel configured and then
    switched off is a deliberate act, and reporting it as absent would be as
    wrong as reporting it as working.
    """
    live = channels()
    paused = [c for c in channels(enabled_only=False) if c not in live]
    return {
        "channels": [_label(c) for c in live],
        "paused": [_label(c) for c in paused],
        "any_configured": bool(live),
        "gap": alerting_gap(),
    }


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
    live = channels()
    if not live:
        # Nowhere to send: not a delivery failure, because nothing was attempted
        # and nothing can be retried. Recorded rather than dropped — this branch
        # used to fall through every other one and say nothing at all.
        _record_drop(title, priority)
        oplog.add("warn", "notify", f'alert "{title}" had nowhere to go '
                                    "— no notification channel is configured")
        return
    sent, failed = [], []
    for cid in live:
        try:
            _SENDERS[cid](_channel_settings(cid), title, message, priority, tags)
            sent.append(_label(cid))
        except Exception as e:  # noqa: BLE001 — one dead channel must not stop the rest
            failed.append(_label(cid))
            oplog.add("warn", "notify", f"{_label(cid)} delivery failed: {e}")
    if sent:
        oplog.add("info", "notify", f'alert "{title}" sent via {", ".join(sent)}')
    _clear_gap()


# ------------------------------------------------- the zero-channel gap

# The ops-log line above is the audit record of a dropped alert, but the ops log
# is where somebody looks *after* they suspect something, and an install that has
# never notified is not a state anybody thinks to suspect. So the drops are also
# counted, and the dashboard says so until a channel exists — the app's own rule
# (CONTEXT.md → notification volume) is that a feature which has silently stopped
# working is worth interrupting for, and alerting is that feature here.
#
# Deliberately quiet until something is actually lost: a homelab owner may
# genuinely want no push notifications, and nagging a working install about a
# channel it does not need is how a warning gets trained away.
#
# Persisted because deploys are `docker compose pull && up -d`. A count held only
# in memory would forget precisely the alerts that were dropped overnight.
GAP_PATH = os.path.join(store.DATA_DIR, "notify.json")

_gap_lock = threading.Lock()


def _read_gap() -> dict:
    try:
        with open(GAP_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        # No ops-log line: this file is a convenience over the log, and the log
        # already holds every drop it would have described.
        return {}
    return d if isinstance(d, dict) else {}


def _write_gap(d: dict) -> None:
    try:
        os.makedirs(store.DATA_DIR, exist_ok=True)
        tmp = GAP_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, GAP_PATH)
    except OSError:
        pass  # bookkeeping must never be what breaks the alert path


def _record_drop(title: str, priority: str) -> None:
    """Count one alert that had nowhere to go.

    `send` has already muted an identical title for `COOLDOWN_S` before this is
    reached, so a flapping system counts once per cooldown rather than once per
    poll — the same shape delivery would have had.
    """
    with _gap_lock:
        d = _read_gap()
        d["count"] = int(d.get("count") or 0) + 1
        d["last_title"] = title
        d["last_priority"] = priority
        d["last_ts"] = time.time()
        d.setdefault("since", d["last_ts"])
        _write_gap(d)


def _clear_gap() -> None:
    """Forget the drops: a channel exists, so the gap they describe is closed.

    Called whichever way delivery went. A configured channel that fails logs its
    own failure and is a different problem — the one this record exists for is
    having nowhere to send at all.
    """
    with _gap_lock:
        if not _read_gap().get("count"):
            return
        _write_gap({})


def alerting_gap() -> dict | None:
    """What alerting has lost for want of a channel, or None if nothing.

    None also once a channel is configured, even with drops still on record: the
    dashboard renders this, and a banner asking for something already done is
    worse than no banner.
    """
    if channels():
        return None
    d = _read_gap()
    if not d.get("count"):
        return None
    return {"count": int(d["count"]), "last_title": d.get("last_title"),
            "last_priority": d.get("last_priority"),
            "last_ts": d.get("last_ts"), "since": d.get("since")}


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
