"""The appliance's features as live objects: current value, access, availability, writes.

Each feature of the profile (see `profile`) becomes an entity. The appliance keeps them up to
date through `/ro/values` (values) and `/ro/descriptionChange` (access, availability, range);
the layer that receives those messages calls `Entity.update()` and then `run_callbacks()`.

Values:

- The appliance sends JSON-typed values, so most need no conversion.
- Enums arrive as numbers; `value` gives the name, `value_raw` the number.
- Booleans (content type 1) can arrive as `0`/`1`; `value` is always a `bool`.
- The profile's own values (`default`, `min`, `max`, `initValue`) are strings in the XML and
  are parsed here: numbers, booleans and quoted strings become Python values.

Writes go through a `Requester` (normally `Session.request`): `/ro/values` for values,
`/ro/selectedProgram` and `/ro/activeProgram` for programs (see hcpy, MIT,
THIRD_PARTY_NOTICES.md, for both message formats).
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .errors import HomeDisconnectError
from .messages import Action, Message
from .profile import FeatureKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping

    from .profile import DeviceProfile, Feature, ProgramOption
    from .profile import Program as ProgramProfile

_LOGGER = logging.getLogger(__name__)

BOOLEAN_CONTENT_TYPE = 0x01

type Requester = Callable[[Message], Awaitable[Message]]
type EntityCallback = Callable[["Entity"], Awaitable[None]]


class AccessError(HomeDisconnectError):
    """The entity can't be written in its current access state."""


class InvalidValueError(HomeDisconnectError, ValueError):
    """A value isn't one the entity accepts."""


class Access(StrEnum):
    """What can be done with an entity right now."""

    NONE = "none"
    READ = "read"
    # Read-only and fixed: the appliance doesn't change it at runtime (e.g. an oven cavity's
    # size and position, the temperature unit on some appliances).
    READ_STATIC = "readstatic"
    READ_WRITE = "readwrite"
    WRITE_ONLY = "writeonly"

    @classmethod
    def parse(cls, value: str | None) -> Access | None:
        """Parse the profile's or the appliance's spelling (`readWrite`, `READ`, ...)."""
        if value is None:
            return None
        try:
            return cls(value.lower())
        except ValueError:
            _LOGGER.debug("Unknown access %r", value)
            return None

    @property
    def readable(self) -> bool:
        """Whether the entity has a value to read."""
        return self in (Access.READ, Access.READ_STATIC, Access.READ_WRITE)

    @property
    def writable(self) -> bool:
        """Whether a write is allowed."""
        return self in (Access.READ_WRITE, Access.WRITE_ONLY)


class Execution(StrEnum):
    """How a program is run."""

    NONE = "none"
    SELECT_ONLY = "selectonly"
    START_ONLY = "startonly"
    SELECT_AND_START = "selectandstart"

    @classmethod
    def parse(cls, value: str | None) -> Execution | None:
        """Parse the profile's spelling (`selectAndStart`, ...)."""
        if value is None:
            return None
        try:
            return cls(value.lower())
        except ValueError:
            return None


def parse_profile_value(text: str | None) -> Any:  # noqa: ANN401 - the type depends on the feature
    """Turn a value from the profile XML into a Python value.

    `"12"` -> 12, `"0.5"` -> 0.5, `"false"` -> False, `'"RNA"'` -> "RNA", and anything that
    isn't JSON (like `NA`) stays the string it is.
    """
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _parse_bool(value: Any) -> bool | None:  # noqa: ANN401
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on")
    return None


