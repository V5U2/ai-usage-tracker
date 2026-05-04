# Codex Usage Observer

This is a local observability setup for your own Codex usage. It runs a local
OTLP/HTTP receiver, stores raw Codex telemetry in SQLite, and extracts simple
token usage rows whenever Codex emits token attributes.

## 1. Start the local receiver

```bash
python3 codex_usage_observer.py serve --port 4318
```

Leave that process running while you use Codex.
By default, the receiver binds only to `127.0.0.1`. Use `--allow-remote` only
on a trusted network; OTEL payloads can contain sensitive local telemetry.

Optional: use a config file to control what gets persisted:

```bash
python3 codex_usage_observer.py --config codex_usage_observer.toml serve --port 4318
```

Start from `codex_usage_observer.example.toml`. The main storage choices are:

- `client_name`: names this machine/client for later aggregation.
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

## Reports

`report` supports these groupings:

- `total`
- `day`
- `model`
- `session`
- `day-model`
- `day-session`

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
