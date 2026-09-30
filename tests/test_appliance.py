import asyncio
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest

from home_disconnect import session as session_module
from home_disconnect.appliance import Appliance
from home_disconnect.entities import AccessError, Entity
from home_disconnect.errors import AlreadyConnectedError, ConnectionFailedError
from home_disconnect.messages import ResponseError
from home_disconnect.profile import parse_profile
from home_disconnect.session import ConnectionState

from .fake_appliance import IV64, PSK64, SESSION_ID, FakeAppliance
from .profile_fixtures import DESCRIPTION, FEATURE_MAPPING

DOOR_UID = 0x020F
POWER_UID = 0x0219
PROGRESS_UID = 0x0212
ECO_UID = 0x1001
SELECTED_UID = 0x0101


def appliance_state(door: int = 1, power: int = 2) -> dict[str, dict[str, Any]]:
    return {
        "/ro/allDescriptionChanges": {
            "data": [
                {"uid": POWER_UID, "access": "readWrite", "available": True},
                {"uid": ECO_UID, "available": True},
            ]
        },
        "/ro/allMandatoryValues": {
            "data": [
                {"uid": DOOR_UID, "value": door},
                {"uid": POWER_UID, "value": power},
                {"uid": SELECTED_UID, "value": ECO_UID},
            ]
        },
    }


@pytest.fixture
async def client_session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def fake() -> AsyncIterator[FakeAppliance]:
    appliance = FakeAppliance()
    appliance.responses.update(appliance_state())
    await appliance.start()
    yield appliance
    await appliance.stop()


def make_appliance(
    client_session: aiohttp.ClientSession,
    fake: FakeAppliance,
    **kwargs: Any,  # noqa: ANN401
) -> Appliance:
    assert fake.server is not None
    return Appliance(
        client_session,
        "127.0.0.1",
        parse_profile(DESCRIPTION, FEATURE_MAPPING),
        PSK64,
        IV64,
        app_name="Test",
        app_id="test-id",
        port=fake.server.port,
        **kwargs,
    )


async def notify(fake: FakeAppliance, resource: str, data: list[dict[str, Any]]) -> None:
    await fake.send(
        {
            "sID": SESSION_ID,
            "msgID": 5,
            "resource": resource,
            "version": 1,
            "action": "NOTIFY",
            "data": data,
        }
    )


async def test_connect_reads_the_full_state(
    client_session: aiohttp.ClientSession, fake: FakeAppliance
) -> None:
    appliance = make_appliance(client_session, fake, info={"deviceID": "stored"})
    await appliance.connect()
    try:
        assert appliance.connected
        assert appliance.get("BSH.Common.Status.DoorState").value == "Closed"  # type: ignore[union-attr]
        assert appliance.settings["BSH.Common.Setting.PowerState"].value == "On"
        assert appliance.selected_program is not None
        assert appliance.selected_program.name == "Dishcare.Dishwasher.Program.Eco50"
        assert appliance.entities.selected_program is not None
        assert appliance.entities.selected_program.value == "Dishcare.Dishwasher.Program.Eco50"
        assert appliance.info["vib"] == "TEST"  # from /ci/info, over the profile's model
        assert appliance.info["deviceID"] == "123"
        assert appliance.info["type"] == "Dishwasher"
        resources = [m["resource"] for m in fake.received]
        assert resources[-2:] == ["/ro/allDescriptionChanges", "/ro/allMandatoryValues"]
        assert set(appliance.status) == {
            "BSH.Common.Status.DoorState",
            "BSH.Common.Status.ProgramProgress",
        }
        assert set(appliance.programs) == {
            "Dishcare.Dishwasher.Program.Eco50",
            "Dishcare.Dishwasher.Program.Quick45",
        }
        assert appliance.events
        assert appliance.commands
        assert appliance.options
        assert appliance.active_program is None  # nothing running
    finally:
        await appliance.close()
    assert appliance.state is ConnectionState.CLOSED


