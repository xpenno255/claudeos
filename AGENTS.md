

## Migration to Docker VM (2026-11-10)

This development checkout now lives on the Proxmox Docker VM. Original paths,
Git history and uncommitted work were retained. Use this checkout for development.
The old Ubuntu desktop copy is retained as a migration backup.

Full original Orca conversation exports, provider transcripts, model selections
and dependency snapshots are preserved in:
`/home/xpenno255/backups/project-migration/remaining-20261110T220633Z`

Read the project-specific morning checklist in that directory before testing
against real services or hardware. The migration did not deploy code, start
project applications, change Home Assistant state, or run scheduled jobs.

The existing live claudeos service uses `/home/xpenno255/docker-projects/ops/compose.yaml` with data at `/home/xpenno255/docker-projects/ops/claudeos/data`. Development checks must use temporary data and mocked connectors. Do not start the poller, scheduled reports, model calls or infrastructure actions as a migration check.
