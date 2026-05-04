# Unraid Template

`ai-usage-tracker.xml` is an Unraid Docker template for the aggregation server.
It runs the published GHCR image and persists the server SQLite database under
`/mnt/user/Docker/ai-usage-tracker`.

Defaults:

- Repository: `ghcr.io/v5u2/ai-usage-tracker:latest`
- Web UI: `http://<unraid-ip>:18418/reports`
- Admin UI: `http://<unraid-ip>:18418/admin`
- Host port: `18418/tcp`
- Container port: `8318/tcp`
- App data: `/mnt/user/Docker/ai-usage-tracker` mapped to `/data`

Import options:

1. Install the template with the deployment helper:

   ```bash
   UNRAID_HOST=root@sanderson-unraid \
   TEMPLATE_NAME=my-ai-usage-tracker.xml \
   deploy/unraid/install-template.sh
   ```

2. Or copy `ai-usage-tracker.xml` to `/boot/config/plugins/dockerMan/templates-user/`
   on the Unraid host, then add the container from Docker templates.
3. Or paste the template URL into Unraid's Docker template repository flow after
   it is available from the default branch:

```text
https://raw.githubusercontent.com/V5U2/ai-usage-tracker/main/unraid/ai-usage-tracker.xml
```

After the container starts, create collector client tokens at `/admin`, then
configure each collector's `[collector]` endpoint to point at the Unraid host:

```toml
[collector]
endpoint = "http://UNRAID_HOST_OR_IP:18418"
api_key = "ait_generated_token_from_admin_ui"
batch_size = 100
timeout_seconds = 10
```
