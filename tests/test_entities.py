from typing import Any

import pytest

from home_disconnect.entities import (
    Access,
    AccessError,
    ActiveProgram,
    Command,
    Entities,
    Entity,
    Event,
    Execution,
    InvalidValueError,
    Option,
    Program,
    SelectedProgram,
    Setting,
    Status,
    parse_profile_value,
)
from home_disconnect.messages import Action, Message, ResponseError
from home_disconnect.profile import parse_profile

from .profile_fixtures import DESCRIPTION, FEATURE_MAPPING

DOOR = "BSH.Common.Status.DoorState"
PROGRESS = "BSH.Common.Status.ProgramProgress"
POWER = "BSH.Common.Setting.PowerState"
SALT = "Dishcare.Dishwasher.Event.SaltNearlyEmpty"
ABORT = "BSH.Common.Command.AbortProgram"
ACKNOWLEDGE = "BSH.Common.Command.AcknowledgeEvent"
REJECT = "BSH.Common.Command.RejectEvent"
DURATION = "BSH.Common.Option.Duration"
ECO = "Dishcare.Dishwasher.Program.Eco50"
QUICK = "Dishcare.Dishwasher.Program.Quick45"


class Recorder:
    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def __call__(self, message: Message) -> Message:
        self.sent.append(message)
        return message.response()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def entities(recorder: Recorder) -> Entities:
    return Entities(parse_profile(DESCRIPTION, FEATURE_MAPPING), recorder)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, None),
        ("12", 12),
        ("0.5", 0.5),
        ("false", False),
        ('"RNA"', "RNA"),
        ("NA", "NA"),
        ('{"x":1}', {"x": 1}),
    ],
)
def test_parse_profile_value(text: str | None, expected: object) -> None:
    assert parse_profile_value(text) == expected


def test_access_and_execution_parse_any_spelling() -> None:
    assert Access.parse("readWrite") is Access.READ_WRITE
    assert Access.parse("READ") is Access.READ
    assert Access.parse("writeOnly") is Access.WRITE_ONLY
    assert Access.parse("readStatic") is Access.READ_STATIC
    assert Access.parse("bogus") is None
    assert Access.parse(None) is None
    assert Access.READ_WRITE.writable
    assert not Access.READ.writable
    assert not Access.READ_STATIC.writable
    assert Access.READ_STATIC.readable
    assert Access.READ.readable
    assert not Access.WRITE_ONLY.readable
    assert not Access.NONE.readable
    assert Execution.parse("selectAndStart") is Execution.SELECT_AND_START
    assert Execution.parse("startOnly") is Execution.START_ONLY
    assert Execution.parse("bogus") is None


def test_entity_classes(entities: Entities) -> None:
    assert isinstance(entities[DOOR], Status)
    assert isinstance(entities[POWER], Setting)
    assert isinstance(entities[SALT], Event)
    assert isinstance(entities[ABORT], Command)
    assert isinstance(entities[DURATION], Option)
    assert isinstance(entities.active_program, ActiveProgram)
    assert isinstance(entities.selected_program, SelectedProgram)
    assert len(entities) == 10  # noqa: PLR2004
    assert set(entities.status) == {DOOR, PROGRESS}
    assert set(entities.settings) == {POWER}
    assert set(entities.events) == {SALT}
    assert set(entities.commands) == {ABORT, ACKNOWLEDGE, REJECT}
    assert set(entities.options) == {DURATION}
    assert entities.get("Not.There") is None


def test_initial_state_from_the_profile(entities: Entities) -> None:
    progress = entities[PROGRESS]
    assert progress.value == 0
    assert (progress.min, progress.max) == (0, 100)
    assert progress.access is Access.READ
    assert progress.available is True
    assert entities[DOOR].value is None


def test_enum_value_and_raw(entities: Entities) -> None:
    door = entities[DOOR]
    assert door.update({"uid": door.uid, "value": 1})
    assert door.value == "Closed"
    assert door.value_raw == 1
    door.update({"value": 7})
    assert door.value is None  # not a value the profile knows
    assert door.enum == {0: "Open", 1: "Closed"}


def test_boolean_values_are_bools(entities: Entities) -> None:
    abort = entities[ABORT]
    abort.update({"value": 1})
    assert abort.value is True
    abort.update({"value": "false"})
    assert abort.value is False


def test_description_change_updates_access_and_range(entities: Entities) -> None:
    progress = entities[PROGRESS]
    assert progress.update(
        {"access": "readWrite", "available": "false", "min": 5, "max": 50, "stepSize": 5}
    )
    assert progress.access is Access.READ_WRITE
    assert progress.available is False
    assert (progress.min, progress.max, progress.step) == (5, 50, 5)
    assert not progress.update({"access": "readWrite"})