async def test_notifications_update_entities_and_run_callbacks(
    client_session: aiohttp.ClientSession, fake: FakeAppliance
) -> None:
    appliance = make_appliance(client_session, fake)
    await appliance.connect()
    changed: asyncio.Queue[Entity] = asyncio.Queue()

    async def callback(entity: Entity) -> None:
        await changed.put(entity)

    door = appliance.status["BSH.Common.Status.DoorState"]
    door.register_callback(callback)
    progress = appliance.status["BSH.Common.Status.ProgramProgress"]
    progress.register_callback(callback)
    try:
        await notify(
            fake, "/ro/values", [{"uid": DOOR_UID, "value": 0}, {"uid": 0x9999, "value": 1}]
        )
        assert await asyncio.wait_for(changed.get(), 5) is door
        assert door.value == "Open"
        await notify(fake, "/ro/descriptionChange", [{"uid": PROGRESS_UID, "available": False}])
        assert await asyncio.wait_for(changed.get(), 5) is progress
        assert progress.available is False
        # An unchanged value runs no callback.
        await notify(fake, "/ro/values", [{"uid": DOOR_UID, "value": 0}])
        await notify(fake, "/ci/registeredDevices", [{"deviceID": "x"}])
        await notify(fake, "/ro/values", [{"uid": DOOR_UID, "value": 1}])
        assert await asyncio.wait_for(changed.get(), 5) is door
        assert changed.empty()
    finally:
        await appliance.close()


async def test_writes_go_to_the_appliance(
    client_session: aiohttp.ClientSession, fake: FakeAppliance
) -> None:
    fake.responses["/ro/values"] = {}
    fake.responses["/ro/activeProgram"] = {}
    appliance = make_appliance(client_session, fake)
    await appliance.connect()
    try:
        await appliance.settings["BSH.Common.Setting.PowerState"].set_value("Off")
        await appliance.programs["Dishcare.Dishwasher.Program.Eco50"].start()
        posts = [(m["resource"], m.get("data")) for m in fake.received if m["action"] == "POST"]
        assert posts == [
            ("/ro/values", [{"uid": POWER_UID, "value": 1}]),
            ("/ro/activeProgram", [{"program": ECO_UID}]),
        ]
        fake.responses["/ro/values"] = {"code": 403}
        with pytest.raises(ResponseError):
            await appliance.settings["BSH.Common.Setting.PowerState"].set_value("On")
    finally:
        await appliance.close()


