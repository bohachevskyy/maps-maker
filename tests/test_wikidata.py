"""Wikidata, offline: recorded SPARQL CSV rows served from the cache."""

import pytest

from mapsvc import derive
from mapsvc.manifest import validate
from mapsvc.statistics import StatisticsError, wikidata

PREFERRED = "http://wikiba.se/ontology#PreferredRank"
NORMAL = "http://wikiba.se/ontology#NormalRank"
# Real values for Ukraine, as the query service returned them.
POPULATION = f"""code,value,date,rank
UA-30,2952301,2022-01-01T00:00:00Z,{NORMAL}
UA-30,2962180,2021-01-01T00:00:00Z,{NORMAL}
UA-14,4200461,2018-01-01T00:00:00Z,{NORMAL}
UA-43,1957801,2014-03-01T00:00:00Z,{NORMAL}
UA-63,2598961,2022-01-01T00:00:00Z,{NORMAL}
UA-63,2600000,2022-01-01T00:00:00Z,{PREFERRED}
UA-77,999,2099-01-01T00:00:00Z,{NORMAL}
UA-77,890457,2022-01-01T00:00:00Z,{NORMAL}
UA-99,12345,,{NORMAL}
"""
AREA = f"""code,value,date,rank
UA-30,839000000,,{NORMAL}
UA-63,31415000000,,{NORMAL}
"""


@pytest.fixture
def recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    (tmp_path / "wikidata").mkdir()
    (tmp_path / "wikidata" / "population_admin_1.csv").write_text(POPULATION)
    (tmp_path / "wikidata" / "area_admin_1.csv").write_text(AREA)
    wikidata._rows.cache_clear()
    yield
    wikidata._rows.cache_clear()


def test_the_most_recent_statement_wins_and_its_year_is_kept(recorded):
    values = wikidata.load("population", "admin_1", "world")
    assert values.values["UA-30"] == 2952301
    assert values.years["UA-14"] == 2018, "mixed vintages are reported, not hidden"
    assert values.provenance["year"] == "2014–2022"


def test_preferred_rank_breaks_a_tie_and_projections_are_ignored(recorded):
    values = wikidata.load("population", "admin_1", "world")
    assert values.values["UA-63"] == 2600000
    assert values.values["UA-77"] == 890457, "a 2099 statement is a projection"


def test_undated_values_are_a_last_resort_and_counted(recorded):
    values = wikidata.load("population", "admin_1", "world")
    assert values.values["UA-99"] == 12345 and "UA-99" not in values.years
    assert values.provenance["note"] == "1 values undated"
    assert "UA-99" not in wikidata.load("population", "admin_1", "world", year=2022).values


def test_area_is_converted_to_square_kilometres(recorded):
    assert wikidata.load("area", "admin_1", "world").values["UA-30"] == pytest.approx(839)


def test_density_is_population_per_area(recorded):
    manifest = validate({"within": ["UKR"], "level": "admin_1",
                         "basemap": {"source": "natural_earth"},
                         "variable": {"source": "ukrstat", "id": "present_population",
                                      "per": {"source": "wikidata", "id": "area"}}})
    values = derive.values(manifest)
    assert values.values["UA-30"] == pytest.approx(2952301 / 839)
    assert values.provenance["unit"] == "people per km²"


def test_a_year_with_no_statements_is_refused(recorded):
    with pytest.raises(StatisticsError) as excinfo:
        wikidata.load("population", "admin_1", "world", year=1990)
    assert excinfo.value.field == "variable.change"