def test_apply_returns_what_changed_and_skips_unknown(entities: Entities) -> None:
    door, power = entities[DOOR], entities[POWER]
    changed = entities.apply(
        [
            {"uid": door.uid, "value": 0},
            {"uid": power.uid, "value": 2},
            {"uid": power.uid, "value": 2},
            {"uid": 0x9999, "value": 1},
            {"value": 1},
            {"uid": "bad"},
        ]
    )
    assert changed == [door, power]
    assert power.value == "On"
    assert entities.apply([{"uid": power.uid, "value": 2}]) == []


def test_program_availability_through_apply(entities: Entities) -> None:
    eco = entities.programs[ECO]
    assert entities.apply([{"uid": eco.uid, "available": False}]) == [eco]
    assert eco.available is False
    assert not eco.update({"access": "read"})


async def test_callbacks(entities: Entities) -> None:
    door = entities[DOOR]
    seen: list[Entity] = []

    async def callback(entity: Entity) -> None:
        seen.append(entity)

    async def broken(entity: Entity) -> None:
        raise RuntimeError(entity.name)

    door.register_callback(broken)
    door.register_callback(callback)
    await door.run_callbacks()
    assert seen == [door]
    door.unregister_callback(callback)
    door.unregister_callback(callback)
    await door.run_callbacks()
    assert seen == [door]


async def test_set_enum_by_name_or_number(entities: Entities, recorder: Recorder) -> None:
    power = entities[POWER]
    await power.set_value("On")
    await power.set_value(10)
    assert [m.data for m in recorder.sent] == [
        [{"uid": power.uid, "value": 2}],
        [{"uid": power.uid, "value": 10}],
    ]
    assert recorder.sent[0].resource == "/ro/values"
    assert recorder.sent[0].action is Action.POST
    assert list(recorder.sent[0].data[0]) == ["uid", "value"]  # type: ignore[index]


@pytest.mark.parametrize("value", ["Sideways", 5, True, None])
async def test_invalid_enum_values_are_rejected(entities: Entities, value: Any) -> None:  # noqa: ANN401
    with pytest.raises(InvalidValueError):
        await entities[POWER].set_value(value)


async def test_read_only_and_unavailable_entities_refuse_writes(entities: Entities) -> None:
    with pytest.raises(AccessError, match="isn't writable"):
        await entities[DOOR].set_value("Open")
    entities[DOOR].update({"access": "readStatic"})
    with pytest.raises(AccessError, match="isn't writable"):
        await entities[DOOR].set_value("Open")
    power = entities[POWER]
    power.update({"available": False})
    with pytest.raises(AccessError, match="isn't available"):
        await power.set_value("On")
    power.update({"available": True, "access": "read"})
    with pytest.raises(AccessError):
        await power.set_value("On")


async def test_numbers_are_range_checked(entities: Entities, recorder: Recorder) -> None:
    progress = entities[PROGRESS]
    progress.update({"access": "readWrite"})
    await progress.set_value(100)
    with pytest.raises(InvalidValueError, match="above the maximum"):
        await progress.set_value(101)
    with pytest.raises(InvalidValueError, match="below the minimum"):
        await progress.set_value(-1)
    assert len(recorder.sent) == 1


async def test_booleans_must_be_bools(entities: Entities, recorder: Recorder) -> None:
    abort = entities[ABORT]
    assert isinstance(abort, Command)
    await abort.execute()
    assert recorder.sent[-1].data == [{"uid": abort.uid, "value": True}]
    with pytest.raises(InvalidValueError):
        await abort.set_value(1)


async def test_set_value_raw_skips_checks(entities: Entities, recorder: Recorder) -> None:
    door = entities[DOOR]
    await door.set_value_raw(99)
    assert recorder.sent[-1].data == [{"uid": door.uid, "value": 99}]


def test_program_details(entities: Entities) -> None:
    eco, quick = entities.programs[ECO], entities.programs[QUICK]
    assert isinstance(eco, Program)
    assert eco.execution is Execution.SELECT_AND_START
    assert quick.execution is Execution.START_ONLY
    assert quick.available is False
    assert [o.name for o in eco.options] == [DURATION]
    assert eco.option_settings[0].default == "60"
    assert quick.options == []


async def test_select_and_start(entities: Entities, recorder: Recorder) -> None:
    eco = entities.programs[ECO]
    duration = entities[DURATION]
    duration.update({"min": 30, "max": 120})
    await eco.select()
    await eco.start({DURATION: 90})
    await eco.start({duration: 60, duration.uid: 120})
    assert [(m.resource, m.data) for m in recorder.sent] == [
        ("/ro/selectedProgram", [{"program": eco.uid}]),
        (
            "/ro/activeProgram",
            [{"program": eco.uid, "options": [{"uid": duration.uid, "value": 90}]}],
        ),
        (
            "/ro/activeProgram",
            [
                {
                    "program": eco.uid,
                    # The same option given twice: the last value wins.
                    "options": [{"uid": duration.uid, "value": 120}],
                }
            ],
        ),
    ]


