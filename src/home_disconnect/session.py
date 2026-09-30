"""A session with one appliance: the handshake, the receive loop and reconnecting.

The handshake follows hcpy (MIT, see THIRD_PARTY_NOTICES.md):

1. The appliance opens with `/ei/initialValues`, which sets the session ID and the first
   message ID. We answer with our device type, name and ID.
2. `/ci/services` lists the services and their versions.
3. Appliances with the `iz` service identify themselves through `/iz/info`. The others want
   `/ci/authentication` with a random nonce, then answer `/ci/info`. Both use the appliance's
   own `ci` version.
4. `/ei/deviceReady` (a NOTIFY) tells appliances with `ei` version 2 that we're ready. Some
   refuse `/ni/` requests without it. Appliances with `ei` version 1 don't get it.
5. `/ni/info`, if the appliance has the `ni` service, returns network details.

After that the session is connected. Reading the appliance's values is up to the layer above.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .errors import (
    AlreadyConnectedError,
    AuthenticationError,
    ConnectionClosedError,
    ConnectionFailedError,
    DecryptionError,
    HomeDisconnectError,
)
from .messages import Action, Message, RequestTracker, ResponseError
from .transport import Transport

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import aiohttp

    from .messages import MessageData

_LOGGER = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 30
REQUEST_TIMEOUT = 20
RECONNECT_INITIAL_DELAY = 5
RECONNECT_MAX_DELAY = 300


class HandshakeError(HomeDisconnectError):
    """The appliance accepted the connection but the handshake failed."""


class ConnectionState(StrEnum):
    """Where the session is."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"


type MessageCallback = Callable[[Message], Awaitable[None]]
type StateCallback = Callable[[ConnectionState], Awaitable[None]]


