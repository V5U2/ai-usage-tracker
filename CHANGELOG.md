# Changelog

Historical entries before `0.4.1` were reconstructed from git tags, merge
commits, and merged pull request history after Release Please was introduced.

## [0.4.2](https://github.com/V5U2/ai-usage-tracker/compare/v0.4.1...v0.4.2) (2026-05-05)


### Bug Fixes

* label Claude Code local OTEL tool usage ([8a8214c](https://github.com/V5U2/ai-usage-tracker/commit/8a8214c57a6ba6c34c348d4c0e613325389fc60c))


### Documentation

* clarify that the aggregation server is optional for local collection and reports ([add1049](https://github.com/V5U2/ai-usage-tracker/commit/add10493a9346ebb1f4b14bb0e69198a079dd968))
* document native Linux systemd deployment for the aggregation server ([b109c09](https://github.com/V5U2/ai-usage-tracker/commit/b109c09a97f7a2ec17158be125bcebc1c6e80281))
* focus collector Linux instructions on Linux first, with WSL2 as a compatibility note ([fe9a7c3](https://github.com/V5U2/ai-usage-tracker/commit/fe9a7c356d7152431a4a1d6010b3b9648eb4383d))
* restructure the README around architecture, collector install, server install, telemetry setup, and usage ([027bf7f](https://github.com/V5U2/ai-usage-tracker/commit/027bf7f3e628364028c95615df546454359cdadd))
* split deployment documentation by runtime role under `deploy/collector` and `deploy/aggregation-server` ([8f43780](https://github.com/V5U2/ai-usage-tracker/commit/8f43780463844ad017656619e40ab17ea645a4c8))
* fill deployment documentation gaps for local-only collectors, Docker loopback publishing, Linux server updates, and config names ([eb25cc5](https://github.com/V5U2/ai-usage-tracker/commit/eb25cc58e7144eab98246a0ce56120e8e2b9d1cb))
* split the combined example TOML into `collector.example.toml` and `server.example.toml` ([1bde1e9](https://github.com/V5U2/ai-usage-tracker/commit/1bde1e94793c20498fd63807778aa75ac93dcdfe))
* license the project under MIT ([5faeeb5](https://github.com/V5U2/ai-usage-tracker/commit/5faeeb520d86b402584871ecc154d71b5f8fc95e))


### Build

* include `LICENSE` in collector release archives ([32a9f82](https://github.com/V5U2/ai-usage-tracker/commit/32a9f82227d974d9b5816aa246d6e8f8c1bc44ae))
* package the full aggregation-server deployment tree in collector release archives ([01dded0](https://github.com/V5U2/ai-usage-tracker/commit/01dded0bc12aa8550e3d95a30bc9a9f7c294d589))
* let local Docker builds inherit `APP_VERSION` while CI still injects release and edge versions ([60b18de](https://github.com/V5U2/ai-usage-tracker/commit/60b18de077fc985d19fc03454acda07817412f7f))

## [0.4.1](https://github.com/V5U2/ai-usage-tracker/compare/80b846cbb9a03a30b10ad15c919209eaa083da44...v0.4.1) (2026-05-05)


### Bug Fixes

* automate patch releases with Release Please ([eda6ce7](https://github.com/V5U2/ai-usage-tracker/commit/eda6ce7c3c429ec3f82fc5da308af8a6f771191e))
* bootstrap Release Please history window ([7aaee2b](https://github.com/V5U2/ai-usage-tracker/commit/7aaee2bab35ebc9f1eae5a2da104ada3b1cf9ffc))
* configure Release Please automation ([0d28398](https://github.com/V5U2/ai-usage-tracker/commit/0d28398004666308915b1b218dd13308b96f0267))
* preserve `latest` for stable release images and skip publishing when no release is created ([52101df](https://github.com/V5U2/ai-usage-tracker/commit/52101dfa7d295b49fe7c13ea58dddde6a96cf171))

## [0.4.0](https://github.com/V5U2/ai-usage-tracker/compare/v0.3.0...80b846cbb9a03a30b10ad15c919209eaa083da44) (2026-05-05)

`0.4.0` is the Release Please bootstrap baseline at commit `80b846c`; no
`v0.4.0` tag exists in the repository.


### Features

* add replay support that refreshes retained OpenRouter Broadcast rows after parser changes ([600d48f](https://github.com/V5U2/ai-usage-tracker/commit/600d48ff9dd7f212fe82b48e5e609b344e98ade2))
* add opt-in OpenAI API cost estimation from token counts ([22e1b91](https://github.com/V5U2/ai-usage-tracker/commit/22e1b91916d87978af2c4458b0dfa541ebef1d19))
* add opt-in Claude API cost estimation from token counts ([2cf461a](https://github.com/V5U2/ai-usage-tracker/commit/2cf461a14b088399e4ce42728f8cc12bd90fe8e8))
* add CLI and web UI version reporting ([d06c627](https://github.com/V5U2/ai-usage-tracker/commit/d06c6270680a34e096c0b7de0b47f2ac88223966))
* allow collectors to force historical resync with the aggregation server ([df518ff](https://github.com/V5U2/ai-usage-tracker/commit/df518ffc94273163e4f8472b1e79c238cf590232))
* allow report-only OpenRouter credit-to-USD normalization ([6c29680](https://github.com/V5U2/ai-usage-tracker/commit/6c29680ffab55bf42046513283f7367b625cd110))


### Bug Fixes

* deduplicate Claude Code OTEL usage without dropping distinct metric points ([80b846c](https://github.com/V5U2/ai-usage-tracker/commit/80b846cbb9a03a30b10ad15c919209eaa083da44))
* keep Claude metric point deduplication precise ([dd78307](https://github.com/V5U2/ai-usage-tracker/commit/dd783074ab17d931cd3613c74dd90a3495b52d37))
* keep blank AI usage environment variables on defaults ([1bf40a1](https://github.com/V5U2/ai-usage-tracker/commit/1bf40a172cd87263968a496b11887f0ac4e18bc0))
* keep zero-cost rows from hiding aggregate cost ([8c2bce4](https://github.com/V5U2/ai-usage-tracker/commit/8c2bce45d22831b0a63c38be3233c31e6f142816))
* prevent unknown cost units from being labeled USD ([5b3fb89](https://github.com/V5U2/ai-usage-tracker/commit/5b3fb89d2ff0057ef59ca1d31e54a82a8dca34b5))
* preserve authoritative zero-cost telemetry ([5814080](https://github.com/V5U2/ai-usage-tracker/commit/5814080be513b1aa183d4bc2008ac9beccd014e0))
* refresh aggregate costs from collector resyncs ([fe10034](https://github.com/V5U2/ai-usage-tracker/commit/fe10034685725cd7bda6ab0e71d58838a173ccbb))
* show dashboard cost totals without hiding unit splits ([465add7](https://github.com/V5U2/ai-usage-tracker/commit/465add7194ec43773350187e35776845433d41a1))


### Documentation

* clarify Cloudflare Access guidance for OpenRouter Broadcast ([c19d25a](https://github.com/V5U2/ai-usage-tracker/commit/c19d25a11632864965e0c380df8d3e81e00cde99))
* clarify provider/source reporting across providers ([f8666cb](https://github.com/V5U2/ai-usage-tracker/commit/f8666cbdbfbe7e81ae5d0dd96dcb1774264426cd))
* document Claude telemetry setup ([367a6dd](https://github.com/V5U2/ai-usage-tracker/commit/367a6dd98b1341e0bb8f3bd0ab98dcbf4dc434f6))
* document vulnerability reporting policy ([fa89299](https://github.com/V5U2/ai-usage-tracker/commit/fa892992f042e2b2828c3ba53e412d7ff4548518))


### Build

* include `sqlite3` in the server Docker image ([e8ad421](https://github.com/V5U2/ai-usage-tracker/commit/e8ad4213c1e599d18675cc40a1293678fe76e980))
* keep release packaging aligned with the tracker package rename ([89bfc75](https://github.com/V5U2/ai-usage-tracker/commit/89bfc756ab99c03ff3e147e98873258dce4008b5))
* publish non-release Docker tags from CI ([5010771](https://github.com/V5U2/ai-usage-tracker/commit/5010771eb8e0b3391c6546a1187e6f1dacf9fbeb))

## [0.3.0](https://github.com/V5U2/ai-usage-tracker/compare/v0.2.0...v0.3.0) (2026-05-05)


### Features

* accept OpenRouter Broadcast usage directly at `POST /v1/traces` ([0d24ff6](https://github.com/V5U2/ai-usage-tracker/commit/0d24ff66284bb335bdaecc7a8b58e890a1795a84))
* improve usage UI clarity and safer collector defaults ([a0ec856](https://github.com/V5U2/ai-usage-tracker/commit/a0ec856df4bc2f607f38505c24497890af565473))
* persist container server config outside the image under `/data/server.toml` ([3bcc106](https://github.com/V5U2/ai-usage-tracker/commit/3bcc106332227fed0a1471504b1110a2e8571929))


### Bug Fixes

* keep OpenRouter cost units aligned with provider-reported credits ([fe4bec7](https://github.com/V5U2/ai-usage-tracker/commit/fe4bec7d6fde824b786bc9392c44ab091ae6c3c1))

## [0.2.0](https://github.com/V5U2/ai-usage-tracker/compare/v0.1.0...v0.2.0) (2026-05-04)


### Features

* add deployment helpers for aggregation servers and collectors ([cd0672b](https://github.com/V5U2/ai-usage-tracker/commit/cd0672bae1f9b3ae393d79fad76491e44715016d))
* forward tool-only telemetry during live collection ([015b81f](https://github.com/V5U2/ai-usage-tracker/commit/015b81ff6e10513ca62cdbaa7632766ea3a370d9))
* ship collector forwarding and release packaging ([8c9f448](https://github.com/V5U2/ai-usage-tracker/commit/8c9f4488d74a283dd03f69f5026221c5c2c4a3f3))


### Documentation

* clarify collector and aggregation config names ([a59f215](https://github.com/V5U2/ai-usage-tracker/commit/a59f215795b31241f3807ecb7f1707af953c3284))
* clarify collector and aggregation server roles ([7e2b6d7](https://github.com/V5U2/ai-usage-tracker/commit/7e2b6d77040b12c02c68d18e7498680643811971))

## [0.1.0](https://github.com/V5U2/ai-usage-tracker/releases/tag/v0.1.0) (2026-05-04)


### Features

* add initial local AI usage tracker with SQLite storage and reports ([4b00ebd](https://github.com/V5U2/ai-usage-tracker/commit/4b00ebd02c8715bc392f350fdc03dd050be97108))
* add storage configuration file support ([0bdf45e](https://github.com/V5U2/ai-usage-tracker/commit/0bdf45e88b8353e6e354a81cc0e593e33c07c099))
* add collector client naming support ([485784f](https://github.com/V5U2/ai-usage-tracker/commit/485784f62c83c702df8f49b42e008cce0c133c30))
* add central usage server and collector sync ([cf371f8](https://github.com/V5U2/ai-usage-tracker/commit/cf371f855d26cc7a9ec3301ccb5063934f477b99))
* track Codex tool usage centrally ([ab98d9e](https://github.com/V5U2/ai-usage-tracker/commit/ab98d9e8b7b858dea19ed2e32ef0feedee0cc064))


### Bug Fixes

* avoid common Unraid host port collisions ([f18ef66](https://github.com/V5U2/ai-usage-tracker/commit/f18ef6626679997107d2aea53fa3e23d9acac71f))
* improve collector retention and serve logging ([61773dc](https://github.com/V5U2/ai-usage-tracker/commit/61773dc695ce29864ae23433c5201ad81419ce67))
* use the Unraid Docker share for server data ([978cfe1](https://github.com/V5U2/ai-usage-tracker/commit/978cfe1a3d50a6a2698f1ec7b4643639d8489d8c))


### Documentation

* document WSL receiver autostart ([819742e](https://github.com/V5U2/ai-usage-tracker/commit/819742eace24dacaba14054f8bd2dd556f8119d9))
