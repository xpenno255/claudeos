"""Encrypted configuration store for ClaudeOS.

Connection settings live in data/config.json. Secret fields (passwords,
API tokens) are encrypted with AES-256-GCM using a machine-local master
key generated on first run at data/master.key (mode 0600). Secrets are
decrypted server-side only and are never returned to the browser.
"""

import base64
import json
import os
import secrets
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# CLAUDEOS_DATA overrides the state directory (containers mount /data here)
DATA_DIR = os.environ.get("CLAUDEOS_DATA") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
KEY_PATH = os.path.join(DATA_DIR, "master.key")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

# Fields treated as secrets per system id.
SECRET_FIELDS = {
    "unifi": ["password"],
    "proxmox": ["token_secret"],
    "docker": [],
    "homeassistant": ["token"],
    "ai": ["api_key"],  # Anthropic API key for analysis features
    # container registry credentials (app/registry.py update checks)
    "registries": ["dockerhub_token", "ghcr_token"],
    # notification channels (app/notify.py) — stored like any other system
    "ntfy": ["topic"],  # the topic name is the only secret ntfy has
    "webhook": [],
    "telegram": ["bot_token"],
    "pushover": ["token", "user_key"],
    "hanotify": [],
}

SYSTEM_IDS = list(SECRET_FIELDS.keys())

SYSTEM_LABELS = {
    "unifi": "UniFi Network",
    "proxmox": "Proxmox VE",
    "docker": "Docker",
    "homeassistant": "Home Assistant",
    "ai": "Claude AI",
    "registries": "Container Registries",
    "ntfy": "ntfy",
    "webhook": "Webhook",
    "telegram": "Telegram",
    "pushover": "Pushover",
    "hanotify": "HA Notify",
}

_lock = threading.Lock()


def _ensure_key() -> bytes:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(KEY_PATH):
        key = secrets.token_bytes(32)
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key
    with open(KEY_PATH, "rb") as f:
        return f.read()


def _encrypt(plaintext: str) -> str:
    key = _ensure_key()
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), b"claudeos")
    return base64.b64encode(nonce + ct).decode("ascii")


def _decrypt(blob: str) -> str:
    key = _ensure_key()
    raw = base64.b64decode(blob)
    return AESGCM(key).decrypt(raw[:12], raw[12:], b"claudeos").decode("utf-8")


def _load_raw() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_raw(cfg: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def save_system(system_id: str, settings: dict) -> None:
    """Persist settings for a system, encrypting secret fields.

    An empty/missing secret field keeps the previously stored value so the
    user never has to re-enter it just to change a hostname.
    """
    if system_id not in SECRET_FIELDS:
        raise ValueError(f"unknown system: {system_id}")
    with _lock:
        cfg = _load_raw()
        existing = cfg.get(system_id, {})
        entry = {}
        for k, v in settings.items():
            if isinstance(v, str):
                v = v.strip()  # pasted tokens often carry a trailing newline
            if k in SECRET_FIELDS[system_id]:
                if v:
                    entry[k] = {"enc": _encrypt(str(v))}
                elif k in existing:
                    entry[k] = existing[k]
            else:
                entry[k] = v
        cfg[system_id] = entry
        _save_raw(cfg)


def delete_system(system_id: str) -> None:
    with _lock:
        cfg = _load_raw()
        cfg.pop(system_id, None)
        _save_raw(cfg)


def get_system(system_id: str, reveal_secrets: bool = False) -> dict | None:
    """Return settings for a system. Secrets are decrypted only when
    reveal_secrets is True (connector use); otherwise they are masked
    as booleans (field_set: True) for the UI."""
    with _lock:
        cfg = _load_raw()
    entry = cfg.get(system_id)
    if entry is None:
        return None
    out = {}
    for k, v in entry.items():
        if isinstance(v, dict) and "enc" in v:
            out[k] = _decrypt(v["enc"]) if reveal_secrets else True
        else:
            out[k] = v
    return out


def public_summary() -> dict:
    """Per-system config summary safe to send to the browser."""
    out = {}
    for sid in SYSTEM_IDS:
        entry = get_system(sid, reveal_secrets=False)
        if entry is None:
            out[sid] = {"configured": False}
        else:
            masked = {}
            for k, v in entry.items():
                masked[k] = "•••••" if k in SECRET_FIELDS[sid] and v else v
            out[sid] = {"configured": True, "settings": masked}
    return out
