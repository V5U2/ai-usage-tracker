# AI Usage Tracker

This is a local and multi-machine observability setup for AI usage. It tracks
token, cost, model, provider, workspace, and tool-call activity from supported
sources such as Codex OTEL telemetry and OpenRouter Broadcast traces. It has two
runtime roles:

- **Local collectors** run beside apps that emit OTEL/HTTP telemetry. A
  collector receives telemetry on `127.0.0.1:4318`, stores raw payload metadata
  and extracted usage rows in a local SQLite database, and can forward compact
  usage/tool events to an aggregation server.
- **The aggregation server** runs once on a trusted host. It accepts compact
  batches from collectors plus optional provider webhooks such as OpenRouter
  Broadcast, stores usage in a server SQLite database, manages collector API
  tokens, and serves web reports.

The collector is still useful without an aggregation server: local summaries,
raw payload inspection, reindexing, and cleanup all work from the local SQLite
database. Add an aggregation server when you want a shared view across multiple
machines.

Typical flow:

```text
AI telemetry source -> local collector -> local SQLite
                              |
                              v
                      aggregation server -> server SQLite -> /reports and /tools

OpenRouter Broadcast --------------------^
```

## Repository layout

- `ai_usage_tracker/collector/`: local OTLP receiver/collector surface,
  including local persistence and forwarding to an aggregation server.
- `ai_usage_tracker/aggregation_server/`: central aggregation server surface,
  including client tokens, ingestion APIs, and web reports.
- `ai_usage_tracker/core.py`: shared implementation used by both components
  and the compatibility CLI.
- `codex_usage_observer.py`: top-level compatibility CLI entry point. The file
  name is retained for existing installs even though the project now tracks
  broader AI usage.
- `docker/`, `Dockerfile`, `docker-compose.yml`: containerized aggregation
  server setup.
- `deploy/`: deployment scripts and templates for Unraid aggregation servers
  and macOS, Linux, or WSL collectors.

## 1. Run a local collector

Run this on every machine where you want to capture local AI telemetry. The
collector binds to loopback by default and should generally stay local-only
because OTEL payloads can include sensitive metadata.

```bash
python3 codex_usage_observer.py serve --port 4318
```

The explicit client command is equivalent:

```bash
python3 codex_usage_observer.py client serve --port 4318
```

Leave that process running while you use a configured AI client. Use
`--allow-remote` only on a trusted network and only when you intentionally want
a collector to receive OTEL from another host.

### WSL/Linux autostart

For a script-driven install, use
`deploy/collector/install-linux-systemd.sh`. It copies the collector into
`~/.local/share/ai-usage-tracker`, writes a config, installs a user systemd
service, and starts it:

```bash
AGGREGATION_ENDPOINT=http://server-host:8318 \
COLLECTOR_API_KEY=ait_generated_token_from_admin_ui \
CLIENT_NAME="$(hostname)" \
deploy/collector/install-linux-systemd.sh
```

For updates, use the update-only script. It preserves the existing config and
credentials, copies code, restarts the service, and prints sync progress:

```bash
deploy/collector/update-linux-systemd.sh
deploy/collector/update-remote-linux-systemd.sh linux-host
```

Tagged releases also include a collector tarball,
`ai-usage-tracker-collector-<tag>.tar.gz`, with the collector code and deploy
scripts. Extract it on a client and run the matching update script to upgrade
without rewriting `codex_usage_observer.toml`.

