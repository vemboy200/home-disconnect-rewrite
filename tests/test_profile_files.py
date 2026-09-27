import io
import json
import logging
import zipfile
from pathlib import Path

import pytest

from home_disconnect.profile import ProfileError
from home_disconnect.profile_files import (
    build_profile_zip,
    load_profiles,
    load_profiles_from_zip,
    profile_filename_stub,
)

from .profile_fixtures import (
    DESCRIPTION,
    FEATURE_MAPPING,
    KEY64,
    OTHER_FEATURE_MAPPING,
    profile_json,
)

NAME = "TEST-DW100-001122334455"


def write_profile(
    folder: Path, name: str = NAME, overrides: dict[str, object] | None = None
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(profile_json(name, **(overrides or {})))
    (folder / f"{name}_DeviceDescription.xml").write_text(DESCRIPTION)
    (folder / f"{name}_FeatureMapping.xml").write_text(FEATURE_MAPPING)


def zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_folder_with_json(tmp_path: Path) -> None:
    write_profile(tmp_path)
    (loaded,) = load_profiles(tmp_path)
    assert loaded.profile.info.model == "DW100"
    assert loaded.connection is not None
    assert loaded.connection.ha_id == NAME
    assert loaded.connection.connection_type == "AES"
    assert loaded.connection.psk64 == KEY64
    assert loaded.connection.iv64 is not None
    assert loaded.json_file == f"{NAME}.json"
    assert loaded.description_xml.startswith(b"<?xml")


def test_zip_file_and_zip_bytes(tmp_path: Path) -> None:
    data = zip_bytes(
        {
            f"{NAME}.json": profile_json(),
            f"{NAME}_DeviceDescription.xml": DESCRIPTION,
            f"{NAME}_FeatureMapping.xml": FEATURE_MAPPING,
        }
    )
    path = tmp_path / "profile.zip"
    path.write_bytes(data)
    assert load_profiles(path)[0].connection is not None
    assert load_profiles_from_zip(data)[0].profile.info.model == "DW100"


def test_zip_with_subfolder_and_macos_junk() -> None:
    data = zip_bytes(
        {
            f"Dishwasher copy/{NAME}.json": profile_json(),
            f"Dishwasher copy/{NAME}_DeviceDescription.xml": DESCRIPTION,
            f"Dishwasher copy/{NAME}_FeatureMapping.xml": FEATURE_MAPPING,
            f"__MACOSX/Dishwasher copy/._{NAME}.json": "\x00\x05junk",
            f"__MACOSX/Dishwasher copy/._{NAME}_DeviceDescription.xml": "\x00\x05junk",
        }
    )
    (loaded,) = load_profiles_from_zip(data)
    assert loaded.description_file == f"Dishwasher copy/{NAME}_DeviceDescription.xml"


def test_several_appliances_in_one_zip() -> None:
    files = {}
    for name in ("TEST-A-1", "TEST-B-2"):
        files[f"{name}.json"] = profile_json(name)
        files[f"{name}_DeviceDescription.xml"] = DESCRIPTION
        files[f"{name}_FeatureMapping.xml"] = FEATURE_MAPPING
    loaded = load_profiles_from_zip(zip_bytes(files))
    assert sorted(p.connection.ha_id for p in loaded if p.connection) == ["TEST-A-1", "TEST-B-2"]


def test_xml_pair_without_json(tmp_path: Path) -> None:
    (tmp_path / "bosch_DW100_DeviceDescription.xml").write_text(DESCRIPTION)
    (tmp_path / "bosch_DW100_FeatureMapping.xml").write_text(FEATURE_MAPPING)
    (loaded,) = load_profiles(tmp_path)
    assert loaded.connection is None
    assert loaded.json_file is None


def test_tls_profile_needs_no_iv(tmp_path: Path) -> None:
    write_profile(tmp_path, overrides={"connectionType": "TLS", "iv": None})
    (loaded,) = load_profiles(tmp_path)
    assert loaded.connection is not None
    assert loaded.connection.connection_type == "TLS"
    assert loaded.connection.iv64 is None


def test_copy_is_ignored_when_the_original_is_valid(tmp_path: Path) -> None:
    write_profile(tmp_path)
    (tmp_path / f"{NAME}_DeviceDescription copy.xml").write_text("broken")
    (loaded,) = load_profiles(tmp_path)
    assert loaded.description_file == f"{NAME}_DeviceDescription.xml"


def test_valid_copy_rescues_a_broken_original(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_profile(tmp_path)
    (tmp_path / f"{NAME}_DeviceDescription.xml").write_text("<broken")
    (tmp_path / f"{NAME}_DeviceDescription copy.xml").write_text(DESCRIPTION)
    with caplog.at_level(logging.WARNING):
        (loaded,) = load_profiles(tmp_path)
    assert loaded.description_file == f"{NAME}_DeviceDescription copy.xml"
    assert "instead of the expected" in caplog.text


def test_every_candidate_broken_names_them_all(tmp_path: Path) -> None:
    write_profile(tmp_path)
    (tmp_path / f"{NAME}_DeviceDescription.xml").write_text("<broken")
    (tmp_path / f"{NAME}_DeviceDescription copy.xml").write_text("also broken")
    with pytest.raises(ProfileError) as raised:
        load_profiles(tmp_path)
    assert f"{NAME}_DeviceDescription.xml: isn't valid XML" in str(raised.value)
    assert f"{NAME}_DeviceDescription copy.xml: isn't valid XML" in str(raised.value)


def test_mismatched_pair_is_reported(tmp_path: Path) -> None:
    write_profile(tmp_path)
    (tmp_path / f"{NAME}_FeatureMapping.xml").write_text(OTHER_FEATURE_MAPPING)
    with pytest.raises(ProfileError, match="don't belong together"):
        load_profiles(tmp_path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"haId": None}, "has no haId"),
        ({"connectionType": "XYZ"}, "unknown connectionType"),
        ({"key": "short"}, "no valid key"),
        ({"key": None}, "no valid key"),
        ({"iv": None}, "AES profile without a valid iv"),
        ({"iv": KEY64}, "AES profile without a valid iv"),
        ({"deviceDescriptionFileName": "missing.xml"}, "refers to missing.xml"),
        ({"featureMappingFileName": "missing.xml"}, "refers to missing.xml"),
    ],
)
def test_bad_json_is_reported_by_name(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    write_profile(tmp_path, overrides=overrides)
    with pytest.raises(ProfileError, match=message) as raised:
        load_profiles(tmp_path)
    assert raised.value.file == f"{NAME}.json"


def test_invalid_json_file(tmp_path: Path) -> None:
    write_profile(tmp_path)
    (tmp_path / f"{NAME}.json").write_text('{"deviceDescriptionFileName": "x.xml", ')
    # Not a profile JSON; the XML pair still loads without connection details.
    (loaded,) = load_profiles(tmp_path)
    assert loaded.connection is None


def test_one_bad_profile_doesnt_hide_a_good_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_profile(tmp_path, "TEST-GOOD-1")
    write_profile(tmp_path, "TEST-BAD-2", {"key": "short"})
    with caplog.at_level(logging.WARNING):
        loaded = load_profiles(tmp_path)
    assert [p.connection.ha_id for p in loaded if p.connection] == ["TEST-GOOD-1"]
    assert "Skipped an invalid profile" in caplog.text


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("missing", "doesn't exist"),
        ("not_zip", "isn't a ZIP file or a folder"),
        ("empty", "contains no appliance profile"),
    ],
)
def test_unusable_paths(tmp_path: Path, setup: str, message: str) -> None:
    path = tmp_path / "input"
    if setup == "not_zip":
        path.write_text("hello")
    elif setup == "empty":
        path.mkdir()
        (path / "readme.txt").write_text("hi")
    with pytest.raises(ProfileError, match=message):
        load_profiles(path)


