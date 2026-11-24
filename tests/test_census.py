"""Census boundaries and ACS estimates, offline."""

import io
import struct
import zipfile

import pytest

from mapsvc import harvest as H, shapefile
from mapsvc.cartography import census
from mapsvc.manifest import ManifestError, validate
from mapsvc.statistics import StatisticsError, census_acs


def _shp(polygons: list[list[list[tuple]]]) -> bytes:
    """A shapefile .shp of Polygon records, laid out per the ESRI spec."""
    records = b""
    for number, rings in enumerate(polygons, start=1):
        points = [p for ring in rings for p in ring]
        parts, offset = [], 0
        for ring in rings:
            parts.append(offset)
            offset += len(ring)
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        content = struct.pack("<i4d2i", 5, min(xs), min(ys), max(xs), max(ys),
                              len(rings), len(points))
        content += struct.pack(f"<{len(parts)}i", *parts)
        content += b"".join(struct.pack("<2d", *p) for p in points)
        records += struct.pack(">2i", number, len(content) // 2) + content
    header = struct.pack(">7i", 9994, 0, 0, 0, 0, 0, (100 + len(records)) // 2)
    header += struct.pack("<2i4d4d", 1000, 5, 0, 0, 0, 0, 0, 0, 0, 0)
    return header + records


def _dbf(rows: list[dict]) -> bytes:
    fields = [("GEOID", 5), ("STATEFP", 2), ("NAME", 20)]
    header_length = 32 + 32 * len(fields) + 1
    record_length = 1 + sum(length for _, length in fields)
    out = struct.pack("<B3BIHH20x", 3, 124, 1, 1, len(rows), header_length, record_length)
    for name, length in fields:
        out += name.encode().ljust(11, b"\x00") + b"C" + b"\x00" * 4 + bytes([length, 0]) + b"\x00" * 14
    out += b"\x0d"
    for row in rows:
        out += b" " + b"".join(str(row[name]).encode().ljust(length)[:length]
                               for name, length in fields)
    return out + b"\x1a"


SQUARE = [(0, 0), (0, 4), (4, 4), (4, 0), (0, 0)]           # clockwise: outer
HOLE = [(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)]              # counter-clockwise
ISLAND = [(10, 0), (10, 1), (11, 1), (11, 0), (10, 0)]
NEVADA = [(20, 0), (20, 1), (21, 1), (21, 0), (20, 0)]


def _zip(polygons, rows) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("cb.shp", _shp(polygons))
        archive.writestr("cb.dbf", _dbf(rows))
        archive.writestr("cb.cpg", "UTF-8")
    return buffer.getvalue()


COUNTIES = _zip(
    [[SQUARE, HOLE], [SQUARE, ISLAND], [NEVADA]],
    [{"GEOID": "06085", "STATEFP": "06", "NAME": "Santa Clara"},
     {"GEOID": "06025", "STATEFP": "06", "NAME": "Imperial"},
     {"GEOID": "32003", "STATEFP": "32", "NAME": "Clark"}])


def test_rings_are_grouped_by_winding_into_holes_and_parts():
    (first, g1), (second, g2), _ = shapefile.read_zip(COUNTIES)
    assert first["NAME"] == "Santa Clara"
    assert g1["type"] == "Polygon" and len(g1["coordinates"]) == 2, "hole stays a hole"
    assert g2["type"] == "MultiPolygon" and len(g2["coordinates"]) == 2


@pytest.fixture
def offline_census(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    (tmp_path / "census").mkdir()
    (tmp_path / "census" / f"cb_{census.VINTAGE}_us_county_20m.zip").write_bytes(COUNTIES)
    (tmp_path / "census" / f"cb_{census.VINTAGE}_us_county_500k.zip").write_bytes(COUNTIES)
    # A recorded slice of acsdt5y2024-b19013.dat, real values.
    acs = tmp_path / "census_acs" / "2024"
    acs.mkdir(parents=True)
    (acs / "b19013.dat").write_text(
        "GEO_ID|B19013_E001|B19013_M001\n"
        "0100000US|80734|166\n"
        "0400000US06|99122|310\n"
        "0500000US06025|57681|2564\n"
        "0500000US06085|164281|1955\n"
        "0500000US32003|78000|900\n"
        "0500000US35011|-666666666|-222222222\n"
        "1400000US06085500100|200000|9000\n")
    census._features.cache_clear()
    census_acs.latest_year.cache_clear()
    monkeypatch.setattr(census_acs.datetime, "date", type("D", (), {
        "today": staticmethod(lambda: type("T", (), {"year": 2025})())}))
    yield tmp_path
    census._features.cache_clear()
    census_acs.latest_year.cache_clear()


def test_counties_carry_fips_and_within_scopes_to_a_state(offline_census):
    boundaries = census.load("admin_2", None, "simplified", ("US-CA",))
    assert [u["keys"] for u in boundaries.units] == [{"FIPS": "06025"}, {"FIPS": "06085"}]
    assert boundaries.keys == ("FIPS",)
    assert census.count("admin_2", None, ("USA",)) == 3


def test_county_scale_is_500k_regionally_but_generalised_nationally():
    assert census._effective_scale("admin_2", "full", ("US-CA",)) == "500k"
    assert census._effective_scale("admin_2", "simplified", ("US-CA",)) == "500k"
    assert census._effective_scale("admin_2", "full", ("USA",)) == "5m"
    assert census._effective_scale("admin_2", "simplified", ("USA",)) == "20m"
    assert census._effective_scale("admin_1", "full", ("USA",)) == "5m"


def test_census_refuses_a_code_it_cannot_scope_to(offline_census):
    with pytest.raises(Exception) as excinfo:
        census.load("admin_2", None, "simplified", ("CAN",))
    assert excinfo.value.field == "within"


def test_acs_keeps_counties_only_and_drops_the_no_estimate_sentinel(offline_census):
    values = census_acs.load("B19013_001E", "admin_2", "world")
    assert values.values == {"06025": 57681, "06085": 164281, "32003": 78000}
    assert values.missing == {"35011": "no_data"}
    assert values.provenance["year"] == "2020–2024 (ACS 5-year)"
    assert values.provenance["unit"] == "2024 US dollars"


def test_acs_states_are_keyed_iso_3166_2(offline_census):
    assert census_acs.load("B19013_001E", "admin_1", "world").values == {"US-CA": 99122}


def test_acs_probes_for_the_newest_release(offline_census, monkeypatch):
    """2025 is not on the server (404), so 2024 is the latest."""
    from mapsvc import fetch

    asked = []

    def get(url, timeout=None, accept=None):
        asked.append(url)
        raise fetch.FetchError("404", 404)
    monkeypatch.setattr(fetch, "get", get)
    monkeypatch.setattr(census_acs.datetime, "date", type("D", (), {
        "today": staticmethod(lambda: type("T", (), {"year": 2026})())}))
    assert census_acs.latest_year("b19013") == 2024
    assert len(asked) == 1 and "/2025/" in asked[0]


def test_acs_probe_treats_401_as_an_unpublished_release(offline_census, monkeypatch):
    """Measured 2026-11-24: census.gov answers 401, not 404, for the 2025
    vintage, which made 'unemployment rate by US state' fail outright."""
    from mapsvc import fetch

    real = fetch.get

    def get(url, timeout=None, accept=None):
        if "/2025/" in url:
            raise fetch.FetchError("401 <html>", 401)
        return real(url, timeout=timeout, accept=accept)
    monkeypatch.setattr(fetch, "get", get)
    monkeypatch.setattr(census_acs.datetime, "date", type("D", (), {
        "today": staticmethod(lambda: type("T", (), {"year": 2026})())}))
    census_acs.latest_year.cache_clear()
    assert census_acs.latest_year("b19013") == 2024
    census_acs.latest_year.cache_clear()


def test_county_income_joins_on_fips_end_to_end(offline_census):
    manifest = validate({"within": ["US-CA"], "level": "admin_2",
                         "basemap": {"source": "census"},
                         "variable": {"source": "census_acs", "id": "B19013_001E"}})
    result = H.harvest(manifest)
    assert {r["id"]: r["value"] for r in result.rows} == {"06025": 57681, "06085": 164281}
    assert result.dropped == []
    assert result.provenance["join_key"] == "FIPS"


def test_county_statistics_on_a_basemap_without_fips_are_refused_before_io():
    with pytest.raises(ManifestError) as excinfo:
        validate({"within": ["US-CA"], "level": "admin_2",
                  "basemap": {"source": "overture"},
                  "variable": {"source": "census_acs", "id": "B19013_001E"}})
    assert excinfo.value.field == "basemap.source"
    assert "census" in str(excinfo.value), "the error names the basemap that works"


def test_an_unknown_acs_id_is_rejected():
    with pytest.raises(ManifestError):
        validate({"within": ["US-CA"], "level": "admin_2", "basemap": {"source": "census"},
                  "variable": {"source": "census_acs", "id": "B99999_001E"}})
    with pytest.raises(StatisticsError):
        census_acs.load("B99999_001E", "admin_2", "world")

def test_usa_means_states_and_dc_not_territories(monkeypatch):
    from mapsvc.cartography import census

    def feature(fips):
        return {"type": "Feature", "properties": {"STATEFP": fips},
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}

    monkeypatch.setattr(census, "_features",
                        lambda level, detail="simplified": tuple(feature(f) for f in ("06", "11", "56", "60", "72")))
    assert census.count("admin_1", None, ["USA"]) == 3
    assert census.count("admin_1", None, ["USA", "US-PR"]) == 4
