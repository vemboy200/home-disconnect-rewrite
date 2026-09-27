import asyncio
from collections.abc import AsyncIterator

import aiohttp
import pytest

from home_disconnect import session as session_module
from home_disconnect.errors import (
    AlreadyConnectedError,
    AuthenticationError,
    ConnectionClosedError,
    ConnectionFailedError,
)
from home_disconnect.messages import Action, Message, ResponseError
from home_disconnect.session import ConnectionState, HandshakeError, Session

from .fake_appliance import FIRST_MESSAGE_ID, IV64, PSK64, SESSION_ID, FakeAppliance


@pytest.fixture
async def client_session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def appliance() -> AsyncIterator[FakeAppliance]:
    fake = FakeAppliance()
    await fake.start()
    yield fake
    await fake.stop()


def make_session(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance, **kwargs: object
) -> Session:
    assert appliance.server is not None
    return Session(
        client_session,
        "127.0.0.1",
        PSK64,
        IV64,
        app_name="Test",
        app_id="test-id",
        port=appliance.server.port,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_handshake_without_iz(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    session = make_session(client_session, appliance)
    await session.connect()
    try:
        assert session.connected
        assert session.service_versions == {"ci": 2, "ei": 2, "ni": 1, "ro": 1}
        assert session.device_info == {"deviceID": "123", "vib": "TEST"}
        assert session.network_info[0]["interfaceID"] == 0
        resources = [(m["resource"], m["action"]) for m in appliance.received]
        assert resources == [
            ("/ei/initialValues", "RESPONSE"),
            ("/ci/services", "GET"),
            ("/ci/authentication", "GET"),
            ("/ci/info", "GET"),
            ("/ei/deviceReady", "NOTIFY"),
            ("/ni/info", "GET"),
        ]
        reply = appliance.received[0]
        assert reply["sID"] == SESSION_ID
        assert reply["data"] == [
            {"deviceType": "Application", "deviceName": "Test", "deviceID": "test-id"}
        ]
        assert appliance.received[1]["msgID"] == FIRST_MESSAGE_ID
        assert appliance.received[2]["version"] == 2  # noqa: PLR2004
        assert "nonce" in appliance.received[2]["data"][0]
    finally:
        await session.close()
    assert session.state is ConnectionState.CLOSED


async def test_handshake_with_iz_skips_authentication(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    appliance.services = {"ci": 3, "ei": 2, "iz": 1, "ro": 1}
    session = make_session(client_session, appliance)
    await session.connect()
    try:
        resources = [m["resource"] for m in appliance.received]
        assert "/ci/authentication" not in resources
        assert "/ci/info" not in resources
        assert "/ni/info" not in resources
        assert session.device_info["vib"] == "IZTEST"
    finally:
        await session.close()


async def test_refused_ni_info_doesnt_fail_the_handshake(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    appliance.responses["/ni/info"] = {"code": 403}
    session = make_session(client_session, appliance)
    await session.connect()
    try:
        assert session.connected
        assert session.network_info == []
    finally:
        await session.close()


async def test_failed_handshake_raises(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    appliance.responses["/ci/info"] = {"code": 400}
    states: list[ConnectionState] = []

    async def on_state(state: ConnectionState) -> None:
        states.append(state)

    session = make_session(client_session, appliance, on_state_change=on_state)
    with pytest.raises(HandshakeError):
        await session.connect()
    assert states == [ConnectionState.CONNECTING, ConnectionState.DISCONNECTED]
    await session.close()


async def test_unreachable_appliance_raises(client_session: aiohttp.ClientSession) -> None:
    fake = FakeAppliance()
    port = await fake.start()
    await fake.stop()
    session = Session(
        client_session, "127.0.0.1", PSK64, IV64, app_name="Test", app_id="id", port=port
    )
    with pytest.raises(ConnectionFailedError):
        await session.connect()
    assert session.state is ConnectionState.DISCONNECTED


async def test_request_and_error_codes(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    appliance.responses["/ro/allMandatoryValues"] = {"data": [{"uid": 539, "value": 2}]}
    appliance.responses["/ro/values"] = {"code": 400}
    session = make_session(client_session, appliance)
    await session.connect()
    try:
        response = await session.request(Message("/ro/allMandatoryValues"))
        assert response.data == [{"uid": 539, "value": 2}]
        assert response.version == 1
        with pytest.raises(ResponseError) as raised:
            await session.request(Message("/ro/values", Action.POST, [{"uid": 1, "value": 0}]))
        assert raised.value.code == 400  # noqa: PLR2004
    finally:
        await session.close()


async def test_request_times_out(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    session = make_session(client_session, appliance, request_timeout=0.2)
    await session.connect()
    try:
        with pytest.raises(TimeoutError):
            await session.request(Message("/ro/unanswered"))
    finally:
        await session.close()


async def test_send_and_request_check_the_action(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    session = make_session(client_session, appliance)
    await session.connect()
    try:
        with pytest.raises(Exception, match="use send"):
            await session.request(Message("/ei/deviceReady", Action.NOTIFY))
        with pytest.raises(Exception, match="use request"):
            await session.send(Message("/ro/values"))
    finally:
        await session.close()


async def test_request_before_connect_raises(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    session = make_session(client_session, appliance)
    with pytest.raises(ConnectionClosedError):
        await session.request(Message("/ro/values"))


async def test_notifications_go_to_the_callback(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    received: asyncio.Queue[Message] = asyncio.Queue()

    async def on_message(message: Message) -> None:
        await received.put(message)

    session = make_session(client_session, appliance, on_message=on_message)
    await session.connect()
    try:
        await appliance.send(
            {
                "sID": SESSION_ID,
                "msgID": 2,
                "resource": "/ro/values",
                "version": 1,
                "action": "NOTIFY",
                "data": [{"uid": 552, "value": 3}],
            }
        )
        message = await asyncio.wait_for(received.get(), 5)
        assert message.action is Action.NOTIFY
        assert message.data == [{"uid": 552, "value": 3}]
    finally:
        await session.close()


async def test_callback_errors_dont_break_the_loop(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    appliance.responses["/ro/allMandatoryValues"] = {"data": []}

    async def on_message(message: Message) -> None:
        raise RuntimeError(message.resource)

    session = make_session(client_session, appliance, on_message=on_message)
    await session.connect()
    try:
        await appliance.send(
            {"sID": SESSION_ID, "msgID": 2, "resource": "/ro/values", "action": "NOTIFY"}
        )
        response = await session.request(Message("/ro/allMandatoryValues"))
        assert response.data == []
    finally:
        await session.close()


async def test_reconnects_after_a_drop(
    client_session: aiohttp.ClientSession,
    appliance: FakeAppliance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "RECONNECT_INITIAL_DELAY", 0.05)
    states: list[ConnectionState] = []
    reconnected = asyncio.Event()

    async def on_state(state: ConnectionState) -> None:
        states.append(state)
        if state is ConnectionState.CONNECTED and len(states) > 2:  # noqa: PLR2004
            reconnected.set()

    session = make_session(client_session, appliance, on_state_change=on_state)
    await session.connect()
    try:
        await appliance.drop()
        await asyncio.wait_for(reconnected.wait(), 5)
        assert states == [
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.RECONNECTING,
            ConnectionState.CONNECTED,
        ]
        assert appliance.connections == 2  # noqa: PLR2004
        assert session.connected
    finally:
        await session.close()


async def test_reconnect_backs_off_while_the_appliance_is_gone(
    client_session: aiohttp.ClientSession,
    appliance: FakeAppliance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "RECONNECT_INITIAL_DELAY", 0.05)
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay: float) -> None:
        if delay:  # other code yields with sleep(0); only record the backoff
            delays.append(delay)
        await real_sleep(0)

    session = make_session(client_session, appliance)
    await session.connect()
    try:
        monkeypatch.setattr("asyncio.sleep", recording_sleep)
        appliance.accepting = False
        await appliance.drop()
        while len(delays) < 4:  # noqa: PLR2004
            await real_sleep(0.01)
        assert delays[:4] == [0.05, 0.1, 0.2, 0.4]
        assert session.state is ConnectionState.RECONNECTING
    finally:
        monkeypatch.setattr("asyncio.sleep", real_sleep)
        await session.close()
    assert session.state.value == "closed"


async def test_no_reconnect_when_disabled(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    disconnected = asyncio.Event()

    async def on_state(state: ConnectionState) -> None:
        if state is ConnectionState.DISCONNECTED:
            disconnected.set()

    session = make_session(client_session, appliance, reconnect=False, on_state_change=on_state)
    await session.connect()
    await appliance.drop()
    await asyncio.wait_for(disconnected.wait(), 5)
    assert appliance.connections == 1
    assert session.close_code is not None
    await session.close()


async def test_pending_request_fails_when_the_connection_drops(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    session = make_session(client_session, appliance, reconnect=False)
    await session.connect()
    try:
        request = asyncio.create_task(session.request(Message("/ro/unanswered")))
        await asyncio.sleep(0.05)
        await appliance.drop()
        with pytest.raises(ConnectionClosedError):
            await request
    finally:
        await session.close()


async def test_drop_reconnects(
    client_session: aiohttp.ClientSession,
    appliance: FakeAppliance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "RECONNECT_INITIAL_DELAY", 0.05)
    back = asyncio.Event()

    async def on_state(state: ConnectionState) -> None:
        if state is ConnectionState.CONNECTED and appliance.connections > 1:
            back.set()

    session = make_session(client_session, appliance, on_state_change=on_state)
    await session.connect()
    try:
        await session.drop()
        await asyncio.wait_for(back.wait(), 5)
        assert session.connected
        assert appliance.connections == 2  # noqa: PLR2004
    finally:
        await session.close()


async def test_handshake_for_older_appliances(client_session: aiohttp.ClientSession) -> None:
    # ci and ei version 1, initialValues version 1, no ni.
    fake = FakeAppliance(services={"ro": 1, "ei": 1, "ci": 1}, initial_version=1)
    port = await fake.start()
    session = Session(
        client_session, "127.0.0.1", PSK64, IV64, app_name="Test", app_id="id", port=port
    )
    try:
        await session.connect()
        sent = [(m["resource"], m["action"], m["version"]) for m in fake.received]
        assert sent == [
            ("/ei/initialValues", "RESPONSE", 1),
            ("/ci/services", "GET", 1),
            ("/ci/authentication", "GET", 1),
            ("/ci/info", "GET", 1),
        ]
        assert fake.received[0]["data"][0]["deviceType"] == 2  # noqa: PLR2004
    finally:
        await session.close()
        await fake.stop()


async def test_handshake_versions_follow_the_services(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    session = make_session(client_session, appliance)
    await session.connect()
    try:
        versions = {m["resource"]: m["version"] for m in appliance.received}
        assert versions["/ci/authentication"] == 2  # noqa: PLR2004
        assert versions["/ci/info"] == 2  # noqa: PLR2004
        assert versions["/ei/deviceReady"] == 2  # noqa: PLR2004
        assert versions["/ni/info"] == 1
    finally:
        await session.close()


async def test_wrong_aes_key_is_an_authentication_error(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    appliance.psk = bytes(32)
    session = make_session(client_session, appliance)
    with pytest.raises(AuthenticationError):
        await session.connect()
    assert session.state is ConnectionState.DISCONNECTED
    await session.close()


async def test_connect_twice_raises(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    session = make_session(client_session, appliance)
    await session.connect()
    try:
        with pytest.raises(AlreadyConnectedError):
            await session.connect()
        assert session.connected
    finally:
        await session.close()
    # Once closed, it can connect again.
    await session.connect()
    assert session.connected
    await session.close()


async def test_close_during_reconnect_is_immediate(
    client_session: aiohttp.ClientSession, appliance: FakeAppliance
) -> None:
    # Fork issue #30: closing while the appliance is unreachable mustn't wait out the backoff.
    reconnecting = asyncio.Event()

    async def on_state(state: ConnectionState) -> None:
        if state is ConnectionState.RECONNECTING:
            reconnecting.set()

    session = make_session(client_session, appliance, on_state_change=on_state)
    await session.connect()
    appliance.accepting = False
    await appliance.drop()
    await asyncio.wait_for(reconnecting.wait(), 5)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await session.close()
    assert loop.time() - start < 0.5  # noqa: PLR2004
    assert session.state.value == "closed"


async def test_unexpected_receive_error_reconnects(
    client_session: aiohttp.ClientSession,
    appliance: FakeAppliance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After the machine sleeps, the appliance may already have given up on the connection;
    # the next receive() fails inside aiohttp. The session must notice and reconnect, not sit
    # there reporting "connected" (seen on ha-dev: hours without a reconnect).
    monkeypatch.setattr(session_module, "RECONNECT_INITIAL_DELAY", 0.05)
    reconnected = asyncio.Event()
    states: list[ConnectionState] = []

    async def on_state(state: ConnectionState) -> None:
        states.append(state)
        if state is ConnectionState.CONNECTED and ConnectionState.RECONNECTING in states:
            reconnected.set()

    session = make_session(client_session, appliance, on_state_change=on_state)
    await session.connect()
    try:
        transport = session._transport  # noqa: SLF001
        websocket = transport._websocket  # noqa: SLF001
        assert websocket is not None

        async def broken_receive(*_: object, **__: object) -> None:
            msg = "something unexpected"
            raise RuntimeError(msg)

        monkeypatch.setattr(websocket, "receive", broken_receive)
        await appliance.send({"sID": SESSION_ID, "msgID": 9, "resource": "/ro/values",
                              "action": "NOTIFY", "data": []})  # fmt: skip
        await asyncio.wait_for(reconnected.wait(), 5)
        assert session.connected
        assert appliance.connections == 2  # noqa: PLR2004
    finally:
        await session.close()
