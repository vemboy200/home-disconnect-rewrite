"""Parse an appliance profile: its DeviceDescription and FeatureMapping XML files.

The two files come together (from the Home Connect Profile Downloader or the account's
`iddf` endpoint):

- **DeviceDescription** lists what the appliance has: statuses, settings, events, commands,
  options, and programs with the options each program takes. Every entry has a hex `uid`.
  Enumeration types list which values each enum allows.
- **FeatureMapping** gives the names: `featureDescription` maps UIDs to names like
  `BSH.Common.Status.DoorState`, `enumDescriptionList` names each enum value, and
  `errorDescription` names error codes.

This module only turns the XML into data; it reads no files (see `profile_files`) and leaves
value conversion to the entity layer. Attribute values like `min`, `max` and `default` stay
the strings the profile contains, because their type depends on the feature's data type.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from .errors import HomeDisconnectError

if TYPE_CHECKING:
    from collections.abc import Iterator

# Below this share of the description's UIDs having a name in the feature mapping, the two
# files almost certainly don't belong together. Real profiles name every UID.
_MIN_NAMED_SHARE = 0.5


class ProfileError(HomeDisconnectError):
    """A profile file is missing, unreadable, or doesn't match its partner."""

    def __init__(self, message: str, file: str | None = None) -> None:
        """Store which file the problem is in, when known."""
        super().__init__(f"{file}: {message}" if file else message)
        self.file = file


class FeatureKind(StrEnum):
    """Which list of the DeviceDescription a feature comes from."""

    STATUS = "status"
    SETTING = "setting"
    EVENT = "event"
    COMMAND = "command"
    OPTION = "option"
    ACTIVE_PROGRAM = "activeProgram"
    SELECTED_PROGRAM = "selectedProgram"


_FEATURE_TAGS = {kind.value: kind for kind in FeatureKind}
_LIST_TAGS = {"statusList", "settingList", "eventList", "commandList", "optionList"}


@dataclass(frozen=True, slots=True)
class Enumeration:
    """The allowed values of an enum and their names, sorted by value."""

    id: int
    key: str | None
    values: dict[int, str]


@dataclass(frozen=True, slots=True)
class Feature:
    """One status, setting, event, command, option, or the active/selected program."""

    uid: int
    name: str
    kind: FeatureKind
    access: str | None = None
    available: bool | None = None
    content_type: int | None = None
    data_type: int | None = None
    enumeration: Enumeration | None = None
    min: str | None = None
    max: str | None = None
    step: str | None = None
    init_value: str | None = None
    default: str | None = None
    # Anything else the element carries, e.g. an event's "level" and "handling" or a
    # program's "fullOptionSet", keyed by the XML attribute name.
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProgramOption:
    """An option as one program uses it, with that program's own access, range and default."""

    uid: int
    name: str
    access: str | None = None
    available: bool | None = None
    min: str | None = None
    max: str | None = None
    step: str | None = None
    init_value: str | None = None
    default: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Program:
    """A program and the options it takes."""

    uid: int
    name: str
    available: bool | None = None
    execution: str | None = None
    group: str | None = None
    options: tuple[ProgramOption, ...] = ()


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """The `description` block: what kind of appliance the profile is for."""

    type: str | None = None
    brand: str | None = None
    model: str | None = None
    version: str | None = None
    revision: str | None = None
    pairable_device_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """Everything the two profile files describe."""

    info: DeviceInfo
    features: dict[int, Feature]
    programs: dict[int, Program]
    enumerations: dict[int, Enumeration]
    errors: dict[int, str]
    # UIDs the description uses that the feature mapping has no name for.
    unnamed_uids: frozenset[int] = frozenset()

    def feature(self, name: str) -> Feature | None:
        """Look up a feature by name, e.g. `BSH.Common.Status.DoorState`."""
        return next((f for f in self.features.values() if f.name == name), None)


def _tag(element: ET.Element) -> str:
    return element.tag.rpartition("}")[2]


def _hex(value: str | None) -> int | None:
    return None if value is None else int(value, 16)


def _bool(value: str | None) -> bool | None:
    return None if value is None else value.lower() == "true"