On WSL with systemd enabled, run the receiver as a user service so it starts
automatically with the WSL user session:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/ai-usage-receiver.service <<'EOF'
[Unit]
Description=AI usage tracker local receiver
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/your-user/ai-usage-tracker
ExecStart=/usr/bin/python3 /home/your-user/ai-usage-tracker/codex_usage_observer.py --config /home/your-user/ai-usage-tracker/codex_usage_observer.toml client serve --host 127.0.0.1 --port 4318
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now ai-usage-receiver.service
```

Useful service commands:

```bash
systemctl --user status ai-usage-receiver.service --no-pager
journalctl --user -u ai-usage-receiver.service -f
systemctl --user restart ai-usage-receiver.service
systemctl --user disable --now ai-usage-receiver.service
```

Check that it is listening:

```bash
ss -ltnp | rg ':4318'
```

Optional: use a config file to control what gets persisted:

```bash
python3 codex_usage_observer.py --config codex_usage_observer.toml serve --port 4318
```

Start from `codex_usage_observer.example.toml`. The main collector choices are:

- `client_name`: names this machine/client for later aggregation.
- `[collector]`: optional aggregation-server endpoint and client API key for forwarding.
- `raw_payload_body`: keep only payload metadata by default. Set true only when
  troubleshooting raw OTEL payload parsing.
- `extracted_attributes`: store extracted attributes as `redacted`, `full`, or `none`.
- `model`, `session_id`, `thread_id`: choose whether these dimensions are stored on usage rows.
- `max_body_bytes`: reject oversized inbound payloads.

The `[aggregation_server]` section is only used when this same checkout is also
started with `server serve`.

### macOS auto-start with launchd

For a script-driven install, use `deploy/collector/install-macos-launchd.sh`.
It copies the collector into `~/Library/Application Support/ai-usage-tracker`,
writes a config, installs a LaunchAgent, and starts it:

```bash
AGGREGATION_ENDPOINT=http://server-host:8318 \
COLLECTOR_API_KEY=ait_generated_token_from_admin_ui \
CLIENT_NAME="$(scutil --get LocalHostName 2>/dev/null || hostname)" \
deploy/collector/install-macos-launchd.sh
```

For updates, use the update-only script. Override `LABEL` or `PYTHON_BIN` if the
local LaunchAgent differs from the installer defaults:

```bash
LABEL=com.example.ai-usage-tracker.receiver \
PYTHON_BIN=/opt/homebrew/bin/python3.13 \
deploy/collector/update-macos-launchd.sh
```

Tagged releases also include a collector tarball,
`ai-usage-tracker-collector-<tag>.tar.gz`, with the collector code and deploy
scripts. Extract it on a client and run the matching update script to upgrade
without rewriting `codex_usage_observer.toml`.

AI clients generally do not start the receiver automatically. On macOS, install
a user LaunchAgent if you want the receiver to start when you log in:

```bash
mkdir -p "$HOME/Library/Application Support/ai-usage-tracker"
cp -R codex_usage_observer.py ai_usage_tracker codex_usage_observer.toml \
  "$HOME/Library/Application Support/ai-usage-tracker/"
mkdir -p "$HOME/Library/Logs/ai-usage-tracker" "$HOME/Library/LaunchAgents"
```

Create `~/Library/LaunchAgents/com.example.ai-usage-tracker.receiver.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.ai-usage-tracker.receiver</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/your-user/Library/Application Support/ai-usage-tracker/codex_usage_observer.py</string>
    <string>--config</string>
    <string>/Users/your-user/Library/Application Support/ai-usage-tracker/codex_usage_observer.toml</string>
    <string>serve</string>
    <string>--port</string>
    <string>4318</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/your-user/Library/Application Support/ai-usage-tracker</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/your-user/Library/Logs/ai-usage-tracker/receiver.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/your-user/Library/Logs/ai-usage-tracker/receiver.err.log</string>
