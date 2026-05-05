# Deployment

This directory contains deployment helpers for the two runtime roles:

- **Aggregation server**: runs once, commonly as a Docker container on Unraid.
- **Collector**: runs beside AI clients on each Mac or Linux machine and
  forwards compact events to the aggregation server. WSL2 uses the Linux path
  when systemd is enabled.

## Unraid Aggregation Server

Publish a release image first so Unraid can pull `ghcr.io/v5u2/ai-usage-tracker:latest`.
For a private GHCR package, log Docker into GHCR on the Unraid host before
creating the container.

Install the Docker template on an Unraid host reachable by SSH:

```bash
UNRAID_HOST=root@unraid-host \
TEMPLATE_NAME=my-ai-usage-tracker.xml \
deploy/unraid/install-template.sh
```

The template file lives at `deploy/unraid/ai-usage-tracker.xml`.

The template defaults to:

- Repository: `ghcr.io/v5u2/ai-usage-tracker:latest`
- Host port: `18418`
- Container port: `8318`
- Persistent data and server config: `/mnt/user/Docker/ai-usage-tracker`
  mounted at `/data`

After the container starts, open:

```text
http://UNRAID_HOST_OR_IP:18418/admin
```

Create a collector client token. The raw token is shown once; use it when
installing collectors.

## Linux Collector

Run this on the Linux machine where an AI client runs:

```bash
AGGREGATION_ENDPOINT=http://UNRAID_HOST_OR_IP:18418 \
COLLECTOR_API_KEY=ait_generated_token_from_admin_ui \
CLIENT_NAME="$(hostname)" \
deploy/collector/install-linux-systemd.sh
```

WSL2 can use the same installer when systemd is enabled in the distribution. If
systemd is disabled, use the manual collector command from the main README or
enable systemd first.

For a Cloudflare Access-protected endpoint, add:

```bash
CF_ACCESS_CLIENT_ID=your.cloudflare.access.client.id \
CF_ACCESS_CLIENT_SECRET=your.cloudflare.access.client.secret
```

`AGGREGATION_ENDPOINT` and `COLLECTOR_API_KEY` are required so a rerun cannot
accidentally overwrite a forwarding config with a local-only collector.

Defaults:

- Install dir: `~/.local/share/ai-usage-tracker`
- Config: `~/.local/share/ai-usage-tracker/ai_usage_tracker.toml`
- Service: `~/.config/systemd/user/ai-usage-collector.service`
- Listen address: `127.0.0.1:4318`

Useful checks:

```bash
systemctl --user status ai-usage-collector.service --no-pager
journalctl --user -u ai-usage-collector.service -f
ss -ltnp | grep ':4318'
python3 ~/.local/share/ai-usage-tracker/ai_usage_tracker.py \
  --config ~/.local/share/ai-usage-tracker/ai_usage_tracker.toml \
  --db ~/.local/share/ai-usage-tracker/ai_usage.sqlite \
  client sync-status
```

Update an already-installed Linux collector without changing its config:

```bash
deploy/collector/update-linux-systemd.sh
```

Update one or more SSH-reachable Linux collectors from this checkout:

```bash
deploy/collector/update-remote-linux-systemd.sh linux-host other-host
```

## macOS Collector

Run this on the Mac where an AI client runs:

```bash
AGGREGATION_ENDPOINT=http://UNRAID_HOST_OR_IP:18418 \
COLLECTOR_API_KEY=ait_generated_token_from_admin_ui \
CLIENT_NAME="$(scutil --get LocalHostName 2>/dev/null || hostname)" \
deploy/collector/install-macos-launchd.sh
```

For a Cloudflare Access-protected endpoint, add:

```bash
CF_ACCESS_CLIENT_ID=your.cloudflare.access.client.id \
CF_ACCESS_CLIENT_SECRET=your.cloudflare.access.client.secret
```

`AGGREGATION_ENDPOINT` and `COLLECTOR_API_KEY` are required so a rerun cannot
accidentally overwrite a forwarding config with a local-only collector.

Defaults:

- Install dir: `~/Library/Application Support/ai-usage-tracker`
- Config: `~/Library/Application Support/ai-usage-tracker/ai_usage_tracker.toml`
- LaunchAgent label: `com.$USER.ai-usage-tracker.collector`
- Logs: `~/Library/Logs/ai-usage-tracker`
- Listen address: `127.0.0.1:4318`

Useful checks:

```bash
launchctl print "gui/$(id -u)/com.$USER.ai-usage-tracker.collector"
lsof -iTCP:4318 -sTCP:LISTEN -n -P
tail -f "$HOME/Library/Logs/ai-usage-tracker/collector.err.log"
```

Update an already-installed macOS collector without changing its config:

```bash
deploy/collector/update-macos-launchd.sh
```

If the LaunchAgent label or Python path differs from the defaults, pass them:

```bash
LABEL=com.example.ai-usage-tracker.receiver \
PYTHON_BIN=/opt/homebrew/bin/python3.13 \
deploy/collector/update-macos-launchd.sh
```

## Collector Packaging Roadmap

The release workflow publishes a collector tarball named
`ai-usage-tracker-collector-<tag>.tar.gz` alongside the server container image.
It contains:

- `ai_usage_tracker.py`
- `ai_usage_tracker/`
- `collector.example.toml`
- `server.example.toml`
- `deploy/collector/`
- `README.md` and `deploy/README.md`

To update a collector from an unpacked release tarball, run the matching update
script from the extracted directory. The update scripts preserve the existing
collector config and credentials:

```bash
# Linux
deploy/collector/update-linux-systemd.sh

# macOS
deploy/collector/update-macos-launchd.sh
```

The current collector deployment model is still source-copy based: install
scripts create config and services, while update scripts copy code over the
existing install and preserve credentials. Future packaging can make this
cleaner:

- Add a small `ai-usage-collector` console entry point via Python packaging so
  installs can use `pipx install ai-usage-tracker` or `uv tool install`.
- For managed fleets, publish OS-native packages later: Homebrew formula for
  macOS and a `.deb`/`.rpm` or systemd-user tarball for Linux.
- Keep config outside the package-owned code directory so upgrades never rewrite
  API keys or Cloudflare Access service-token credentials.

## OTEL Source Config

Each collector expects AI clients to export OTEL logs to loopback. For Codex,
add this to `~/.codex/config.toml`:

```toml
[otel]
environment = "local"
log_user_prompt = false
exporter = { otlp-http = {
  endpoint = "http://127.0.0.1:4318/v1/logs",
  protocol = "json"
}}
```

Restart the AI client after changing OTEL settings. The collector stores events locally
first, then forwards unsynced usage and tool events to the aggregation server.
