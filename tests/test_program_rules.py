from typing import Any

import aiohttp
import pytest

from home_disconnect import Appliance
from home_disconnect.entities import AccessError, Entities, Execution, Option, Program
from home_disconnect.messages import Message
from home_disconnect.profile import parse_profile

DESCRIPTION = """<?xml version="1.0" encoding="UTF-8"?>
<device xmlns="http://www.home-connect.com/schemas/DeviceDescription/20140417">
  <description><type>Oven</type><brand>TEST</brand><model>OV1</model></description>
  <statusList access="read" available="true" uid="0102">
    <status access="read" available="true" refCID="01" refDID="00" uid="0300"/>
    <status access="read" available="true" refCID="10" refDID="82" uid="0301"/>
  </statusList>
  <optionList access="readWrite" available="true" uid="0106">
    <option access="readWrite" available="true" refCID="10" refDID="82" min="0" max="7200"
            uid="0228"/>
    <option access="readWrite" available="true" refCID="07" refDID="A4" min="30" max="250"
            uid="1400"/>
    <option access="readWrite" available="true" refCID="07" refDID="A4" min="30" max="100"
            uid="1401"/>
    <option access="readWrite" available="true" refCID="05" refDID="8B" uid="1402"/>
    <option access="readWrite" available="true" refCID="10" refDID="82" min="0" max="86400"
            uid="1403"/>
    <option access="read" available="true" refCID="10" refDID="82" uid="1404"/>
  </optionList>
  <programGroup available="true" uid="0107">
    <program available="true" execution="selectAndStart" uid="2001">
      <option access="readWrite" available="true" default="1800" max="3600" min="60"
              refUID="0228"/>
      <option access="readWrite" available="true" default="180" refUID="1400"/>
      <option access="readWrite" available="true" default="60" refUID="1401"/>
      <option access="readWrite" available="true" refUID="1402"/>
      <option access="readWrite" available="true" refUID="1403"/>
      <option access="read" available="true" refUID="1404"/>
    </program>
    <program available="true" execution="startOnly" uid="2002">
      <option access="readWrite" available="true" default="5400" refUID="0228"/>
    </program>
    <program available="true" execution="selectOnly" uid="2003">
      <option access="readWrite" available="true" default="200" refUID="1400"/>
    </program>
    <program available="true" execution="none" uid="2004"/>
  </programGroup>
  <activeProgram access="readWrite" uid="0100"/>
  <selectedProgram access="readWrite" fullOptionSet="false" uid="0101"/>
</device>
"""

FEATURE_MAPPING = """<?xml version="1.0" encoding="UTF-8"?>
<featureMappingFile xmlns="http://www.home-connect.com/schemas/FeatureMapping/20140417">
  <featureDescription>
    <feature refUID="0100">BSH.Common.Root.ActiveProgram</feature>
    <feature refUID="0101">BSH.Common.Root.SelectedProgram</feature>
    <feature refUID="0300">Cooking.Oven.Status.MeatprobePlugged</feature>
    <feature refUID="0301">BSH.Common.Status.ProgramProgress</feature>
    <feature refUID="0228">BSH.Common.Option.Duration</feature>
    <feature refUID="1400">Cooking.Oven.Option.SetpointTemperature</feature>
    <feature refUID="1401">Cooking.Oven.Option.MeatProbeTemperature</feature>
    <feature refUID="1402">ConsumerProducts.CoffeeMaker.Option.DisplayName</feature>
    <feature refUID="1403">BSH.Common.Option.FinishInRelative</feature>
    <feature refUID="1404">Test.Option.ReadOnly</feature>
    <feature refUID="2001">Cooking.Oven.Program.HeatingMode.Bake</feature>
    <feature refUID="2002">Cooking.Oven.Program.Cleaning</feature>
    <feature refUID="2003">Cooking.Oven.Program.Preheat</feature>
    <feature refUID="2004">Cooking.Oven.Program.Weird</feature>
  </featureDescription>
</featureMappingFile>
"""

DURATION, TEMPERATURE, PROBE, DISPLAY, FINISH_IN, READ_ONLY = (
    0x0228,
    0x1400,
    0x1401,
    0x1402,
    0x1403,
    0x1404,
)
BAKE, CLEAN, PREHEAT, WEIRD = 0x2001, 0x2002, 0x2003, 0x2004


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def __call__(self, message: Message) -> Message:
        self.sent.append((message.resource, message.data))
        return message.response()


