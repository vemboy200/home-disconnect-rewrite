"""
Turn a profile stored by home-disconnect 1.x back into DeviceDescription.xml/FeatureMapping.xml.

Home Connect Local 1.x stores each appliance's profile in its config entry as the
dictionary home-disconnect 1.x's parser produced, not as the original XML. This
module converts that stored dictionary back into the two XML files once, so
`home_disconnect.parse_profile()` can read it (e.g. when migrating a config
entry to home-disconnect 2.x). New profiles keep their original XML
(`LoadedProfile.description_xml`/`feature_mapping_xml`) and don't need this.

The code is Home Connect Local's own (vemboy200); only the input format comes
from home-disconnect 1.x. The notes below describe what the 1.x dictionary
had already lost, so the regenerated files differ from BSH's originals in
three ways - none of which affect parsing them again:

- Error code descriptions are not restored. parse_feature_mapping() reads
  errorDescription/error while parsing but never stores the result anywhere in
  DeviceDescription, so that data is already gone by the time this function
  ever sees it. A single placeholder entry is emitted purely to keep the
  regenerated FeatureMapping.xml structurally valid.
- Enum IDs are not preserved. parse_element() replaces the original
  enumerationType/refENID reference with the fully-resolved {value: name}
  dict and discards the numeric ID, so fresh IDs are assigned here instead
  (deduplicated - identical choice lists share one ID, same as the original
  files do).
- Original nesting of statusList/settingList/etc is not preserved. Nested
  sub-lists are flattened into their parent list by parse_elements()
  regardless of depth, and wrapper-level attributes on those *List elements
  are never read at all - so every element is emitted directly under one
  flat *List per category.

One more thing the 1.x dictionary never had: per-program option ranges. It
only kept each program option's access, availability and default, so the
regenerated files have no per-program min/max/stepSize. The option's own
range (from the optionList) is still there; loading the appliance's original
profile files again brings the per-program ranges back.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The 1.x dictionary shapes: a whole description, one element, one program option.
type DeviceDescription = Mapping[str, Any]
type EntityDescription = Mapping[str, Any]
type OptionDescription = Mapping[str, Any]

# Enum IDs are invented fresh (see module docstring), starting well above any
# real refCID (small ints) or refUID (seen up to ~4 hex digits) range so there
# is no realistic chance of collision.
_ENUM_ID_START = 0xF000

_LIST_TAGS = {
    "status": "statusList",
    "setting": "settingList",
    "event": "eventList",
    "command": "commandList",
    "option": "optionList",
}
_SINGLE_TAGS = ("activeProgram", "selectedProgram", "protectionPort")

_BOOL_ATTRS = (
    "available",
    "notifyOnChange",
    "passwordProtected",
    "liveUpdate",
    "fullOptionSet",
    "validate",
)
_HEX_ATTRS = ("refCID", "refDID")
_PASSTHROUGH_ATTRS = (
    "access",
    "execution",
    "min",
    "max",
    "stepSize",
    "initValue",
    "default",
    "level",
    "handling",
)


def _escape(value: Any) -> str:
    text = str(value)
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _hex(value: int) -> str:
    return format(value, "04X")


class _EnumRegistry:
    """Assigns and deduplicates fresh IDs for enumeration choice lists."""

    def __init__(self) -> None:
        self._ids: dict[tuple[tuple[int, str], ...], int] = {}
        self._next_id = _ENUM_ID_START

    def id_for(self, enumeration: dict[int, str]) -> int:
        key = tuple(sorted(enumeration.items()))
        if key not in self._ids:
            self._ids[key] = self._next_id
            self._next_id += 1
        return self._ids[key]

    def items(self) -> list[tuple[int, dict[int, str]]]:
        return [(enum_id, dict(key)) for key, enum_id in self._ids.items()]


def _element_attrs(element: EntityDescription, enums: _EnumRegistry) -> str:
    parts = []
    if "uid" in element:
        parts.append(f'uid="{_hex(element["uid"])}"')
    parts.extend(f'{key}="{_hex(element[key])}"' for key in _HEX_ATTRS if key in element)
    parts.extend(f'{key}="{str(element[key]).lower()}"' for key in _BOOL_ATTRS if key in element)
    parts.extend(f'{key}="{_escape(element[key])}"' for key in _PASSTHROUGH_ATTRS if key in element)
    enumeration = element.get("enumeration")
    if enumeration is not None:
        parts.append(f'enumerationType="{_hex(enums.id_for(enumeration))}"')
    return " ".join(parts)


def _option_ref_attrs(option: OptionDescription) -> str:
    parts = []
    if "refUID" in option:
        parts.append(f'refUID="{_hex(option["refUID"])}"')
    parts.extend(
        f'{key}="{_escape(option[key])}"' for key in ("access", "default") if key in option
    )
    parts.extend(
        f'{key}="{str(option[key]).lower()}"'
        for key in ("available", "liveUpdate")
        if key in option
    )
    return " ".join(parts)


def _element_xml(tag: str, element: EntityDescription, enums: _EnumRegistry) -> str:
    attrs = _element_attrs(element, enums)
    options = element.get("options")
    if not options:
        return f"<{tag} {attrs} />"
    inner = "".join(f"<option {_option_ref_attrs(option)} />" for option in options)
    return f"<{tag} {attrs}>{inner}</{tag}>"


def _feature_description_xml(features: dict[int, str]) -> str:
    entries = "".join(
        f'<feature refUID="{_hex(uid)}">{_escape(name)}</feature>' for uid, name in features.items()
    )
    return f"<featureDescription>{entries}</featureDescription>"


def _enum_description_list_xml(enums: _EnumRegistry) -> str:
    entries = []
    for enum_id, values in enums.items():
        members = "".join(
            f'<enumMember refValue="{value}">{_escape(name)}</enumMember>'
            for value, name in values.items()
        )
        entries.append(f'<enumDescription refENID="{_hex(enum_id)}">{members}</enumDescription>')
    return f"<enumDescriptionList>{''.join(entries)}</enumDescriptionList>"


def serialize_legacy_description(description: DeviceDescription) -> tuple[str, str]:
    """
    Turn a home-disconnect 1.x description back into DeviceDescription.xml/FeatureMapping.xml.

    Args:
    ----
        description (DeviceDescription): A description as home-disconnect 1.x
            parsed it (and Home Connect Local 1.x stored it).

    Returns:
    -------
        tuple[str, str]: (device_description_xml, feature_mapping_xml)

    """
    enums = _EnumRegistry()
    features: dict[int, str] = {}
    body_parts: list[str] = []

    info = description.get("info", {})
    body_parts.append(
        "<description>"
        f"<type>{_escape(info.get('type', ''))}</type>"
        f"<brand>{_escape(info.get('brand', ''))}</brand>"
        f"<model>{_escape(info.get('model', ''))}</model>"
        f"<version>{_escape(info.get('version', 0))}</version>"
        f"<revision>{_escape(info.get('revision', 0))}</revision>"
        "</description>"
    )

    for key, list_tag in _LIST_TAGS.items():
        elements = description.get(key) or []
        inner = "".join(_element_xml(key, element, enums) for element in elements)
        body_parts.append(f"<{list_tag}>{inner}</{list_tag}>")
        for element in elements:
            if "uid" in element and "name" in element:
                features[element["uid"]] = element["name"]

    programs = description.get("program") or []
    inner = "".join(_element_xml("program", program, enums) for program in programs)
    body_parts.append(f"<programGroup>{inner}</programGroup>")
    for program in programs:
        if "uid" in program and "name" in program:
            features[program["uid"]] = program["name"]

    for key in _SINGLE_TAGS:
        element = description.get(key)
        if element is not None:
            body_parts.append(_element_xml(key, element, enums))
            if "uid" in element and "name" in element:
                features[element["uid"]] = element["name"]

    device_description_xml = (
        '<?xml version="1.0" encoding="UTF-8"?><device>' + "".join(body_parts) + "</device>"
    )

    feature_mapping_xml = (
        '<?xml version="1.0" encoding="utf-8"?><featureMappingFile>'
        + _feature_description_xml(features)
        + '<errorDescription><error refEID="0001">Unknown</error></errorDescription>'
        + _enum_description_list_xml(enums)
        + "</featureMappingFile>"
    )

    return device_description_xml, feature_mapping_xml
