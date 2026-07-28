# Per-Caller Reasoning Effort on the AI Analyses

`app/ai.py` sends no `output_config.effort` on any call. All four analyses —
`analyze_health`, `analyze_ha_logs`, `analyze_zha`, `analyze_unifi_event` — run at
the API default of `high`, and that is deliberate rather than an omission.

This covers every variant of "tune the effort per caller": a constant per analysis,
an effort field on the Setup page, dropping the interactive callers to `low`, or
raising the weekly report to `xhigh`.

## Why this is out of scope

**The knob has no number behind it.** Only the weekly report is metered (#60):
`reports.price()` prices its `_usage` and stores `{input, output, model, usd}` on
the report record. The other three attach `_usage` and render bare token counts,
and nothing converts them to a cost or keeps them — they are fire-and-forget, a
result returned to the browser plus an ops-log line. So there is no baseline to
sweep against, and no way to tell afterwards whether a change helped. Tuning a
setting whose effect you cannot measure produces a number somebody later trusts
because it looks deliberate.

**Four values is four things to keep true, for one key and one model.**
`toolloop.py` does carry an `EFFORT` constant, and it earned one: it drives a loop
with an iteration cap, where effort changes how many turns the loop takes and
therefore compounds. A single-shot analysis has no such multiplier. Copying the
pattern across because it exists would spread a tuning surface into a place whose
cost shape does not justify it.

**The direction that would matter most is the one that breaks things.** On
`claude-opus-5`, `max_tokens` caps thinking *and* response text together, and
`analyze_health` runs at `max_tokens=10000` — the tightest budget of the four
relative to its output:

```python
def analyze_health(data: dict) -> dict:
    user = ("Weekly homelab snapshot:\n"
            f"```json\n{json.dumps(data, indent=1)[:120000]}\n```")
    return ask_json(REPORT_SYSTEM_PROMPT, user, REPORT_SCHEMA, max_tokens=10000)
```

Raise that caller's effort and thinking can consume the budget, which arrives as
`stop_reason: max_tokens` → the "analysis was truncated" `ValueError` in
`_sdk_ask_json` → a *scheduled* report that raises retries three times at
half-hour intervals under the #27 logic. A mis-set constant on that one caller is a
billed, looping failure, which is the exact failure class this repo tests for. The
published guidance for `xhigh`/`max` is `max_tokens` ≥ 64k; 10k is not close.

**Nobody could say what was being optimised.** Latency and cost point opposite ways
for the interactive callers, and the question was never answered because neither
pressure is actually being felt: one owner, one lab, a report that runs 52 times a
year and is already metered if the figure is ever wanted.

## What was established while deciding this

Worth keeping, because it is the part that has to be re-checked rather than
re-derived:

- `effort` is GA — no beta header — and belongs **inside** `output_config`, not
  top-level. Omitting it is exactly equivalent to `high`.
- `claude-opus-5` supports all five levels (`low`, `medium`, `high`, `xhigh`,
  `max`). `low` and `medium` are strong on this model, so a sweep would not
  obviously have gone the direction the ticket assumed.
- Effort is not a lever for output *length*; it changes reasoning depth and token
  spend. Shortening a response is a prompting change.

## If this is reconsidered

State the target before touching the constant, because it decides the instrument:

- **Latency** (the plausible one — a human waits on the three button-press
  callers): the right instrument is a stopwatch, not a meter. Token counts do not
  answer it, so extending metering is not a prerequisite for this version.
- **Cost**: metering has to come first, and the open design question is *where the
  number goes* — the three interactive analyses persist nothing, so the ops-log
  entry each already writes is the natural home. Note this extension is **not
  itself rejected**; it was only ever a prerequisite for the sweep and lost its
  motivation when the sweep did. If per-analysis cost visibility is ever wanted for
  its own sake, that is a fresh ticket on its own merits.

Whichever it is: do not raise `analyze_health` above `high` without raising its
`max_tokens` in the same change.

## Prior requests

- #64 — "The four AI analyses all run at default effort, and only one of them is
  metered" (split out of #62; closed as out of scope during triage)
