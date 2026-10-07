"""GISCO NUTS boundaries and Eurostat values, offline, from recorded shapes."""

import json

import pytest

from mapsvc import harvest as H
from mapsvc.cartography import gisco
from mapsvc.manifest import ManifestError, validate
from mapsvc.statistics import StatisticsError, eurostat

GDP = "nama_10r_2gdp?freq=A&unit=PPS_EU27_2020_HAB"


def _square(x, y):
    return {"type": "Polygon", "coordinates": [[[x, y], [x + 1, y], [x + 1, y + 1],
                                                [x, y + 1], [x, y]]]}


def _region(code, country, iso3, eu, x, y):
    return {"type": "Feature", "geometry": _square(x, y),
            "properties": {"NUTS_ID": code, "LEVL_CODE": 2, "CNTR_CODE": country,
                           "NAME_LATN": code, "ISO3_CODE": iso3,
                           "EU_STAT": "T" if eu else "F"}}


REGIONS = {"type": "FeatureCollection", "features": [
    _region("LU00", "LU", "LUX", True, 6, 49),
    _region("BG31", "BG", "BGR", True, 23, 43),
    _region("EL30", "EL", "GRC", True, 23, 37),
    _region("FRY2", "FR", "FRA", True, -61, 14),
    _region("NO08", "NO", "NOR", False, 10, 59),
]}

# A JSON-stat 2.0 cube shaped like the API's: freq x unit x geo x time.
CUBE = {
    "label": "Gross domestic product (GDP) at current market prices by NUTS 2 region",
    "updated": "2026-02-10T11:00:00+0100",
    "id": ["freq", "unit", "geo", "time"], "size": [1, 1, 6, 2],
    "dimension": {
        "freq": {"category": {"index": {"A": 0}}},
        "unit": {"category": {"index": {"PPS_EU27_2020_HAB": 0}}},
        "geo": {"category": {"index": {"LU00": 0, "BG31": 1, "EL30": 2, "FRY2": 3,
                                       "NO08": 4, "LU": 5}}},
        "time": {"category": {"index": {"2023": 0, "2024": 1}}},
    },
    # LU00 2024=97700; BG31 2024=16700; EL30 only 2023; FRY2 2024 provisional.
    "value": {"0": 95100, "1": 97700, "3": 16700, "4": 20000, "7": 27500,
              "9": 80000, "11": 100000},
    "status": {"7": "p"},
}
SHAPE = {"label": CUBE["label"], "dims": [
    ["freq", "Time frequency", [["A", "Annual"]]],
    ["unit", "Unit of measure", [["MIO_EUR", "Million euro"],
                                 ["EUR_HAB", "Euro per inhabitant"],
                                 ["PPS_EU27_2020_HAB",
                                  "Purchasing power standard (PPS, EU27 from 2020), per inhabitant"]]],
]}
TOC = ('"title"\t"code"\t"type"\t"last update of data"\t"last table structure change"'
       '\t"data start"\t"data end"\t"values"\n'
       '"    Gross domestic product (GDP) at current market prices by NUTS 2 region"'
       '\t"nama_10r_2gdp"\t"dataset"\t"10.02.2026"\t"10.02.2026"\t"2000"\t"2024"\t1\n'
       '"    Gross domestic product (GDP) at current market prices by NUTS 2 region"'
       '\t"nama_10r_2gdp"\t"dataset"\t"10.02.2026"\t"10.02.2026"\t"2000"\t"2024"\t1\n'
       '"    GDP and main components (national)"\t"nama_10_gdp"\t"dataset"\t""\t""\t"1995"\t"2025"\t1\n')


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    (tmp_path / "gisco").mkdir()
    (tmp_path / "gisco" / f"NUTS_RG_20M_{gisco.VINTAGE}_LEVL_2.geojson").write_text(
        json.dumps(REGIONS))
    (tmp_path / "eurostat" / "dimensions").mkdir(parents=True)
    (tmp_path / "eurostat" / "dimensions" / "nama_10r_2gdp.json").write_text(json.dumps(SHAPE))
    requested = []

    def fake_get(url, timeout=None, accept=None):
        requested.append(url)
        if "/toc/" in url:
            return TOC.encode()
        if "/data/nama_10r_2gdp?" in url:
            return json.dumps(CUBE).encode()
        raise AssertionError(url)
    monkeypatch.setattr(eurostat.fetch, "get", fake_get)
    caches = (gisco._features, eurostat._toc, eurostat._dimensions, eurostat._cube,
              eurostat.search)
    for cached in caches:
        cached.cache_clear()
    yield requested
    for cached in caches:
        cached.cache_clear()


