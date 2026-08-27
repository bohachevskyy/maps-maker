"""Statistics returns values keyed for joining, and no geometry."""

import pytest

from mapsvc import harvest as H
from mapsvc import statistics
from mapsvc.statistics import StatisticsError, natural_earth

BOX = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))

    def install(features):
        monkeypatch.setattr(H, "load_source", lambda level="admin_0": {"features": features})
    return install


def test_values_come_back_keyed_by_iso_with_no_geometry(offline):
    offline([{"type": "Feature", "geometry": BOX,
              "properties": {"ADM0_A3": "FRA", "CONTINENT": "Europe",
                             "GDP_MD": 2715518, "GDP_YEAR": 2019}}])
    values = natural_earth.load("GDP_MD", "admin_0", "europe")
    assert values.values == {"FRA": 2715518}
    assert values.provenance["key"] == "ADM0_A3"
    assert values.provenance["year"] == "2019"
    assert "geometry" not in str(values.values)


def test_admin_1_values_are_keyed_by_iso_3166_2(offline):
    offline([{"type": "Feature", "geometry": BOX,
              "properties": {"adm0_a3": "UKR", "adm1_code": "UKR-1",
                             "iso_3166_2": "UA-05", "type": "Oblast'"}}])
    values = natural_earth.load("type", "admin_1", "UKR")
    assert values.values == {"UA-05": "Oblast'"}
    assert values.provenance["key"] == "iso_3166_2"


def test_a_variable_from_the_wrong_level_names_the_variable_field(offline):
    offline([])
    with pytest.raises(StatisticsError) as excinfo:
        natural_earth.load("GDP_MD", "admin_1", "UKR")
    assert excinfo.value.field == "variable.id"
    assert "admin_0 variable" in str(excinfo.value)


def test_natural_earth_has_nothing_below_admin_1():
    assert natural_earth.variables("admin_2") == {}


def test_an_unknown_source_names_the_variable_source_field():
    with pytest.raises(StatisticsError) as excinfo:
        statistics.load("world_bank", "GDP_MD", "admin_0", "world")
    assert excinfo.value.field == "variable.source"
