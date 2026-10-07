"""`per`, `change`, join keys, and the sources that need them. Offline."""

import types

import pytest

from mapsvc import derive, harvest as H, statistics
from mapsvc.manifest import ManifestError, validate
from mapsvc.statistics import Capabilities, Values, Variable, ukrstat

BOX = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}


# --- ukrstat -------------------------------------------------------------

def test_ukrstat_regions_sum_to_the_national_total():
    """The vendored table is checked against Ukrstat's own total row."""
    rows = ukrstat._rows()
    total = next(r for r in rows if r["iso_3166_2"] == "UA")
    for column in ("present_population", "resident_population"):
        regions = sum(int(r[column]) for r in rows
                      if r["iso_3166_2"] != "UA" and r[column])
        assert regions == int(total[column])


def test_ukrstat_carries_the_reference_values_and_all_27_units():
    values = ukrstat.load("present_population", "admin_1", "world")
    assert values.values["UA-30"] == 2952301       # Kyiv city
    assert values.values["UA-63"] == 2598961       # Kharkiv
    assert values.values["UA-14"] > values.values["UA-30"]
    assert len(values.values) + len(values.missing) == 27


def test_crimea_and_sevastopol_are_not_estimated_rather_than_unjoined():
    values = ukrstat.load("present_population", "admin_1", "world")
    assert values.missing == {"UA-43": "not_estimated", "UA-40": "not_estimated"}
    assert "occupied" in values.provenance["note"]


# --- a fake time-series pair of sources ------------------------------------

SERIES = {
    "pop": {2000: {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0},
            2020: {"AAA": 150.0, "BBB": 180.0, "DDD": 10.0}},
    "fuel": {2000: {"AAA": 1000.0, "BBB": 0.0},
             2020: {"AAA": 3000.0, "BBB": 900.0, "CCC": 5.0}},
}


def _series_module():
    module = types.ModuleType("mapsvc.statistics.series_bank")
    module.SOURCE = "series_bank"
    module.CAPABILITIES = Capabilities(
        source="series_bank", levels=("admin_0",), key="ISO3",
        license="public domain", attribution="Series Bank",
        description="test", time_series=True)
    module.VARIABLES = {"admin_0": {
        "pop": Variable(id="pop", label="population", unit="people", level="count"),
        "fuel": Variable(id="fuel", label="fuel", unit="barrels", level="count"),
    }}
    calls = []

    def load(variable_id, level, region, year=None):
        year = 2020 if year is None else year
        calls.append((variable_id, year))
        unit = module.VARIABLES["admin_0"][variable_id].unit
        return Values(values=dict(SERIES[variable_id][year]),
                      years={k: year for k in SERIES[variable_id][year]},
                      provenance={"source": "Series Bank", "variable": variable_id,
                                  "unit": unit, "level": "count", "year": str(year),
                                  "license": "public domain",
                                  "attribution": "Series Bank"})
    module.load = load
    module.calls = calls
    return module


@pytest.fixture
def series(monkeypatch):
    module = _series_module()
    real = statistics._providers()
    monkeypatch.setattr(statistics, "_providers",
                        lambda: {**real, "series_bank": module})
    for cached in (statistics.sources, statistics.fixed_variables):
        cached.cache_clear()
    yield module
    for cached in (statistics.sources, statistics.fixed_variables):
        cached.cache_clear()


def _manifest(**variable):
    return validate({"bbox": [-5, -5, 5, 5], "level": "admin_0",
                     "basemap": {"source": "natural_earth"},
                     "variable": {"source": "series_bank", **variable}})


def test_per_divides_and_names_the_unit(series):
    values = derive.values(_manifest(id="fuel", per={"source": "series_bank", "id": "pop"}))
    assert values.values == {"AAA": 20.0, "BBB": 5.0}
    assert values.provenance["unit"] == "barrels per person"
    assert values.provenance["variable"] == "fuel per person"
    assert values.provenance["level"] == "ratio"
    assert values.provenance["per"]["id"] == "pop"
    # CCC has fuel but no population in 2020: no divisor, not a zero.
    assert values.missing["CCC"] == "no_divisor"


def test_change_is_percent_between_the_two_years(series):
    values = derive.values(_manifest(id="pop", change={"from": 2000, "to": 2020}))
    assert values.values["AAA"] == pytest.approx(50.0)
    assert values.values["BBB"] == pytest.approx(-10.0), "decline must stay negative"
    assert values.missing == {"CCC": "no_data", "DDD": "no_data"}
    assert values.provenance["year"] == "2000–2020"
    assert values.provenance["unit"] == "% change"
    assert values.provenance["level"] == "ratio"


def test_change_of_a_per_capita_value_divides_within_each_year(series):
    values = derive.values(_manifest(id="fuel", per={"source": "series_bank", "id": "pop"},
                                     change={"from": 2000, "to": 2020}))
    # AAA: 10/person in 2000, 20/person in 2020.
    assert values.values == {"AAA": pytest.approx(100.0)}
    assert ("pop", 2000) in series.calls and ("pop", 2020) in series.calls