class Entity:
    """One feature of the appliance with its live state."""

    def __init__(self, feature: Feature, requester: Requester) -> None:
        """Start from the profile's values; the appliance's messages update them."""
        self.feature = feature
        self.uid = feature.uid
        self.name = feature.name
        self._requester = requester
        self._callbacks: list[EntityCallback] = []
        self.access = Access.parse(feature.access)
        self.available = feature.available
        self.min = parse_profile_value(feature.min)
        self.max = parse_profile_value(feature.max)
        self.step = parse_profile_value(feature.step)
        self.default = parse_profile_value(feature.default)
        self.enum: dict[int, str] | None = (
            dict(feature.enumeration.values) if feature.enumeration else None
        )
        self._reverse_enum = {name: value for value, name in (self.enum or {}).items()}
        self.value_raw: Any = parse_profile_value(feature.init_value)
        self._value_shadow: Any = None

    def __repr__(self) -> str:
        """Show the name and state, for debugging."""
        return f"<{type(self).__name__} {self.name} value={self.value!r} access={self.access}>"

    @property
    def value(self) -> Any:  # noqa: ANN401 - the type depends on the feature
        """The current value: an enum's name, a bool for booleans, else the raw value."""
        return self._convert(self.value_raw)

    def _convert(self, raw: Any) -> Any:  # noqa: ANN401
        if raw is None:
            return None
        if self.enum is not None:
            try:
                return self.enum.get(int(raw))
            except (TypeError, ValueError):
                return None
        if self.feature.content_type == BOOLEAN_CONTENT_TYPE:
            return _parse_bool(raw)
        return raw

    def update(self, data: Mapping[str, Any]) -> bool:
        """Apply one item of `/ro/values` or `/ro/descriptionChange`. Returns whether it changed."""
        before = self._state()
        if "value" in data:
            self.value_raw = data["value"]
            self._value_shadow = data["value"]
        if "access" in data:
            self.access = Access.parse(data["access"])
        if "available" in data:
            self.available = _parse_bool(data["available"])
        if "min" in data:
            self.min = data["min"]
        if "max" in data:
            self.max = data["max"]
        if "stepSize" in data:
            self.step = data["stepSize"]
        if "default" in data:
            self.default = data["default"]
        return self._state() != before

    def _state(self) -> tuple[Any, ...]:
        return (self.value_raw, self.access, self.available, self.min, self.max, self.step,
                self.default)  # fmt: skip

    def register_callback(self, callback: EntityCallback) -> None:
        """Call `callback(entity)` after every change."""
        self._callbacks.append(callback)

    def unregister_callback(self, callback: EntityCallback) -> None:
        """Stop calling `callback`."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    async def run_callbacks(self) -> None:
        """Call every registered callback. One that raises doesn't stop the others."""
        for callback in list(self._callbacks):
            try:
                await callback(self)
            except Exception:
                _LOGGER.exception("Error in callback for %s", self.name)

    def to_raw(self, value: Any) -> Any:  # noqa: ANN401
        """Convert a value (an enum name, a bool, a number) into what the appliance expects."""
        if self.enum is not None:
            if isinstance(value, str) and value in self._reverse_enum:
                return self._reverse_enum[value]
            if isinstance(value, int) and not isinstance(value, bool) and value in self.enum:
                return value
            msg = f"{value!r} isn't one of {list(self._reverse_enum)} for {self.name}"
            raise InvalidValueError(msg)
        if self.feature.content_type == BOOLEAN_CONTENT_TYPE:
            if not isinstance(value, bool):
                msg = f"{self.name} takes true or false, not {value!r}"
                raise InvalidValueError(msg)
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self._check_range(value)
        return value

    def _check_range(self, value: float) -> None:
        low, high = self.min, self.max
        if isinstance(low, (int, float)) and value < low:
            msg = f"{value} is below the minimum {low} of {self.name}"
            raise InvalidValueError(msg)
        if isinstance(high, (int, float)) and value > high:
            msg = f"{value} is above the maximum {high} of {self.name}"
            raise InvalidValueError(msg)

    def ensure_writable(self) -> None:
        """Raise `AccessError` unless the entity can be written right now."""
        if self.access is None or not self.access.writable:
            msg = f"{self.name} isn't writable (access {self.access})"
            raise AccessError(msg)
        if self.available is False:
            msg = f"{self.name} isn't available right now"
            raise AccessError(msg)

    async def set_value(self, value: Any) -> None:  # noqa: ANN401
        """Write a value (an enum name, a bool, a number) after checking access and range."""
        self.ensure_writable()
        await self.set_value_raw(self.to_raw(value))

    async def set_value_raw(self, value: Any) -> None:  # noqa: ANN401
        """Write a raw value without any checks."""
        await self._requester(
            Message("/ro/values", Action.POST, [{"uid": self.uid, "value": value}])
        )
        self._value_shadow = value

    @property
    def value_shadow(self) -> Any:  # noqa: ANN401 - the type depends on the feature
        """The last raw value the appliance reported or that was written successfully.

        Appliances don't always report a value back after a write (options often don't while
        no program runs), so `value_raw` can stay behind. This is the value to send again,
        e.g. when starting a program with the current options.
        """
        return self._value_shadow

    def dump(self) -> dict[str, Any]:
        """Return the entity's state, for diagnostics."""
        return {
            "uid": self.uid,
            "name": self.name,
            "type": type(self).__name__,
            "value": self.value,
            "value_raw": self.value_raw,
            "value_shadow": self.value_shadow,
            "access": self.access,
            "available": self.available,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "default": self.default,
            "enum": self.enum,
        }


class Status(Entity):
    """A read-only state reported by the appliance, like the door state."""


class Setting(Entity):
    """An appliance setting, like the power state or child lock."""


