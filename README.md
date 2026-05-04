# Codex Usage Observer

This is a local and multi-machine observability setup for Codex usage. It has
two runtime roles:

- **Local collectors** run beside Codex on each machine. A collector receives
  Codex OTEL/HTTP telemetry on `127.0.0.1:4318`, stores raw payloads and
  extracted usage rows in a local SQLite database, and can forward compact
  usage/tool events to an aggregation server.
- **The aggregation server** runs once on a trusted host. It accepts compact
  batches from collectors, stores multi-machine usage in a server SQLite
  database, manages collector API tokens, and serves web reports.

The collector is still useful without an aggregation server: local summaries,
raw payload inspection, reindexing, and cleanup all work from the local SQLite
database. Add an aggregation server when you want a shared view across multiple
machines.

Typical flow:

```text
Codex OTEL -> local collector -> local SQLite
                         |
                         v
                 aggregation server -> server SQLite -> /reports and /tools
```

## Repository layout

- `codex_usage_tracker/collector/`: local OTLP receiver/collector surface,
  including local persistence and forwarding to an aggregation server.
- `codex_usage_tracker/aggregation_server/`: central aggregation server surface,
  including client tokens, ingestion APIs, and web reports.
- `codex_usage_tracker/core.py`: shared implementation used by both components
  and the compatibility CLI.
- `codex_usage_observer.py`: top-level compatibility CLI entry point.
- `docker/`, `Dockerfile`, `docker-compose.yml`: containerized aggregation
  server setup.
- `unraid/`: Unraid Docker template for deploying the aggregation server.
- `deploy/`: deployment scripts for Unraid aggregation servers and macOS,
  Linux, or WSL collectors.

## 1. Run a local collector

Run this on every machine where you want to capture Codex usage. The collector
binds to loopback by default and should generally stay local-only because OTEL
payloads can include sensitive metadata.

```bash
python3 codex_usage_observer.py serve --port 4318
```

The explicit client command is equivalent:

```bash
python3 codex_usage_observer.py client serve --port 4318
```

Leave that process running while you use Codex. Use `--allow-remote` only on a
trusted network and only when you intentionally want a collector to receive OTEL
from another host.

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

On WSL with systemd enabled, run the receiver as a user service so it starts
automatically with the WSL user session:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/codex-usage-receiver.service <<'EOF'
[Unit]
Description=Codex usage tracker local receiver
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/james/ai-usage-tracker
ExecStart=/usr/bin/python3 /home/james/ai-usage-tracker/codex_usage_observer.py --config /home/james/ai-usage-tracker/codex_usage_observer.toml client serve --host 127.0.0.1 --port 4318
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now codex-usage-receiver.service
```

Useful service commands:

```bash
systemctl --user status codex-usage-receiver.service --no-pager
journalctl --user -u codex-usage-receiver.service -f
systemctl --user restart codex-usage-receiver.service
systemctl --user disable --now codex-usage-receiver.service
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
- `raw_payload_body`: store full raw OTEL payload bodies, or keep only metadata.
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

Codex does not start the receiver automatically. On macOS, install a user
LaunchAgent if you want the receiver to start when you log in:

```bash
mkdir -p "$HOME/Library/Application Support/ai-usage-tracker"
cp -R codex_usage_observer.py codex_usage_tracker codex_usage_observer.toml \
  "$HOME/Library/Application Support/ai-usage-tracker/"
mkdir -p "$HOME/Library/Logs/ai-usage-tracker" "$HOME/Library/LaunchAgents"
```

Create `~/Library/LaunchAgents/com.james.ai-usage-tracker.receiver.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.james.ai-usage-tracker.receiver</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/james/Library/Application Support/ai-usage-tracker/codex_usage_observer.py</string>
    <string>--config</string>
    <string>/Users/james/Library/Application Support/ai-usage-tracker/codex_usage_observer.toml</string>
    <string>serve</string>
    <string>--port</string>
    <string>4318</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/james/Library/Application Support/ai-usage-tracker</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/james/Library/Logs/ai-usage-tracker/receiver.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/james/Library/Logs/ai-usage-tracker/receiver.err.log</string>
</dict>
</plist>
```

Replace `james` in the paths and label if installing for another macOS user.
Keeping the service copy under `~/Library/Application Support` avoids macOS
privacy restrictions that can block background agents from reading projects
under `~/Documents`.

Load and start the agent:

```bash
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.james.ai-usage-tracker.receiver.plist"
launchctl kickstart -k "gui/$(id -u)/com.james.ai-usage-tracker.receiver"
```

Check service state and the listening port:

```bash
launchctl print "gui/$(id -u)/com.james.ai-usage-tracker.receiver"
lsof -iTCP:4318 -sTCP:LISTEN -n -P
```

Stop and unload it:

