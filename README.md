# home-disconnect

Talk to Home Connect appliances (Bosch, Siemens, Thermador, Neff, Gaggenau and the other BSH brands) directly over your local network, without the Home Connect cloud. It's the library behind the [Home Connect Local](https://github.com/vemboy200/homeconnect_local_hass) Home Assistant integration.

The name is the point: once you have an appliance's profile, everything runs locally, and you can even tell the appliance to stop talking to BSH's servers altogether (see [Disconnecting from the cloud](#disconnecting-from-the-cloud)).

> [!NOTE]
> This repository is the MIT-licensed rewrite that becomes `home-disconnect` 2.0.0. The `home-disconnect` 1.x releases are a fork of [homeconnect_websocket](https://github.com/chris-mc1/homeconnect_websocket), which has no license, so they can't carry one. Once the rewrite is complete, this repository takes over the `home-disconnect` name. See [PROVENANCE.md](PROVENANCE.md) for where every part comes from.

## What it does

- **Connects** to an appliance over its local encrypted WebSocket, in both schemes appliances use: TLS-PSK (port 443) and AES (port 80).
- **Keeps the connection up**: handshake, heartbeat, automatic reconnect with backoff, and a full re-read of the appliance's state after every reconnect, so values never go stale.
- **Reads the appliance's profile** (the DeviceDescription and FeatureMapping files) from a ZIP, uploaded bytes, a folder, or straight from your Home Connect account.
- **Turns every feature into an entity** with a typed value, live access and availability, callbacks on change, and checked writes.
- **Selects and starts programs** with the right options, following the rules different appliances need.
- **Toggles the appliance's cloud connection**.

## Installation

```bash
pip install home-disconnect
```

Python 3.13 or newer (TLS-PSK needs the standard library support added in 3.13).

## Quick start

```python
import asyncio

import aiohttp

from home_disconnect import Appliance, load_profiles


async def main() -> None:
    # A profile from the Home Connect Profile Downloader (ZIP or folder).
    loaded = load_profiles("THERMADOR-DWHD660WFP-68A40E0FC8F1.zip")[0]
    connection = loaded.connection  # key, IV and connection type from the profile's JSON

    async with aiohttp.ClientSession() as session:
        appliance = Appliance(
            session,
            "192.168.1.130",  # the appliance's address
            loaded.profile,
            connection.psk64,
            connection.iv64,
            app_name="My app",
            app_id="my-app-1",
        )
        await appliance.connect()
        try:
            print(appliance.status["BSH.Common.Status.DoorState"].value)  # "Closed"
            print(appliance.selected_program.name if appliance.selected_program else None)
        finally:
            await appliance.close()


asyncio.run(main())
```

More in the [usage guide](docs/usage.md): getting a profile, reading and writing values, callbacks, programs, connection states, errors and exporting profiles.

## Disconnecting from the cloud

Most Home Connect appliances have a setting, `BSH.Common.Setting.AllowBackendConnection`, that decides whether they connect to BSH's servers at all. With it off, the appliance stays reachable on your network through this library, but stops reporting to, and taking commands from, the Home Connect cloud.

```python
print(appliance.cloud_connection_allowed)  # True
print(appliance.cloud_connected)  # True: connected to BSH's servers right now

await appliance.set_cloud_connection(allowed=False)  # disconnect from the cloud
await appliance.set_cloud_connection(allowed=True)  # and back
```

Things to know before you turn it off:

- **The official Home Connect app and anything else that goes through the cloud** (voice assistants, the cloud Home Assistant integration, other partner services) stop reaching the appliance.
- **Software updates** come from BSH's servers, so turn it back on now and then to get them.
- **Pairing a new appliance** still needs the cloud: the app pairs it, and the profile with its local key comes from your Home Connect account. Get the profile first, then disconnect.
- **You can always turn it back on**, from this library or from the appliance's own settings menu.

Appliances without the setting raise `AccessError` from `set_cloud_connection()`, and both properties return `None` for them.

## Contributing and licensing

The code is MIT-licensed ([LICENSE](LICENSE)). Parts are based on the MIT-licensed [hcpy](https://github.com/hcpy2-0/hcpy) and on code moved from Home Connect Local; their notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and [PROVENANCE.md](PROVENANCE.md) lists where each module comes from. Code from `homeconnect_websocket` (and so from `home-disconnect` 1.x) must not be copied here, including its tests and protocol document.

```bash
uv sync
uv run pytest
uv run ruff check
uv run mypy
```
