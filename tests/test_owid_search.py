"""OWID search quality and time series, against recorded responses."""

import json
import pathlib
import urllib.parse

import pytest

from mapsvc.statistics import owid

RECORDED = json.loads((pathlib.Path(__file__).parent / "fixtures" / "owid_search.json").read_text())

CSV = """Entity,Code,Year,Population
Bulgaria,BGR,2000,8000000
Bulgaria,BGR,2023,6800000
Kosovo,OWID_KOS,2000,1700000
Kosovo,OWID_KOS,2023,1600000
World,OWID_WRL,2000,6000000000
"""
META = {"chart": {"title": "Population", "citation": "UN WPP (2024)"},
        "columns": {"Population (historical)": {"titleShort": "Population",
                                                "unit": "people", "type": "Integer"}}}


@pytest.fixture
def recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    requested = []

    def fake_get(url, timeout=None):
        requested.append(url)
        if "/api/search" in url:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
            return json.dumps(RECORDED[query])
        if ".metadata.json" in url:
            return json.dumps(META)
        if ".csv" in url:
            return CSV
        raise AssertionError(url)
    monkeypatch.setattr(owid, "_get", fake_get)
    owid.search.cache_clear()
    yield requested
    owid.search.cache_clear()


def test_population_returns_the_population_chart_first(recorded):
    """OWID's own first hit is the population-and-demography explorer, which
    404s on /grapher/. The chart has to win."""
    ids = [c.id for c in owid.search("population")]
    assert ids[0] == "population"
    assert "population-and-demography" not in ids
    assert "covid" not in ids, "explorer views are not grapher charts"


def test_a_multi_dimensional_view_keeps_its_parameters(recorded):
    """The bare slug 'electricity-mix' is total generation in TWh -- measured.
    Only the parameters make it the renewables share."""
    ids = [c.id for c in owid.search("share of electricity from renewables")]
    assert ids[0] == ("electricity-mix?frequency=annual&metric=share_of_generation"
                      "&source=renewables")
    assert "electricity-mix" not in ids


def test_charts_with_no_map_tab_are_not_offered(recorded):
    """Line and scatter charts carry several indicators; only one would map."""
    ids = [c.id for c in owid.search("share of electricity from renewables")]
    assert "electricity-fossil-renewables-nuclear-line" not in ids
    assert "co2-per-capita-vs-renewable-electricity" not in ids
    assert len(ids) == len(set(ids)), "one id, one candidate"


def test_a_view_id_fetches_its_view_and_caches_under_a_safe_name(recorded, tmp_path):
    view = "electricity-mix?frequency=annual&metric=share_of_generation&source=renewables"
    owid.load(view, "admin_0", "world")
    assert any(u.endswith("/grapher/electricity-mix.csv?frequency=annual&metric="
                          "share_of_generation&source=renewables") for u in recorded)
    names = [p.name for p in (tmp_path / "owid" / "data").iterdir()]
    assert names == ["electricity-mix_frequency_annual_metric_share_of_generation_source_renewables.csv"]


def test_a_year_selects_that_years_observation(recorded):
    values = owid.load("population", "admin_0", "world", year=2000)
    assert values.values["BGR"] == 8_000_000
    assert values.years["BGR"] == 2000
    assert values.provenance["year"] == "2000"
    with pytest.raises(owid.StatisticsError) as excinfo:
        owid.load("population", "admin_0", "world", year=1990)
    assert excinfo.value.field == "variable.change"


def test_kosovo_is_a_country_not_an_aggregate(recorded):
    values = owid.load("population", "admin_0", "world")
    assert values.values["KOS"] == 1_600_000
    assert "OWID_WRL" not in values.values
