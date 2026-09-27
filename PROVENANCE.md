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

Add a row whenever a module lands, naming its source (written here, hcpy, or Home Connect Local) and, for the last two, the original file.
