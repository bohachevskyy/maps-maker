"""Statistics returns values keyed for joining, and no geometry."""

import pytest

from mapsvc import harvest as H
from mapsvc import statistics
from mapsvc.statistics import (Capabilities, StatisticsError, Variable,
                               natural_earth)

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


# --- Our World in Data ----------------------------------------------------

CSV = """Entity,Code,Year,Life expectancy
Afghanistan,AFG,2022,62.0
Afghanistan,AFG,2023,62.9
Japan,JPN,2023,84.7
Africa,,2023,64.0
World,OWID_WRL,2023,73.2
Nowhere,NOW,2099,99.9
Blank,BLK,2023,
"""


META = {"chart": {"title": "Life expectancy", "citation": "UN WPP (2024)"},
        "columns": {"Period life expectancy at birth": {
            "unit": "years", "titleShort": "Life expectancy",
            "type": "Numeric", "timespan": "1543-2023"}}}


@pytest.fixture
def owid_csv(monkeypatch, tmp_path):
    """Stub the HTTP transport, not the parsing -- the parsing is what is tested."""
    import json as _json
    from mapsvc.statistics import owid
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))

    def fake_get(url, timeout=None):
        if url.endswith(".metadata.json"):
            return _json.dumps(META)
        if url.endswith(".csv"):
            return CSV
        raise AssertionError(f"unexpected url {url}")
    monkeypatch.setattr(owid, "_get", fake_get)
    return owid


def test_owid_takes_the_latest_observation_per_country(owid_csv):
    values = owid_csv.load("life-expectancy", "admin_0", "world")
    assert values.values["AFG"] == 62.9, "2023 must win over 2022"
    assert values.values["JPN"] == 84.7


def test_owid_skips_aggregates_that_are_not_countries(owid_csv):
    values = owid_csv.load("life-expectancy", "admin_0", "world")
    assert "OWID_WRL" not in values.values, "World is not a country"
    assert set(values.values) == {"AFG", "JPN"}


def test_owid_ignores_projections_beyond_the_current_year(owid_csv):
    """population-density and median-age carry UN projections to 2100;
    mapping a forecast as though it were an observation would be a lie."""
    values = owid_csv.load("life-expectancy", "admin_0", "world")
    assert "NOW" not in values.values


def test_owid_skips_blank_cells(owid_csv):
    assert "BLK" not in owid_csv.load("life-expectancy", "admin_0", "world").values


def test_owid_keys_by_iso3_and_carries_its_licence(owid_csv):
    values = owid_csv.load("life-expectancy", "admin_0", "world")
    assert values.provenance["key"] == "ISO3"
    assert values.provenance["license"] == "CC BY 4.0"
    assert "Our World in Data" in values.provenance["attribution"]
    assert values.provenance["year"] == "2023"


def test_owid_is_country_level_only(owid_csv):
    with pytest.raises(StatisticsError) as excinfo:
        owid_csv.load("life-expectancy", "admin_1", "UKR")
    assert excinfo.value.field == "level"


def test_owid_metadata_replaces_the_deleted_catalogue(owid_csv):
    """Unit and measurement level come from OWID, not from a dict here."""
    v = owid_csv.describe("life-expectancy")
    assert (v.unit, v.level, v.label) == ("years", "ratio", "Life expectancy")
    assert v.citation == "UN WPP (2024)"


def test_owid_numeric_is_a_ratio_never_a_count(owid_csv):
    """A 0 in an OWID series is an observation, so the count sentinel must not fire."""
    assert owid_csv.describe("life-expectancy").level == "ratio"