def make_entities(
    recorder: Recorder, *, selected_flag: str = "false", active_flag: str | None = None
) -> Entities:
    description = DESCRIPTION.replace(
        'fullOptionSet="false" uid="0101"', f'fullOptionSet="{selected_flag}" uid="0101"'
    )
    if active_flag is not None:
        description = description.replace(
            '<activeProgram access="readWrite" uid="0100"/>',
            f'<activeProgram access="readWrite" fullOptionSet="{active_flag}" uid="0100"/>',
        )
    return Entities(parse_profile(description, FEATURE_MAPPING), recorder)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def entities(recorder: Recorder) -> Entities:
    return make_entities(recorder)


def program(entities: Entities, uid: int) -> Program:
    return entities.programs_by_uid[uid]


def option(entities: Entities, uid: int) -> Option:
    found = entities.by_uid[uid]
    assert isinstance(found, Option)
    return found


def test_writable_options(entities: Entities) -> None:
    bake = program(entities, BAKE)
    # Read-only left out; the meat probe too, since no probe is plugged in.
    assert [o.uid for o in bake.writable_options()] == [DURATION, TEMPERATURE, DISPLAY, FINISH_IN]
    # An option the appliance doesn't offer right now is left out.
    option(entities, DISPLAY).update({"available": False})
    assert DISPLAY not in [o.uid for o in bake.writable_options()]
    # With the probe plugged in, its setpoint can go in.
    entities.apply([{"uid": 0x0300, "value": True}])
    assert PROBE in [o.uid for o in bake.writable_options()]


def test_known_values_skip_unknown_and_out_of_range(entities: Entities) -> None:
    bake = program(entities, BAKE)
    assert bake.known_option_values() == {}
    entities.apply([{"uid": TEMPERATURE, "value": 200}, {"uid": DURATION, "value": 5000}])
    # 5000 s is valid for the option in general (max 7200) but not for Bake (max 3600).
    assert bake.known_option_values() == {TEMPERATURE: 200}
    entities.apply([{"uid": DURATION, "value": 1200}])
    assert bake.known_option_values() == {TEMPERATURE: 200, DURATION: 1200}


def test_full_values_fill_gaps_with_the_program_default_then_the_minimum(
    entities: Entities,
) -> None:
    bake = program(entities, BAKE)
    entities.apply([{"uid": DURATION, "value": 5000}])  # out of Bake's range
    assert bake.full_option_values() == {
        DURATION: 1800,  # Bake's own default replaces the out-of-range value
        TEMPERATURE: 180,  # Bake's default
        FINISH_IN: 0,  # no default: the option's minimum
        # DisplayName has no value, default or minimum: left out rather than sent as null.
    }


def test_locked(entities: Entities) -> None:
    duration = option(entities, DURATION)
    assert duration.lockable
    assert not duration.locked
    duration.update({"access": "read"})
    assert duration.locked
    duration.update({"access": "none"})  # not applicable, not locked
    assert not duration.locked
    progress = entities.by_uid[0x0301]
    progress.update({"access": "read"})
    assert not progress.lockable
    assert not progress.locked
    assert entities.selected_program is not None
    assert entities.active_program is not None
    assert entities.selected_program.lockable
    assert not entities.active_program.lockable


async def test_start_program_sends_known_values_plus_given_options(
    client_session: aiohttp.ClientSession, recorder: Recorder
) -> None:
    appliance = make_appliance(client_session, recorder)
    entities = appliance.entities
    entities.apply([{"uid": 0x0101, "value": BAKE}, {"uid": TEMPERATURE, "value": 200}])
    await appliance.start_program(options={"BSH.Common.Option.FinishInRelative": 3600})
    await appliance.start_program(include_current_options=False)
    assert recorder.sent == [
        (
            "/ro/activeProgram",
            [
                {
                    "program": BAKE,
                    "options": [
                        {"uid": TEMPERATURE, "value": 200},
                        {"uid": FINISH_IN, "value": 3600},
                    ],
                }
            ],
        ),
        ("/ro/activeProgram", [{"program": BAKE, "options": []}]),
    ]


async def test_start_program_sends_a_full_set_when_the_appliance_wants_one(
    client_session: aiohttp.ClientSession, recorder: Recorder
) -> None:
    appliance = make_appliance(client_session, recorder, active_flag="true")
    appliance.entities.apply([{"uid": 0x0101, "value": BAKE}])
    await appliance.start_program(include_current_options=False)
    (resource, data) = recorder.sent[0]
    assert resource == "/ro/activeProgram"
    assert {o["uid"]: o["value"] for o in data[0]["options"]} == {
        DURATION: 1800,
        TEMPERATURE: 180,
        FINISH_IN: 0,
    }


