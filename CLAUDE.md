# ClaudeOS — agent notes

Homelab mission-control web app: Python-stdlib HTTP server (`python3 server.py`,
port 8321) + no-build ES-module frontend in `public/`. No Node/npm in this
environment. See `README.md` for architecture and the JSON API surface.

## Tests

```bash
python3 -m unittest discover -s tests
```

Standard library only — no pytest, no dev dependency. Coverage is **deliberately
narrow**, and the bar is one thing: failure modes that are *silent and
expensive*. Eight modules clear it.

- `app/labissues.py` (with `app/triagelog.py`) — takes its GitHub caller, its
  analysis run and its tracker writes as arguments, so re-triage loops and
  budget overruns are tested with no network, no Anthropic call and no
  credentials.
- `app/store.py` — one invariant only: saving never silently loses a secret
  (#39 destroyed a real credential during an unrelated edit and said nothing).
- `app/reports.py` — the weekly schedule, and the meter beside it. A failed
  scheduled report used to re-attempt every five minutes for the rest of the
  week, and the failure that loops is the one that gets billed (#27, ~$870 worst
  case). `generate()` takes its snapshot, its analysis call and its clock as
  arguments so the expensive paths are tested with none of them. The meter earns
  its own class for a different reason (#60): a wrong *rate* is invisible, and
  `toolloop._PRICES` is Sonnet's, sits one import away, applies to the same
  `_usage` shape, and would under-report the app's priciest call by an order of
  magnitude while looking plausible. One test exists only to assert the two
  constants differ, because every other test passes if they don't.
- `app/notify.py` — the zero-channel path only. With nothing configured, every
  alert the app raised was discarded and *nothing recorded that it happened*
  (#41), so a failing disk's `urgent` warning spent its whole window in silence.
  The gap is recorded before any sender is reached, so this needs no network.
- `app/httpclient.py` — URL scrubbing only. Telegram carries its bot token in
  the URL path, so every error built from that URL leaked it to the browser, to
  `data/opslog.jsonl` and — via `recent_warnings` — to the Anthropic API (#45).
  `urlopen` is substituted, so this needs no network.
- `app/offhours.py` (with the poller branch that reads it) — the boundary only.
  The feature exists to create silence, so what is tested is where the silence
  stops: a NAS still unreachable once its window and grace have passed has not
  gone to sleep, it has failed to wake, and must alert like any other outage.
  The window arithmetic is tested beside it because an overnight window crosses
  midnight and an off-by-one there just moves the silence, looking like nothing.

- `app/connectors/unifi.py` — event *identity* only, not the connector. The
  backups argument in a different place: everything else the report says is
  checkable against something, but an attribution is not. UniFi phrases one
  device two ways — `{SRC_CLIENT}` keyed by MAC with no address, `{SRC_IP}` keyed
  by address with no name — so one NAS arrived as two offending hosts and a
  `serious` finding named a third machine entirely, fluently and with real
  numbers (#59). What is tested is the join and the two places it gives up: an
  unresolvable host must say so, and an external address must stay bare. The
  identity map is an argument to `_render_msg`, so the render tests substitute
  nothing; the two that reach the controller stub `_call` with recorded payloads.

- `app/backups.py` — status evaluation with the clock injected, persistence across
  a restart, anomaly baselines on thin history, and four more that review turned
  up: shell-shaped failure payloads, token exposure, discovery outage vs absence,
  and the alert latch under two concurrent sweepers (#50). Every other surface
  measures reachability, which announces itself; a backup fails by *not
  happening*, so a job wrongly showing `ok` reports safety that does not exist.
  Not hypothetical: probing the cluster before the build found 25 consecutive
  nightly vzdump failures nobody knew about. `evaluate(jobs, now)` takes its
  clock as an argument, so no test waits and none touches the network.

Everything else has no tests and this is **not** a request to backfill them; add
a seam only where a module earns one, and say in the test file why it did.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (`xpenno255/claudeos`) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
