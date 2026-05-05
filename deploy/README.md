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

The template can also be copied manually to the Unraid host:

```text
/boot/config/plugins/dockerMan/templates-user/my-ai-usage-tracker.xml
```

Or imported from the raw template URL after it is available on the default
branch:

```text
https://raw.githubusercontent.com/V5U2/ai-usage-tracker/main/deploy/unraid/ai-usage-tracker.xml
```

After starting the container, open `http://<unraid-ip>:18418/reports` to view
usage reports.

## Docker Aggregation Server

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

The image includes the `sqlite3` CLI for operational inspection and repair of
the persisted server database:

```bash
docker exec ai-usage-tracker-server sqlite3 /data/ai_usage_server.sqlite ".tables"
```

Stop the server:

```bash
docker compose down
```

The image packages `docker/server.toml` as a default. On first start, the
entrypoint copies it to `/data/server.toml`; later starts use the persisted
`/data/server.toml`, so container upgrades do not overwrite local config.
New containers use `/data/ai_usage_server.sqlite`.

The container runs:

```bash
python ai_usage_tracker.py --config /data/server.toml server serve \
  --host 0.0.0.0 --port 8318 --server-db /data/ai_usage_server.sqlite \
  --allow-remote
```

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

AI clients generally do not start the receiver automatically. On macOS, install
a user LaunchAgent if you want the receiver to start when you log in. The
script above is the preferred path, but the manual equivalent is:

```bash
mkdir -p "$HOME/Library/Application Support/ai-usage-tracker"
cp -R ai_usage_tracker.py ai_usage_tracker \
  "$HOME/Library/Application Support/ai-usage-tracker/"
cp collector.example.toml \
  "$HOME/Library/Application Support/ai-usage-tracker/ai_usage_tracker.toml"
mkdir -p "$HOME/Library/Logs/ai-usage-tracker" "$HOME/Library/LaunchAgents"
```

Edit the installed `ai_usage_tracker.toml` before starting the LaunchAgent.

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
    <string>/Users/your-user/Library/Application Support/ai-usage-tracker/ai_usage_tracker.py</string>
    <string>--config</string>
    <string>/Users/your-user/Library/Application Support/ai-usage-tracker/ai_usage_tracker.toml</string>
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

## Cloudflare Access

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

## GitHub Actions Releases and Images

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

Release Please runs on pushes to `main` and opens or updates a release PR when
there are conventional commits since the last release. Merge the Release Please
PR to tag the release, create the GitHub Release, attach the collector tarball,
and publish the stable container image.

Release Please updates:

- `CHANGELOG.md`
- `.release-please-manifest.json`
- `APP_VERSION` in `ai_usage_tracker/core.py`

Use conventional PR or squash-merge titles for user-facing changes, such as
`fix: ...`, `feat: ...`, or `docs: ...`. Use `Release-As: x.y.z` in a commit
footer only when a specific version is required.

Release images are pushed to GitHub Container Registry:

```text
ghcr.io/v5u2/ai-usage-tracker
```

Published image tags include the release tag. Stable tags also update `latest`;
prerelease tags should be marked as prereleases in the release PR workflow. The
`latest` tag is reserved for stable releases and is not updated by ordinary
commits or pull requests.

Manual tag-based releases remain available as a fallback through the `Release`
workflow. Push a release tag only when bypassing Release Please is intended:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Manual releases can be started from the GitHub Actions UI with a `tag` input.
Do not manually bump versions, tag releases, or publish images unless that
release action is intended.

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
