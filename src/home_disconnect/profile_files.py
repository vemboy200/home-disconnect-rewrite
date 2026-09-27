"""Find and load appliance profiles from a ZIP file, ZIP bytes or a folder, and build exports.

A profile is three files, as the Home Connect Profile Downloader writes them:

- `<name>.json` with the connection details (`haId`, `connectionType`, `key`, `iv` for AES)
  and the names of the other two files (`deviceDescriptionFileName`, `featureMappingFileName`);
- `<name>_DeviceDescription.xml` and `<name>_FeatureMapping.xml`.

One ZIP or folder can hold several appliances. A pair of XML files without a JSON (like Home
Connect Local's "safe" export, which leaves out the key) loads as a profile without
connection details.

Candidates are checked, not trusted: a broken file is reported by name, and when there's more
than one candidate for the same file (e.g. `..._DeviceDescription copy.xml`), the exact name
is tried first and the others only if it's invalid.

The loading functions read files and are blocking; run them in an executor from async code.
`build_profile_zip()` (Home Connect Local's export, vemboy200) builds a ZIP in memory.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from .crypto import decode_key
from .profile import DeviceProfile, ProfileError, parse_profile

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_LOGGER = logging.getLogger(__name__)

DESCRIPTION_SUFFIX = "_DeviceDescription"
FEATURE_MAPPING_SUFFIX = "_FeatureMapping"
PSK_LENGTH = 32
IV_LENGTH = 16
# Profiles are tens to a few hundred kilobytes; anything far bigger isn't one.
MAX_FILE_SIZE = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ConnectionDetails:
    """What the profile's JSON says about connecting to the appliance."""

    ha_id: str
    connection_type: Literal["AES", "TLS"]
    psk64: str
    iv64: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LoadedProfile:
    """One appliance's profile and the files it came from."""

    profile: DeviceProfile
    connection: ConnectionDetails | None
    description_file: str
    feature_mapping_file: str
    json_file: str | None = None
    description_xml: bytes = b""
    feature_mapping_xml: bytes = b""


def _is_junk(path: PurePosixPath) -> bool:
    return any(part == "__MACOSX" for part in path.parts) or path.name.startswith("._")


class _Files:
    """The readable files of a ZIP or folder, by relative POSIX path."""

    def __init__(self, paths: list[PurePosixPath], read: Callable[[PurePosixPath], bytes]) -> None:
        self.paths = [p for p in paths if not _is_junk(p)]
        self._read = read

    def read(self, path: PurePosixPath) -> bytes:
        return self._read(path)


def _files_from_zip(archive: zipfile.ZipFile, label: str) -> _Files:
    infos = {PurePosixPath(i.filename): i for i in archive.infolist() if not i.is_dir()}

    def read(path: PurePosixPath) -> bytes:
        info = infos[path]
        if info.file_size > MAX_FILE_SIZE:
            msg = "is too large to be a profile file"
            raise ProfileError(msg, f"{label}/{path}")
        return archive.read(info)

    return _Files(list(infos), read)


def _files_from_folder(folder: Path) -> _Files:
    paths = [p.relative_to(folder) for p in folder.rglob("*") if p.is_file()]

    def read(path: PurePosixPath) -> bytes:
        full = folder / path
        if full.stat().st_size > MAX_FILE_SIZE:
            msg = "is too large to be a profile file"
            raise ProfileError(msg, str(path))
        return full.read_bytes()

    return _Files([PurePosixPath(p.as_posix()) for p in paths], read)


def _parse_connection(data: bytes, file: str) -> ConnectionDetails:
    try:
        raw = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        msg = f"isn't valid JSON ({err})"
        raise ProfileError(msg, file) from err
    if not isinstance(raw, dict):
        msg = "isn't a JSON object"
        raise ProfileError(msg, file)
    return connection_from_mapping(raw, file)


