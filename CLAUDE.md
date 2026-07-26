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
expensive*. Two modules clear it.

- `app/labissues.py` (with `app/triagelog.py`) — takes its GitHub caller, its
  analysis run and its tracker writes as arguments, so re-triage loops and
  budget overruns are tested with no network, no Anthropic call and no
  credentials.
- `app/store.py` — one invariant only: saving never silently loses a secret
  (#39 destroyed a real credential during an unrelated edit and said nothing).

Everything else has no tests and this is **not** a request to backfill them; add
a seam only where a module earns one, and say in the test file why it did.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (`xpenno255/claudeos`) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
