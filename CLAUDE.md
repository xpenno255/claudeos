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
expensive*. Four modules clear it.

- `app/labissues.py` (with `app/triagelog.py`) — takes its GitHub caller, its
  analysis run and its tracker writes as arguments, so re-triage loops and
  budget overruns are tested with no network, no Anthropic call and no
  credentials.
- `app/store.py` — one invariant only: saving never silently loses a secret
  (#39 destroyed a real credential during an unrelated edit and said nothing).
- `app/reports.py` — the weekly schedule only. A failed scheduled report used to
  re-attempt every five minutes for the rest of the week, and the failure that
  loops is the one that gets billed (#27, ~$870 worst case). `generate()` takes
  its snapshot, its analysis call and its clock as arguments so the expensive
  paths are tested with none of them.
- `app/notify.py` — the zero-channel path only. With nothing configured, every
  alert the app raised was discarded and *nothing recorded that it happened*
  (#41), so a failing disk's `urgent` warning spent its whole window in silence.
  The gap is recorded before any sender is reached, so this needs no network.

Everything else has no tests and this is **not** a request to backfill them; add
a seam only where a module earns one, and say in the test file why it did.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (`xpenno255/claudeos`) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
