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

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .entities import AccessError, Entities, Execution
from .errors import AlreadyConnectedError, HomeDisconnectError
from .messages import Action, Message, ResponseError
from .session import ConnectionState, Session

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping
    from datetime import datetime

    import aiohttp

    from .entities import (
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

APPLIANCE_DATETIME = "BSH.Common.Setting.ApplianceDateTime"
START_IN_RELATIVE = "BSH.Common.Option.StartInRelative"
FINISH_IN_RELATIVE = "BSH.Common.Option.FinishInRelative"
# Roughly one broadcast cycle of the dryer in fork issue #384, which reports its active program
# as writable for a moment about every 30 seconds.
ACTIVE_PROGRAM_WINDOW = 35
_FINISH_IN_FALLBACK_CODES = (501, 541)

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
    def active_program(self) -> Program | None:
        """The program that's running, if any; the entity is `entities.active_program`."""
        root = self.entities.active_program
        return root.program if root is not None else None

    @property
    def selected_program(self) -> Program | None:
        """The selected program, if any; the entity is `entities.selected_program`."""
        root = self.entities.selected_program
        return root.program if root is not None else None

    async def start_program(
        self,
        program: Program | None = None,
        options: Mapping[Entity | str | int, Any] | None = None,
        *,
        include_current_options: bool = True,
    ) -> None:
        """Start a program (by default the selected one) with the right set of options.

        On appliances that want every option in a program write (`full_option_set`), that's
        the complete set; otherwise the options with a known value. `options` go on top (e.g.
        `{"BSH.Common.Option.FinishInRelative": 3600}` for a delayed start).
        `include_current_options=False` sends only `options` on appliances that don't need a
        full set (a hood's fan, for one, starts its venting program without them).
        """
        program = program or self.selected_program
        if program is None:
            msg = "No program is selected"
            raise AccessError(msg)
        if program.full_option_set:
            current = program.full_option_values()
        elif include_current_options:
            current = program.known_option_values()
        else:
            current = {}
        await program.start(options, raw_options=current)

    async def select_program(self, program: Program) -> None:
        """Select a program the way the appliance expects it.

        - Appliances whose selected program wants a full option set get the program with a
          complete option set. A select-only program is selected; otherwise it's started,
          because such appliances reject a bare select. Only the selected program's own flag
          counts here: a coffee maker that flags just its active program would otherwise brew
          on every selection.
        - Otherwise a program that can be selected is selected without options, so the
          appliance applies its own defaults instead of a value left over from another program
          that may be out of range for this one (fork issue #9).
        - A start-only program is started with its known option values.
        """
        root = self.entities.selected_program
        if root is not None and root.full_option_set:
            options = program.full_option_values()
            if program.execution is Execution.SELECT_ONLY:
                self._ensure_selectable(root)
                await program.select(raw_options=options)
            else:
                await program.start(raw_options=options)
        elif program.execution in (Execution.SELECT_ONLY, Execution.SELECT_AND_START):
            self._ensure_selectable(root)
            await program.select()
        elif program.execution is Execution.START_ONLY:
            await program.start(raw_options=program.known_option_values())
        else:
            msg = f"{program.name} can't be selected or started (execution {program.execution})"
            raise AccessError(msg)

    async def set_values(self, values: Mapping[Entity | str | int, Any]) -> None:
        """Write several values in one message, each converted and checked like `set_value()`.

        Entities can be given as entities, names or UIDs. Some appliances reject a write that
        combines certain values (a hood's ambient light refuses a power-on together with a
        color), so write those one at a time instead.
        """
        data: list[dict[str, Any]] = []
        for key, value in values.items():
            entity = self._entity_for(key)
            entity.ensure_writable()
            data.append({"uid": entity.uid, "value": entity.to_raw(value)})
        if data:
            await self.session.request(Message("/ro/values", Action.POST, data))

    async def set_datetime(self, when: datetime) -> None:
        """Set the appliance's clock (`BSH.Common.Setting.ApplianceDateTime`).

        The appliance wants its local time as a naive ISO 8601 timestamp, so pass the local
        time; a time zone on `when` is dropped, not converted.
        """
        clock = self._entity_for(APPLIANCE_DATETIME)
        await clock.set_value(when.replace(tzinfo=None, microsecond=0).isoformat())

    async def set_start_in(self, seconds: int) -> None:
        """Delay the selected program's start by `seconds` (`StartInRelative`)."""
        await self._entity_for(START_IN_RELATIVE).set_value(seconds)

    async def set_finish_in(self, seconds: int) -> None:
        """Make the selected program finish in `seconds` (`FinishInRelative`).

        Some appliances refuse `FinishInRelative` on its own (a Siemens dryer, fork issue
        #384, answers 501 or 541) and only accept it together with the active program, while
        the active program is briefly writable. For those two codes this waits for that
        window (up to `ACTIVE_PROGRAM_WINDOW` seconds) and writes both in one message.
        """
        finish_in = self._entity_for(FINISH_IN_RELATIVE)
        try:
            await finish_in.set_value(seconds)
        except ResponseError as err:
            if err.code not in _FINISH_IN_FALLBACK_CODES:
                raise
        else:
            return
        root = self.entities.active_program
        program = self.selected_program
        if root is None or program is None:
            msg = "No program is selected"
            raise AccessError(msg) from None
        try:
            async with asyncio.timeout(ACTIVE_PROGRAM_WINDOW):
                await root.wait_until_writable()
        except TimeoutError:
            msg = "The appliance didn't open a window to set the finish time"
            raise AccessError(msg) from None
        await self.session.request(
            Message(
                "/ro/values",
                Action.POST,
                [
                    {"uid": finish_in.uid, "value": finish_in.to_raw(seconds)},
                    {"uid": root.uid, "value": program.uid},
                ],
            )
        )

    def _entity_for(self, key: Entity | str | int) -> Entity:
        if isinstance(key, int):
            entity = self.entities.by_uid.get(key)
        elif isinstance(key, str):
            entity = self.entities.get(key)
        else:
            entity = key
        if entity is None:
            msg = f"This appliance has no {key}"
            raise AccessError(msg)
        return entity

    @staticmethod
    def _ensure_selectable(root: SelectedProgram | None) -> None:
        if root is not None and root.locked:
            msg = (
                "The appliance isn't accepting a program selection right now (the selected "
                "program is read-only, e.g. while it's off or a delayed start is armed)"
            )
            raise AccessError(msg)

    async def connect(self) -> None:
        """Connect, run the handshake and read the appliance's full state.

        Raises when any of it fails; nothing is left connected in that case.
        """
        if self.state in (
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.RECONNECTING,
        ):
            msg = f"Appliance is already {self.state}"
            raise AlreadyConnectedError(msg)
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