def test_gisco_regions_carry_nuts_and_within_understands_eu_iso_and_nuts(offline):
    assert {u["id"] for u in gisco.load("nuts_2", None, "simplified", ("EU",)).units} == {
        "BG31", "EL30", "FRY2", "LU00"}
    assert [u["keys"] for u in gisco.load("nuts_2", None, "simplified", ("GRC",)).units] == [
        {"NUTS": "EL30"}], "ISO3 GRC is NUTS EL"
    assert gisco.count("nuts_2", None, ("GR",)) == 1, "ISO2 GR is NUTS EL"
    assert gisco.count("nuts_2", None, ("FRY",)) == 1, "a NUTS code contains its children"
    assert gisco.count("nuts_2", (-25, 34, 45, 72), ("EU",)) == 3, "a bbox drops Martinique"


def test_gisco_credits_eurogeographics(offline):
    provenance = gisco.load("nuts_2", None, "simplified", ("EU",)).provenance
    assert provenance["attribution"] == "© EuroGeographics for the administrative boundaries"


def test_eurostat_takes_the_latest_year_per_region_at_the_requested_level(offline):
    values = eurostat.load(GDP, "nuts_2", "world")
    assert values.values == {"LU00": 97700, "BG31": 16700, "EL30": 20000,
                             "FRY2": 27500, "NO08": 80000}
    assert "LU" not in values.values, "a country is not a NUTS 2 region"
    assert values.years["EL30"] == 2023
    assert values.provenance["note"] == "1 values provisional or estimated"
    assert any("geoLevel=nuts2" in url for url in offline)


def test_eurostat_answers_for_a_given_year(offline):
    values = eurostat.load(GDP, "nuts_2", "world", year=2023)
    assert values.values["LU00"] == 95100
    with pytest.raises(StatisticsError) as excinfo:
        eurostat.load(GDP, "nuts_2", "world", year=2001)
    assert excinfo.value.field == "variable.change"


def test_search_offers_one_candidate_per_unit_with_the_cell_in_the_id(offline):
    found = eurostat.search("GDP per inhabitant", 8)
    ids = [c.id for c in found]
    assert "nama_10r_2gdp?freq=A&unit=PPS_EU27_2020_HAB" in ids
    assert "nama_10r_2gdp?freq=A&unit=MIO_EUR" in ids
    assert ids.index("nama_10r_2gdp?freq=A&unit=EUR_HAB") < ids.index(
        "nama_10r_2gdp?freq=A&unit=MIO_EUR"), "per inhabitant matches the query better"
    assert "nama_10_gdp" not in " ".join(ids), "national tables are not regional"


def test_describe_reads_unit_and_level_from_the_pinned_cell(offline):
    variable = eurostat.describe(GDP)
    assert variable.unit.startswith("Purchasing power standard")
    assert variable.level == "ratio"
    assert eurostat.describe("nama_10r_2gdp?freq=A&unit=MIO_EUR").level == "count"
    with pytest.raises(StatisticsError):
        eurostat.describe("nama_10r_2gdp?unit=NOPE")


def test_the_eu_map_joins_on_nuts_and_states_the_drawn_vintage(offline):
    result = H.harvest(validate({
        "within": ["EU"], "level": "nuts_2", "basemap": {"source": "gisco"},
        "variable": {"source": "eurostat", "id": GDP}}))
    assert {r["id"] for r in result.rows} == {"BG31", "EL30", "FRY2", "LU00"}
    assert result.provenance["join_key"] == "NUTS"
    assert result.provenance["year"] == "2023–2024"


def test_eurostat_needs_a_basemap_with_nuts_codes():
    with pytest.raises(ManifestError) as excinfo:
        validate({"within": ["FRA"], "level": "admin_1", "basemap": {"source": "overture"},
                  "variable": {"source": "eurostat", "id": GDP}})
    assert excinfo.value.field == "level"
    with pytest.raises(ManifestError) as excinfo:
        validate({"within": ["FRA"], "level": "nuts_2", "basemap": {"source": "overture"},
                  "variable": {"source": "eurostat", "id": GDP}})
    assert excinfo.value.field == "level", "overture has no nuts_2"