def test_upload_that_isnt_a_zip() -> None:
    with pytest.raises(ProfileError, match="upload: isn't a ZIP file"):
        load_profiles_from_zip(b"not a zip")


def test_build_safe_export_has_no_key() -> None:
    data = build_profile_zip(DESCRIPTION, FEATURE_MAPPING, stub="test_DW100")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert sorted(archive.namelist()) == [
            "test_DW100_DeviceDescription.xml",
            "test_DW100_FeatureMapping.xml",
        ]
    (loaded,) = load_profiles_from_zip(data)
    assert loaded.connection is None
    assert loaded.description_xml == DESCRIPTION.encode()


def test_build_full_export_imports_again(tmp_path: Path) -> None:
    write_profile(tmp_path)
    (original,) = load_profiles(tmp_path)
    assert original.connection is not None
    data = build_profile_zip(
        original.description_xml,
        original.feature_mapping_xml,
        stub=profile_filename_stub("TEST", "DW100"),
        connection=original.connection,
        info={"brand": "TEST", "vib": "DW100", "type": "Dishwasher", "mac": "00-11"},
    )
    (loaded,) = load_profiles_from_zip(data)
    assert loaded.connection is not None
    assert loaded.connection.ha_id == original.connection.ha_id
    assert loaded.connection.psk64 == original.connection.psk64
    assert loaded.connection.iv64 == original.connection.iv64
    assert loaded.connection.raw["vib"] == "DW100"
    assert loaded.json_file == "test_DW100.json"
    assert loaded.description_xml == original.description_xml


def test_build_tls_export_has_no_iv(tmp_path: Path) -> None:
    write_profile(tmp_path, overrides={"connectionType": "TLS", "iv": None})
    (original,) = load_profiles(tmp_path)
    assert original.connection is not None
    data = build_profile_zip(
        original.description_xml,
        original.feature_mapping_xml,
        stub="x",
        connection=original.connection,
    )
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        profile = json.loads(archive.read("x.json"))
    assert "iv" not in profile
    assert profile["connectionType"] == "TLS"
    assert profile["brand"] == ""


def test_profile_filename_stub() -> None:
    assert profile_filename_stub("THERMADOR", "DWHD660WFP") == "thermador_DWHD660WFP"
    assert profile_filename_stub(None, None) == "unknown_appliance"