def _parse_root(xml: str | bytes, expected_root: str, file: str | None) -> ET.Element:
    try:
        root = ET.fromstring(xml)  # noqa: S314 - expat limits entity expansion; no DTDs here
    except ET.ParseError as err:
        msg = f"isn't valid XML ({err})"
        raise ProfileError(msg, file) from err
    if _tag(root) != expected_root:
        kind = "DeviceDescription" if expected_root == "device" else "FeatureMapping"
        msg = f"isn't a {kind} (root element is <{_tag(root)}>)"
        raise ProfileError(msg, file)
    return root


def _children(element: ET.Element, tag: str) -> Iterator[ET.Element]:
    return (child for child in element if _tag(child) == tag)


@dataclass(slots=True)
class _Mapping:
    names: dict[int, str]
    errors: dict[int, str]
    enum_names: dict[int, tuple[str | None, dict[int, str]]]


def _parse_feature_mapping(xml: str | bytes, file: str | None) -> _Mapping:
    root = _parse_root(xml, "featureMappingFile", file)
    mapping = _Mapping(names={}, errors={}, enum_names={})
    try:
        for section in root:
            match _tag(section):
                case "featureDescription":
                    for item in _children(section, "feature"):
                        mapping.names[int(item.attrib["refUID"], 16)] = (item.text or "").strip()
                case "errorDescription":
                    for item in _children(section, "error"):
                        mapping.errors[int(item.attrib["refEID"], 16)] = (item.text or "").strip()
                case "enumDescriptionList":
                    for item in _children(section, "enumDescription"):
                        members = {
                            int(member.attrib["refValue"]): (member.text or "").strip()
                            for member in _children(item, "enumMember")
                        }
                        mapping.enum_names[int(item.attrib["refENID"], 16)] = (
                            item.attrib.get("enumKey"),
                            members,
                        )
    except (KeyError, ValueError) as err:
        msg = f"has an entry with a missing or invalid ID ({err})"
        raise ProfileError(msg, file) from err
    if not mapping.names:
        msg = "names no features"
        raise ProfileError(msg, file)
    return mapping


_COMMON_ATTRIBUTES = {"uid", "refUID", "access", "available", "refCID", "refDID", "enumerationType",
                      "min", "max", "stepSize", "initValue", "default"}  # fmt: skip