class Session:
    """One appliance connection with its handshake, keeping itself connected if asked to.

    `on_message` gets every message that isn't the response to one of our requests, most
    importantly the appliance's NOTIFY messages. `on_state_change` gets every state change.
    """

    def __init__(
        self,
        client_session: aiohttp.ClientSession,
        host: str,
        psk64: str,
        iv64: str | None = None,
        *,
        app_name: str,
        app_id: str,
        on_message: MessageCallback | None = None,
        on_state_change: StateCallback | None = None,
        reconnect: bool = True,
        port: int | None = None,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        """Set up the session. Nothing connects until `connect()`."""
        self._transport = Transport(client_session, host, psk64, iv64, port=port)
        self._app_name = app_name
        self._app_id = app_id
        self._on_message = on_message
        self._on_state_change = on_state_change
        self._reconnect = reconnect
        self._request_timeout = request_timeout
        self._tracker: RequestTracker | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._retry_now = asyncio.Event()
        self._closing = False
        self.state = ConnectionState.DISCONNECTED
        self.service_versions: dict[str, int] = {}
        self.device_info: dict[str, Any] = {}
        self.network_info: MessageData = []

    @property
    def connected(self) -> bool:
        """Whether the handshake is done and the connection is up."""
        return self.state is ConnectionState.CONNECTED

    @property
    def close_code(self) -> int | None:
        """The WebSocket close code of the last connection."""
        return self._transport.close_code

    async def connect(self) -> None:
        """Connect and run the handshake once. Raises if either fails.

        With `reconnect=True`, a connection that later drops is reopened in the background.
        """
        if self.state in (
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.RECONNECTING,
        ):
            msg = f"Session is already {self.state}"
            raise AlreadyConnectedError(msg)
        self._closing = False
        await self._set_state(ConnectionState.CONNECTING)
        try:
            await self._open()
        except BaseException:
            await self._transport.close()
            await self._set_state(ConnectionState.DISCONNECTED)
            raise
        await self._set_state(ConnectionState.CONNECTED)

    async def close(self) -> None:
        """Close the connection and stop reconnecting."""
        self._closing = True
        for task in (self._reconnect_task, self._receive_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reconnect_task = None
        self._receive_task = None
        await self._transport.close()
        if self._tracker is not None:
            self._tracker.fail_all(ConnectionClosedError(self._transport.close_code))
        await self._set_state(ConnectionState.CLOSED)

    def retry_now(self) -> None:
        """Skip the wait before the next reconnect attempt and start the backoff over.

        For when something else says the appliance is back, such as it announcing itself on the
        network again. Does nothing unless the session is reconnecting.
        """
        if self.state is ConnectionState.RECONNECTING:
            self._retry_now.set()

    async def drop(self) -> None:
        """Close the current connection without stopping.

        With `reconnect=True` the session reconnects with backoff, as after any other drop.
        Used when a connection is up but unusable, e.g. the appliance won't answer.
        """
        await self._transport.close()

    async def request(self, message: Message) -> Message:
        """Send a request and wait for its response.

        Raises `ResponseError` for an error code, `ConnectionClosedError` when the connection
        drops first, and `TimeoutError` when the appliance doesn't answer in time.
        """
        tracker = self._require_tracker()
        prepared, future = tracker.prepare(message)
        if future is None:
            msg = f"{message.action} messages get no response; use send()"
            raise HomeDisconnectError(msg)
        try:
            await self._transport.send(prepared.to_json())
            async with asyncio.timeout(self._request_timeout):
                return await future
        finally:
            if prepared.message_id is not None:
                tracker.discard(prepared.message_id)
            if not future.done():
                future.cancel()

    async def send(self, message: Message) -> None:
        """Send a message that gets no response, like a NOTIFY."""
        prepared, future = self._require_tracker().prepare(message)
        if future is not None:
            future.cancel()
            msg = f"{message.action} messages get a response; use request()"
            raise HomeDisconnectError(msg)
        await self._transport.send(prepared.to_json())

    def _require_tracker(self) -> RequestTracker:
        if self._tracker is None or not self._transport.connected:
            raise ConnectionClosedError(self._transport.close_code)
        return self._tracker

    async def _open(self) -> None:
        """Open the transport, run the handshake and start the receive loop."""
        await self._transport.connect()
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            try:
                initial = Message.from_json(await self._transport.receive())
            except DecryptionError as err:
                # With AES, a wrong key or IV shows up as the first frame failing its MAC.
                msg = "The appliance's first message didn't decrypt; the key or IV is wrong"
                raise AuthenticationError(msg) from err
            if initial.resource != "/ei/initialValues" or not initial.data:
                msg = f"Expected /ei/initialValues first, got {initial.resource}"
                raise HandshakeError(msg)
            try:
                self._tracker = RequestTracker(
                    session_id=int(initial.session_id or 0),
                    next_message_id=int(initial.data[0]["edMsgID"]),
                )
            except (KeyError, TypeError, ValueError) as err:
                msg = "/ei/initialValues has no usable session or message ID"
                raise HandshakeError(msg) from err
            device_type: str | int = "Application" if (initial.version or 1) > 1 else 2
            await self._transport.send(
                initial.response(
                    [
                        {
                            "deviceType": device_type,
                            "deviceName": self._app_name,
                            "deviceID": self._app_id,
                        }
                    ]
                ).to_json()
            )
            self._receive_task = asyncio.create_task(self._receive_loop(self._tracker))
            await self._handshake()

    async def _handshake(self) -> None:
        try:
            services = await self.request(Message("/ci/services", version=1))
            self.service_versions = {
                str(item["service"]): int(item["version"]) for item in services.data or []
            }
            tracker = self._require_tracker()
            tracker.service_versions = self.service_versions

            # Requests use each service's own version (see RequestTracker), so older
            # appliances with ci/ei version 1 get version 1 messages.
            if "iz" in self.service_versions:
                info = await self.request(Message("/iz/info"))
            else:
                nonce = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
                await self.request(Message("/ci/authentication", Action.GET, [{"nonce": nonce}]))
                info = await self.request(Message("/ci/info"))
            if info.data:
                self.device_info = dict(info.data[0])

            # Only appliances with ei version 2 expect deviceReady.
            if self.service_versions.get("ei", 1) >= 2:  # noqa: PLR2004
                await self.send(Message("/ei/deviceReady", Action.NOTIFY))

            if "ni" in self.service_versions:
                try:
                    self.network_info = (await self.request(Message("/ni/info"))).data or []
                except ResponseError as err:
                    _LOGGER.debug("Appliance refused /ni/info: %s", err)
        except (ResponseError, KeyError, TypeError, ValueError) as err:
            msg = f"Handshake failed: {err}"
            raise HandshakeError(msg) from err

    async def _receive_loop(self, tracker: RequestTracker) -> None:
        """Read messages until the connection drops, then reconnect if asked to."""
        error: HomeDisconnectError
        try:
            while True:
                message = Message.from_json(await self._transport.receive())
                if tracker.resolve(message):
                    continue
                if self._on_message is not None:
                    try:
                        await self._on_message(message)
                    except Exception:
                        _LOGGER.exception("Error in message callback for %s", message.resource)
        except HomeDisconnectError as err:
            error = err
        except Exception as err:  # noqa: BLE001 - any failure here means the connection is gone
            _LOGGER.warning("Unexpected error in the receive loop: %r", err)
            error = ConnectionClosedError(self._transport.close_code)
            error.__cause__ = err
        tracker.fail_all(error)
        await self._transport.close()
        if self._closing:
            return
        _LOGGER.debug("Connection lost: %s", error)
        if self._reconnect and self.state is ConnectionState.CONNECTED:
            await self._set_state(ConnectionState.RECONNECTING)
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        elif self.state is ConnectionState.CONNECTED:
            await self._set_state(ConnectionState.DISCONNECTED)

    async def _reconnect_loop(self) -> None:
        delay: float = RECONNECT_INITIAL_DELAY
        while not self._closing:
            if await self._wait_to_retry(delay):
                delay = RECONNECT_INITIAL_DELAY
            try:
                await self._open()
            except (HomeDisconnectError, TimeoutError) as err:
                await self._transport.close()
                _LOGGER.debug("Reconnect failed, retrying in %ss: %s", delay, err)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
                continue
            # A retry_now() during the successful attempt mustn't cut the next drop's first wait.
            self._retry_now.clear()
            await self._set_state(ConnectionState.CONNECTED)
            return

    async def _wait_to_retry(self, delay: float) -> bool:
        """Wait `delay` seconds, or until `retry_now()`. Returns whether `retry_now()` ended it."""
        try:
            async with asyncio.timeout(delay):
                await self._retry_now.wait()
        except TimeoutError:
            return False
        finally:
            self._retry_now.clear()
        return True

    async def _set_state(self, state: ConnectionState) -> None:
        if state is self.state:
            return
        self.state = state
        if self._on_state_change is not None:
            try:
                await self._on_state_change(state)
            except Exception:
                _LOGGER.exception("Error in state callback for %s", state)


__all__ = [
    "AlreadyConnectedError",
    "AuthenticationError",
    "ConnectionClosedError",
    "ConnectionFailedError",
    "ConnectionState",
    "HandshakeError",
    "Session",
]
