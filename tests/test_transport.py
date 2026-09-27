import asyncio
import base64
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from home_disconnect.crypto import AesCodec
from home_disconnect.errors import ConnectionClosedError, ConnectionFailedError, DecryptionError
from home_disconnect.transport import PSK_IDENTITY, Transport

PSK = bytes(range(32))
IV = bytes(range(16, 32))
PSK64 = base64.urlsafe_b64encode(PSK).decode().rstrip("=")
IV64 = base64.urlsafe_b64encode(IV).decode().rstrip("=")

Handler = Callable[[web.WebSocketResponse], Awaitable[None]]


async def start_server(handler: Handler, ssl_context: ssl.SSLContext | None = None) -> TestServer:
    async def websocket_view(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        await handler(websocket)
        return websocket

    app = web.Application()
    app.router.add_get("/homeconnect", websocket_view)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server(ssl=ssl_context)
    return server


def psk_server_context(psk: bytes = PSK) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers("PSK")
    context.set_psk_server_callback(
        lambda identity: psk if identity == PSK_IDENTITY else b"", identity_hint=PSK_IDENTITY
    )
    return context


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client_session:
        yield client_session


def test_scheme_follows_the_profile(session: aiohttp.ClientSession) -> None:
    aes = Transport(session, "192.168.1.5", PSK64, IV64)
    tls = Transport(session, "192.168.1.5", PSK64)
    assert aes.encrypted_with_aes
    assert str(aes.url) == "ws://192.168.1.5/homeconnect"
    assert not tls.encrypted_with_aes
    assert str(tls.url) == "wss://192.168.1.5/homeconnect"


def test_ipv6_host_is_bracketed(session: aiohttp.ClientSession) -> None:
    transport = Transport(session, "fd00::1", PSK64, IV64)
    assert str(transport.url) == "ws://[fd00::1]/homeconnect"


async def test_aes_exchange(session: aiohttp.ClientSession) -> None:
    async def appliance(websocket: web.WebSocketResponse) -> None:
        codec = AesCodec(PSK, IV, role="appliance")
        await websocket.send_bytes(codec.encrypt('{"resource":"/ei/initialValues"}'))
        request = codec.decrypt(await websocket.receive_bytes())
        await websocket.send_bytes(codec.encrypt(f"echo {request}"))
        await websocket.receive()

    server = await start_server(appliance)
    try:
        async with Transport(session, "127.0.0.1", PSK64, IV64, port=server.port) as transport:
            assert await transport.receive() == '{"resource":"/ei/initialValues"}'
            await transport.send('{"resource":"/ci/services"}')
            assert await transport.receive() == 'echo {"resource":"/ci/services"}'
    finally:
        await server.close()


async def test_aes_reconnect_starts_new_chains(session: aiohttp.ClientSession) -> None:
    async def appliance(websocket: web.WebSocketResponse) -> None:
        # A fresh codec per connection, like the appliance.
        codec = AesCodec(PSK, IV, role="appliance")
        await websocket.send_bytes(codec.encrypt("hello"))
        await websocket.close()

    server = await start_server(appliance)
    try:
        transport = Transport(session, "127.0.0.1", PSK64, IV64, port=server.port)
        for _ in range(2):
            await transport.connect()
            assert await transport.receive() == "hello"
            with pytest.raises(ConnectionClosedError):
                await transport.receive()
    finally:
        await server.close()


async def test_aes_wrong_key_raises_decryption_error(session: aiohttp.ClientSession) -> None:
    async def appliance(websocket: web.WebSocketResponse) -> None:
        codec = AesCodec(bytes(32), IV, role="appliance")
        await websocket.send_bytes(codec.encrypt("hello"))
        await websocket.receive()

    server = await start_server(appliance)
    try:
        async with Transport(session, "127.0.0.1", PSK64, IV64, port=server.port) as transport:
            with pytest.raises(DecryptionError):
                await transport.receive()
    finally:
        await server.close()


async def test_tls_psk_exchange(session: aiohttp.ClientSession) -> None:
    async def appliance(websocket: web.WebSocketResponse) -> None:
        await websocket.send_str('{"resource":"/ei/initialValues"}')
        request = await websocket.receive_str()
        await websocket.send_str(f"echo {request}")
        await websocket.receive()

    server = await start_server(appliance, psk_server_context())
    try:
        async with Transport(session, "127.0.0.1", PSK64, port=server.port) as transport:
            assert await transport.receive() == '{"resource":"/ei/initialValues"}'
            await transport.send("ping")
            assert await transport.receive() == "echo ping"
    finally:
        await server.close()


async def test_tls_psk_wrong_key_fails_to_connect(session: aiohttp.ClientSession) -> None:
    async def appliance(websocket: web.WebSocketResponse) -> None:
        await websocket.receive()

    server = await start_server(appliance, psk_server_context(bytes(32)))
    try:
        transport = Transport(session, "127.0.0.1", PSK64, port=server.port)
        with pytest.raises(ConnectionFailedError):
            async with asyncio.timeout(5):
                await transport.connect()
    finally:
        await server.close()


async def test_close_code_from_appliance(session: aiohttp.ClientSession) -> None:
    async def appliance(websocket: web.WebSocketResponse) -> None:
        await websocket.close(code=1000)

    server = await start_server(appliance)
    try:
        transport = Transport(session, "127.0.0.1", PSK64, IV64, port=server.port)
        await transport.connect()
        with pytest.raises(ConnectionClosedError) as raised:
            await transport.receive()
        assert raised.value.code == 1000  # noqa: PLR2004
        assert transport.close_code == 1000  # noqa: PLR2004
        assert not transport.connected
    finally:
        await server.close()


async def test_unreachable_host_fails_to_connect(session: aiohttp.ClientSession) -> None:
    server = await start_server(lambda websocket: websocket.receive())  # type: ignore[arg-type,return-value]
    port = server.port
    await server.close()
    transport = Transport(session, "127.0.0.1", PSK64, IV64, port=port)
    with pytest.raises(ConnectionFailedError):
        async with asyncio.timeout(5):
            await transport.connect()


async def test_send_before_connect_raises(session: aiohttp.ClientSession) -> None:
    transport = Transport(session, "127.0.0.1", PSK64, IV64)
    with pytest.raises(ConnectionClosedError):
        await transport.send("hello")


async def test_close_leaves_the_session_open(session: aiohttp.ClientSession) -> None:
    async def appliance(websocket: web.WebSocketResponse) -> None:
        await websocket.receive()

    server = await start_server(appliance)
    try:
        async with Transport(session, "127.0.0.1", PSK64, IV64, port=server.port):
            pass
        assert not session.closed
    finally:
        await server.close()