</dict>
</plist>
```

Replace `your-user` in the paths and label if installing for another macOS user.
Keeping the service copy under `~/Library/Application Support` avoids macOS
privacy restrictions that can block background agents from reading projects
under `~/Documents`.

Load and start the agent:

```bash
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.example.ai-usage-tracker.receiver.plist"
launchctl kickstart -k "gui/$(id -u)/com.example.ai-usage-tracker.receiver"
```

Check service state and the listening port:

```bash
launchctl print "gui/$(id -u)/com.example.ai-usage-tracker.receiver"
lsof -iTCP:4318 -sTCP:LISTEN -n -P
```

Stop and unload it:

```bash
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.example.ai-usage-tracker.receiver.plist"
```

## 2. Configure telemetry sources

The tracker is source-agnostic once it receives OTEL/HTTP JSON or normalized
server-side usage events. Configure each AI provider or client to send telemetry
to either a local collector or the aggregation server.

### Codex OTEL telemetry

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

The important points are `otlp-http`, a local `/v1/logs` endpoint, and `json`.
The receiver also stores non-JSON payloads raw, but token extraction needs JSON.
Codex batches telemetry asynchronously, so start a fresh Codex session after
changing this config and check the observer again after the session has ended.

### Claude Code OTEL telemetry

Claude Code can use the same local collector when it exports OTLP/HTTP JSON.
Enable the metrics exporter for token and cost usage, and the logs exporter for
tool activity:

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

For beta Claude Code traces, point the traces exporter at the same collector:

```bash
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces
```

The collector normalizes `claude_code.token.usage` metrics with `type` values
of `input`, `output`, `cacheRead`, and `cacheCreation`; it also stores
`claude_code.cost.usage` as USD cost and recognizes Claude Code tool result and
tool decision events.

## Aggregation server

Run one aggregation server when you want multiple collectors to report into a
single place. Collectors authenticate with per-collector API tokens created from
the server's admin UI. The server stores only token hashes, not the raw tokens.

Start the aggregation server:

```bash
python3 codex_usage_observer.py --config codex_usage_observer.toml server serve
```

By default it binds to `127.0.0.1:8318`. Use `--allow-remote` only on a trusted
LAN; the MVP admin UI has no login and relies on network placement.

Open `/admin` in a browser to create, rename, revoke, and delete revoked
collector tokens. New tokens are shown once. Only token hashes are stored in the
server SQLite DB. In the web UI, "collector" means the machine or ingestion
process that reports usage to the aggregation server. Provider-side sources such
as OpenRouter workspaces, projects, or API keys should be reported separately
from collector identity.

Configure each collector with the generated token:

```toml
client_name = "work-laptop"

[collector]
endpoint = "http://server-host:8318"
api_key = "ait_generated_token_from_admin_ui"
batch_size = 100
timeout_seconds = 10
```

Each collector keeps its own local reports and queues unsynced rows. It tracks
the server target it last synced each event to. If `client_name`,
`[collector].endpoint`, or `[collector].api_key` changes, historical usage
becomes pending for that new target and is sent on collector startup or the next
manual sync. During normal collection, each payload that extracts usage also
drains up to `[collector].batch_size` pending usage rows and tool rows. Run a
manual retry with:

```bash
python3 codex_usage_observer.py client sync
```

Check collector sync progress with:

```bash
python3 codex_usage_observer.py client sync-status
python3 codex_usage_observer.py client sync-status --errors 5
```

The server accepts compact usage and tool-event batches from collectors at
`POST /api/v1/usage-events`. Open `/reports` in a browser to view token totals
grouped by provider, provider-side source, and model by default, with filters
for date, collector, model, session, source kind, workspace, API key, model
provider, grouping, and row limit.
Open `/tools` to view captured tool calls grouped by collector and tool. The web UI
keeps UTC timestamps in the page data and displays them in the browser's local
time zone. Report APIs are available at `GET /api/v1/reports/usage`,
`GET /api/v1/reports/tools`, and `GET /api/v1/stats` using
`Authorization: Bearer <aggregation_server.admin_api_key>`.

### OpenRouter Broadcast ingest

The aggregation server can also accept OpenRouter Broadcast OTLP/HTTP JSON
traces directly at `POST /v1/traces`. This is separate from collector sync:
collectors keep using `/api/v1/usage-events`, and Broadcast rows are stored under
the reserved synthetic client name `openrouter-broadcast`.

Enable the machine-ingest path in the server config:

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
destination pointing at:

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
secrets as report dimensions; OpenRouter API-key reporting uses non-secret labels
or IDs from the trace metadata when available. Secret-shaped attributes in
`attributes_json` still follow the redaction rules.

OpenRouter-focused report groupings are available through `/reports` query
parameters and the report APIs, including:

```text
/reports?group_by=workspace&source_kind=openrouter_broadcast
/reports?group_by=api-key&source_kind=openrouter_broadcast
/reports?group_by=model-provider&source_kind=openrouter_broadcast
```

The default `/reports` view is `provider-source-model`. In that view, `provider`
is the system where usage came from, such as Codex, Claude Code, or OpenRouter.
`source` is the identity inside that provider: for collector-synced Codex usage
it is normally the collector or workspace, while for OpenRouter it is the
workspace first, then API key label if no workspace is available.

Retained Broadcast payloads can be replayed after parser changes:

```bash
python3 codex_usage_observer.py --config codex_usage_observer.toml \
  server replay-broadcast --replay-status ingested
