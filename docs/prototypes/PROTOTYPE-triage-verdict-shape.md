# PROTOTYPE — triage verdict shape

**Throwaway.** Answers one question for
[Triage verdict shape](https://github.com/xpenno255/claudeos/issues/20): what does the
triage comment ClaudeOS posts back onto a lab issue actually look like?

Neither branch of the `/prototype` skill fits cleanly — this is a *document format*, not a
state machine or a UI route. So the artifact is what the ticket itself asked for: the same
verdict drafted three ways against one real issue, to be reacted to rather than argued about.

## The material is real

Drafted against [homelab#1](https://github.com/xpenno255/homelab/issues/1) —
*"Intermittent zigbee update issues with utility tumble dryer plug"* — using live reads from
the configured Home Assistant on 2026-07-25, via the same connector functions
`app/tools.py` exposes to the agent (`zha_devices`, `error_log`, `updates`).

Facts every draft below is built from:

| Fact | Value | Source |
| --- | --- | --- |
| Device | `Utility Tumble Dryer Plug`, SONOFF S60ZBTPG, Router, mains | `zha_devices` |
| Signal | LQI **116**, RSSI −70, `available: true` | `zha_devices` |
| Cohort | 28 other S60ZBTPG plugs on the mesh | `zha_devices` |
| Mesh LQI | 83 mains routers — min 88, p25 112, **median 132**, p75 160, max 180 | `zha_devices` |
| Rank | dryer plug sits at the **36th percentile** — below median, not an outlier | derived |
| Pending update | `Utility Tumble Dryer Plug Firmware` → `available: true` (6 of 255 update entities are) | `updates` |
| Versions | `installed_version: None`, `latest_version: None` | `updates` |
| OTA log | 2 × `zha ... Received unknown event: DeviceFirmwareInfoUpdatedEvent` (×4 @ 13:39, ×18 @ 13:23 UTC) | `error_log` |
| Re-interview | `zigpy.device: [0xe09f] / [0x037e] Re-interview failed` — **neither is this device** (its nwk is 64839 = 0xFD47) | `error_log` |
| Log depth | error log returned **50 lines total** | `error_log` |

### Four honesty cases this real issue happens to produce

These are why drafting against real data mattered — none would have been invented:

1. **The obvious hypothesis is unsupported.** "Weak signal" is the natural guess, and the data
   doesn't back it: the plug is mid-pack, and same-model peers at comparable LQI update fine.
   The format must be able to say *the likely explanation is wrong*.
2. **Version fields are `None`.** The `updates` tool succeeded and returned nothing useful —
   `chat.py`'s system prompt is explicit that `no_data` is **not** the same as healthy.
3. **Adjacent evidence invites misattribution.** Two `Re-interview failed` warnings sit right
   there in the log and belong to *other* devices. A format must make evidence traceable enough
   that a wrong attribution is visible to a reader.
4. **Readings are noisy and point-in-time.** LQI read 120, then 116 minutes later. A verdict that
   states a bare number implies more precision than exists.

---

## Variant A — prose-first

> ## Triage
>
> **Signal strength is probably not your problem.** The plug reports LQI 116 / RSSI −70, which
> puts it at the 36th percentile of the 83 mains-powered routers on your mesh — below the median
> of 132, but well inside normal range and far from the weakest. Twenty-eight other S60ZBTPG
> plugs are on the mesh, several at comparable signal, and you say those update fine. That
> undercuts the obvious explanation rather than supporting it.
>
> What I can see: the pending update is real (`Utility Tumble Dryer Plug Firmware` reports
> `available: true` — one of only 6 across 255 update entities). ZHA logged
> `DeviceFirmwareInfoUpdatedEvent` as an *unknown event* twice today, ×4 at 13:39 and ×18 at
> 13:23 UTC — consistent with OTA traffic that ZHA isn't fully handling.
>
> What I can't see: both `installed_version` and `latest_version` come back empty, so I cannot
> tell you which firmware you're on or what you'd be moving to. The error log returned only 50
> lines, so anything older than a few hours is invisible to me. There are two
> `Re-interview failed` warnings in that log — they're for `0xe09f` and `0x037e`, and this plug
> is `0xFD47`, so they are **not** this device.
>
> **Suggested next step:** rather than retrying the update, capture what's actually happening.
> Enable debug logging on the ZHA OTA path and retry once:
>
> ```yaml
> # configuration.yaml — remove when done
> logger:
>   logs:
>     zigpy.ota: debug
>     zha.application.ota: debug
> ```
>
> Then retry the update and check the log for the block/chunk sequence. If it stalls at a
> consistent offset, that points at the OTA provider or the image rather than the mesh.
>
> *Not confident this is diagnosed. The strongest thing I can say is what it isn't.*

## Variant B — structured-first

> ## Triage
>
> | | |
> | --- | --- |
> | **Verdict** | Not diagnosed — leading hypothesis refuted |
> | **Confidence** | Low |
> | **Severity** | Minor (cosmetic/annoyance; device is online and functioning) |
> | **System** | Home Assistant / ZHA |
> | **Device** | `Utility Tumble Dryer Plug` · SONOFF S60ZBTPG · nwk `0xFD47` |
>
> **Findings**
>
> | # | Finding | Evidence | Status |
> | --- | --- | --- | --- |
> | 1 | Signal is adequate; weak-mesh hypothesis not supported | LQI 116, RSSI −70; 36th pct of 83 routers (median 132) | `success` |
> | 2 | Pending firmware update is genuine | `update.*` → `available: true`, 6 of 255 | `success` |
> | 3 | ZHA is logging OTA events it doesn't recognise | `DeviceFirmwareInfoUpdatedEvent` ×4 @13:39, ×18 @13:23 UTC | `success` |
> | 4 | Firmware versions unknown | `installed_version: None`, `latest_version: None` | `no_data` |
> | 5 | Log history shallow — only 50 lines available | `error_log` | `truncated` |
> | 6 | `Re-interview failed` warnings present but for other devices (`0xe09f`, `0x037e`) | `error_log` | `excluded` |
>
> **Remediation** — diagnostic, not a fix:
>
> ```yaml
> logger:
>   logs:
>     zigpy.ota: debug
>     zha.application.ota: debug
> ```
>
> Retry the update, then inspect for a consistent stall offset.

## Variant C — hybrid (prose + hidden machine block)

> Variant A's prose verbatim, followed by:
>
> ```text
> <!-- claudeos-triage
> {"v":1,"issue":1,"run":"2026-07-25T15:41:02Z","model":"claude-sonnet-5",
>  "verdict":"not_diagnosed","confidence":"low","severity":"minor",
>  "system":"homeassistant","subject":"Utility Tumble Dryer Plug",
>  "refuted":["weak_mesh_signal"],
>  "evidence":[{"tool":"ha_zha_devices","status":"success"},
>              {"tool":"ha_updates","status":"no_data","note":"versions null"},
>              {"tool":"ha_error_log","status":"truncated","note":"50 lines"}],
>  "remediation":{"kind":"diagnostic","executable":false},
>  "cost":{"input":18422,"output":1140,"usd":0.0482}}
> --> 
> ```
>
> GitHub renders HTML comments invisibly, so a human sees only the prose. The app parses the
> block for dashboard fields, and its presence doubles as the **idempotency marker**
> [Which issues get triaged](https://github.com/xpenno255/claudeos/issues/19) needs — which
> matters because a fine-grained PAT posts as its human owner, so author identity cannot
> distinguish ClaudeOS's comments from yours.

---

## What the drafts actually surfaced

- **The verdict field wants three values, not two.** `diagnosed` / `not_diagnosed` misses the case
  this real issue landed in: *the leading hypothesis was actively refuted*. That is genuinely
  useful output and neither label captures it.
- **Per-finding status beats a per-comment status.** One triage run mixed `success`, `no_data`, and
  `truncated` across six findings. A single confidence figure at the top would have hidden that.
- **Remediation is not always a fix.** The honest next step here is a *diagnostic* — collect better
  evidence — not a repair. A format assuming "the fix" will manufacture one.
- **Excluded evidence deserves a row.** Naming the two `Re-interview failed` warnings and saying
  *these are not this device* is more valuable than silently omitting them, because a human
  scanning the same log will otherwise draw the wrong conclusion.
- **Structured-first reads like a report, prose-first reads like a colleague.** On a one-device
  issue the table felt bureaucratic; the prose carried the reasoning better. That may invert for a
  multi-system issue.

---

## VERDICT — what this prototype settled

**Variant C (hybrid) wins.** Prose comment for humans, followed by a
`<!-- claudeos-triage {...} -->` HTML comment that GitHub renders invisibly. One artifact serves
three needs: a readable comment, dashboard fields, and the idempotency marker
[Which issues get triaged](https://github.com/xpenno255/claudeos/issues/19) requires — necessary
because a fine-grained PAT posts as its human owner, so author identity cannot distinguish
ClaudeOS's comments from the human's.

**Verdict vocabulary — four values, plus a refutation list:**

| `verdict` | Means | Why it is its own value |
| --- | --- | --- |
| `diagnosed` | Cause identified | — |
| `refuted` | Leading hypothesis ruled out, cause still open | The most useful real outcome; `not_diagnosed` misrepresents it as failure |
| `inconclusive` | Could not tell | Distinct from refuted: nothing was ruled out either |
| `no_fault_found` | Looked, found nothing wrong | Must not be conflated with *untriaged* on the dashboard (#23) |

`refuted[]` carries the specific hypotheses ruled out, independent of the verdict value.
This issue resolves to `verdict: "refuted"`, `refuted: ["weak_mesh_signal"]`.

**Also settled by the drafts:**

- **Status is per-finding, not per-comment** — `success` | `no_data` | `truncated` | `excluded`,
  mirroring `tools.envelope()` and adding `truncated`/`excluded`. One run mixed three of them.
- **Remediation carries a `kind`** — `fix` | `diagnostic` | `none`. The honest output here was a
  diagnostic (enable `zigpy.ota` debug logging and retry), not a repair. A schema assuming "the
  fix" manufactures one.
- **Excluded evidence gets a row**, with the reason. Naming the two `Re-interview failed` warnings
  as belonging to other devices is worth more than omitting them.

**Left to the spec, deliberately:** exact JSON field names and the full schema. This prototype
settled the *shape* and the *vocabulary*; `/to-spec` can name fields.
