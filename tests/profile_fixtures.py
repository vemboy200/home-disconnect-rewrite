"""Small, made-up profile files in the real format, for tests."""

import base64
import json

DESCRIPTION = """<?xml version="1.0" encoding="UTF-8"?>
<device xmlns="http://www.home-connect.com/schemas/DeviceDescription/20140417">
  <description>
    <type>Dishwasher</type><brand>TEST</brand><model>DW100</model>
    <version>2</version><revision>1</revision>
    <pairableDeviceTypes><deviceType>Application</deviceType></pairableDeviceTypes>
  </description>
  <statusList access="read" available="true" uid="0102">
    <status access="read" available="true" enumerationType="0201" refCID="03" refDID="80"
            uid="020F"/>
    <statusList access="read" available="true" uid="0110">
      <status access="read" available="true" initValue="0" max="100" min="0" refCID="0010"
              refDID="0082" uid="0212"/>
    </statusList>
  </statusList>
  <settingList access="readWrite" available="true" uid="0103">
    <setting access="readWrite" available="true" enumerationType="1001" refCID="03"
             refDID="80" uid="0219"/>
  </settingList>
  <eventList uid="0104">
    <event enumerationType="0001" handling="acknowledge" level="alert" refCID="03" refDID="80"
           uid="0231"/>
  </eventList>
  <commandList access="writeOnly" available="true" uid="0105">
    <command access="writeOnly" available="true" refCID="01" refDID="00" uid="0226"/>
  </commandList>
  <optionList access="readWrite" available="true" uid="0106">
    <option access="readWrite" available="true" refCID="10" refDID="82" uid="0228"/>
  </optionList>
  <programGroup available="true" uid="0107">
    <programGroup available="true" uid="0108">
      <program available="true" execution="selectAndStart" uid="1001">
        <option access="readWrite" available="true" default="60" max="120" min="30"
                stepSize="10" liveUpdate="true" refUID="0228"/>
      </program>
    </programGroup>
    <program available="false" execution="startOnly" uid="1002"/>
  </programGroup>
  <activeProgram access="readWrite" uid="0100"/>
  <selectedProgram access="readWrite" fullOptionSet="true" uid="0101"/>
  <enumerationTypeList>
    <enumerationType enid="0201"><enumeration value="1"/><enumeration value="0"/></enumerationType>
    <enumerationType enid="1001"><enumeration value="2"/><enumeration value="10"/>
      <enumeration value="1"/></enumerationType>
    <enumerationType enid="0001"><enumeration value="0"/><enumeration value="1"/>
      <enumeration value="2"/></enumerationType>
  </enumerationTypeList>
</device>
"""

FEATURE_MAPPING = """<?xml version="1.0" encoding="UTF-8"?>
<featureMappingFile xmlns="http://www.home-connect.com/schemas/FeatureMapping/20140417">
  <featureDescription>
    <feature refUID="0100">BSH.Common.Root.ActiveProgram</feature>
    <feature refUID="0101">BSH.Common.Root.SelectedProgram</feature>
    <feature refUID="0108">Dishcare.Dishwasher.ProgramGroup.Main</feature>
    <feature refUID="020F">BSH.Common.Status.DoorState</feature>
    <feature refUID="0212">BSH.Common.Status.ProgramProgress</feature>
    <feature refUID="0219">BSH.Common.Setting.PowerState</feature>
    <feature refUID="0231">Dishcare.Dishwasher.Event.SaltNearlyEmpty</feature>
    <feature refUID="0226">BSH.Common.Command.AbortProgram</feature>
    <feature refUID="0228">BSH.Common.Option.Duration</feature>
    <feature refUID="1001">Dishcare.Dishwasher.Program.Eco50</feature>
    <feature refUID="1002">Dishcare.Dishwasher.Program.Quick45</feature>
  </featureDescription>
  <errorDescription>
    <error refEID="0010">BSH.Common.Error.Unknown</error>
  </errorDescription>
  <enumDescriptionList>
    <enumDescription refENID="0201" enumKey="BSH.Common.EnumType.DoorState">
      <enumMember refValue="0">Open</enumMember><enumMember refValue="1">Closed</enumMember>
      <enumMember refValue="2">Locked</enumMember>
    </enumDescription>
    <enumDescription refENID="1001" enumKey="BSH.Common.EnumType.PowerState">
      <enumMember refValue="1">Off</enumMember><enumMember refValue="2">On</enumMember>
      <enumMember refValue="10">Standby</enumMember>
    </enumDescription>
    <enumDescription refENID="0001" enumKey="BSH.Common.EnumType.EventPresentState">
      <enumMember refValue="0">Off</enumMember><enumMember refValue="1">Present</enumMember>
      <enumMember refValue="2">Confirmed</enumMember>
    </enumDescription>
  </enumDescriptionList>
</featureMappingFile>
"""

OTHER_FEATURE_MAPPING = """<?xml version="1.0" encoding="UTF-8"?>
<featureMappingFile xmlns="http://www.home-connect.com/schemas/FeatureMapping/20140417">
  <featureDescription>
    <feature refUID="0999">Cooking.Oven.Status.Something</feature>
  </featureDescription>
</featureMappingFile>
"""

KEY64 = base64.urlsafe_b64encode(bytes(32)).decode().rstrip("=")
IV64 = base64.urlsafe_b64encode(bytes(16)).decode().rstrip("=")


def profile_json(name: str = "TEST-DW100-001122334455", **overrides: object) -> str:
    data: dict[str, object] = {
        "haId": name,
        "type": "Dishwasher",
        "vib": "DW100",
        "connectionType": "AES",
        "key": KEY64,
        "iv": IV64,
        "deviceDescriptionFileName": f"{name}_DeviceDescription.xml",
        "featureMappingFileName": f"{name}_FeatureMapping.xml",
    }
    data.update(overrides)
    return json.dumps({k: v for k, v in data.items() if v is not None})