```bash
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.james.ai-usage-tracker.receiver.plist"
```

## 2. Configure Codex telemetry

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

## Aggregation server

Run one aggregation server when you want multiple collectors to report into a
single place. Collectors authenticate with per-client API tokens created from
the server's admin UI. The server stores only token hashes, not the raw tokens.

Start the aggregation server:

```bash
python3 codex_usage_observer.py --config codex_usage_observer.toml server serve
```

By default it binds to `127.0.0.1:8318`. Use `--allow-remote` only on a trusted
LAN; the MVP admin UI has no login and relies on network placement.

Open `/admin` in a browser to create, rename, revoke, and delete revoked
collector tokens. New tokens are shown once. Only token hashes are stored in the
server SQLite DB.

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
manual sync. Run a manual retry with:

```bash
python3 codex_usage_observer.py client sync
```

The server accepts compact usage and tool-event batches at `POST /api/v1/usage-events`.
Open `/reports` in a browser to view token totals grouped by client and model by
default, with filters for date, client, model, session, grouping, and row limit.
Open `/tools` to view Codex tool calls grouped by client and tool. Report APIs
are available at `GET /api/v1/reports/usage`, `GET /api/v1/reports/tools`, and
`GET /api/v1/stats` using `Authorization: Bearer <aggregation_server.admin_api_key>`.

Older configs using `[server]` for collector forwarding and `[central_server]`
for aggregation-server settings are still accepted. Prefer `[collector]` and
`[aggregation_server]` for new configs.

### Run the aggregation server with Docker

The repository includes a Docker setup for the aggregation server component. It
binds the container service to `127.0.0.1:8318` on the host and stores the
server SQLite database under `./data/server`.

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

The container uses `docker/server.toml` and runs:

```bash
python codex_usage_observer.py --config /app/server.toml server serve \
  --host 0.0.0.0 --port 8318 --server-db /data/codex_usage_server.sqlite \
  --allow-remote
```

### GitHub Actions releases and images

CI runs on pull requests, pushes to `main`, and manual dispatch. It runs the
Python unittest suite on Python 3.9 and 3.12, then builds the Docker image
without pushing it.

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
update `latest`; prerelease tags such as `v0.1.0-rc.1` do not.

Manual releases can be started from the GitHub Actions UI with a `tag` input.
The release workflow also creates or updates the matching GitHub Release with
generated release notes. Do not bump versions, tag releases, or publish images
unless that release action is intended.

### Unraid deployment

An Unraid Docker template is available at `unraid/ai-usage-tracker.xml`. It
deploys the aggregation server from GHCR, maps host port `18418` to the
container's `8318/tcp`, and persists server SQLite data at
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
https://raw.githubusercontent.com/V5U2/ai-usage-tracker/main/unraid/ai-usage-tracker.xml
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

To report Codex tool calls captured from OTEL:

```bash
python3 codex_usage_observer.py tools-report
python3 codex_usage_observer.py tools-report --group-by day-tool
python3 codex_usage_observer.py tools-report --group-by event --event-name ""
```

To inspect extracted event attributes:

```bash
python3 codex_usage_observer.py samples --limit 5
```

To inspect raw telemetry received from Codex:

```bash
python3 codex_usage_observer.py stats
python3 codex_usage_observer.py raw --limit 20
python3 codex_usage_observer.py dump-raw 1
```

Raw payloads are stored locally for troubleshooting and may include account
metadata or prompt-related telemetry depending on your Codex configuration. Do
not commit `codex_usage.sqlite*` files or share `dump-raw` output publicly.
Extracted usage samples redact common credential and account fields.
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
- `day`
- `model`
- `session`
- `day-model`
- `day-session`

Server report APIs and the `/reports` web page also support `client`,
`client-model`, `day-client`, and `day-model-client`.

`tools-report` supports `total`, `day`, `tool`, `session`, `event`,
`day-tool`, and `day-session`. By default it reports completed
`codex.tool_result` events; pass `--event-name ""` to include decisions too.

Filters use UTC timestamps. A plain date such as `2026-05-01` is accepted for
`--since` and `--until`. Output formats are `table`, `csv`, and `json`.

## Limits

This uses whatever Codex emits through OTEL. If your build does not emit token
attributes, the receiver still stores the raw telemetry payloads, but the summary
will show no extracted token events. In that case, inspect `samples` and the raw
SQLite payloads, then add the emitted attribute names to `TOKEN_KEYS` in
`codex_usage_observer.py`.

The parser already recognises common OpenTelemetry/LLM usage names such as:

- `input_tokens`, `prompt_tokens`, `gen_ai.usage.input_tokens`
- `output_tokens`, `completion_tokens`, `gen_ai.usage.output_tokens`
- `total_tokens`, `gen_ai.usage.total_tokens`
- cached and reasoning token variants
