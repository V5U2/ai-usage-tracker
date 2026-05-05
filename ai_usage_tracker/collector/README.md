# Collector Component

The collector component is the local OTLP receiver. It accepts AI client telemetry,
stores raw payloads and extracted usage events in a local SQLite database, and
forwards compact usage events to an aggregation server when `[collector]` is
configured.

Primary runtime command:

```bash
python3 ai_usage_tracker.py --config ai_usage_tracker.toml serve --port 4318
```

Equivalent explicit client command:

```bash
python3 ai_usage_tracker.py --config ai_usage_tracker.toml client serve --port 4318
```

Relevant implementation surface:

- `Receiver`: local HTTP handler for OTLP payloads
- `serve`: collector server command implementation
- `sync`, `sync_pending_usage`, `sync_all_pending_usage`: forwarding to the aggregation server
- `extract_usage`, `insert_payload`, `insert_usage`: parsing and local persistence helpers
