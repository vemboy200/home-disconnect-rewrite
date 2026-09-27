import json
from collections.abc import Mapping
from typing import Any

from home_disconnect import parse_profile
from home_disconnect.legacy import serialize_legacy_description
from home_disconnect.profile import FeatureKind

# A description in the shape home-disconnect 1.x parsed it and Home Connect Local 1.x stored it.
LEGACY: dict[str, Any] = {
    "info": {"type": "Dishwasher", "brand": "TEST", "model": "DW100", "version": 2, "revision": 1},
    "status": [
        {
            "uid": 0x020F,
            "name": "BSH.Common.Status.DoorState",
            "access": "read",
            "available": True,
            "refCID": 3,
            "refDID": 0x80,
            "enumeration": {0: "Open", 1: "Closed"},
        },
        {
            "uid": 0x0212,
            "name": "BSH.Common.Status.ProgramProgress",
            "access": "read",
            "available": True,
            "min": "0",
            "max": "100",
        },
    ],
    "setting": [
        {
            "uid": 0x0219,
            "name": "BSH.Common.Setting.PowerState",
            "access": "readwrite",
            "available": True,
            "enumeration": {1: "Off", 2: "On"},
        }
    ],
    "event": [
        {
            "uid": 0x0231,
            "name": "Dishcare.Dishwasher.Event.SaltNearlyEmpty",
            "level": "alert",
            "handling": "acknowledge",
            "enumeration": {0: "Off", 1: "Present", 2: "Confirmed"},
        }
    ],
    "command": [{"uid": 0x0226, "name": "BSH.Common.Command.AbortProgram", "access": "writeonly"}],
    "option": [{"uid": 0x0228, "name": "BSH.Common.Option.Duration", "access": "readwrite"}],
    "program": [
        {
            "uid": 0x1001,
            "name": "Dishcare.Dishwasher.Program.Eco50",
            "available": True,
            "execution": "selectandstart",
            "options": [{"refUID": 0x0228, "access": "readwrite", "default": "60"}],
        }
    ],
    "activeProgram": {
        "uid": 0x0100,
        "name": "BSH.Common.Root.ActiveProgram",
        "access": "readwrite",
    },
    "selectedProgram": {
        "uid": 0x0101,
        "name": "BSH.Common.Root.SelectedProgram",
        "access": "readwrite",
        "fullOptionSet": True,
    },
}


def check(description: Mapping[str, Any]) -> None:
    profile = parse_profile(*serialize_legacy_description(description))
    assert profile.info.model == "DW100"
    assert profile.unnamed_uids == frozenset()
    door = profile.feature("BSH.Common.Status.DoorState")
    assert door is not None
    assert door.kind is FeatureKind.STATUS
    assert door.enumeration is not None
    assert door.enumeration.values == {0: "Open", 1: "Closed"}
    progress = profile.feature("BSH.Common.Status.ProgramProgress")
    assert progress is not None
    assert (progress.min, progress.max) == ("0", "100")
    salt = profile.feature("Dishcare.Dishwasher.Event.SaltNearlyEmpty")
    assert salt is not None
    assert salt.extra == {"level": "alert", "handling": "acknowledge"}
    selected = profile.feature("BSH.Common.Root.SelectedProgram")
    assert selected is not None
    assert selected.extra == {"fullOptionSet": "true"}
    eco = profile.programs[0x1001]
    assert eco.name == "Dishcare.Dishwasher.Program.Eco50"
    assert eco.options[0].name == "BSH.Common.Option.Duration"
    assert eco.options[0].default == "60"
    assert len(profile.features) == 8  # noqa: PLR2004


def test_legacy_description_round_trips() -> None:
    check(LEGACY)


def test_legacy_description_as_stored_in_json() -> None:
    # A config entry stores it as JSON, which turns the enum keys into strings.
    check(json.loads(json.dumps(LEGACY)))