def connection_from_mapping(raw: Mapping[str, Any], source: str) -> ConnectionDetails:
    """Check and build connection details from a profile's JSON fields.

    `source` names where they came from, for error messages.
    """
    file = source
    ha_id, connection_type, key = raw.get("haId"), raw.get("connectionType"), raw.get("key")
    if not isinstance(ha_id, str) or not ha_id:
        msg = "has no haId"
        raise ProfileError(msg, file)
    if connection_type not in ("AES", "TLS"):
        msg = f"has an unknown connectionType {connection_type!r}"
        raise ProfileError(msg, file)
    if not isinstance(key, str) or _decoded_length(key) != PSK_LENGTH:
        msg = f"has no valid key (expected {PSK_LENGTH} bytes)"
        raise ProfileError(msg, file)
    iv = raw.get("iv")
    if connection_type == "AES" and (not isinstance(iv, str) or _decoded_length(iv) != IV_LENGTH):
        msg = f"is an AES profile without a valid iv (expected {IV_LENGTH} bytes)"
        raise ProfileError(msg, file)
    return ConnectionDetails(
        ha_id=ha_id,
        connection_type=connection_type,
        psk64=key,
        iv64=iv if connection_type == "AES" else None,
        raw=dict(raw),
    )


def _decoded_length(value: str) -> int:
    try:
        return len(decode_key(value))
    except ValueError:
        return -1


def _looks_like_profile_json(data: bytes) -> bool:
    try:
        raw = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(raw, dict) and "deviceDescriptionFileName" in raw


def _candidates(files: _Files, exact: PurePosixPath) -> list[PurePosixPath]:
    """Return the exact file, then its copies (same name plus e.g. " copy" or " (1)").

    Returns nothing when the exact file is missing: guessing another file of the same kind
    could pair one appliance's key with another appliance's description.
    """
    if exact not in files.paths:
        return []
    copies = [
        path
        for path in files.paths
        if path != exact
        and path.parent == exact.parent
        and path.suffix.lower() == ".xml"
        and path.stem.startswith(exact.stem)
    ]
    return [exact, *sorted(copies)]


def _load_pair(
    files: _Files,
    description: PurePosixPath,
    feature_mapping: PurePosixPath,
    connection: ConnectionDetails | None,
    json_file: str | None,
) -> LoadedProfile:
    """Load a description/mapping pair, trying copies when the exact files are invalid."""
    description_candidates = _candidates(files, description)
    mapping_candidates = _candidates(files, feature_mapping)
    if not description_candidates:
        msg = f"refers to {description.name}, which isn't there"
        raise ProfileError(msg, json_file or str(description))
    if not mapping_candidates:
        msg = f"refers to {feature_mapping.name}, which isn't there"
        raise ProfileError(msg, json_file or str(feature_mapping))

    problems: list[str] = []
    for mapping_path in mapping_candidates:
        mapping_xml = files.read(mapping_path)
        for description_path in description_candidates:
            description_xml = files.read(description_path)
            try:
                profile = parse_profile(
                    description_xml,
                    mapping_xml,
                    description_file=str(description_path),
                    feature_mapping_file=str(mapping_path),
                )
            except ProfileError as err:
                problems.append(str(err))
                continue
            if (description_path, mapping_path) != (description, feature_mapping):
                _LOGGER.warning(
                    "Using %s and %s instead of the expected %s and %s",
                    description_path,
                    mapping_path,
                    description.name,
                    feature_mapping.name,
                )
            return LoadedProfile(
                profile=profile,
                connection=connection,
                description_file=str(description_path),
                feature_mapping_file=str(mapping_path),
                json_file=json_file,
                description_xml=description_xml,
                feature_mapping_xml=mapping_xml,
            )
    raise ProfileError("no valid profile files:\n- " + "\n- ".join(dict.fromkeys(problems)))


def _load_json_profiles(
    files: _Files, used: set[PurePosixPath], errors: list[ProfileError]
) -> list[LoadedProfile]:
    profiles: list[LoadedProfile] = []
    for path in sorted(files.paths):
        if path.suffix.lower() != ".json":
            continue
        data = files.read(path)
        if not _looks_like_profile_json(data):
            continue
        # Whatever happens, this JSON's XML files belong to it: a broken JSON must not let
        # its pair load later as a profile without a key.
        named = json.loads(data)
        used.update(
            path.parent / str(name)
            for name in (
                named.get("deviceDescriptionFileName"),
                named.get("featureMappingFileName"),
            )
            if name
        )
        used.update(
            path.parent / f"{path.stem}{suffix}.xml"
            for suffix in (DESCRIPTION_SUFFIX, FEATURE_MAPPING_SUFFIX)
        )
        try:
            connection = _parse_connection(data, str(path))
            description = path.parent / str(connection.raw["deviceDescriptionFileName"])
            mapping = path.parent / str(connection.raw.get("featureMappingFileName", ""))
            profiles.append(_load_pair(files, description, mapping, connection, str(path)))
        except ProfileError as err:
            errors.append(err)
    return profiles


