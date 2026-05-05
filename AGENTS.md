# AGENTS.md

Project-level instructions for coding agents working in this repository.

## Repository shape

This is a stdlib Python project for collecting Codex OTEL usage locally and
optionally forwarding compact usage events to a central server. The central
server can run directly with Python or through Docker Compose.

Key entry points:

- `codex_usage_observer.py`: compatibility CLI entry point
- `ai_usage_tracker/core.py`: receiver, server, reporting, sync, and storage logic
- `ai_usage_tracker/collector/`: local OTLP receiver, collector persistence, and forwarding surface
- `ai_usage_tracker/aggregation_server/`: central ingestion, token admin, and reporting surface
- `test_codex_usage_observer.py`: unittest coverage
- `Dockerfile`, `docker-compose.yml`, `docker/server.toml`: local central-server container setup

## Required close-out checks

1. Keep documentation in sync with behavior, setup, operations, and release-impact changes.
2. Run the relevant tests and checks before considering a task complete.
3. Rebuild/restart affected local services when runtime behavior changes.

## Documentation rule

- If a change affects receiver/server behavior, CLI flags, config keys, collector forwarding auth, Docker setup, launchd setup, sync semantics, reporting, storage, or operational workflows, update the relevant docs in the same task.
- At minimum, review whether changes are needed in:
  - `README.md`
  - `codex_usage_observer.example.toml`
  - `docker/server.toml`
  - `deploy/unraid/ai-usage-tracker.xml`
  - launchd or Docker instructions in the README
- For release-facing changes, also review whether updates are needed in changelog, release notes, migration notes, or version references if those files are later added.
- Do not leave user-facing copy or docs knowingly inconsistent with implementation.
- If a change is not user-facing, avoid unnecessary changelog noise unless this repo later adopts an every-change changelog policy.

## Test rule

- Run the narrowest meaningful verification first, then broader checks when warranted.
- For Python code changes, run:

```bash
python3 -m unittest -v
```

- For Docker server changes, rebuild and restart the affected service, then verify the relevant endpoint:

```bash
docker compose up -d --build
docker compose ps
```

- Useful endpoint checks:

```bash
curl -i http://127.0.0.1:8318/
curl -fsSL http://127.0.0.1:8318/reports
curl -fsSL http://127.0.0.1:8318/admin
```

- If the active launchd receiver copy under `~/Library/Application Support/ai-usage-tracker` must pick up code changes, update that service copy and restart:

```bash
launchctl kickstart -k "gui/$(id -u)/com.example.ai-usage-tracker.receiver"
```

- If a check cannot be run, state that explicitly in the final handoff and explain why.

## Working rule

- Prefer finishing implementation, documentation updates, and verification in the same change.
- Do not treat docs or tests as optional follow-up work.
- Keep changes small and reversible; avoid new dependencies unless explicitly requested.
- This project currently works with the system Python 3.9, so preserve compatibility unless the user explicitly chooses to raise the minimum Python version.
- For Docker or long-running receiver/server behavior, rebuild or restart only the affected service when required by the change, then rerun the relevant checks.
- Default to a PR-first workflow for non-trivial changes.
- Direct commits to `main` are the exception and should be limited to narrow, low-risk updates such as typo-only docs edits, comment-only cleanup, or tiny obvious config fixes.

## Release automation rule

- This repo does not currently declare release automation.
- Do not bump versions, tag releases, publish images, or publish artifacts unless explicitly asked.
- If release automation, changelog, or release-note files are later added, use conventional PR or squash-merge titles where appropriate:
  - `feat: ...`
  - `fix: ...`
  - `docs: ...`
  - `chore: ...`
- Use `!` or `BREAKING CHANGE:` only for intentional major-version changes.
- Include release-impact context in PR summaries for user-facing changes: what changed, who is affected, and any migration or rollback notes.