class Event(Entity):
    """An event, like "salt nearly empty". Its value is usually Off/Present/Confirmed."""

    def __init__(self, feature: Feature, requester: Requester, entities: Entities) -> None:
        """Keep the other entities, to find the acknowledge/reject commands."""
        super().__init__(feature, requester)
        self._entities = entities

    async def acknowledge(self) -> None:
        """Acknowledge the event on the appliance, as its panel or the app would."""
        await self._run_event_command("BSH.Common.Command.AcknowledgeEvent")

    async def reject(self) -> None:
        """Reject the event, for events that ask for a decision."""
        await self._run_event_command("BSH.Common.Command.RejectEvent")

    async def _run_event_command(self, name: str) -> None:
        command = self._entities.get(name)
        if not isinstance(command, Command):
            msg = f"This appliance has no {name}"
            raise AccessError(msg)
        await command.execute(value=self.uid)

    @property
    def level(self) -> str | None:
        """How serious the event is (`hint`, `warning`, `alert`, `critical`)."""
        return self.feature.extra.get("level")

    @property
    def handling(self) -> str | None:
        """What the user is expected to do (`none`, `acknowledge`, `decision`)."""
        return self.feature.extra.get("handling")


class Command(Entity):
    """A command, like "abort program"."""

    async def execute(self, *, value: Any = True) -> None:  # noqa: ANN401
        """Trigger the command. Most commands take `True`."""
        await self.set_value(value)


class Option(Entity):
    """An option a program can take, like the duration or the temperature."""


class Program:
    """A program the appliance can run."""

    def __init__(self, program: ProgramProfile, requester: Requester, entities: Entities) -> None:
        """Set up from the profile; availability updates through `update()`."""
        self.profile = program
        self.uid = program.uid
        self.name = program.name
        self.available = program.available
        self.execution = Execution.parse(program.execution)
        self._requester = requester
        self._entities = entities
        self._callbacks: list[EntityCallback] = []

    def __repr__(self) -> str:
        """Show the name, for debugging."""
        return f"<Program {self.name} available={self.available}>"

    @property
    def full_option_set(self) -> bool:
        """Whether starting or selecting this program must send every option.

        A `fullOptionSet` flag on the program itself wins; otherwise the appliance-wide flag
        from the selected or active program applies.
        """
        own = _parse_bool(self.profile.extra.get("fullOptionSet"))
        if own is not None:
            return own
        return self._entities.full_option_set

    @property
    def option_settings(self) -> tuple[ProgramOption, ...]:
        """The options this program takes, with its own access, range and default."""
        return self.profile.options

    @property
    def options(self) -> list[Option]:
        """The option entities this program takes."""
        return [
            option
            for setting in self.profile.options
            if isinstance(option := self._entities.by_uid.get(setting.uid), Option)
        ]

    def update(self, data: Mapping[str, Any]) -> bool:
        """Apply a `/ro/descriptionChange` item for this program. Returns whether it changed."""
        if "available" not in data:
            return False
        available = _parse_bool(data["available"])
        changed = available != self.available
        self.available = available
        return changed

    def register_callback(self, callback: EntityCallback) -> None:
        """Call `callback(program)` after every change."""
        self._callbacks.append(callback)

    def unregister_callback(self, callback: EntityCallback) -> None:
        """Stop calling `callback`."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    async def run_callbacks(self) -> None:
        """Call every registered callback."""
        for callback in list(self._callbacks):
            try:
                await callback(self)  # type: ignore[arg-type]
            except Exception:
                _LOGGER.exception("Error in callback for %s", self.name)

    def _payload(self, options: Mapping[Entity | str | int, Any] | None) -> list[dict[str, Any]]:
        item: dict[str, Any] = {"program": self.uid}
        if options is not None:
            item["options"] = [
                {"uid": option.uid, "value": option.to_raw(value)}
                for option, value in self._entities.resolve_options(options)
            ]
        return [item]

    async def select(self, options: Mapping[Entity | str | int, Any] | None = None) -> None:
        """Select this program, optionally with option values."""
        await self._requester(Message("/ro/selectedProgram", Action.POST, self._payload(options)))

    async def start(self, options: Mapping[Entity | str | int, Any] | None = None) -> None:
        """Start this program, optionally with option values."""
        await self._requester(Message("/ro/activeProgram", Action.POST, self._payload(options)))


class _ProgramRoot(Entity):
    """The active or selected program. Its raw value is a program's UID."""

    def __init__(self, feature: Feature, requester: Requester, entities: Entities) -> None:
        super().__init__(feature, requester)
        self._entities = entities

    @property
    def program(self) -> Program | None:
        """The program this points to, or `None`."""
        raw = self.value_raw
        if raw is None or isinstance(raw, bool):
            return None
        try:
            return self._entities.programs_by_uid.get(int(raw))
        except (TypeError, ValueError):
            return None

    @property
    def value(self) -> str | None:
        """The program's name."""
        program = self.program
        return program.name if program else None

    @property
    def full_option_set(self) -> bool:
        """Whether the appliance wants every option sent with the program (from the profile)."""
        return _parse_bool(self.feature.extra.get("fullOptionSet")) is True