def _load_xml_pairs(
    files: _Files, used: set[PurePosixPath], errors: list[ProfileError]
) -> list[LoadedProfile]:
    """Load DeviceDescription/FeatureMapping pairs that no JSON claimed."""
    profiles: list[LoadedProfile] = []
    for path in sorted(files.paths):
        if path in used or path.suffix != ".xml" or not path.stem.endswith(DESCRIPTION_SUFFIX):
            continue
        prefix = path.stem.removesuffix(DESCRIPTION_SUFFIX)
        mapping = path.with_name(f"{prefix}{FEATURE_MAPPING_SUFFIX}.xml")
        if mapping not in files.paths or mapping in used:
            continue
        try:
            profiles.append(_load_pair(files, path, mapping, None, None))
        except ProfileError as err:
            errors.append(err)
    return profiles


def _load(files: _Files) -> list[LoadedProfile]:
    used: set[PurePosixPath] = set()
    errors: list[ProfileError] = []
    profiles = _load_json_profiles(files, used, errors)
    profiles += _load_xml_pairs(files, used, errors)
    if not profiles:
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ProfileError("no valid profile:\n- " + "\n- ".join(str(e) for e in errors))
        msg = "contains no appliance profile (no DeviceDescription/FeatureMapping pair)"
        raise ProfileError(msg)
    for error in errors:
        _LOGGER.warning("Skipped an invalid profile: %s", error)
    return profiles


def load_profiles(path: str | Path) -> list[LoadedProfile]:
    """Load every appliance profile in a ZIP file or a folder."""
    path = Path(path)
    if path.is_dir():
        return _load(_files_from_folder(path))
    if not path.is_file():
        msg = "doesn't exist"
        raise ProfileError(msg, str(path))
    try:
        with zipfile.ZipFile(path) as archive:
            return _load(_files_from_zip(archive, path.name))
    except zipfile.BadZipFile as err:
        msg = "isn't a ZIP file or a folder"
        raise ProfileError(msg, str(path)) from err


def load_profiles_from_zip(data: bytes) -> list[LoadedProfile]:
    """Load every appliance profile in a ZIP file held in memory, e.g. an upload."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return _load(_files_from_zip(archive, "upload"))
    except zipfile.BadZipFile as err:
        msg = "isn't a ZIP file"
        raise ProfileError(msg, "upload") from err


def profile_filename_stub(brand: str | None, model: str | None) -> str:
    """Build a `{brand}_{model}` file name, instead of the MAC-based original."""
    return f"{(brand or 'unknown').lower()}_{model or 'appliance'}"


def build_profile_zip(
    description_xml: str | bytes,
    feature_mapping_xml: str | bytes,
    *,
    stub: str,
    connection: ConnectionDetails | None = None,
    info: Mapping[str, Any] | None = None,
) -> bytes:
    """Build a profile ZIP that `load_profiles_from_zip()` (and the setup upload) can read.

    Without `connection` it's a "safe" export: only the two XML files, which never contain the
    key, the MAC or the serial number, so it can be shared (e.g. attached to an issue). With
    `connection` it also has the `.json` with the key (and IV for AES), in the shape the Home
    Connect Profile Downloader writes, so it can be imported again. `info` fills the JSON's
    `brand`, `vib`, `mac` and `type` fields.
    """
    description_file = f"{stub}{DESCRIPTION_SUFFIX}.xml"
    mapping_file = f"{stub}{FEATURE_MAPPING_SUFFIX}.xml"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(description_file, description_xml)
        archive.writestr(mapping_file, feature_mapping_xml)
        if connection is not None:
            details = info or {}
            profile: dict[str, Any] = {
                "haId": connection.ha_id,
                "brand": details.get("brand", ""),
                "vib": details.get("vib", ""),
                "mac": details.get("mac", ""),
                "type": details.get("type", ""),
                "featureMappingFileName": mapping_file,
                "deviceDescriptionFileName": description_file,
                "connectionType": connection.connection_type,
                "key": connection.psk64,
            }
            if connection.iv64:
                profile["iv"] = connection.iv64
            archive.writestr(f"{stub}.json", json.dumps(profile, indent=2))
    return buffer.getvalue()