class _DescriptionParser:
    def __init__(self, mapping: _Mapping, file: str | None) -> None:
        self._mapping = mapping
        self._file = file
        self._enum_values: dict[int, list[int]] = {}
        self.enumerations: dict[int, Enumeration] = {}
        self.features: dict[int, Feature] = {}
        self.programs: dict[int, Program] = {}
        self.used_uids: set[int] = set()

    def name(self, uid: int) -> str:
        self.used_uids.add(uid)
        return self._mapping.names.get(uid, f"{uid:04X}")

    def parse(self, root: ET.Element) -> DeviceInfo:
        info = DeviceInfo()
        for section in root:
            if _tag(section) == "enumerationTypeList":
                self._parse_enumerations(section)
        # Profiles that went through Home Connect Local's export have no enumerationTypeList;
        # their enums are only in the feature mapping, with every named value allowed.
        for enum_id, (key, names) in self._mapping.enum_names.items():
            self.enumerations.setdefault(
                enum_id, Enumeration(id=enum_id, key=key, values=dict(sorted(names.items())))
            )
        for section in root:
            tag = _tag(section)
            if tag == "description":
                info = self._parse_info(section)
            elif tag in _LIST_TAGS:
                self._parse_list(section)
            elif tag in ("activeProgram", "selectedProgram"):
                self._add_feature(section, _FEATURE_TAGS[tag])
            elif tag == "programGroup":
                self._parse_program_group(section, None)
        return info

    def _parse_enumerations(self, section: ET.Element) -> None:
        for item in _children(section, "enumerationType"):
            enum_id = int(item.attrib["enid"], 16)
            values = [int(value.attrib["value"]) for value in _children(item, "enumeration")]
            key, names = self._mapping.enum_names.get(enum_id, (None, {}))
            if "subsetOf" in item.attrib and not values:
                parent = self._mapping.enum_names.get(int(item.attrib["subsetOf"], 16))
                names = parent[1] if parent else names
            self.enumerations[enum_id] = Enumeration(
                id=enum_id,
                key=key,
                values=dict(
                    sorted(
                        {value: names.get(value, str(value)) for value in values}.items()
                        if values
                        else names.items()
                    )
                ),
            )

    @staticmethod
    def _parse_info(section: ET.Element) -> DeviceInfo:
        texts = {_tag(child): (child.text or "").strip() or None for child in section}
        pairable = next(_children(section, "pairableDeviceTypes"), None)
        return DeviceInfo(
            type=texts.get("type"),
            brand=texts.get("brand"),
            model=texts.get("model"),
            version=texts.get("version"),
            revision=texts.get("revision"),
            pairable_device_types=tuple(
                (child.text or "").strip()
                for child in (pairable if pairable is not None else ())
                if child.text
            ),
        )

    def _parse_list(self, section: ET.Element) -> None:
        for child in section:
            tag = _tag(child)
            if tag in _LIST_TAGS:
                self._parse_list(child)
            elif tag in _FEATURE_TAGS:
                self._add_feature(child, _FEATURE_TAGS[tag])

    def _add_feature(self, element: ET.Element, kind: FeatureKind) -> None:
        attrs = element.attrib
        uid = int(attrs["uid"], 16)
        enum_id = _hex(attrs.get("enumerationType"))
        self.features[uid] = Feature(
            uid=uid,
            name=self.name(uid),
            kind=kind,
            access=attrs.get("access"),
            available=_bool(attrs.get("available")),
            content_type=_hex(attrs.get("refCID")),
            data_type=_hex(attrs.get("refDID")),
            enumeration=None if enum_id is None else self.enumerations.get(enum_id),
            min=attrs.get("min"),
            max=attrs.get("max"),
            step=attrs.get("stepSize"),
            init_value=attrs.get("initValue"),
            default=attrs.get("default"),
            extra={k: v for k, v in attrs.items() if k not in _COMMON_ATTRIBUTES},
        )

    def _parse_program_group(self, section: ET.Element, group: str | None) -> None:
        for child in section:
            tag = _tag(child)
            if tag == "programGroup":
                self._parse_program_group(child, self.name(int(child.attrib["uid"], 16)))
            elif tag == "program":
                self._add_program(child, group)

    def _add_program(self, element: ET.Element, group: str | None) -> None:
        uid = int(element.attrib["uid"], 16)
        options = []
        for option in _children(element, "option"):
            attrs = option.attrib
            option_uid = int(attrs["refUID"], 16)
            options.append(
                ProgramOption(
                    uid=option_uid,
                    name=self.name(option_uid),
                    access=attrs.get("access"),
                    available=_bool(attrs.get("available")),
                    min=attrs.get("min"),
                    max=attrs.get("max"),
                    step=attrs.get("stepSize"),
                    init_value=attrs.get("initValue"),
                    default=attrs.get("default"),
                    extra={k: v for k, v in attrs.items() if k not in _COMMON_ATTRIBUTES},
                )
            )
        self.programs[uid] = Program(
            uid=uid,
            name=self.name(uid),
            available=_bool(element.attrib.get("available")),
            execution=element.attrib.get("execution"),
            group=group,
            options=tuple(options),
        )


def parse_profile(
    description_xml: str | bytes,
    feature_mapping_xml: str | bytes,
    *,
    description_file: str | None = None,
    feature_mapping_file: str | None = None,
) -> DeviceProfile:
    """Parse a DeviceDescription and its FeatureMapping.

    The file names are only used in error messages. Raises `ProfileError` when either file
    isn't valid, or when they don't belong together.
    """
    mapping = _parse_feature_mapping(feature_mapping_xml, feature_mapping_file)
    root = _parse_root(description_xml, "device", description_file)
    parser = _DescriptionParser(mapping, description_file)
    try:
        info = parser.parse(root)
    except (KeyError, ValueError) as err:
        msg = f"has an entry with a missing or invalid attribute ({err})"
        raise ProfileError(msg, description_file) from err
    if not parser.features and not parser.programs:
        msg = "describes no features or programs"
        raise ProfileError(msg, description_file)
    unnamed = frozenset(uid for uid in parser.used_uids if uid not in mapping.names)
    if len(unnamed) > len(parser.used_uids) * (1 - _MIN_NAMED_SHARE):
        files = " and ".join(f for f in (description_file, feature_mapping_file) if f)
        msg = (
            f"{len(unnamed)} of {len(parser.used_uids)} UIDs have no name in the feature "
            "mapping; the two files don't belong together"
        )
        raise ProfileError(msg, files or None)
    return DeviceProfile(
        info=info,
        features=parser.features,
        programs=parser.programs,
        enumerations=parser.enumerations,
        errors=mapping.errors,
        unnamed_uids=unnamed,
    )
