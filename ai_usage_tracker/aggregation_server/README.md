# Aggregation Server Component

The aggregation server is the central service for multi-machine usage data. It
accepts compact event batches from collectors, stores them in server-side
SQLite, manages collector client tokens, and renders the `/reports` web view.

Primary runtime command:

```bash
python3 codex_usage_observer.py --config codex_usage_observer.toml server serve
```

Docker runs the same component with `/data` persistence:

```bash
docker compose up -d --build
```

Relevant implementation surface:

- `ServerReceiver`: central HTTP handler for admin, reports, and ingestion APIs
- `server_serve`: aggregation server command implementation
- `create_client_token`, `revoke_client`, `delete_revoked_client`: client token lifecycle
- `ingest_usage_events`, `server_report_rows`, `server_stats_dict`: aggregation and reporting helpers
