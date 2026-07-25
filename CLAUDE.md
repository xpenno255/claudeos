# ClaudeOS — agent notes

Homelab mission-control web app: Python-stdlib HTTP server (`python3 server.py`,
port 8321) + no-build ES-module frontend in `public/`. No Node/npm in this
environment. See `README.md` for architecture and the JSON API surface.

## Tests

```bash
python3 -m unittest discover -s tests
```

Standard library only — no pytest, no dev dependency. Coverage is **deliberately
narrow**: `app/labissues.py` is the one module with a test seam, because it takes
its GitHub caller as an argument and its failure modes (re-triage loops, budget
overruns) are silent and expensive. The rest of the app has no tests and this is
not a request to backfill them; add a seam only where a module earns one.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (`xpenno255/claudeos`) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
