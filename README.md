# AI Usage Tracker

AI Usage Tracker is a local and multi-machine observability tool for AI usage.
It collects token, cost, model, provider, workspace, API key, session, and
tool-call activity from supported telemetry sources such as OpenTelemetry JSON
and OpenRouter Broadcast traces.

It has two runtime roles:

- **Collector**: runs beside AI clients, receives OTEL/HTTP payloads on
  `127.0.0.1:4318`, stores local SQLite history, extracts usage rows, and can
  forward compact events to a server.
- **Aggregation server**: optional; runs once on a trusted host when you want
  shared multi-machine reporting or direct provider ingest. It accepts collector
  sync batches and optional provider webhooks, stores a server SQLite database,
  manages collector API tokens, and serves web reports.

The collector works on its own. You do not need an aggregation server for local
capture, local SQLite history, summaries, reports, samples, reindexing, cleanup,
or cost backfills. Add the aggregation server only when you want shared reports
across machines or direct provider ingest such as OpenRouter Broadcast.

## Architecture

```text
AI client OTEL -> local collector -> local SQLite
                          |
                          v
                  aggregation server -> server SQLite -> /reports and /tools

OpenRouter Broadcast ------------------^
```

The collector receives raw OTEL/HTTP JSON from local AI clients. It can keep raw
payload metadata for troubleshooting, but stores extracted usage and tool events
as normalized rows. When server forwarding is configured, the collector sends
only compact usage and tool-event batches to `POST /api/v1/usage-events`.

The aggregation server stores collector-synced events and OpenRouter Broadcast
events in one server database. Reports separate local collector identity from
provider-side source identity such as OpenRouter workspace, API key, and model
provider.

## Repository Layout

- `ai_usage_tracker.py`: top-level CLI entry point.
- `ai_usage_tracker/core.py`: shared receiver, parser, storage, reporting, and
  server implementation.
- `ai_usage_tracker/collector/`: local collector surface.
- `ai_usage_tracker/aggregation_server/`: central ingestion and reporting
  surface.
- `collector.example.toml`: collector configuration example.
- `server.example.toml`: aggregation server configuration example.
- `docker/`, `Dockerfile`, `docker-compose.yml`: containerized server setup.
- `deploy/`: deployment scripts and detailed operations notes for Linux,
  macOS, WSL2, Unraid, Docker, Cloudflare Access, and release images.

## Installation

Use the same source checkout for both roles. The project is stdlib Python and is
kept compatible with Python 3.9+.

```bash
git clone https://github.com/V5U2/ai-usage-tracker.git
cd ai-usage-tracker
python3 ai_usage_tracker.py client version
```

For managed installs, use the role-specific deployment guides:

- Collector: [deploy/collector/README.md](deploy/collector/README.md)
- Aggregation server: [deploy/aggregation-server/README.md](deploy/aggregation-server/README.md)

### 1. Install A Collector

Run this on each machine where an AI client emits telemetry:

```bash
python3 ai_usage_tracker.py serve --port 4318
```

The explicit collector command is equivalent:

```bash
python3 ai_usage_tracker.py client serve --port 4318
```

The collector binds to loopback by default. Keep it local unless you
intentionally want another host to send OTEL payloads to it:

```bash
python3 ai_usage_tracker.py client serve --host 0.0.0.0 --port 4318 --allow-remote
```

Start from `collector.example.toml` when you want persistent settings:

```bash
cp collector.example.toml collector.toml
python3 ai_usage_tracker.py --config collector.toml client serve --port 4318
```

The main collector config choices are:

- `client_name`: names this machine or ingestion process for server reports.
- `[collector].endpoint`: aggregation server URL for forwarding.
- `[collector].api_key`: collector token created in the server admin UI.
- `[collector].batch_size`: number of pending rows sent per sync batch.
- `[collector].timeout_seconds`: outbound sync timeout.
- `raw_payload_body`: stores raw inbound payload bodies when troubleshooting.
- `extracted_attributes`: stores extracted attributes as `redacted`, `full`, or
  `none`.