```

Useful selectors include `--payload-id`, `--since`, `--until`,
`--replay-status`, and `--limit`. Replay reuses the live Broadcast parser and is
idempotent against the derived OpenRouter event id.

### Cloudflare Access in front of the aggregation server

When the aggregation server is exposed through Cloudflare Access, keep the web
UI protected with your identity provider and use a Cloudflare Access service
token for headless collectors. The collector still sends the app-level
`api_key` as `Authorization: Bearer ...`; the Cloudflare service token only
gets the request through Cloudflare Access.

Create a Cloudflare Access service token for collectors, add a Service Auth
policy for the aggregation app or at least `/api/v1/usage-events`, then configure
each collector:

```toml
client_name = "work-laptop"

[collector]
endpoint = "https://usage.example.com"
api_key = "ait_generated_token_from_admin_ui"
cloudflare_access_client_id = "your.cloudflare.access.client.id"
cloudflare_access_client_secret = "your.cloudflare.access.client.secret"
batch_size = 100
timeout_seconds = 10
```

Collector sync requests then include:

```http
CF-Access-Client-Id: your.cloudflare.access.client.id
CF-Access-Client-Secret: your.cloudflare.access.client.secret
Authorization: Bearer ait_generated_token_from_admin_ui
```

Do not publicly bypass `/api/v1/usage-events` unless the origin is otherwise
locked down. A Cloudflare Service Auth policy keeps the endpoint machine-only
while preserving normal browser authentication for `/reports`, `/tools`, and
`/admin`.

If collector sync redirects to the Cloudflare Access login page, verify the
Access policy includes a Service Auth rule for that service token and that the
rule covers `/api/v1/usage-events`. On macOS, prefer a modern Python build such
as Homebrew Python for launchd collectors; Apple's `/usr/bin/python3` may use an
older LibreSSL that Cloudflare rejects before Access auth runs.

Older configs using `[server]` for collector forwarding and `[central_server]`
for aggregation-server settings are still accepted. Prefer `[collector]` and
`[aggregation_server]` for new configs.

### Run the aggregation server with Docker

The repository includes a Docker setup for the aggregation server component. It
binds the container service to `127.0.0.1:8318` on the host and stores the
server SQLite database plus persistent server config under `./data/server`.

```bash
docker compose up -d --build
```

Open the admin UI at `http://127.0.0.1:8318/admin` to create client tokens.
Open reports at `http://127.0.0.1:8318/reports` to view usage events and token
counts. Open tool reports at `http://127.0.0.1:8318/tools`.

Check status and logs:

```bash
docker compose ps
docker logs --tail 50 ai-usage-tracker-server
```

Stop the server:

```bash
docker compose down
```

The image packages `docker/server.toml` as a default. On first start, the
entrypoint copies it to `/data/server.toml`; later starts use the persisted
`/data/server.toml`, so container upgrades do not overwrite local config.

The container runs:

```bash
python codex_usage_observer.py --config /data/server.toml server serve \
  --host 0.0.0.0 --port 8318 --server-db /data/codex_usage_server.sqlite \
  --allow-remote
```

### GitHub Actions releases and images

CI runs on pull requests, pushes to `main`, and manual dispatch. It runs the
Python unittest suite on Python 3.9 and 3.12, then builds the Docker image.
Internal pull requests and `main` pushes publish non-release GHCR tags:

```text
ghcr.io/v5u2/ai-usage-tracker:pr-<number>
ghcr.io/v5u2/ai-usage-tracker:sha-<shortsha>
ghcr.io/v5u2/ai-usage-tracker:edge
```

The `edge` tag is only updated by pushes to `main`. Pull requests publish
`pr-<number>` plus the immutable commit SHA tag. Forked pull requests build the
image but do not push to GHCR.

Release publishing runs when a `v*` tag is pushed, or from manual dispatch of
the `Release` workflow. It builds the server container and pushes it to GitHub
Container Registry:

```text
ghcr.io/v5u2/ai-usage-tracker
```

Push a release tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Published image tags include the release tag. Stable tags without a hyphen also
update `latest`; prerelease tags such as `v0.1.0-rc.1` do not. The `latest` tag
is reserved for stable releases and is not updated by ordinary commits or pull
requests.

Manual releases can be started from the GitHub Actions UI with a `tag` input.
The release workflow also creates or updates the matching GitHub Release with
generated release notes. Do not bump versions, tag releases, or publish images
unless that release action is intended.

