# Codex Usage Observer

This is a local and multi-machine observability setup for Codex usage. The
client runs a local OTLP/HTTP receiver, stores extracted usage rows in SQLite,
and can forward compact usage events to a central server. The server aggregates
usage across machines and includes a simple LAN-only admin UI for client tokens.

## 1. Start the local receiver

```bash
python3 codex_usage_observer.py serve --port 4318
```

The explicit client command is equivalent:

```bash
python3 codex_usage_observer.py client serve --port 4318
```

Leave that process running while you use Codex.
By default, the receiver binds only to `127.0.0.1`. Use `--allow-remote` only
on a trusted network; OTEL payloads can contain sensitive local telemetry.

### WSL autostart

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

Start from `codex_usage_observer.example.toml`. The main storage choices are:

- `client_name`: names this machine/client for later aggregation.
- `[server]`: optional central server endpoint and client API key.
- `[central_server]`: bind address and database for the central server.
- `raw_payload_body`: store full raw OTEL payload bodies, or keep only metadata.
- `extracted_attributes`: store extracted attributes as `redacted`, `full`, or `none`.
- `model`, `session_id`, `thread_id`: choose whether these dimensions are stored on usage rows.
- `max_body_bytes`: reject oversized inbound payloads.

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

## Central server

Start the central server:

```bash
python3 codex_usage_observer.py --config codex_usage_observer.toml server serve
```

By default it binds to `127.0.0.1:8318`. Use `--allow-remote` only on a trusted
LAN; the MVP admin UI has no login and relies on network placement.

Open `/admin` in a browser to create, rename, and revoke client tokens. New
tokens are shown once. Only token hashes are stored in the server SQLite DB.

Configure each client with the generated token:

```toml
client_name = "work-laptop"

[server]
endpoint = "http://server-host:8318"
api_key = "ait_generated_token_from_admin_ui"
batch_size = 100
timeout_seconds = 10
```

The client keeps local reports and queues unsynced rows. Run a manual retry with:

```bash
python3 codex_usage_observer.py client sync
```

The server accepts compact usage batches at `POST /api/v1/usage-events`. Report
APIs are available at `GET /api/v1/reports/usage` and `GET /api/v1/stats` using
`Authorization: Bearer <central_server.admin_api_key>`.

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

Server report APIs also support `client`, `day-client`, and
`day-model-client`.

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
