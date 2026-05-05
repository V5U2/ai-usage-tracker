# Deployment

Deployment helpers are split by runtime role:

- [Collector](collector/README.md): Linux systemd, WSL2 notes, macOS launchd,
  collector updates, collector release tarballs, and OTEL source setup.
- [Aggregation server](aggregation-server/README.md): Docker Compose, Unraid,
  Cloudflare Access, server image tags, and release workflows.

The collector runs beside AI clients and can forward compact usage events to an
aggregation server. The aggregation server runs once on a trusted host and
serves `/admin`, `/reports`, and `/tools`.