### Unraid deployment

An Unraid Docker template is available at `deploy/unraid/ai-usage-tracker.xml`. It
deploys the aggregation server from GHCR, maps host port `18418` to the
container's `8318/tcp`, and persists server SQLite data plus `server.toml` at
`/mnt/user/Docker/ai-usage-tracker`.

Install the template on a remote Unraid host:

```bash
UNRAID_HOST=root@unraid-host \
TEMPLATE_NAME=my-ai-usage-tracker.xml \
deploy/unraid/install-template.sh
```

Or copy the template manually to the Unraid host:

```text
/boot/config/plugins/dockerMan/templates-user/my-ai-usage-tracker.xml
```

Or import it from the raw template URL after it is available on the default
branch:

```text
https://raw.githubusercontent.com/V5U2/ai-usage-tracker/main/deploy/unraid/ai-usage-tracker.xml
```

After starting the container, open `http://<unraid-ip>:18418/admin` to create
collector tokens and `http://<unraid-ip>:18418/reports` to view usage reports.

## 3. Check token totals

```bash
python3 codex_usage_observer.py summary
```

For richer reporting:

```bash
python3 codex_usage_observer.py report --group-by day-model
python3 codex_usage_observer.py report --group-by session --since 2026-05-01
python3 codex_usage_observer.py report --group-by total --format csv
```

To report captured tool calls:

```bash
python3 codex_usage_observer.py tools-report
python3 codex_usage_observer.py tools-report --group-by day-tool
python3 codex_usage_observer.py tools-report --group-by event --event-name ""
```

To inspect extracted event attributes:

```bash
python3 codex_usage_observer.py samples --limit 5
```

To inspect raw telemetry received by the local collector:

```bash
python3 codex_usage_observer.py stats
python3 codex_usage_observer.py raw --limit 20
python3 codex_usage_observer.py dump-raw 1
```

Raw payload bodies are not stored by default. If you temporarily set
`raw_payload_body = true` for troubleshooting, raw OTEL payloads may include
account metadata or prompt-related telemetry depending on your AI client or
provider configuration. Do not commit `codex_usage.sqlite*` files or share
`dump-raw` output publicly. Extracted usage samples redact common credential and
account fields.
If you change storage settings after collecting data, run `reindex` to rebuild
extracted usage rows from stored raw payloads:

```bash
python3 codex_usage_observer.py --config codex_usage_observer.toml reindex
```

To also apply storage-retention settings to existing rows, for example clearing
previously stored raw payload bodies after setting `raw_payload_body = false`,
run:

```bash
python3 codex_usage_observer.py --config codex_usage_observer.toml cleanup
```

## Reports

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

Server report APIs and the `/reports` web page also support `client`,
`client-model`, `day-client`, `day-model-client`, `source`, `workspace`,
`api-key`, and `model-provider`. The `client*` group names are kept for API
compatibility; the web UI labels them as collectors.

`tools-report` supports `total`, `day`, `tool`, `session`, `event`,
`day-tool`, and `day-session`. By default it reports completed
`codex.tool_result` events when reporting Codex telemetry; pass
`--event-name ""` to include decisions too.

Filters use UTC timestamps. A plain date such as `2026-05-01` is accepted for
`--since` and `--until`. CLI and API output keep server UTC timestamps; the web
views display those UTC values in the browser's local time zone. Output formats
are `table`, `csv`, and `json`.

## Limits

This uses whatever each source emits through OTEL or provider webhooks. If a
source does not emit recognized token attributes, the receiver still stores the
raw telemetry payload metadata, but the summary will show no extracted token
events. In that case, inspect `samples` and retained raw SQLite payloads, then
add the emitted attribute names to the parser in `ai_usage_tracker/core.py`.

The parser already recognises common OpenTelemetry/LLM usage names such as:

- `input_tokens`, `prompt_tokens`, `gen_ai.usage.input_tokens`
- `output_tokens`, `completion_tokens`, `gen_ai.usage.output_tokens`
- `total_tokens`, `gen_ai.usage.total_tokens`
- `claude_code.token.usage` metrics with `type` set to `input`, `output`,
  `cacheRead`, or `cacheCreation`
- `claude_code.cost.usage`
- cached and reasoning token variants
