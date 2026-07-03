"""Claude-powered analysis for ClaudeOS.

Calls the Anthropic Messages API (model: claude-opus-4-8) with structured
JSON output so results render as ranked, categorised findings.

Preferred path: the official `anthropic` SDK (installed in .venv — run the
server with .venv/bin/python3). It provides automatic retries on 429/5xx
and typed errors. If the SDK isn't importable (plain system Python), a raw
urllib fallback keeps the feature working.
"""

import json

from . import store
from .httpclient import HttpError, request

try:
    import anthropic
    HAS_SDK = True
except ImportError:
    anthropic = None
    HAS_SDK = False

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-opus-4-8"
API_VERSION = "2023-06-01"
TIMEOUT = 540  # Opus with adaptive thinking can take minutes on big logs

LOG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string",
                    "description": "2-3 sentence overall health assessment of this Home Assistant instance"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "serious", "warning", "info"]},
                    "category": {"type": "string",
                                 "description": "short category, e.g. integration, zigbee, network, database, automation, hardware"},
                    "title": {"type": "string", "description": "one-line issue title"},
                    "detail": {"type": "string", "description": "what is happening and why, referencing log evidence"},
                    "recommendation": {"type": "string",
                                       "description": "concrete fix steps, referencing HA UI paths or config where possible"},
                    "affected": {"type": "string", "description": "affected integration/entity/component, or 'general'"},
                },
                "required": ["severity", "category", "title", "detail", "recommendation", "affected"],
            },
        },
    },
    "required": ["summary", "issues"],
}

ZHA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "description": "2-3 sentence assessment of the Zigbee mesh health"},
        "grade": {"type": "string", "enum": ["A", "B", "C", "D", "F"],
                  "description": "overall mesh health grade"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "serious", "warning", "info"]},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "recommendation": {"type": "string"},
                },
                "required": ["severity", "title", "detail", "recommendation"],
            },
        },
    },
    "required": ["summary", "grade", "findings"],
}


def _settings() -> dict:
    s = store.get_system("ai", reveal_secrets=True)
    if not s or not s.get("api_key"):
        raise LookupError("AI is not configured — add your Anthropic API key on the Setup page")
    return s


# ------------------------------------------------------------ SDK path

def _sdk_client(key: str):
    return anthropic.Anthropic(api_key=key, timeout=TIMEOUT)


def _sdk_test(key: str) -> dict:
    try:
        m = _sdk_client(key).models.retrieve(MODEL)
    except anthropic.AuthenticationError as e:
        raise ConnectionError("Anthropic rejected the API key (401) — check it was pasted "
                              "exactly from console.anthropic.com") from e
    except anthropic.APIConnectionError as e:
        raise ConnectionError(f"cannot reach the Anthropic API: {e}") from e
    return {"ok": True, "detail": f"key valid — analysis model: {m.display_name} (via SDK)"}


def _sdk_ask_json(key: str, system_prompt: str, user_text: str, schema: dict,
                  max_tokens: int) -> dict:
    client = _sdk_client(key)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user_text}],
        )
    except anthropic.AuthenticationError as e:
        raise ConnectionError("Anthropic API key rejected (401) — re-enter it on the Setup page") from e
    except anthropic.RateLimitError as e:
        raise ConnectionError("Anthropic rate limit hit (429) — wait a minute and retry") from e
    except anthropic.APIStatusError as e:
        raise ConnectionError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ConnectionError(f"cannot reach the Anthropic API: {e}") from e

    if resp.stop_reason == "refusal":
        raise ValueError("the model declined to analyse this content")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise ValueError(f"no analysis returned (stop_reason: {resp.stop_reason})")
    if resp.stop_reason == "max_tokens":
        raise ValueError("analysis was truncated — try again (the log slice may be too large)")
    out = json.loads(text)
    out["_usage"] = {"input_tokens": resp.usage.input_tokens,
                     "output_tokens": resp.usage.output_tokens}
    return out


# ------------------------------------------------------------ raw fallback

def _raw_test(key: str) -> dict:
    try:
        m = request(
            "GET", f"https://api.anthropic.com/v1/models/{MODEL}",
            headers={"x-api-key": key, "anthropic-version": API_VERSION},
            verify_tls=True, timeout=20,
        )
    except HttpError as e:
        if e.status == 401:
            raise ConnectionError("Anthropic rejected the API key (401) — check it was pasted "
                                  "exactly from console.anthropic.com") from e
        raise
    return {"ok": True, "detail": f"key valid — analysis model: {m.get('display_name', MODEL)} "
                                  "(raw HTTP — run the server with .venv/bin/python3 to use the SDK)"}


