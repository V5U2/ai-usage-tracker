# Deployment

This directory contains deployment helpers for the two runtime roles:

- **Aggregation server**: runs once, commonly as a Docker container on Unraid.
- **Collector**: runs beside Codex on each Mac, Linux, or WSL machine and
  forwards compact events to the aggregation server.

## Unraid Aggregation Server

Publish a release image first so Unraid can pull `ghcr.io/v5u2/ai-usage-tracker:latest`.
For a private GHCR package, log Docker into GHCR on the Unraid host before
creating the container.

Install the Docker template on an Unraid host reachable by SSH:

```bash
UNRAID_HOST=root@sanderson-unraid \
TEMPLATE_NAME=my-ai-usage-tracker.xml \
deploy/unraid/install-template.sh
```

The template defaults to:

- Repository: `ghcr.io/v5u2/ai-usage-tracker:latest`
- Host port: `18418`
- Container port: `8318`
- Persistent data: `/mnt/user/Docker/ai-usage-tracker` mounted at `/data`

After the container starts, open:

```text
http://UNRAID_HOST_OR_IP:18418/admin
```

Create a collector client token. The raw token is shown once; use it when
installing collectors.

## Linux or WSL Collector

Run this on the Linux or WSL machine where Codex runs:

```bash
AGGREGATION_ENDPOINT=http://UNRAID_HOST_OR_IP:18418 \
COLLECTOR_API_KEY=ait_generated_token_from_admin_ui \
CLIENT_NAME="$(hostname)" \
deploy/collector/install-linux-systemd.sh
```

`AGGREGATION_ENDPOINT` and `COLLECTOR_API_KEY` are required so a rerun cannot
accidentally overwrite a forwarding config with a local-only collector.

Defaults:

- Install dir: `~/.local/share/ai-usage-tracker`
- Config: `~/.local/share/ai-usage-tracker/codex_usage_observer.toml`
- Service: `~/.config/systemd/user/codex-usage-collector.service`
- Listen address: `127.0.0.1:4318`

Useful checks:

```bash
systemctl --user status codex-usage-collector.service --no-pager
journalctl --user -u codex-usage-collector.service -f
ss -ltnp | grep ':4318'
```

## macOS Collector

Run this on the Mac where Codex runs:

```bash
AGGREGATION_ENDPOINT=http://UNRAID_HOST_OR_IP:18418 \
COLLECTOR_API_KEY=ait_generated_token_from_admin_ui \
CLIENT_NAME="$(scutil --get LocalHostName 2>/dev/null || hostname)" \
deploy/collector/install-macos-launchd.sh
```

`AGGREGATION_ENDPOINT` and `COLLECTOR_API_KEY` are required so a rerun cannot
accidentally overwrite a forwarding config with a local-only collector.

Defaults:

- Install dir: `~/Library/Application Support/ai-usage-tracker`
- Config: `~/Library/Application Support/ai-usage-tracker/codex_usage_observer.toml`
- LaunchAgent label: `com.$USER.ai-usage-tracker.collector`
- Logs: `~/Library/Logs/ai-usage-tracker`
- Listen address: `127.0.0.1:4318`

Useful checks:

```bash
launchctl print "gui/$(id -u)/com.$USER.ai-usage-tracker.collector"
lsof -iTCP:4318 -sTCP:LISTEN -n -P
tail -f "$HOME/Library/Logs/ai-usage-tracker/collector.err.log"
```

## Codex OTEL Config

Each collector expects Codex to export OTEL logs to loopback:

```toml
[otel]
environment = "local"
log_user_prompt = false
exporter = { otlp-http = {
  endpoint = "http://127.0.0.1:4318/v1/logs",
  protocol = "json"
}}
```

Restart Codex after changing OTEL settings. The collector stores events locally
first, then forwards unsynced usage and tool events to the aggregation server.