async def test_reconnect_reads_what_changed_while_disconnected(
    client_session: aiohttp.ClientSession,
    fake: FakeAppliance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fork issue #119: a change made while the connection was down must show after reconnecting.
    monkeypatch.setattr(session_module, "RECONNECT_INITIAL_DELAY", 0.05)
    states: list[ConnectionState] = []
    reconnected = asyncio.Event()

    async def on_state(state: ConnectionState) -> None:
        states.append(state)
        if state is ConnectionState.CONNECTED and ConnectionState.RECONNECTING in states:
            reconnected.set()

    appliance = make_appliance(client_session, fake, on_connection_state=on_state)
    await appliance.connect()
    door = appliance.status["BSH.Common.Status.DoorState"]
    power = appliance.settings["BSH.Common.Setting.PowerState"]
    assert (door.value, power.value) == ("Closed", "On")
    try:
        fake.responses.update(appliance_state(door=0, power=1))
        await fake.drop()
        await asyncio.wait_for(reconnected.wait(), 5)
        assert (door.value, power.value) == ("Open", "Off")
        assert states == [
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.RECONNECTING,
            ConnectionState.CONNECTED,
        ]
    finally:
        await appliance.close()


async def test_failed_refresh_after_reconnect_retries_instead_of_showing_stale_state(
    client_session: aiohttp.ClientSession,
    fake: FakeAppliance,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(session_module, "RECONNECT_INITIAL_DELAY", 0.05)
    states: list[ConnectionState] = []
    reconnected = asyncio.Event()

    async def on_state(state: ConnectionState) -> None:
        states.append(state)
        if state is ConnectionState.CONNECTED and ConnectionState.RECONNECTING in states:
            reconnected.set()

    appliance = make_appliance(client_session, fake, on_connection_state=on_state)
    await appliance.connect()
    try:
        # The appliance comes back but refuses to report its values at first.
        fake.responses["/ro/allMandatoryValues"] = {"code": 503}
        await fake.drop()

        # The refused attempt, then one more.
        await asyncio.wait_for(fake.wait_for_connections(3), 5)
        assert not appliance.connected
        assert states[-1] is ConnectionState.RECONNECTING
        assert "couldn't read its state (error 503)" in caplog.text
        fake.responses.update(appliance_state(door=0))
        await asyncio.wait_for(reconnected.wait(), 5)
        assert appliance.status["BSH.Common.Status.DoorState"].value == "Open"
        assert states.count(ConnectionState.CONNECTED) == 2  # noqa: PLR2004
    finally:
        await appliance.close()


async def test_failed_first_read_fails_connect(
    client_session: aiohttp.ClientSession, fake: FakeAppliance
) -> None:
    fake.responses["/ro/allMandatoryValues"] = {"code": 400}
    states: list[ConnectionState] = []

    async def on_state(state: ConnectionState) -> None:
        states.append(state)

    appliance = make_appliance(client_session, fake, on_connection_state=on_state)
    with pytest.raises(ResponseError):
        await appliance.connect()
    assert states == [ConnectionState.CONNECTING, ConnectionState.DISCONNECTED]
    assert not appliance.connected


async def test_unreachable_appliance(client_session: aiohttp.ClientSession) -> None:
    fake = FakeAppliance()
    await fake.start()
    await fake.stop()
    appliance = make_appliance(client_session, fake)
    with pytest.raises(ConnectionFailedError):
        await appliance.connect()
    assert appliance.state is ConnectionState.DISCONNECTED


async def test_manual_refresh_and_dump(
    client_session: aiohttp.ClientSession, fake: FakeAppliance
) -> None:
    appliance = make_appliance(client_session, fake)
    await appliance.connect()
    try:
        fake.responses.update(appliance_state(power=1))
        await appliance.refresh()
        assert appliance.settings["BSH.Common.Setting.PowerState"].value == "Off"
        dump = appliance.dump()
        assert dump["state"] is ConnectionState.CONNECTED
        assert dump["service_versions"]["ro"] == 1
        assert dump["entities"]["BSH.Common.Setting.PowerState"]["value"] == "Off"
        assert repr(appliance) == "<Appliance TEST connected>"
    finally:
        await appliance.close()


async def test_state_callback_errors_are_contained(
    client_session: aiohttp.ClientSession, fake: FakeAppliance
) -> None:
    async def broken(state: ConnectionState) -> None:
        raise RuntimeError(state)

    appliance = make_appliance(client_session, fake, on_connection_state=broken)
    await appliance.connect()
    assert appliance.connected
    await appliance.close()


async def test_connect_twice_raises_without_dropping_the_connection(
    client_session: aiohttp.ClientSession, fake: FakeAppliance
) -> None:
    appliance = make_appliance(client_session, fake)
    await appliance.connect()
    try:
        with pytest.raises(AlreadyConnectedError):
            await appliance.connect()
        assert appliance.connected
        assert fake.connections == 1
    finally:
        await appliance.close()


async def test_get_network_info(client_session: aiohttp.ClientSession, fake: FakeAppliance) -> None:
    appliance = make_appliance(client_session, fake)
    await appliance.connect()
    try:
        fake.responses["/ni/info"] = {"data": [{"interfaceID": 0, "rssi": -61}]}
        assert await appliance.get_network_info() == [{"interfaceID": 0, "rssi": -61}]
        assert appliance.session.network_info == [{"interfaceID": 0, "rssi": -61}]
        appliance.session.service_versions.pop("ni")
        with pytest.raises(AccessError, match="no network information"):
            await appliance.get_network_info()
    finally:
        await appliance.close()


async def test_retry_now_reconnects_the_appliance(
    client_session: aiohttp.ClientSession,
    fake: FakeAppliance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "RECONNECT_INITIAL_DELAY", 60)
    reconnecting = asyncio.Event()
    reconnected = asyncio.Event()

    async def on_state(state: ConnectionState) -> None:
        if state is ConnectionState.RECONNECTING:
            reconnecting.set()
        elif state is ConnectionState.CONNECTED and reconnecting.is_set():
            reconnected.set()

    appliance = make_appliance(client_session, fake, on_connection_state=on_state)
    await appliance.connect()
    try:
        await fake.drop()
        await asyncio.wait_for(reconnecting.wait(), 5)
        appliance.retry_now()
        await asyncio.wait_for(reconnected.wait(), 5)
        assert appliance.connected
    finally:
        await appliance.close()
