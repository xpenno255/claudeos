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

# An exact repeat is muted for this long (see `send` for what counts as one).
COOLDOWN_S = 300
_mute_lock = threading.Lock()
_last_sent: dict = {}  # ("key", key) | ("title", title) -> (title, ts)


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
            # Previews off: alert text can carry a URL from an unauthenticated
            # source (a heartbeat's own report, model prose), and a preview has
            # Telegram's servers fetch it with no click (ADR-0002, amended).
            json_body={"chat_id": s["chat_id"], "text": f"{title}\n{message}",
                       "link_preview_options": {"is_disabled": True}},
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
         tags: list | None = None, background: bool = True,
         key: str | None = None) -> None:
    """Fan an alert out to every enabled channel. Never raises.

    Delivery runs on a daemon thread by default so callers (the poller,
    request handlers) never block on a slow push service.

    **The mute.** An exact repeat within `COOLDOWN_S` is dropped here, and that
    is what deduplicates the latch-then-notify callers. Each of them records its
    transition and then sends, and wherever two threads can read the latch before
    either writes it, both send: labissues' credential latch is a plain global,
    and any caller can be reached by a sweeper and a route at once. The second
    send is an exact repeat, and this is the last thing that stops it.

    **What counts as a repeat depends on `key`**, the subject the alert is about
    (`"poller:homeassistant"`, `"smart:pve:/dev/sda"`):

    * With a key, the same title for the same key is muted, and nothing else is.
      A different title for that key is a change of state and is never muted, so
      "X is DOWN" after "X recovered" always goes through. A second outage inside
      five minutes used to be muted, and since the poller alerts only on a
      transition it was never raised again: the last message the owner had was
      "recovered", for a system that was down (#72).
    * Without a key, an identical title is muted, as it always was. A caller
      whose titles carry their own identity (a lab issue's number) needs no more.
    """
    if _muted(title, key, time.time()):
        return
    if background:
        threading.Thread(target=_fan_out, args=(title, message, priority, tags),
                         kwargs={"delays": RETRY_DELAYS_S},
                         name="claudeos-notify", daemon=True).start()
    else:
        # A caller that waits on delivery must not wait out the retries.
        _fan_out(title, message, priority, tags, delays=())


def _muted(title: str, key: str | None, now: float) -> bool:
    """True if this repeats an alert sent within `COOLDOWN_S`; recorded if not."""
    slot = ("key", key) if key is not None else ("title", title)
    with _mute_lock:
        # Expired entries go, so keys and titles cannot pile up for the life of
        # the process. Whatever is left is by definition inside the cooldown.
        for s in [s for s, (_, ts) in _last_sent.items() if now - ts >= COOLDOWN_S]:
            del _last_sent[s]
        last = _last_sent.get(slot)
        if last is not None and last[0] == title:
            return True
        _last_sent[slot] = (title, now)
        return False


# Waits before each re-attempt at a channel that failed, in seconds (about 5
# minutes in all). Every caller alerts once per *transition* and records the
# transition before it sends, so whatever this send loses is lost for good: a
# failing backup, a host going down, a SMART warning is never raised a second
# time. One attempt with a 6s timeout used to be all an alert got, so one
# Telegram blip ate it, with only an ops-log line left behind, and nobody reads
# that until they already suspect something. Runs on the notify thread, so
# nothing waits on it.
RETRY_DELAYS_S = (15, 60, 240)


def _fan_out(title, message, priority, tags, *, delays=(), sleep=time.sleep):
    live = channels()
    if not live:
        # Nowhere to send: not a delivery failure, because nothing was attempted
        # and nothing can be retried. Recorded rather than dropped — this branch
        # used to fall through every other one and say nothing at all.
        _record_drop(title, priority)
        oplog.add("warn", "notify", f'alert "{title}" had nowhere to go '
                                    "— no notification channel is configured")
        return
    sent, pending = [], list(live)
    attempts = 0
    raised = time.localtime()
    for wait in (0, *delays):
        if not pending:
            break
        if wait:
            sleep(wait)
        attempts += 1
        # A retry can land after a newer alert about the same thing (a DOWN
        # arriving after its "recovered"), and notify cannot tell which alerts
        # concern the same thing. So a late copy says when it was raised, and the
        # owner can put the messages back in order.
        body = message if attempts == 1 else (
            f"{message}\n(raised {time.strftime('%H:%M', raised)}, delivered late)")
        failing = []
        for cid in pending:
            try:
                _SENDERS[cid](_channel_settings(cid), title, body, priority, tags)
                sent.append(_label(cid))
            except Exception as e:  # noqa: BLE001 — one dead channel must not stop the rest
                failing.append(cid)
                oplog.add("warn", "notify",
                          f"{_label(cid)} delivery failed (attempt {attempts}): {e}")
        pending = failing
    if sent:
        oplog.add("info", "notify", f'alert "{title}" sent via {", ".join(sent)}')
    if pending:
        # The line that says the alert itself is gone, as opposed to one try failing.
        oplog.add("error", "notify",
                  f'alert "{title}" was not delivered via '
                  f'{", ".join(_label(c) for c in pending)} after {attempts} attempts')
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

    `send` has already muted an exact repeat for `COOLDOWN_S` before this is
    reached, so a repeated alert counts once per cooldown rather than once per
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


def telegram_chat_ids() -> dict:
    """Chats that have messaged the bot, read with the stored token.

    Exists so nobody ever has to build a `getUpdates` URL by hand. The Setup
    card used to tell them to, and both halves of that advice did damage (#54):
    it put a live bot token in a browser URL bar — history, sync, and whatever
    sits in between, the exact spread #45 was fixed for — and then *consumed the
    message it was telling you to go and read*, because `getUpdates` without an
    offset acknowledges the pending updates and they do not come back.

    Two details that make this safe to press:

    * `offset=-1` returns the most recent update **without** acknowledging the
      rest, so a queue somebody has already drained still yields something and
      pressing twice is not destructive.
    * An empty result is the normal first state, not a fault, so it comes back
      `ok` with no chats and the caller asks for a message rather than reporting
      an error.

    The token never leaves the server, which is the reason this is a route at all
    rather than a better-worded hint.
    """
    s = store.get_system("telegram", reveal_secrets=True)
    if not (s or {}).get("bot_token"):
        raise LookupError("no Telegram bot token stored yet — paste the token from "
                          "@BotFather and press SAVE first, then try again")
    r = request("GET",
                f"https://api.telegram.org/bot{s['bot_token']}/getUpdates?offset=-1",
                verify_tls=True)
    if not isinstance(r, dict) or not r.get("ok"):
        detail = (r or {}).get("description") if isinstance(r, dict) else None
        raise ConnectionError(f"Telegram rejected getUpdates: {detail or 'unexpected reply'}")

    # Keyed by id so one chat that sent several messages is offered once.
    chats = {}
    for u in r.get("result") or []:
        msg = (u.get("message") or u.get("edited_message")
               or u.get("channel_post") or u.get("my_chat_member") or {})
        chat = msg.get("chat") or {}
        if (cid := chat.get("id")) is None:
            continue
        who = (" ".join(filter(None, (chat.get("first_name"), chat.get("last_name"))))
               or chat.get("title") or chat.get("username") or chat.get("type") or "chat")
        chats[str(cid)] = {"chat_id": str(cid), "label": who, "type": chat.get("type")}
    return {"ok": True, "chats": list(chats.values())}


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
