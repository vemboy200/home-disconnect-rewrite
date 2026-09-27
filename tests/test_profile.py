import pytest

from home_disconnect.profile import FeatureKind, ProfileError, parse_profile

from .profile_fixtures import DESCRIPTION, FEATURE_MAPPING, OTHER_FEATURE_MAPPING


def test_device_info() -> None:
    profile = parse_profile(DESCRIPTION, FEATURE_MAPPING)
    assert profile.info.type == "Dishwasher"
    assert profile.info.brand == "TEST"
    assert profile.info.model == "DW100"
    assert profile.info.version == "2"
    assert profile.info.pairable_device_types == ("Application",)


def test_features_by_kind_including_nested_lists() -> None:
    profile = parse_profile(DESCRIPTION, FEATURE_MAPPING)
    kinds = {feature.name: feature.kind for feature in profile.features.values()}
    assert kinds == {
        "BSH.Common.Status.DoorState": FeatureKind.STATUS,
        "BSH.Common.Status.ProgramProgress": FeatureKind.STATUS,
        "BSH.Common.Setting.PowerState": FeatureKind.SETTING,
        "Dishcare.Dishwasher.Event.SaltNearlyEmpty": FeatureKind.EVENT,
        "BSH.Common.Command.AbortProgram": FeatureKind.COMMAND,
        "BSH.Common.Command.AcknowledgeEvent": FeatureKind.COMMAND,
        "BSH.Common.Command.RejectEvent": FeatureKind.COMMAND,
        "BSH.Common.Option.Duration": FeatureKind.OPTION,
        "BSH.Common.Root.ActiveProgram": FeatureKind.ACTIVE_PROGRAM,
        "BSH.Common.Root.SelectedProgram": FeatureKind.SELECTED_PROGRAM,
    }
    assert profile.unnamed_uids == frozenset()


def test_feature_attributes() -> None:
    profile = parse_profile(DESCRIPTION, FEATURE_MAPPING)
    progress = profile.feature("BSH.Common.Status.ProgramProgress")
    assert progress is not None
    assert progress.uid == 0x0212  # noqa: PLR2004
    assert progress.access == "read"
    assert progress.available is True
    assert (progress.content_type, progress.data_type) == (0x10, 0x82)
    assert (progress.min, progress.max, progress.init_value) == ("0", "100", "0")
    event = profile.feature("Dishcare.Dishwasher.Event.SaltNearlyEmpty")
    assert event is not None
    assert event.extra == {"handling": "acknowledge", "level": "alert"}
    selected = profile.feature("BSH.Common.Root.SelectedProgram")
    assert selected is not None
    assert selected.extra == {"fullOptionSet": "true"}
    assert profile.feature("Not.There") is None


def test_enumerations_are_subsets_sorted_by_value() -> None:
    profile = parse_profile(DESCRIPTION, FEATURE_MAPPING)
    door = profile.feature("BSH.Common.Status.DoorState")
    assert door is not None
    assert door.enumeration is not None
    # The description only allows 0 and 1, so "Locked" isn't offered.
    assert door.enumeration.values == {0: "Open", 1: "Closed"}
    assert door.enumeration.key == "BSH.Common.EnumType.DoorState"
    power = profile.feature("BSH.Common.Setting.PowerState")
    assert power is not None
    assert power.enumeration is not None
    assert list(power.enumeration.values) == [1, 2, 10]


def test_enumerations_only_in_the_feature_mapping() -> None:
    # Home Connect Local's export leaves out the description's enumerationTypeList.
    description = DESCRIPTION.split("<enumerationTypeList>")[0] + "</device>"
    profile = parse_profile(description, FEATURE_MAPPING)
    door = profile.feature("BSH.Common.Status.DoorState")
    assert door is not None
    assert door.enumeration is not None
    assert door.enumeration.values == {0: "Open", 1: "Closed", 2: "Locked"}


def test_programs_with_groups_and_options() -> None:
    profile = parse_profile(DESCRIPTION, FEATURE_MAPPING)
    eco = profile.programs[0x1001]
    assert eco.name == "Dishcare.Dishwasher.Program.Eco50"
    assert eco.available is True
    assert eco.execution == "selectAndStart"
    assert eco.group == "Dishcare.Dishwasher.ProgramGroup.Main"
    (duration,) = eco.options
    assert duration.name == "BSH.Common.Option.Duration"
    assert (duration.min, duration.max, duration.step, duration.default) == (
        "30",
        "120",
        "10",
        "60",
    )
    assert duration.extra == {"liveUpdate": "true"}
    quick = profile.programs[0x1002]
    assert quick.available is False
    assert quick.extra == {"fullOptionSet": "false"}
    assert eco.extra == {}
    assert quick.group is None
    assert quick.options == ()


def test_errors() -> None:
    profile = parse_profile(DESCRIPTION, FEATURE_MAPPING)
    assert profile.errors == {0x10: "BSH.Common.Error.Unknown"}


def test_bytes_input() -> None:
    profile = parse_profile(DESCRIPTION.encode(), FEATURE_MAPPING.encode())
    assert profile.info.model == "DW100"


@pytest.mark.parametrize(
    ("description", "mapping", "message"),
    [
        ("not xml", FEATURE_MAPPING, "desc.xml: isn't valid XML"),
        (DESCRIPTION, "<broken", "map.xml: isn't valid XML"),
        (FEATURE_MAPPING, FEATURE_MAPPING, "desc.xml: isn't a DeviceDescription"),
        (DESCRIPTION, DESCRIPTION, "map.xml: isn't a FeatureMapping"),
        (
            DESCRIPTION,
            OTHER_FEATURE_MAPPING,
            "desc.xml and map.xml: .* don't belong together",
        ),
        (
            '<device xmlns="x"><statusList uid="1"/></device>',
            FEATURE_MAPPING,
            "describes no features",
        ),
        (
            '<device xmlns="x"><statusList><status uid="zz"/></statusList></device>',
            FEATURE_MAPPING,
            "missing or invalid attribute",
        ),
        (
            DESCRIPTION,
            '<featureMappingFile xmlns="x"><featureDescription/></featureMappingFile>',
            "names no features",
        ),
        (
            DESCRIPTION,
            (
                '<featureMappingFile xmlns="x"><featureDescription><feature>X</feature>'
                "</featureDescription></featureMappingFile>"
            ),
            "missing or invalid ID",
        ),
    ],
)
def test_bad_files_are_reported_by_name(description: str, mapping: str, message: str) -> None:
    with pytest.raises(ProfileError, match=message) as raised:
        parse_profile(
            description, mapping, description_file="desc.xml", feature_mapping_file="map.xml"
        )
    assert raised.value.file is not None or "describes no features" in message
