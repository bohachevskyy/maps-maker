"""BLS LAUS county annual averages, offline from a recorded workbook slice."""

import io
import zipfile

import pytest

from mapsvc.statistics import StatisticsError, bls

ROWS = [
    ("LAUS Code", "State FIPS Code", "County FIPS Code",
     "County Name/State Abbreviation", "Year", "Labor Force", "Employed",
     "Unemployed", "Unemployment Rate (%)"),
    ("CN4820100000000", "48", "201", "Harris County, TX", "2025",
     "2535852", "2420722", "115130", "4.5"),
    ("CN4800100000000", "48", "001", "Anderson County, TX", "2025",
     "20162", "19306", "856", "4.2"),
]


def _workbook(rows) -> bytes:
    strings = [value for row in rows for value in row]
    shared = "".join(f"<si><t>{value}</t></si>" for value in strings)
    xml_rows = []
    for number, row in enumerate(rows, start=2):
        cells = "".join(
            f'<c r="{column}{number}" t="s"><v>{strings.index(value)}</v></c>'
            for column, value in zip("ABCDEFGHI", row))
        xml_rows.append(f'<row r="{number}">{cells}</row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        book.writestr("xl/workbook.xml",
                      f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
                      '<sheet name="laucnty25" sheetId="1" r:id="rId1"/>'
                      '</sheets></workbook>')
        book.writestr("xl/_rels/workbook.xml.rels",
                      '<Relationships xmlns="http://schemas.openxmlformats.org/'
                      'package/2006/relationships"><Relationship Id="rId1" '
                      'Target="worksheets/sheet1.xml"/></Relationships>')
        book.writestr("xl/sharedStrings.xml", f'<sst xmlns="{main}">{shared}</sst>')
        book.writestr("xl/worksheets/sheet1.xml",
                      f'<worksheet xmlns="{main}"><sheetData>{"".join(xml_rows)}'
                      '</sheetData></worksheet>')
    return buffer.getvalue()


@pytest.fixture
def laus(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    payload = _workbook(ROWS)
    monkeypatch.setattr(bls.fetch, "get", lambda *args, **kwargs: payload)
    monkeypatch.setattr(bls.datetime, "date", type(
        "Date", (), {"today": staticmethod(lambda: type("Today", (), {"year": 2026})())}))
    bls.latest_year.cache_clear()
    bls._rows.cache_clear()
    yield
    bls.latest_year.cache_clear()
    bls._rows.cache_clear()


def test_bls_is_fips_keyed_and_uses_the_published_percent_rate(laus):
    values = bls.load("unemployment_rate", "admin_2", "world")
    assert values.values == {"48201": 4.5, "48001": 4.2}
    assert values.provenance["unit"] == "%"
    assert values.provenance["year"] == "2025"
    assert "not seasonally adjusted annual average" in values.provenance["note"]


def test_bls_sets_the_descriptive_contact_user_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    seen = {}
    def get(*args, **kwargs):
        seen.update(kwargs)
        return _workbook(ROWS)
    monkeypatch.setattr(bls.fetch, "get", get)
    bls._data(2025)
    assert seen["user_agent"] == bls.BLS_USER_AGENT
    assert "contact:" in seen["user_agent"]


def test_bls_also_exposes_labor_force_and_unemployed_counts(laus):
    assert bls.load("labor_force", "admin_2", "world").values["48201"] == 2535852
    assert bls.load("unemployed", "admin_2", "world").values["48201"] == 115130


def test_bls_rejects_html_without_caching_it(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setattr(bls.fetch, "get", lambda *args, **kwargs: b"<html>denied</html>")
    with pytest.raises(StatisticsError, match="not return"):
        bls._data(2025)
    assert not (tmp_path / "bls" / "laucnty25.xlsx").exists()
