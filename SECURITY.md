# Security Policy

## Supported Versions

Security fixes target the latest released version and the current `main` branch.
Older versions are best-effort only; upgrade to the latest release before
reporting an issue that may already be fixed.

## Reporting a Vulnerability

Please do not open a public issue with vulnerability details, tokens, database
contents, raw telemetry payloads, Cloudflare Access service credentials, or
other secrets.

Use GitHub's private vulnerability reporting flow for this repository when it
is available. If private reporting is not available, open a minimal public issue
asking for a private security contact and omit technical details until a private
channel is established.

Include:

- Affected version or commit.
- Deployment mode, such as local collector, Docker aggregation server, or Unraid
  container.
- Impact and affected component.
- Reproduction steps or a proof of concept using dummy credentials.
- Any relevant logs with secrets redacted.

## Project Security Notes

Collectors and aggregation servers can handle API keys, collector tokens,
Cloudflare Access service credentials, OpenRouter Broadcast payloads, and local
SQLite usage databases. Treat all configuration files and database files as
sensitive operational data.

When reporting or debugging security issues:

- Redact bearer tokens, collector API keys, Cloudflare Access client secrets,
  OpenRouter headers, and provider API keys.
- Do not attach live SQLite databases unless a private channel has been agreed.
- Prefer sanitized payload samples that preserve field names but replace values.
- Note whether the server is exposed directly, behind a reverse proxy, or behind
  Cloudflare Access.

## Response Expectations

Reports will be triaged for severity, reproducibility, and affected versions.
Accepted vulnerabilities should be fixed on a private branch or coordinated PR
when practical, then released with appropriate notes. Public disclosure should
wait until a fix or mitigation is available unless there is active exploitation
or a clear user-protection reason to disclose sooner.