def test_harvest_keeps_a_negative_change_and_carries_derived_provenance(series, monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    features = [{"type": "Feature", "geometry": BOX,
                 "properties": {"ADM0_A3": code, "NAME": code, "CONTINENT": "X"}}
                for code in ("AAA", "BBB", "CCC")]
    monkeypatch.setattr(H, "load_source", lambda level="admin_0": {"features": features})
    result = H.harvest(_manifest(id="pop", change={"from": 2000, "to": 2020}))
    assert {r["id"]: round(r["value"], 3) for r in result.rows} == {"AAA": 50.0, "BBB": -10.0}
    assert result.provenance["change"]["from"] == 2000
    assert result.provenance["statistics_level"] == "ratio"
    assert [d["id"] for d in result.dropped] == ["CCC"]


# --- manifest ----------------------------------------------------------------

def test_change_needs_a_time_series_source():
    with pytest.raises(ManifestError) as excinfo:
        validate({"within": ["UKR"], "level": "admin_1",
                  "variable": {"source": "ukrstat", "id": "present_population",
                               "change": {"from": 2000, "to": 2022}}})
    assert excinfo.value.field == "variable.change"


@pytest.mark.parametrize("change", [
    {"from": 2020, "to": 2000}, {"from": 2000}, {"from": "2000", "to": 2020},
    {"from": 2000, "to": 2090}, {"from": True, "to": 2020},
    {"from": 2000, "to": 2020, "by": "year"},
])
def test_a_malformed_change_is_rejected(series, change):
    with pytest.raises(ManifestError) as excinfo:
        _manifest(id="pop", change=change)
    assert excinfo.value.field == "variable.change"


def test_unknown_variable_keys_are_rejected():
    with pytest.raises(ManifestError) as excinfo:
        validate({"within": ["UKR"], "level": "admin_1",
                  "variable": {"source": "ukrstat", "id": "present_population",
                               "normalize": "area"}})
    assert excinfo.value.field == "variable.normalize"


def test_per_is_checked_like_a_variable(series):
    with pytest.raises(ManifestError) as excinfo:
        _manifest(id="fuel", per={"source": "series_bank", "id": "nope"})
    assert excinfo.value.field == "variable.per.id"
    with pytest.raises(ManifestError) as excinfo:
        _manifest(id="fuel", per={"source": "series_bank", "id": "fuel"})
    assert excinfo.value.field == "variable.per"
    with pytest.raises(ManifestError) as excinfo:
        _manifest(id="fuel", per={"source": "series_bank", "id": "pop", "x": 1})
    assert excinfo.value.field == "variable.per.x"


def test_per_and_change_enter_the_cache_key(series):
    plain = _manifest(id="fuel").data_key()
    per = _manifest(id="fuel", per={"source": "series_bank", "id": "pop"}).data_key()
    assert plain != per
    assert per["variable"]["per"] == {"source": "series_bank", "id": "pop"}
    assert "per" not in plain["variable"], "old manifests keep their cache key shape"


# --- join keys -----------------------------------------------------------------

def test_join_keys_are_declared_per_level():
    assert statistics.key_at("natural_earth", "admin_0") == "ISO3"
    assert statistics.key_at("natural_earth", "admin_1") == "ISO3166_2"
    assert statistics.key_at("ukrstat", "admin_1") == "ISO3166_2"


def test_a_basemap_without_the_statistics_key_is_refused_before_io(monkeypatch):
    """Overture admin_2 has no standard code; no stats source may claim to join."""
    from mapsvc import cartography
    monkeypatch.setattr(cartography, "keys", lambda source, level: ())
    with pytest.raises(ManifestError) as excinfo:
        validate({"within": ["UKR"], "level": "admin_1",
                  "basemap": {"source": "overture"},
                  "variable": {"source": "ukrstat", "id": "present_population"}})
    assert excinfo.value.field == "basemap.source"
    assert "ISO3166_2" in str(excinfo.value)


def test_harvest_reports_not_estimated_units_by_reason(monkeypatch, tmp_path):
    from mapsvc.cartography import Boundaries
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    units = [{"id": code, "keys": {"ISO3166_2": code}, "name": code, "geometry": BOX}
             for code in ("UA-30", "UA-43", "PL-02")]
    monkeypatch.setattr(H.cartography, "count", lambda *a, **k: 3)
    monkeypatch.setattr(H.cartography, "load", lambda *a, **k: Boundaries(
        units=units, keys=("ISO3166_2",), provenance={"source": "test"}))
    result = H.harvest(validate({
        "within": ["UKR"], "level": "admin_1", "basemap": {"source": "overture"},
        "variable": {"source": "ukrstat", "id": "present_population"}}))
    assert [r["id"] for r in result.rows] == ["UA-30"]
    assert {d["id"]: d["reason"] for d in result.dropped} == {
        "PL-02": "no_join", "UA-43": "not_estimated"}


def test_natural_earth_countries_join_on_iso3_not_adm0_a3():
    """ADM0_A3 is SDS for South Sudan; every other source says SSD."""
    from mapsvc import registry
    assert registry.iso3_of({"ADM0_A3": "SDS", "ISO_A3": "SSD"}) == "SSD"
    assert registry.iso3_of({"ADM0_A3": "KOS", "ISO_A3": "-99"}) == "KOS"
    assert registry.iso3_of({"ADM0_A3": "IOA", "ISO_A3": "-99", "ISO_A3_EH": "AUS"}) == "IOA"
