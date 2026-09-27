# home-disconnect (rewrite)

A local WebSocket client for Home Connect appliances (Bosch, Siemens, Thermador, Neff, Gaggenau and other BSH brands), used by the [Home Connect Local](https://github.com/vemboy200/homeconnect_local_hass) Home Assistant integration.

This repository is the MIT-licensed rewrite that will become `home-disconnect` 2.0.0. The current `home-disconnect` 1.x is a fork of [homeconnect_websocket](https://github.com/chris-mc1/homeconnect_websocket), which has no license, so it can't carry one either. Once the rewrite is complete, this repository takes over the `home-disconnect` name.

Parts are based on the MIT-licensed [hcpy](https://github.com/hcpy2-0/hcpy). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the notices and [PROVENANCE.md](PROVENANCE.md) for where each part comes from.

**Status:** early development, nothing usable yet.

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run mypy
```