def _raw_ask_json(key: str, system_prompt: str, user_text: str, schema: dict,
                  max_tokens: int) -> dict:
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "thinking": {"type": "adaptive"},
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
        "messages": [{"role": "user", "content": user_text}],
    }
    try:
        resp = request(
            "POST", API_URL,
            headers={"x-api-key": key, "anthropic-version": API_VERSION},
            json_body=body, verify_tls=True, timeout=TIMEOUT,
        )
    except HttpError as e:
        if e.status == 401:
            raise ConnectionError("Anthropic API key rejected (401) — re-enter it on the Setup page") from e
        if e.status == 429:
            raise ConnectionError("Anthropic rate limit hit (429) — wait a minute and retry") from e
        detail = ""
        try:
            detail = json.loads(e.body).get("error", {}).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            pass
        raise ConnectionError(f"Anthropic API error {e.status}: {detail or e.body[:200]}") from e

    stop = resp.get("stop_reason")
    if stop == "refusal":
        raise ValueError("the model declined to analyse this content")
    text = next((b.get("text") for b in resp.get("content", []) if b.get("type") == "text"), None)
    if not text:
        raise ValueError(f"no analysis returned (stop_reason: {stop})")
    if stop == "max_tokens":
        raise ValueError("analysis was truncated — try again (the log slice may be too large)")
    out = json.loads(text)
    usage = resp.get("usage", {})
    out["_usage"] = {"input_tokens": usage.get("input_tokens"),
                     "output_tokens": usage.get("output_tokens")}
    return out


# ------------------------------------------------------------ public API

def test(settings: dict) -> dict:
    key = settings["api_key"]
    return _sdk_test(key) if HAS_SDK else _raw_test(key)


def ask_json(system_prompt: str, user_text: str, schema: dict, max_tokens: int = 16000) -> dict:
    key = _settings()["api_key"]
    if HAS_SDK:
        return _sdk_ask_json(key, system_prompt, user_text, schema, max_tokens)
    return _raw_ask_json(key, system_prompt, user_text, schema, max_tokens)


# ------------------------------------------------------------ analyses

LOG_SYSTEM_PROMPT = """You are a senior Home Assistant administrator reviewing the error log of a
homelab HAOS instance. Identify real, actionable issues.

Rules:
- Deduplicate: repeated occurrences of the same underlying problem are ONE issue; mention the
  repetition count in the detail.
- Rank by real-world impact on a home: crashes/failed startups/unavailable core services are
  critical; failing integrations or devices are serious; noisy-but-harmless deprecation warnings
  are info.
- Ignore benign one-off warnings that need no action, but do surface patterns.
- Recommendations must be concrete: name the integration, the Settings menu path, the YAML key,
  or the exact next diagnostic step. Never say just "check the logs".
- If the log is largely clean, say so in the summary and return few or no issues."""

ZHA_SYSTEM_PROMPT = """You are a Zigbee mesh expert reviewing a Home Assistant ZHA network. You get a
device inventory (LQI, RSSI, availability, power source, device type) and recent
zigbee-related log lines.

Assess mesh health: coverage and router/end-device ratio, weak links (low LQI/RSSI),
offline or flaky devices, interference signs, and improvement opportunities (router
placement, adding repeaters, channel issues, battery replacements). Grade the mesh
A-F. Findings must be specific to the devices named in the data. Recommendations must
be actionable for a homelab user."""


def analyze_ha_logs(log_text: str, context: str) -> dict:
    # keep the newest slice — errors at the end are the freshest
    log_slice = log_text[-60000:]
    user = (f"Instance context: {context}\n\n"
            f"Home Assistant error log (most recent last):\n\n```\n{log_slice}\n```")
    return ask_json(LOG_SYSTEM_PROMPT, user, LOG_SCHEMA)


def analyze_zha(devices: list, log_lines: list, context: str) -> dict:
    user = (f"Instance context: {context}\n\n"
            f"ZHA device inventory ({len(devices)} devices):\n"
            f"```json\n{json.dumps(devices, indent=1)}\n```\n\n"
            f"Recent zigbee-related log lines ({len(log_lines)}):\n"
            f"```\n" + "\n".join(log_lines[-200:]) + "\n```")
    return ask_json(ZHA_SYSTEM_PROMPT, user, ZHA_SCHEMA)
