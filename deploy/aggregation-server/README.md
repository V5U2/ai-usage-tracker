# Aggregation Server Deployment

The aggregation server runs once on a trusted host. It accepts compact batches
from collectors, stores a server SQLite database, manages collector API tokens,
and serves `/admin`, `/reports`, and `/tools`.

## Linux Systemd

Use this path when you want to run the aggregation server directly on a Linux
host without Docker. The service runs from a source checkout, keeps config under
`/etc/ai-usage-tracker`, and stores the server SQLite database under
`/var/lib/ai-usage-tracker`.

Create a dedicated service user and install the code:

```bash
sudo useradd --system --home /var/lib/ai-usage-tracker \
  --shell /usr/sbin/nologin ai-usage-tracker
sudo mkdir -p /opt/ai-usage-tracker /etc/ai-usage-tracker /var/lib/ai-usage-tracker
sudo rm -rf /opt/ai-usage-tracker/ai_usage_tracker
sudo cp -R ai_usage_tracker.py ai_usage_tracker /opt/ai-usage-tracker/
sudo cp server.example.toml /etc/ai-usage-tracker/server.toml
sudo chown -R root:root /opt/ai-usage-tracker /etc/ai-usage-tracker
sudo chown -R ai-usage-tracker:ai-usage-tracker /var/lib/ai-usage-tracker
```

Edit `/etc/ai-usage-tracker/server.toml` before starting the service. At
minimum, set a strong `[aggregation_server].admin_api_key` if you will use the
report APIs, and configure `[openrouter_broadcast]` only when OpenRouter should
send traces directly to the server.

Create `/etc/systemd/system/ai-usage-tracker-server.service`:

```ini
[Unit]
Description=AI Usage Tracker aggregation server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ai-usage-tracker
Group=ai-usage-tracker
WorkingDirectory=/opt/ai-usage-tracker
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 /opt/ai-usage-tracker/ai_usage_tracker.py --config /etc/ai-usage-tracker/server.toml server serve --host 127.0.0.1 --port 8318 --server-db /var/lib/ai-usage-tracker/ai_usage_server.sqlite
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/ai-usage-tracker

[Install]
WantedBy=multi-user.target
```

Start and inspect it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai-usage-tracker-server.service
sudo systemctl status ai-usage-tracker-server.service --no-pager
journalctl -u ai-usage-tracker-server.service -f
curl -fsSL http://127.0.0.1:8318/reports >/dev/null
```

With `--host 127.0.0.1`, put a reverse proxy, VPN, or Cloudflare Tunnel in
front of the service for remote collectors and browser access. If you
intentionally expose it on a trusted LAN, change `--host` to `0.0.0.0`, add
`--allow-remote`, and restrict access with a firewall or authenticated proxy.

Update an existing Linux install by copying the new code over the old code and
restarting the service. Do not overwrite `/etc/ai-usage-tracker/server.toml` or
the SQLite database:

```bash
sudo rm -rf /opt/ai-usage-tracker/ai_usage_tracker
sudo cp -R ai_usage_tracker.py ai_usage_tracker /opt/ai-usage-tracker/
sudo chown -R root:root /opt/ai-usage-tracker
sudo systemctl restart ai-usage-tracker-server.service
```

## Docker Compose

The repository includes a Docker setup for the aggregation server component. It
publishes the container service on `127.0.0.1:8318` on the host and stores the
server SQLite database plus persistent server config under `./data/server`.

```bash
docker compose up -d --build
```

Open the admin UI at `http://127.0.0.1:8318/admin` to create client tokens.
Open reports at `http://127.0.0.1:8318/reports` to view usage events and token
counts. Open tool reports at `http://127.0.0.1:8318/tools`.

Report previews:

![Aggregation server token usage report](../../docs/assets/aggregation-server-token-usage.png)

![Aggregation server tool usage report](../../docs/assets/aggregation-server-tool-usage.png)

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

The application inside the container listens on all container interfaces, but
`docker-compose.yml` publishes it on host loopback only. Put a reverse proxy,
VPN, or tunnel in front of it for remote collectors, or intentionally change the
Compose port binding when exposing it on a trusted LAN.

## Unraid

Publish a release image first so Unraid can pull
`ghcr.io/v5u2/ai-usage-tracker:latest`. For a private GHCR package, log Docker
into GHCR on the Unraid host before creating the container.

Install the Docker template on an Unraid host reachable by SSH:

```bash
UNRAID_HOST=root@unraid-host \
TEMPLATE_NAME=my-ai-usage-tracker.xml \
deploy/aggregation-server/unraid/install-template.sh
```

The template file lives at
`deploy/aggregation-server/unraid/ai-usage-tracker.xml`.

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
https://raw.githubusercontent.com/V5U2/ai-usage-tracker/main/deploy/aggregation-server/unraid/ai-usage-tracker.xml
```

After starting the container, open `http://<unraid-ip>:18418/reports` to view
usage reports.

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

## GitHub Actions Releases And Images

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