def test_an_unknown_owid_slug_is_a_404_from_owid_not_a_local_lookup(monkeypatch, tmp_path):
    """There is no catalogue to check against any more; OWID decides."""
    import urllib.error
    from mapsvc.statistics import owid
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))

    def not_found(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
    monkeypatch.setattr(owid.urllib.request, "urlopen", not_found)

    with pytest.raises(StatisticsError) as excinfo:
        owid.load("gross-national-happiness", "admin_0", "world")
    assert excinfo.value.field == "variable.id"
    assert "no chart" in str(excinfo.value)


def test_a_ratio_is_not_subject_to_the_zero_sentinel():
    """0% internet use is a real observation; 0 population is a placeholder."""
    from mapsvc.harvest import _no_data_reason
    assert _no_data_reason(0, "count") == "no_data"
    assert _no_data_reason(0, "ratio") is None
    assert _no_data_reason(-99, "ratio") == "no_data"


# --- registering a source -------------------------------------------------

def _fake_module():
    """A statistics source declared exactly as the docstring describes."""
    import types
    module = types.ModuleType("mapsvc.statistics.fake_bank")
    module.SOURCE = "fake_bank"
    module.CAPABILITIES = Capabilities(
        source="fake_bank", levels=("admin_0",), key="ISO3",
        license="CC BY 4.0", attribution="Fake Bank (CC BY 4.0)",
        searchable=False, description="a fixed test source")
    module.VARIABLES = {"admin_0": {
        "FB.LIT.RATE": Variable(id="FB.LIT.RATE", label="adult literacy",
                                unit="% of adults", level="ratio"),
    }}
    module.load = lambda variable_id, level, region: Values(
        values={"UKR": 99.8}, provenance={"source": "Fake Bank", "level": "ratio",
                                          "unit": "% of adults", "key": "ISO3"})
    return module


@pytest.fixture
def with_fake_source(monkeypatch):
    """Register a source the way a real one is registered, and nothing else."""
    module = _fake_module()
    real = statistics._providers()
    monkeypatch.setattr(statistics, "_providers",
                        lambda: {**real, "fake_bank": module})
    for cached in (statistics.sources, statistics.fixed_variables):
        cached.cache_clear()
    yield module
    for cached in (statistics.sources, statistics.fixed_variables):
        cached.cache_clear()


def test_registering_a_source_needs_no_change_anywhere_else(with_fake_source):
    """Declaring SOURCE/LICENSE/ATTRIBUTION/VARIABLES and a load() is the whole
    procedure -- nothing branches on the name."""
    assert "fake_bank" in statistics.sources()
    assert statistics.variables_for("admin_0", "fake_bank") == with_fake_source.VARIABLES["admin_0"]
    assert statistics.variables_for("admin_1", "fake_bank") == {}
    assert statistics.sources_for("FB.LIT.RATE") == ["fake_bank"]
    assert statistics.level_of("FB.LIT.RATE") == "admin_0"
    assert "FB.LIT.RATE" in statistics.fixed_variables()


def test_a_new_source_reaches_the_validator_and_the_agent(with_fake_source):
    from mapsvc import agent
    from mapsvc.manifest import validate

    m = validate({"region": "world", "level": "admin_0",
                  "variable": {"source": "fake_bank", "id": "FB.LIT.RATE"},
                  "classify": {"method": "quantile", "k": 5}, "ramp": "YlGnBu"})
    assert m.variable_source == "fake_bank"

    agent.schema.cache_clear()
    props = agent.schema()["properties"]["manifest"]["properties"]
    assert "fake_bank" in props["variable"]["properties"]["source"]["enum"]
    assert "FB.LIT.RATE" in agent.instructions()
    agent.schema.cache_clear()


def test_a_variable_from_a_different_source_is_still_caught(with_fake_source):
    from mapsvc.manifest import ManifestError, validate
    with pytest.raises(ManifestError) as excinfo:
        validate({"region": "world", "level": "admin_0",
                  "variable": {"source": "fake_bank", "id": "GDP_MD"},
                  "classify": {"method": "quantile", "k": 5}, "ramp": "YlGnBu"})
    assert excinfo.value.field == "variable.source"
    assert "natural_earth" in str(excinfo.value)