async def test_start_program_needs_a_program(
    client_session: aiohttp.ClientSession, recorder: Recorder
) -> None:
    appliance = make_appliance(client_session, recorder)
    with pytest.raises(AccessError, match="No program is selected"):
        await appliance.start_program()
    await appliance.start_program(program(appliance.entities, CLEAN))
    assert recorder.sent == [("/ro/activeProgram", [{"program": CLEAN, "options": []}])]


async def test_select_program_without_full_option_set(
    client_session: aiohttp.ClientSession, recorder: Recorder
) -> None:
    appliance = make_appliance(client_session, recorder)
    entities = appliance.entities
    entities.apply([{"uid": DURATION, "value": 4000}])
    await appliance.select_program(program(entities, BAKE))
    await appliance.select_program(program(entities, PREHEAT))
    await appliance.select_program(program(entities, CLEAN))
    assert recorder.sent == [
        # Selectable programs go without options (no stale values from another program).
        ("/ro/selectedProgram", [{"program": BAKE}]),
        ("/ro/selectedProgram", [{"program": PREHEAT}]),
        # A start-only program is started with its known values.
        (
            "/ro/activeProgram",
            [{"program": CLEAN, "options": [{"uid": DURATION, "value": 4000}]}],
        ),
    ]
    with pytest.raises(AccessError, match="can't be selected or started"):
        await appliance.select_program(program(entities, WEIRD))


async def test_select_program_refuses_while_the_selection_is_locked(
    client_session: aiohttp.ClientSession, recorder: Recorder
) -> None:
    appliance = make_appliance(client_session, recorder)
    appliance.entities.apply([{"uid": 0x0101, "access": "read"}])
    with pytest.raises(AccessError, match="isn't accepting a program selection"):
        await appliance.select_program(program(appliance.entities, BAKE))
    assert recorder.sent == []


async def test_select_program_with_full_option_set(
    client_session: aiohttp.ClientSession, recorder: Recorder
) -> None:
    appliance = make_appliance(client_session, recorder, selected_flag="true")
    entities = appliance.entities
    await appliance.select_program(program(entities, PREHEAT))
    await appliance.select_program(program(entities, BAKE))
    assert recorder.sent[0] == (
        "/ro/selectedProgram",
        [{"program": PREHEAT, "options": [{"uid": TEMPERATURE, "value": 200}]}],
    )
    # Select-and-start programs go to activeProgram on these appliances.
    assert recorder.sent[1][0] == "/ro/activeProgram"
    assert recorder.sent[1][1][0]["program"] == BAKE


async def test_active_only_flag_doesnt_turn_selecting_into_starting(
    client_session: aiohttp.ClientSession, recorder: Recorder
) -> None:
    # A Siemens EQ.9 coffee maker flags only its active program; selecting a beverage must
    # not brew it. Starting still sends the full set.
    appliance = make_appliance(client_session, recorder, active_flag="true")
    entities = appliance.entities
    await appliance.select_program(program(entities, BAKE))
    assert recorder.sent == [("/ro/selectedProgram", [{"program": BAKE}])]


@pytest.fixture
async def client_session() -> Any:  # noqa: ANN401
    async with aiohttp.ClientSession() as session:
        yield session


def make_appliance(
    client_session: aiohttp.ClientSession, recorder: Recorder, **flags: str
) -> Appliance:
    appliance = Appliance(
        client_session,
        "127.0.0.1",
        parse_profile(DESCRIPTION, FEATURE_MAPPING),
        "AAAA",
        app_name="Test",
        app_id="id",
    )
    # No connection needed: the rules only decide what to send.
    appliance.entities = make_entities(recorder, **flags)
    return appliance


def test_missing_execution_means_select_and_start(recorder: Recorder) -> None:
    description = DESCRIPTION.replace(
        '<program available="true" execution="selectAndStart" uid="2001">',
        '<program available="true" uid="2001">',
    )
    entities = Entities(parse_profile(description, FEATURE_MAPPING), recorder)
    bake = program(entities, BAKE)
    assert bake.execution is Execution.SELECT_AND_START
    assert bake.update({"execution": "startOnly"})
    assert bake.execution.value == "startonly"
    assert not bake.update({"execution": "startOnly"})
    assert not bake.update({"execution": "bogus"})
    assert bake.execution.value == "startonly"
