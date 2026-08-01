"""Synthetic EcoSpold1 fixture zip for the BAFU source tests.

Three hand-written datasets — an electricity plant, the natural-gas supply it
links to, and an obsolete material — cover every branch the sources handle:
sector routing, number->uuid technosphere resolution, biosphere in/out flows,
all three observed uncertainty types, and the obsolete markers. No real BAFU
rows: everything here is invented, so the fixture is safe in public git.
"""

import io
import zipfile

from sentier_importers.core.types import RawData

UUID_ELEC = "aaaaaaaa-1111-2222-3333-444444444444"
UUID_GAS = "bbbbbbbb-1111-2222-3333-444444444444"
UUID_OBSOLETE = "cccccccc-1111-2222-3333-444444444444"

_ELEC = """<?xml version='1.0' encoding='UTF-8'?>
<ecoSpold>
  <dataset generator="openLCA" number="101" timestamp="2026-01-01T00:00:00">
    <metaInformation>
      <processInformation>
        <referenceFunction amount="1.0" category="electricity" subCategory="production mix"
          datasetRelatesToProduct="true" infrastructureProcess="false"
          generalComment="Test electricity plant. UUID: {uuid_elec}"
          name="Electricity, at test plant" unit="kWh"/>
        <geography location="CH" text="Switzerland"/>
        <technology text="Combined cycle test turbine"/>
      </processInformation>
    </metaInformation>
    <flowData>
      <exchange category="electricity" subCategory="production mix" location="CH"
        meanValue="1.0" name="Electricity, at test plant" number="101"
        uncertaintyType="0" unit="kWh">
        <outputGroup>0</outputGroup>
      </exchange>
      <exchange category="natural gas" subCategory="supply" location="CH"
        meanValue="2.0" name="Natural gas, test supply" number="102"
        uncertaintyType="1" standardDeviation95="1.21" unit="MJ">
        <inputGroup>5</inputGroup>
      </exchange>
      <exchange CASNumber="000124-38-9" category="emissions to air" subCategory="unspecified"
        meanValue="0.5" name="Carbon dioxide, fossil" number="201"
        uncertaintyType="2" standardDeviation95="0.1" unit="kg">
        <outputGroup>4</outputGroup>
      </exchange>
      <exchange category="resources" subCategory="in water"
        meanValue="0.01" name="Water, river" number="202"
        uncertaintyType="0" unit="m3">
        <inputGroup>4</inputGroup>
      </exchange>
    </flowData>
  </dataset>
</ecoSpold>
""".format(
    uuid_elec=UUID_ELEC
)

_GAS = """<?xml version='1.0' encoding='UTF-8'?>
<ecoSpold>
  <dataset generator="openLCA" number="102" timestamp="2026-01-01T00:00:00">
    <metaInformation>
      <processInformation>
        <referenceFunction amount="1.0" category="natural gas" subCategory="supply"
          datasetRelatesToProduct="true" infrastructureProcess="false"
          generalComment="Test gas supply." name="Natural gas, test supply" unit="MJ"/>
        <geography location="CH" text="Switzerland"/>
      </processInformation>
    </metaInformation>
    <flowData>
      <exchange category="natural gas" subCategory="supply" location="CH"
        meanValue="1.0" name="Natural gas, test supply" number="102"
        uncertaintyType="0" unit="MJ">
        <outputGroup>0</outputGroup>
      </exchange>
    </flowData>
  </dataset>
</ecoSpold>
"""

_OBSOLETE = """<?xml version='1.0' encoding='UTF-8'?>
<ecoSpold>
  <dataset generator="openLCA" number="103" timestamp="2026-01-01T00:00:00">
    <metaInformation>
      <processInformation>
        <referenceFunction amount="1.0" category="material, obsolete"
          subCategory="chemicals, obsolete\\organic" datasetRelatesToProduct="true"
          infrastructureProcess="false" generalComment="Old test material."
          name="xx Old material, at plant" unit="kg"/>
        <geography location="RER" text="Europe"/>
      </processInformation>
    </metaInformation>
    <flowData>
      <exchange category="material, obsolete" subCategory="chemicals, obsolete\\organic"
        location="RER" meanValue="1.0" name="xx Old material, at plant" number="103"
        uncertaintyType="0" unit="kg">
        <outputGroup>0</outputGroup>
      </exchange>
    </flowData>
  </dataset>
</ecoSpold>
"""


def fixture_zip() -> RawData:
    """The three-dataset EcoSpold1 zip as fetched ``RawData``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"ecoSpold files/process_{UUID_ELEC}.xml", _ELEC)
        z.writestr(f"ecoSpold files/process_{UUID_GAS}.xml", _GAS)
        z.writestr(f"ecoSpold files/process_{UUID_OBSOLETE}.xml", _OBSOLETE)
    return RawData(content=buf.getvalue(), source_url="file://bafu-fixture.zip")