- `model`, `session_id`, `thread_id`: controls which dimensions are retained on
  usage rows.
- `max_body_bytes`: rejects oversized inbound payloads.

For autostart and update scripts:

- Linux systemd: [deploy/collector/README.md#linux-collector](deploy/collector/README.md#linux-collector)
- macOS launchd: [deploy/collector/README.md#macos-collector](deploy/collector/README.md#macos-collector)
- WSL2: use the Linux path when systemd is enabled.

### 2. Optional: Install The Aggregation Server

Run one server when multiple collectors should report into a shared view:

```bash
cp server.example.toml server.toml
python3 ai_usage_tracker.py --config server.toml server serve
```

By default the server binds to `127.0.0.1:8318`. Use `--allow-remote` only on a
trusted LAN or behind authentication:

```bash
python3 ai_usage_tracker.py --config server.toml server serve \
  --host 0.0.0.0 --port 8318 --allow-remote
```

Open the admin UI to create collector API tokens:

```text
http://127.0.0.1:8318/admin
```

New tokens are shown once. The server stores token hashes, not raw tokens. Add a
generated token to each collector:

```toml
client_name = "work-laptop"

[collector]
endpoint = "http://server-host:8318"
api_key = "ait_generated_token_from_admin_ui"
batch_size = 100
timeout_seconds = 10
```

Each collector keeps local reports and queues unsynced rows. If `client_name`,
`[collector].endpoint`, or `[collector].api_key` changes, historical usage
becomes pending for that new target and is sent on collector startup or the next
manual sync.

Server deployment options:

- Linux systemd: [deploy/aggregation-server/README.md#linux-systemd](deploy/aggregation-server/README.md#linux-systemd)
- Docker Compose: [deploy/aggregation-server/README.md#docker-compose](deploy/aggregation-server/README.md#docker-compose)
- Unraid: [deploy/aggregation-server/README.md#unraid](deploy/aggregation-server/README.md#unraid)
- Cloudflare Access: [deploy/aggregation-server/README.md#cloudflare-access](deploy/aggregation-server/README.md#cloudflare-access)

## Configure Telemetry Sources

The tracker is source-agnostic once it receives OTEL/HTTP JSON or normalized
server-side usage events. Configure each AI client to send telemetry to the
local collector, or configure provider webhooks to send supported server-side
events to the aggregation server.

### Codex OTEL

Add this to `~/.codex/config.toml`:

```toml
[otel]
environment = "local"
log_user_prompt = false
exporter = { otlp-http = {
  endpoint = "http://127.0.0.1:4318/v1/logs",
  protocol = "json"
}}
```

The important settings are `otlp-http`, a local `/v1/logs` endpoint, and
`json`. The receiver also stores non-JSON payloads raw, but token extraction
needs JSON. Codex batches telemetry asynchronously, so start a fresh Codex
session after changing this config and check the collector after the session
ends.

### Claude Code OTEL

Claude Code can use the same local collector when it exports OTLP/HTTP JSON.
For persistent local tracking, add the OTEL variables to
`~/.claude/settings.json` under the top-level `env` key:

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://127.0.0.1:4318/v1/metrics",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://127.0.0.1:4318/v1/logs"
  }
}
```

If `~/.claude/settings.json` already exists, merge these keys into its existing
`env` object instead of replacing the whole file. Restart Claude Code after
editing settings.

For beta Claude Code traces, add these keys to the same `env` object:

```json
{
  "env": {
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:4318/v1/traces"
  }
}
```

For a one-off shell session:

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_METRICS_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_LOGS_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://127.0.0.1:4318/v1/metrics
export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs
claude
```

The collector normalizes `claude_code.token.usage` metrics with `type` values
of `input`, `output`, `cacheRead`, and `cacheCreation`; it also stores
`claude_code.cost.usage` as USD cost and recognizes Claude Code tool result and
tool decision events.

Claude Code can emit the same API request through metrics, logs, and beta
traces. To avoid double counting, usage totals are taken from the metric
streams. `api_request` logs and `claude_code.llm_request` trace spans are
retained as raw payload context and tool/event telemetry, but are not inserted
as separate usage rows. If Claude Code retries the same OTLP payload with stable
trace/span identifiers, the collector treats duplicate usage or tool rows as
already accepted and still returns a successful response.

### Claude Desktop / Cowork OTEL

Claude Desktop's Cowork OpenTelemetry export is separate from Claude Code
telemetry. Anthropic documents it for Team and Enterprise plans, requires Claude
Desktop 1.1.4173 or later, and exposes it through `Organization settings >
Cowork`. Individual Pro or Max accounts do not currently have a documented way
to point ordinary Claude Desktop chat telemetry at a local collector.

When Cowork OTEL is available, configure it with the collector base URL and
HTTP/JSON:

```text
OTLP endpoint: http://127.0.0.1:4318
Protocol: HTTP/JSON
Headers: leave empty for a local collector
```

Claude Desktop should append the OTLP signal paths itself, so use the base URL
rather than `/v1/logs`, `/v1/metrics`, or `/v1/traces`. Do not point Claude
Desktop at the aggregation server's `/api/v1/usage-events` endpoint; that route
accepts this app's compact collector sync format, not raw OTLP.

Desktop/Cowork telemetry can include prompt text, tool parameters, file paths,
and user/account attributes. Keep the collector local or put an authenticated
OTEL collector in front of it before exposing telemetry beyond the machine.

### OpenRouter Broadcast

The aggregation server can accept OpenRouter Broadcast OTLP/HTTP JSON traces at
`POST /v1/traces`. This is separate from collector sync: collectors keep using
`/api/v1/usage-events`, and Broadcast rows are stored under the reserved
synthetic client name `openrouter-broadcast`.

Enable the machine-ingest path in `server.toml`:

```toml
[openrouter_broadcast]
enabled = true
api_key = "change-me"
# Optional exact-match check for a custom header that reaches the origin.
# Do not use this for Cloudflare Access service-token headers; Cloudflare
# consumes those at the edge and does not pass them through reliably.
# required_header_name = "X-OpenRouter-Broadcast-Secret"
# required_header_value = "change-me-too"
retain_payload_body = true
```

Configure OpenRouter Broadcast with an OpenTelemetry Collector or Webhook
destination:

```text
https://usage.example.com/v1/traces
```

Use custom headers:

```json
{
  "Authorization": "Bearer change-me",
  "CF-Access-Client-Id": "your.cloudflare.access.client.id",
  "CF-Access-Client-Secret": "your.cloudflare.access.client.secret"
}
```

The `Authorization` header is validated by this app. Cloudflare Access headers
only get the request through Cloudflare; do not configure app-level validation
against `CF-Access-Client-Id` or `CF-Access-Client-Secret` because Cloudflare
does not pass those service-token headers through to the origin reliably.

For privacy, enable OpenRouter Broadcast Privacy Mode when you only need usage,
cost, model, provider, timing, and metadata. The server never stores raw bearer
secrets as report dimensions; OpenRouter API-key reporting uses non-secret
labels or IDs from the trace metadata when available. Secret-shaped attributes
in `attributes_json` still follow the redaction rules.

OpenRouter-focused report groupings:

```text
/reports?group_by=workspace&source_kind=openrouter_broadcast
/reports?group_by=api-key&source_kind=openrouter_broadcast
/reports?group_by=model-provider&source_kind=openrouter_broadcast
```

Retained Broadcast payloads can be replayed after parser changes:

```bash
python3 ai_usage_tracker.py --config server.toml \
  server replay-broadcast --replay-status ingested
```

Useful selectors include `--payload-id`, `--since`, `--until`,
`--replay-status`, and `--limit`. Replay reuses the live Broadcast parser and is
idempotent against the derived OpenRouter event id.

## Usage

### Collector Commands

Show a local token summary:

```bash
python3 ai_usage_tracker.py summary
```

Run usage reports:

```bash
python3 ai_usage_tracker.py report --group-by day-model
python3 ai_usage_tracker.py report --group-by session --since 2026-05-01
python3 ai_usage_tracker.py report --group-by total --format csv
```

Report captured tool calls:

```bash
python3 ai_usage_tracker.py tools-report
python3 ai_usage_tracker.py tools-report --group-by day-tool
python3 ai_usage_tracker.py tools-report --group-by event --event-name ""
```

Inspect extracted or raw telemetry:

```bash
python3 ai_usage_tracker.py samples --limit 5
python3 ai_usage_tracker.py stats
python3 ai_usage_tracker.py raw --limit 20
python3 ai_usage_tracker.py dump-raw 1
```

Sync with the aggregation server:

```bash
python3 ai_usage_tracker.py client sync
python3 ai_usage_tracker.py client sync --all
python3 ai_usage_tracker.py client sync-status
python3 ai_usage_tracker.py client sync-status --errors 5
```

`client sync --all` forces a full resend so the aggregation server can refresh
duplicate historical rows after cost estimation, parser, or reporting changes.

Rebuild or clean local extracted data:

```bash
python3 ai_usage_tracker.py --config collector.toml reindex
python3 ai_usage_tracker.py --config collector.toml cleanup
python3 ai_usage_tracker.py --config collector.toml backfill-costs
```

Use `reindex` after changing storage settings or parser behavior. Use
`cleanup` to apply retention settings to existing rows, such as clearing stored
raw payload bodies after setting `raw_payload_body = false`. Use
`backfill-costs` after enabling pricing estimates for data that was already
collected.

Raw payload bodies are not stored by default. If temporarily enabled, raw OTEL
payloads may include account metadata or prompt-related telemetry depending on
the AI client or provider. Do not commit `ai_usage.sqlite*` files or share
`dump-raw` output publicly. Extracted usage samples redact common credential and
account fields.

### Server Usage

Open web pages:

```text
http://127.0.0.1:8318/admin
http://127.0.0.1:8318/reports
http://127.0.0.1:8318/tools
```

The admin UI creates, renames, revokes, and deletes revoked collector tokens.
In the web UI, "collector" means the machine or ingestion process that reports
usage to the aggregation server. Provider-side sources such as OpenRouter
workspaces, projects, or API keys are reported separately from collector
identity.

The server accepts compact usage and tool-event batches from collectors at
`POST /api/v1/usage-events`. Report APIs are available at:

```text
GET /api/v1/reports/usage
GET /api/v1/reports/tools
GET /api/v1/stats
```

Use `Authorization: Bearer <aggregation_server.admin_api_key>` for report APIs.
Open `/reports` to view token totals grouped by provider, provider-side source,
and model by default, with filters for date, collector, model, session, source
kind, workspace, API key, model provider, grouping, and row limit. Open `/tools`
to view captured tool calls grouped by collector and tool. The web UI keeps UTC
timestamps in page data and displays them in the browser's local time zone.

Backfill server-side estimated costs:

```bash
python3 ai_usage_tracker.py --config server.toml \
  server backfill-costs --server-db ai_usage_server.sqlite
```

For Docker deployments:

```bash
docker exec ai-usage-tracker python ai_usage_tracker.py \
  --config /data/server.toml \
  server backfill-costs \
  --server-db /data/ai_usage_server.sqlite
```

## Reports And Costs

`report` supports these groupings:

- `total`
- `provider`
- `provider-source`
- `provider-source-model`
- `day`
- `model`
- `session`
- `day-model`
- `day-session`

Server report APIs and `/reports` also support `client`, `client-model`,
`day-client`, `day-model-client`, `source`, `workspace`, `api-key`, and
`model-provider`. The `client*` group names are kept for API compatibility; the
web UI labels them as collectors.

`tools-report` supports `total`, `day`, `tool`, `session`, `event`, `day-tool`,
and `day-session`. By default it reports all recognized tool events; pass
`--event-name <event>` to focus on one event type.

Filters use UTC timestamps. A plain date such as `2026-05-01` is accepted for
`--since` and `--until`. CLI and API output keep server UTC timestamps; web
views display those UTC values in the browser's local time zone. Output formats
are `table`, `csv`, and `json`.

The default `/reports` view is `provider-source-model`. In that view, `provider`
is the system where usage came from, such as Codex, Claude Code, or OpenRouter.
`source` is the identity inside that provider: for collector-synced local usage
it is normally the collector or workspace, while for OpenRouter it is the
workspace first, then API key label if no workspace is available.

For known OpenAI and Claude API model names, the `model` column uses the matched
pricing family label, such as `claude-sonnet-4.5`, so provider-prefixed and
dated aliases group together. Raw model names remain stored unchanged.

Some local telemetry sources emit token counts but not cost. You can opt in to
estimated USD costs for known OpenAI API model names and Claude API model names:

```toml
[pricing]
estimate_openai_api_costs = true
estimate_claude_api_costs = true
include_reasoning_tokens_as_output = true
# Reporting-only. Stored OpenRouter rows still keep the provider-reported
# credits unit.
report_openrouter_credits_as_usd = false
```

Provider-reported costs always take precedence. Estimates use the built-in
OpenAI and Claude API rates current when this release was built, so review
`ai_usage_tracker/core.py` when provider pricing changes. OpenAI estimates
charge cached input tokens at cached-input rates, remaining input at input
rates, and output plus reasoning tokens at output rates. Claude estimates charge
cache read tokens at cache-hit rates, cache creation tokens at 5-minute
cache-write rates, remaining input at input rates, and output at output rates.

If your OpenRouter account treats one credit as one USD, set
`report_openrouter_credits_as_usd = true` on the aggregation server. This only
normalizes OpenRouter Broadcast costs while rendering reports and dashboard
summaries; it does not rewrite stored rows or change provider-reported units in
the raw data.

Report cost totals are grouped by the visible report dimensions. Rows with no
cost or zero cost do not split otherwise matching groups, and a group only shows
`mixed` when it contains nonzero costs in more than one unit. The web dashboard
summary renders mixed-unit totals as a per-unit breakdown, such as
`12.34 USD + 0.05 credits`, because those values are not additive.

## Limits

This uses whatever each source emits through OTEL or provider webhooks. If a
source does not emit recognized token attributes, the receiver still stores raw
telemetry payload metadata, but the summary will show no extracted token events.
In that case, inspect `samples` and retained raw SQLite payloads, then add the
emitted attribute names to the parser in `ai_usage_tracker/core.py`.

The parser already recognizes common OpenTelemetry and LLM usage names such as:

- `input_tokens`, `prompt_tokens`, `gen_ai.usage.input_tokens`
- `output_tokens`, `completion_tokens`, `gen_ai.usage.output_tokens`
- `total_tokens`, `gen_ai.usage.total_tokens`
- `claude_code.token.usage` metrics with `type` set to `input`, `output`,
  `cacheRead`, or `cacheCreation`
- `claude_code.cost.usage`
- cached and reasoning token variants

When `[pricing].estimate_openai_api_costs = true` or
`[pricing].estimate_claude_api_costs = true`, known OpenAI API and Claude model
names without provider-reported costs also get estimated USD cost values from
embedded API pricing tables.

Older configs using `[server]` for collector forwarding and `[central_server]`
for aggregation-server settings are still accepted. Prefer `[collector]` and
`[aggregation_server]` for new configs.

## License

MIT. See [LICENSE](LICENSE).