class ActiveProgram(_ProgramRoot):
    """The program that's running."""


class SelectedProgram(_ProgramRoot):
    """The program that's selected but not (yet) running."""


_ENTITY_CLASSES: dict[FeatureKind, type[Entity]] = {
    FeatureKind.STATUS: Status,
    FeatureKind.SETTING: Setting,
    FeatureKind.COMMAND: Command,
    FeatureKind.OPTION: Option,
}


class Entities:
    """Every entity and program of one appliance, by UID and by name."""

    def __init__(self, profile: DeviceProfile, requester: Requester) -> None:
        """Create the entities for a profile. `requester` sends the writes."""
        self.by_uid: dict[int, Entity] = {}
        self.by_name: dict[str, Entity] = {}
        self.programs_by_uid: dict[int, Program] = {}
        self.programs: dict[str, Program] = {}
        self.active_program: ActiveProgram | None = None
        self.selected_program: SelectedProgram | None = None
        for feature in profile.features.values():
            entity: Entity
            if feature.kind is FeatureKind.EVENT:
                entity = Event(feature, requester, self)
            elif feature.kind is FeatureKind.ACTIVE_PROGRAM:
                entity = self.active_program = ActiveProgram(feature, requester, self)
            elif feature.kind is FeatureKind.SELECTED_PROGRAM:
                entity = self.selected_program = SelectedProgram(feature, requester, self)
            else:
                entity = _ENTITY_CLASSES[feature.kind](feature, requester)
            self.by_uid[entity.uid] = entity
            self.by_name[entity.name] = entity
        for program_profile in profile.programs.values():
            program = Program(program_profile, requester, self)
            self.programs_by_uid[program.uid] = program
            self.programs[program.name] = program

    def __iter__(self) -> Iterator[Entity]:
        """Iterate over every entity (programs not included)."""
        return iter(self.by_uid.values())

    def __len__(self) -> int:
        """Count the entities."""
        return len(self.by_uid)

    def __getitem__(self, name: str) -> Entity:
        """Look up an entity by name."""
        return self.by_name[name]

    def get(self, name: str) -> Entity | None:
        """Look up an entity by name, or `None`."""
        return self.by_name.get(name)

    def _of_type[T: Entity](self, kind: type[T]) -> dict[str, T]:
        return {e.name: e for e in self.by_uid.values() if isinstance(e, kind)}

    @property
    def status(self) -> dict[str, Status]:
        """Statuses by name."""
        return self._of_type(Status)

    @property
    def settings(self) -> dict[str, Setting]:
        """Settings by name."""
        return self._of_type(Setting)

    @property
    def events(self) -> dict[str, Event]:
        """Events by name."""
        return self._of_type(Event)

    @property
    def commands(self) -> dict[str, Command]:
        """Commands by name."""
        return self._of_type(Command)

    @property
    def options(self) -> dict[str, Option]:
        """Options by name."""
        return self._of_type(Option)

    @property
    def full_option_set(self) -> bool:
        """Whether the appliance wants every option sent with a program.

        True when either the selected or the active program says so: appliances don't always
        put the flag on both.
        """
        return any(
            root is not None and root.full_option_set
            for root in (self.selected_program, self.active_program)
        )

    def resolve_options(
        self, options: Mapping[Entity | str | int, Any]
    ) -> Iterable[tuple[Option, Any]]:
        """Turn `{option, name or UID: value}` into `(Option, value)` pairs."""
        for key, value in options.items():
            option: Entity | None
            if isinstance(key, Entity):
                option = key
            elif isinstance(key, int):
                option = self.by_uid.get(key)
            else:
                option = self.by_name.get(key)
            if not isinstance(option, Option):
                msg = f"{key!r} isn't an option of this appliance"
                raise InvalidValueError(msg)
            yield option, value

    def apply(self, items: Iterable[Mapping[str, Any]]) -> list[Entity | Program]:
        """Apply `/ro/values` or `/ro/descriptionChange` items. Returns what changed.

        Items for UIDs the profile doesn't know are skipped.
        """
        changed: list[Entity | Program] = []
        for item in items:
            try:
                uid = int(item["uid"])
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("Skipping an item without a valid uid: %s", item)
                continue
            target: Entity | Program | None = self.by_uid.get(uid) or self.programs_by_uid.get(uid)
            if target is None:
                _LOGGER.debug("Skipping an update for unknown UID %s", uid)
                continue
            if target.update(item) and target not in changed:
                changed.append(target)
        return changed

    def dump(self) -> dict[str, Any]:
        """Every entity's and program's state, for diagnostics."""
        return {
            "entities": {e.name: e.dump() for e in self.by_uid.values()},
            "programs": {
                p.name: {"uid": p.uid, "available": p.available, "execution": p.execution}
                for p in self.programs_by_uid.values()
            },
        }
