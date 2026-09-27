# Provenance

This library is a reimplementation of the local Home Connect WebSocket protocol, written to replace the `home-disconnect` 1.x line. 1.x is a fork of [chris-mc1/homeconnect_websocket](https://github.com/chris-mc1/homeconnect_websocket), which has no license ([upstream issue #69](https://github.com/chris-mc1/homeconnect_websocket/issues/69)), so its code can't be used here. This file records where everything in this repository comes from, so that the MIT license on it holds up.

## Rules

- **No code from homeconnect_websocket or home-disconnect 1.x.** Not copied, not edited, not translated line by line. That includes its tests and its protocol document (`doc/Home_Connect_Protocol.md`). Protocol facts (message format, resource names, the encryption schemes, the handshake order) aren't copyrightable and can be implemented from any source; the text and code describing them are.
- **hcpy** (MIT) may be used, with its notice in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and an entry in the table below.
- **Code moved from Home Connect Local** (MIT) may be used the same way.
- **openHAB's Home Connect Direct binding** (EPL-2.0) is reference only. Don't copy from it.
- Everything else is written here, from the protocol as observed on real appliances or from the sources above.

A note on honesty: the people writing this have read homeconnect_websocket closely while maintaining 1.x, so this isn't a strict clean-room implementation. The rules above are how it stays a genuine rewrite rather than a copy.

## Where each part comes from

| Part | Source | Notes |
| --- | --- | --- |
| Packaging, CI, publishing | Written for this repository | |
| `errors.py` | Written for this repository | |
| `crypto.py` (AES scheme) | Scheme from hcpy's `HCSocket.py` ([hcpy2-0/hcpy](https://github.com/hcpy2-0/hcpy)); code written here | Key derivation, CBC chaining, padding and MAC chaining follow hcpy. Implemented with `cryptography` and the standard library instead of pycryptodome. Checked byte for byte against hcpy's `encrypt`/`decrypt` with the same padding bytes. |
| `transport.py` (WebSocket, TLS-PSK) | Parameters from hcpy's `HCSocket.py`; code written here | URLs, ports, PSK identity `HCCOM_Local_App`, TLS 1.2 and the `PSK` cipher string come from hcpy's native Python 3.13 TLS-PSK path. The async aiohttp connection is new. |
| `messages.py` (message envelope, request matching) | Message format from hcpy's `HCDevice.py` ([hcpy2-0/hcpy](https://github.com/hcpy2-0/hcpy)); code written here | Field names, actions, where the session and message IDs come from, and that responses echo the request's `msgID` follow hcpy's `get`/`reply`/`handle_message`. The dataclass, parsing and `RequestTracker` are new. Checked live against a real appliance and the simulator. |
| `session.py` (handshake, receive loop, reconnect) | Handshake order from hcpy's `HCDevice.py` ([hcpy2-0/hcpy](https://github.com/hcpy2-0/hcpy)); code written here | The `/ei/initialValues` reply, `/ci/services`, `iz` vs `/ci/authentication` + `/ci/info`, `/ei/deviceReady` and `/ni/info` follow hcpy's `handle_message`/`reconnect`. The async session, state machine, receive loop and backoff are new. Checked live against a real appliance (authentication path) and the simulator (`iz` path). |
| `profile.py` (DeviceDescription and FeatureMapping parser) | Written here from the XML files of real profiles | hcpy's `HCxml2json.py` was a reference for which sections exist (features, errors, enums); its code wasn't used. Nested lists, program groups and per-program options, enum subsets, the fallback to the feature mapping's enums (for Home Connect Local's exports) and the mismatch check come from reading eight real profiles (Thermador and Bosch dishwashers, oven, freezer, washer, washer-dryer). |
| `profile_files.py` (loading from ZIP, bytes or folder) | Written for this repository | The file layout follows the Home Connect Profile Downloader's output. |
| `entities.py` (entities, values, writes, programs) | Written for this repository | The write formats (`/ro/values` items with `uid` first; `/ro/selectedProgram` and `/ro/activeProgram` with `program` and `options`) and the boolean/enum value handling follow hcpy's `HCDevice.py`. The public names (`value`, `value_raw`, `enum`, `access`, `available`, `set_value`, `register_callback`, `Access.READ_WRITE`, `Execution.SELECT_AND_START`, ...) match how Home Connect Local uses home-disconnect 1.x, so the integration can switch with few changes; the implementation is new. Checked against live values from a real appliance and the simulator. |
| `appliance.py` (appliance: session + entities) | Written for this repository | Reading `/ro/allDescriptionChanges` and `/ro/allMandatoryValues` after connecting, and applying `/ro/values` and `/ro/descriptionChange` NOTIFYs, follow hcpy's `HCDevice.py`. Only reporting "connected" once that read has worked, and reconnecting when it fails, are new (prompted by Home Connect Local issue #119). |
| `__init__.py` (public API) | Written for this repository | |
| `legacy.py` (1.x stored description -> XML) | Home Connect Local's own code, by vemboy200 | A 1:1 copy of home-disconnect 1.x's `description_serializer.py`, which `git blame` attributes entirely to vemboy200 (231 of 231 lines), so its author relicenses it here. Only the function name, docstrings and type aliases changed. Its input is the dictionary shape home-disconnect 1.x's parser produced; that's a data format, and no 1.x code is used. |
| `build_profile_zip()`, `profile_filename_stub()` in `profile_files.py` | Home Connect Local's `export_profile.py`, by vemboy200 | Entirely vemboy200's in `git blame` (66 of 66 lines). Ported: it now writes the original XML instead of regenerating it, and takes the connection details as arguments instead of a Home Assistant config entry. |
| `tests/test_crypto.py`, `tests/test_transport.py`, `tests/test_messages.py`, `tests/test_session.py`, `tests/fake_appliance.py`, `tests/test_profile.py`, `tests/test_profile_files.py`, `tests/profile_fixtures.py`, `tests/test_entities.py`, `tests/test_appliance.py`, `tests/test_legacy.py` | Written for this repository | |

Add a row whenever a module lands, naming its source (written here, hcpy, or Home Connect Local) and, for the last two, the original file.
