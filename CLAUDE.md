# ClaudeOS — agent notes

Homelab mission-control web app: Python-stdlib HTTP server (`python3 server.py`,
port 8321) + no-build ES-module frontend in `public/`. No Node/npm in this
environment. See `README.md` for architecture and the JSON API surface.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (`xpenno255/claudeos`) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
