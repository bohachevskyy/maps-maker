"""EIA SEDS, offline: a recorded slice of the real files, served from cache."""

import io
import zipfile

import pytest

from mapsvc import derive, xlsx
from mapsvc.manifest import validate
from mapsvc.statistics import StatisticsError, eia

# Real rows from Complete_SEDS.csv (release 2024F), trimmed.
SEDS = """Data_Status,MSN,StateCode,Year,Data
"2024F","DFTCP","NY","2023",62645
"2024F","DFTCP","NY","2024",58462
"2024F","DFTCP","TX","2023",189451
"2024F","DFTCP","TX","2024",186538
"2024F","DFTCP","US","2024",1387893
"2024F","DFTCP","WY","2024",13642
"2024F","DFTCP","X3","2024",5
"2024F","B1TCP","TX","2024",0
"2024F","TPOPP","NY","2023",19787
"2024F","TPOPP","NY","2024",20001
"2024F","TPOPP","TX","2023",30719
"2024F","TPOPP","TX","2024",31319
"2024F","TPOPP","WY","2024",587
"""

# Rows from Codes_and_Descriptions.xlsx, "MSN descriptions" sheet.
CODES = [
    ("B1TCP", "Renewable diesel total consumption", "Thousand barrels"),
    ("DFTCP", "Distillate fuel oil total consumption", "Thousand barrels"),
    ("DFTCB", "Distillate fuel oil total consumption", "Billion Btu"),
    ("DFACP", "Distillate fuel oil consumed by the transportation sector", "Thousand barrels"),
    ("TPOPP", "Resident population including Armed Forces", "Thousand"),
    ("ESTCD", "Electricity average price, all sectors", "Dollars per million Btu"),
]


def _workbook(rows) -> bytes:
    """A real .xlsx, built the way Excel lays one out: shared strings + a sheet."""
    strings = [s for row in rows for s in row]
    shared = "".join(f"<si><t>{s}</t></si>" for s in strings)
    cells = []
    for r, row in enumerate(rows, start=12):
        refs = "".join(f'<c r="{col}{r}" t="s"><v>{strings.index(v)}</v></c>'
                       for col, v in zip("BCD", row))
        cells.append(f'<row r="{r}">{refs}</row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        book.writestr("xl/workbook.xml",
                      f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
                      '<sheet name="MSN descriptions" sheetId="1" r:id="rId1"/>'
                      "</sheets></workbook>")
        book.writestr("xl/_rels/workbook.xml.rels",
                      '<Relationships xmlns="http://schemas.openxmlformats.org/'
                      'package/2006/relationships"><Relationship Id="rId1" '
                      'Target="worksheets/sheet1.xml"/></Relationships>')
        book.writestr("xl/sharedStrings.xml", f'<sst xmlns="{main}">{shared}</sst>')
        book.writestr("xl/worksheets/sheet1.xml",
                      f'<worksheet xmlns="{main}"><sheetData>{"".join(cells)}'
                      "</sheetData></worksheet>")
    return buffer.getvalue()


@pytest.fixture
def seds(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    (tmp_path / "eia").mkdir()
    with zipfile.ZipFile(tmp_path / "eia" / "Complete_SEDS.zip", "w") as archive:
        archive.writestr("complete_seds.csv", SEDS)
    (tmp_path / "eia" / "Codes_and_Descriptions.xlsx").write_bytes(_workbook(CODES))
    for cached in (eia.catalogue, eia._series, eia.search):
        cached.cache_clear()
    yield eia
    for cached in (eia.catalogue, eia._series, eia.search):
        cached.cache_clear()


def test_the_xlsx_reader_reads_shared_strings_by_column():
    rows = xlsx.sheets(_workbook(CODES))["MSN descriptions"]
    assert rows[1] == {"B": "DFTCP", "C": "Distillate fuel oil total consumption",
                       "D": "Thousand barrels"}


def test_values_are_keyed_iso_3166_2_and_leave_out_non_states(seds):
    values = seds.load("DFTCP", "admin_1", "world")
    assert set(values.values) == {"US-NY", "US-TX", "US-WY"}, "US and X3 are not states"
    assert values.provenance["year"] == "2024"
    assert values.provenance["license"] == "public domain"


def test_thousand_units_are_converted_so_ratios_read_plainly(seds):
    values = seds.load("DFTCP", "admin_1", "world")
    assert values.values["US-TX"] == 186_538_000
    assert values.provenance["unit"] == "barrels per year"
    assert seds.describe("TPOPP").unit == "people"
    assert seds.describe("ESTCD").level == "ratio"
    assert seds.describe("DFTCP").level == "count"


def test_a_zero_is_an_observation_in_seds(seds):
    values = seds.load("B1TCP", "admin_1", "world")
    assert values.values == {"US-TX": 0.0}
    assert values.provenance["zero_is_data"] is True


def test_a_year_can_be_asked_for_and_an_absent_one_is_refused(seds):
    assert seds.load("DFTCP", "admin_1", "world", year=2023).values["US-TX"] == 189_451_000
    with pytest.raises(StatisticsError) as excinfo:
        seds.load("DFTCP", "admin_1", "world", year=1990)
    assert excinfo.value.field == "variable.change"


def test_diesel_finds_distillate_fuel_oil_before_renewable_diesel(seds):
    ids = [c.id for c in seds.search("diesel consumption")]
    assert ids.index("DFTCP") < ids.index("B1TCP")
    assert ids.index("DFTCP") < ids.index("DFTCB"), "physical units before Btu"
    assert [c.id for c in seds.search("population")] == ["TPOPP"]


def test_diesel_per_capita_matches_the_hand_computed_ratio(seds):
    manifest = validate({"within": ["USA"], "level": "admin_1",
                         "basemap": {"source": "natural_earth"},
                         "variable": {"source": "eia", "id": "DFTCP",
                                      "per": {"source": "eia", "id": "TPOPP"}}})
    values = derive.values(manifest)
    assert values.values["US-WY"] == pytest.approx(13642 / 587)
    assert values.values["US-NY"] == pytest.approx(58462 / 20001)
    assert values.values["US-WY"] > values.values["US-NY"]
    assert values.provenance["unit"] == "barrels per person per year"


def test_eia_is_state_level_only(seds):
    with pytest.raises(StatisticsError) as excinfo:
        seds.load("DFTCP", "admin_0", "world")
    assert excinfo.value.field == "level"
