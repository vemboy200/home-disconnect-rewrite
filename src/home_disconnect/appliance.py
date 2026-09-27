"""One appliance: its session, its entities, and keeping them in step.

After every connect and reconnect, the appliance's full state is read again
(`/ro/allDescriptionChanges`, then `/ro/allMandatoryValues`), so nothing that changed while
the connection was down stays stale. Only once that has worked is the appliance reported as
connected. If it fails after a reconnect, the connection is dropped and retried with backoff,
instead of showing the old values as if they were current.

While connected, the appliance's NOTIFY messages on `/ro/values` and `/ro/descriptionChange`
update the entities, and each changed entity's callbacks run once per message.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .entities import Entities
from .errors import HomeDisconnectError
from .messages import Action, Message, ResponseError
from .session import ConnectionState, Session

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    import aiohttp

    from .entities import (
        ActiveProgram,
        Command,
        Entity,
        Event,
        Option,
        Program,
        SelectedProgram,
        Setting,
        Status,
    )
    from .profile import DeviceProfile

_LOGGER = logging.getLogger(__name__)

_UPDATE_RESOURCES = ("/ro/values", "/ro/descriptionChange")

type ConnectionCallback = Callable[[ConnectionState], Awaitable[None]]


class Appliance:
    """A Home Connect appliance on the local network."""

    def __init__(
        self,
        client_session: aiohttp.ClientSession,
        host: str,
        profile: DeviceProfile,
        psk64: str,
        iv64: str | None = None,
        *,
        app_name: str,
        app_id: str,
        on_connection_state: ConnectionCallback | None = None,
        reconnect: bool = True,
        port: int | None = None,
        info: dict[str, Any] | None = None,
    ) -> None:
        """Set up the appliance. Nothing connects until `connect()`.

        `info` seeds `self.info` (e.g. values stored from an earlier connection); the
        appliance's `/ci/info` or `/iz/info` answer updates it on every connect.
        """
        self.profile = profile
        self.session = Session(
            client_session,
            host,
            psk64,
            iv64,
            app_name=app_name,
            app_id=app_id,
            on_message=self._on_message,
            on_state_change=self._on_session_state,
            reconnect=reconnect,
            port=port,
        )
        self.entities = Entities(profile, self.session.request)
        self._on_connection_state = on_connection_state
        self._connecting = False
        self.state = ConnectionState.DISCONNECTED
        self.info: dict[str, Any] = {
            key: value
            for key, value in (
                ("type", profile.info.type),
                ("brand", profile.info.brand),
                ("vib", profile.info.model),
            )
            if value is not None
        }
        self.info.update(info or {})

    def __repr__(self) -> str:
        """Show the model and state, for debugging."""
        return f"<Appliance {self.info.get('vib')} {self.state}>"

    @property
    def connected(self) -> bool:
        """Whether the appliance is connected and its state has been read."""
        return self.state is ConnectionState.CONNECTED

    # Shortcuts to the entities, with the names Home Connect Local uses.
    @property
    def status(self) -> dict[str, Status]:
        """Statuses by name."""
        return self.entities.status

    @property
    def settings(self) -> dict[str, Setting]:
        """Settings by name."""
        return self.entities.settings

    @property
    def events(self) -> dict[str, Event]:
        """Events by name."""
        return self.entities.events

    @property
    def commands(self) -> dict[str, Command]:
        """Commands by name."""
        return self.entities.commands

    @property
    def options(self) -> dict[str, Option]:
        """Options by name."""
        return self.entities.options

    @property
    def programs(self) -> dict[str, Program]:
        """Programs by name."""
        return self.entities.programs

    @property
    def active_program(self) -> ActiveProgram | None:
        """The active program entity."""
        return self.entities.active_program

    @property
    def selected_program(self) -> SelectedProgram | None:
        """The selected program entity."""
        return self.entities.selected_program

    async def connect(self) -> None:
        """Connect, run the handshake and read the appliance's full state.

        Raises when any of it fails; nothing is left connected in that case.
        """
        self._connecting = True
        await self._set_state(ConnectionState.CONNECTING)
        try:
            await self.session.connect()
            await self._refresh()
        except BaseException:
            await self.session.close()
            await self._set_state(ConnectionState.DISCONNECTED)
            raise
        finally:
            self._connecting = False
        await self._set_state(ConnectionState.CONNECTED)

    async def close(self) -> None:
        """Disconnect and stop reconnecting."""
        await self.session.close()
        await self._set_state(ConnectionState.CLOSED)

    async def refresh(self) -> None:
        """Read the appliance's full state again."""
        await self._refresh()

    async def _refresh(self) -> None:
        self.info.update(self.session.device_info)
        # Access and availability first, so values land on up-to-date entities.
        for resource in ("/ro/allDescriptionChanges", "/ro/allMandatoryValues"):
            response = await self.session.request(Message(resource))
            await self._apply(response.data or [])

    async def _apply(self, items: Iterable[dict[str, Any]]) -> None:
        for changed in self.entities.apply(items):
            await changed.run_callbacks()

    async def _on_message(self, message: Message) -> None:
        if message.action is Action.NOTIFY and message.resource in _UPDATE_RESOURCES:
            await self._apply(message.data or [])
        else:
            _LOGGER.debug("Ignoring %s %s", message.action, message.resource)

    async def _on_session_state(self, state: ConnectionState) -> None:
        if self._connecting:
            # connect() reads the state and reports the result itself.
            return
        if state is ConnectionState.CONNECTED:
            try:
                await self._refresh()
            except (HomeDisconnectError, TimeoutError) as err:
                reason = (
                    f"error {err.code}" if isinstance(err, ResponseError) else type(err).__name__
                )
                _LOGGER.warning(
                    "Reconnected to %s but couldn't read its state (%s); retrying",
                    self.info.get("vib"),
                    reason,
                )
                await self.session.drop()
                return
        await self._set_state(state)

    async def _set_state(self, state: ConnectionState) -> None:
        if state is self.state:
            return
        self.state = state
        if self._on_connection_state is not None:
            try:
                await self._on_connection_state(state)
            except Exception:
                _LOGGER.exception("Error in connection state callback for %s", state)

    def get(self, name: str) -> Entity | None:
        """Look up an entity by name."""
        return self.entities.get(name)

    def dump(self) -> dict[str, Any]:
        """Return the appliance's state, for diagnostics."""
        return {
            "info": self.info,
            "state": self.state,
            "service_versions": self.session.service_versions,
            **self.entities.dump(),
        }
