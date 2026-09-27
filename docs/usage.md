# Usage guide

Everything below is `async` and runs on an `aiohttp.ClientSession` you own and pass in. The library never closes it, so an application like Home Assistant can share its own session.

- [Getting a profile](#getting-a-profile)
- [Connecting](#connecting)
- [Reading values](#reading-values)
- [Writing values](#writing-values)
- [Programs](#programs)
- [Callbacks](#callbacks)
- [Connection states and reconnecting](#connection-states-and-reconnecting)
- [The cloud connection](#the-cloud-connection)
- [Errors](#errors)
- [Exporting profiles](#exporting-profiles)
- [Lower-level pieces](#lower-level-pieces)

## Getting a profile

An appliance can only be reached with its **profile**: two XML files that describe its features (DeviceDescription and FeatureMapping), plus its local key (and, for AES appliances, an IV). There are two ways to get one.

### From files

The [Home Connect Profile Downloader](https://github.com/bruestel/homeconnect-profile-downloader) writes a ZIP with a `.json` (the key) and the two XML files per appliance. `load_profiles()` reads a ZIP file or a folder, and `load_profiles_from_zip()` reads ZIP bytes, e.g. an upload:

```python
from home_disconnect import load_profiles, load_profiles_from_zip

profiles = load_profiles("profiles.zip")  # or a folder
profiles = load_profiles_from_zip(uploaded_bytes)

for loaded in profiles:
    print(loaded.profile.info.brand, loaded.profile.info.model)
    print(loaded.connection.connection_type if loaded.connection else "no key")
```

These functions read files, so call them from an executor in async code.

Each result is a `LoadedProfile`:

- `profile`: the parsed profile (`DeviceProfile`),
- `connection`: the key, IV and type from the JSON (`None` for an XML pair without one),
- `description_xml` / `feature_mapping_xml`: the original files, to store or export unchanged.

Bad files are reported by name with a `ProfileError`: invalid XML, the wrong kind of file, a DeviceDescription and FeatureMapping from different appliances, a JSON with a missing or wrong-length key. When a folder has copies (`..._DeviceDescription copy.xml`), the exact file wins and a valid copy only stands in for a broken original. macOS `__MACOSX` junk is ignored.

### From a Home Connect account

`home_disconnect.account` signs in with the Home Connect app's own client (the same one the Profile Downloader uses) and fetches every appliance's profile and key:

```python
from home_disconnect import SignIn, fetch_profiles

sign_in = SignIn.start("EU")  # "EU", "NA" or "CN"
print(sign_in.authorize_url)  # open this and sign in

# The browser ends on an hcauth://... address it can't open; paste it back.
code = sign_in.code_from_redirect(pasted_url)
token = await sign_in.exchange(session, code)

profiles = await fetch_profiles(session, token.access_token, "EU")
```

`fetch_profiles()` returns the same `LoadedProfile` objects as the file loaders. Demo appliances are skipped, and so is an appliance whose profile can't be fetched, as long as at least one works.

## Connecting

```python
from home_disconnect import Appliance

appliance = Appliance(
    session,
    host,  # IP address or hostname
    loaded.profile,
    loaded.connection.psk64,
    loaded.connection.iv64,  # None for TLS appliances
    app_name="My app",
    app_id="my-app-1",  # a stable ID for your app
    on_connection_state=on_state,  # optional, see below
)
await appliance.connect()
...
await appliance.close()
```

`connect()` runs the handshake and then reads the appliance's full state before it returns. It raises if any of that fails, and leaves nothing connected in that case. Wrap it in `asyncio.timeout()` to bound how long it may take.

`appliance.info` holds what the appliance reports about itself (`deviceID`, `vib`, `eNumber`, `swVersion`, ...), updated on every connect.

## Reading values

Every feature of the profile is an entity:

```python
door = appliance.status["BSH.Common.Status.DoorState"]
door.value  # "Closed": enum values come as their names
door.value_raw  # 1: what the appliance sent
door.access  # Access.READ
door.available  # True

appliance.settings["BSH.Common.Setting.PowerState"].value  # "On"
appliance.get("BSH.Common.Option.RemainingProgramTime")  # any entity by name, or None
```

The collections: `status`, `settings`, `events`, `commands`, `options`, `programs`, and `appliance.entities` for everything (by name or UID).

- **Enums** come as names in `value` and as numbers in `value_raw`; `enum` has the whole mapping.
- **Booleans** are always `bool`, even when the appliance sends `0`/`1`.
- **Everything else** is the value the appliance sent.
- `min`, `max`, `step` and `default` come from the profile and update when the appliance changes them.
- **Access** is one of `Access.NONE`, `READ`, `READ_STATIC` (fixed values, like an oven cavity's size), `READ_WRITE` and `WRITE_ONLY`. Use `access.readable` / `access.writable` rather than comparing to one value.
- **Locked** entities (`entity.locked`): options, settings and the selected program turn read-only while they can't be changed (e.g. options while a program runs), rather than disappearing.

## Writing values

```python
await appliance.settings["BSH.Common.Setting.PowerState"].set_value("Off")  # enum by name
await appliance.settings["BSH.Common.Setting.ChildLock"].set_value(True)

# Several values in one message:
await appliance.set_values({
    "Cooking.Common.Setting.Lighting": True,
    "Cooking.Common.Setting.LightingBrightness": 80,
})
```

`set_value()` checks access and availability (`AccessError`), converts enum names, requires real booleans, and range-checks numbers (`InvalidValueError`) before anything is sent. `set_value_raw()` skips the checks.

Helpers for common writes:

```python
await appliance.set_datetime(datetime.now())  # the appliance's clock; naive local time
await appliance.set_start_in(3600)  # StartInRelative, in seconds
await appliance.set_finish_in(7200)  # FinishInRelative, with a fallback some dryers need
await appliance.commands["BSH.Common.Command.PauseProgram"].execute()
await appliance.events["Dishcare.Dishwasher.Event.SaltNearlyEmpty"].acknowledge()
```

## Programs

```python
program = appliance.programs["Dishcare.Dishwasher.Program.Eco50"]
program.available  # the appliance can take it right now
program.execution  # Execution.SELECT_AND_START, SELECT_ONLY or START_ONLY
program.options  # the option entities it takes

await appliance.select_program(program)
await appliance.start_program()  # the selected program
await appliance.start_program(options={"BSH.Common.Option.FinishInRelative": 3600})
```

`select_program()` and `start_program()` choose which options to send, following what different appliances need:

- Appliances that want every option with a program (`full_option_set`) get a complete set: known values, then the program's own defaults, then the options' minimums.
- A plain selection goes without options, so the appliance uses its own defaults instead of a value left over from another program.
- Starting sends the options' last known values, leaving out read-only options, options the appliance doesn't offer at the moment, values outside the program's own range, and a meat probe setpoint with no probe plugged in.

`program.select()` and `program.start()` send exactly the options you pass, for full control.

`appliance.selected_program` and `appliance.active_program` return the `Program` (or `None`); the underlying entities are `appliance.entities.selected_program` and `.active_program`.

## Callbacks

```python
async def door_changed(entity) -> None:
    print(entity.name, entity.value)

door.register_callback(door_changed)
door.unregister_callback(door_changed)
```

A callback runs once per message that changes the entity (value, access, availability or range), not for repeats. Programs have callbacks too (availability and execution changes). A callback that raises is logged and doesn't stop the others.

## Connection states and reconnecting

```python
from home_disconnect import ConnectionState

async def on_state(state: ConnectionState) -> None:
    print(state)  # CONNECTING, CONNECTED, RECONNECTING, DISCONNECTED, CLOSED
```

- `CONNECTED` is only reported once the appliance's state has been read, including after a reconnect. If that read fails after a reconnect, the connection is dropped and retried rather than showing old values as current.
- A dropped connection is retried with backoff, from 5 seconds doubling to 5 minutes, until it's back or `close()` is called. Pass `reconnect=False` to handle reconnecting yourself; the state then goes `DISCONNECTED`.
- A WebSocket ping goes out after 20 seconds without traffic, and a missing answer counts as a dropped connection.
- `appliance.connected` is `True` only in `CONNECTED`.

## The cloud connection

See [Disconnecting from the cloud](../README.md#disconnecting-from-the-cloud) in the README: `appliance.cloud_connection_allowed`, `appliance.cloud_connected` and `appliance.set_cloud_connection(allowed=...)`.

## Errors

Everything the library raises is a `HomeDisconnectError`:

| Error | When |
| --- | --- |
| `ConnectionFailedError` | The appliance can't be reached |
| `AuthenticationError` | It was reached but the key or IV is wrong (a `ConnectionFailedError` too) |
| `HandshakeError` | Connected, but the handshake failed |
| `AlreadyConnectedError` | `connect()` on something already connecting or connected |
| `ConnectionClosedError` | The connection dropped (has the close `code`) |
| `ResponseError` | The appliance answered a request with an error `code` |
| `AccessError` | A write isn't possible right now (read-only, unavailable, locked, missing feature) |
| `InvalidValueError` | A value the entity doesn't accept (also a `ValueError`) |
| `ProfileError` | A profile file is missing, invalid or doesn't match (has the `file`) |
| `AccountError` | Signing in or fetching profiles failed |

## Exporting profiles

```python
from home_disconnect import build_profile_zip, profile_filename_stub

stub = profile_filename_stub("THERMADOR", "DWHD660WFP")  # "thermador_DWHD660WFP"

# Safe to share: only the two XML files, no key, MAC or serial number.
safe = build_profile_zip(loaded.description_xml, loaded.feature_mapping_xml, stub=stub)

# Full: also the JSON with the key, so it can be loaded again.
full = build_profile_zip(
    loaded.description_xml, loaded.feature_mapping_xml,
    stub=stub, connection=loaded.connection,
    info={"brand": "THERMADOR", "vib": "DWHD660WFP", "type": "Dishwasher"},
)
```

Profiles stored by Home Connect Local 1.x (a parsed dictionary rather than XML) can be turned back into XML once with `serialize_legacy_description()`.

## Lower-level pieces

`Appliance` is built from smaller parts you can use on their own:

- `Session`: one connection with its handshake, `request()`/`send()` and reconnecting, without entities.
- `Transport`: the encrypted WebSocket alone, text in and out.
- `Message` / `Action`: the JSON messages.
- `parse_profile()`: the XML parser, without any file access.
- `Entities`: the entity collection for a profile, with any function that sends messages.