async def test_program_options_are_checked(entities: Entities) -> None:
    eco = entities.programs[ECO]
    entities[DURATION].update({"min": 30, "max": 120})
    with pytest.raises(InvalidValueError, match="isn't an option"):
        await eco.start({POWER: "On"})
    with pytest.raises(InvalidValueError, match="isn't an option"):
        await eco.start({"Not.There": 1})
    with pytest.raises(InvalidValueError, match="above the maximum"):
        await eco.start({DURATION: 500})


def test_selected_and_active_program(entities: Entities) -> None:
    selected = entities.selected_program
    active = entities.active_program
    assert selected is not None
    assert active is not None
    eco = entities.programs[ECO]
    selected.update({"value": eco.uid})
    assert selected.program is eco
    assert selected.value == ECO
    assert selected.full_option_set is True
    active.update({"value": 0})
    assert active.program is None
    assert active.value is None
    assert active.full_option_set is False


async def test_program_callbacks(entities: Entities) -> None:
    eco = entities.programs[ECO]
    seen: list[object] = []

    async def callback(program: object) -> None:
        seen.append(program)

    eco.register_callback(callback)
    await eco.run_callbacks()
    eco.unregister_callback(callback)
    await eco.run_callbacks()
    assert seen == [eco]


def test_dump(entities: Entities) -> None:
    entities[DOOR].update({"value": 1})
    dump = entities.dump()
    assert dump["entities"][DOOR]["value"] == "Closed"
    assert dump["entities"][DOOR]["type"] == "Status"
    assert dump["programs"][ECO]["execution"] is Execution.SELECT_AND_START
    assert repr(entities[DOOR]).startswith("<Status BSH.Common.Status.DoorState")


async def test_value_shadow_follows_reports_and_successful_writes(entities: Entities) -> None:
    duration = entities[DURATION]
    assert duration.value_shadow is None
    await duration.set_value(45)
    # The appliance didn't report it back, but the shadow remembers what was written.
    assert duration.value_raw is None
    assert duration.value_shadow == 45  # noqa: PLR2004
    duration.update({"value": 60})
    assert duration.value_shadow == 60  # noqa: PLR2004
    assert entities.dump()["entities"][DURATION]["value_shadow"] == 60  # noqa: PLR2004


async def test_failed_write_leaves_the_shadow_alone(entities: Entities) -> None:
    async def refuse(message: Message) -> Message:
        raise ResponseError(400, message.resource)

    refusing = Entities(parse_profile(DESCRIPTION, FEATURE_MAPPING), refuse)
    duration = refusing[DURATION]
    with pytest.raises(ResponseError):
        await duration.set_value(45)
    assert duration.value_shadow is None
    assert entities[DURATION].value_shadow is None


async def test_acknowledge_and_reject_events(entities: Entities, recorder: Recorder) -> None:
    salt = entities[SALT]
    assert isinstance(salt, Event)
    await salt.acknowledge()
    await salt.reject()
    assert [m.data for m in recorder.sent] == [
        [{"uid": entities[ACKNOWLEDGE].uid, "value": salt.uid}],
        [{"uid": entities[REJECT].uid, "value": salt.uid}],
    ]


async def test_acknowledge_without_the_command(recorder: Recorder) -> None:
    description = DESCRIPTION.replace('uid="0006"', 'uid="0906"')
    entities = Entities(parse_profile(description, FEATURE_MAPPING), recorder)
    salt = entities[SALT]
    assert isinstance(salt, Event)
    with pytest.raises(AccessError, match=r"no BSH\.Common\.Command\.AcknowledgeEvent"):
        await salt.acknowledge()


def test_full_option_set(recorder: Recorder) -> None:
    # The fixture has fullOptionSet="true" on the selected program only.
    entities = Entities(parse_profile(DESCRIPTION, FEATURE_MAPPING), recorder)
    assert entities.full_option_set
    assert entities.programs[ECO].full_option_set  # appliance-wide
    assert not entities.programs[QUICK].full_option_set  # the program's own flag wins

    only_active = DESCRIPTION.replace(
        '<selectedProgram access="readWrite" fullOptionSet="true"',
        '<selectedProgram access="readWrite" fullOptionSet="false"',
    ).replace(
        '<activeProgram access="readWrite"',
        '<activeProgram access="readWrite" fullOptionSet="true"',
    )
    assert Entities(parse_profile(only_active, FEATURE_MAPPING), recorder).full_option_set

    neither = DESCRIPTION.replace('fullOptionSet="true"', 'fullOptionSet="false"')
    entities = Entities(parse_profile(neither, FEATURE_MAPPING), recorder)
    assert not entities.full_option_set
    assert not entities.programs[ECO].full_option_set
