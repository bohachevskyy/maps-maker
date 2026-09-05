import copy

import pytest

from mapsvc.manifest import ManifestError, validate

BASE = {
    "bbox": [-5.0, 35.0, 40.0, 60.0],
    "level": "admin_0",
    "basemap": {"source": "natural_earth", "detail": "simplified"},
    "variable": {"source": "natural_earth", "id": "GDP_MD"},
}


def manifest(**overrides):
    raw = copy.deepcopy(BASE)
    raw.update(overrides)
    return raw


def rejects(raw) -> ManifestError:
    with pytest.raises(ManifestError) as excinfo:
        validate(raw)
    return excinfo.value


def test_the_example_manifest_validates():
    m = validate(manifest())
    assert m.bbox == (-5.0, 35.0, 40.0, 60.0)
    assert m.variable_id == "GDP_MD"


# --- bbox -----------------------------------------------------------------

def test_bbox_must_have_four_numbers():
    assert rejects(manifest(bbox=[1, 2, 3])).field == "bbox"
    assert rejects(manifest(bbox="europe")).field == "bbox"
    assert rejects(manifest(bbox=[1, 2, 3, "x"])).field == "bbox"


def test_bbox_corners_must_be_the_right_way_round():
    """A silently inverted window renders a convincing map of the wrong place."""
    assert rejects(manifest(bbox=[40, 35, -5, 60])).field == "bbox"   # east/west
    assert rejects(manifest(bbox=[-5, 60, 40, 35])).field == "bbox"   # north/south


def test_bbox_must_be_on_the_planet():
    assert rejects(manifest(bbox=[-200, 35, -100, 60])).field == "bbox"
    assert rejects(manifest(bbox=[-5, -95, 40, 60])).field == "bbox"


def test_a_degenerate_bbox_is_rejected():
    assert rejects(manifest(bbox=[10, 35, 10, 60])).field == "bbox"
    assert rejects(manifest(bbox=[-5, 40, 40, 40])).field == "bbox"


def test_bbox_is_required():
    raw = manifest()
    del raw["bbox"]
    assert rejects(raw).field == "bbox"


# --- fields that no longer exist ------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("region", "europe"),
    ("normalize", "POP_EST"),
    ("classify", {"method": "quantile", "k": 5}),
    ("ramp", "YlGnBu"),
    ("projection", "mercator"),
    ("missing", "grey"),
])
def test_render_time_fields_are_rejected_not_ignored(field, value):
    """These are derived now. Accepting and ignoring them would let a caller
    believe they had chosen something they had not."""
    error = rejects(manifest(**{field: value}))
    assert error.field == field
    assert "derived" in str(error) or "expected one of" in str(error)


def test_the_nominal_sequential_pairing_is_unrepresentable():
    """The rule that used to be validated now cannot be expressed: there is no
    ramp field to get wrong."""
    m = validate(manifest(variable={"source": "natural_earth", "id": "SUBREGION"}))
    assert m.variable_id == "SUBREGION"
    assert not hasattr(m, "ramp")


# --- level and basemap ----------------------------------------------------

def test_rejects_an_unknown_level():
    assert rejects(manifest(level="admin_9")).field == "level"


def test_a_basemap_must_serve_the_level():
    assert rejects(manifest(level="admin_2",
                            basemap={"source": "natural_earth"})).field == "level"
    assert validate(manifest(level="admin_2", variable=None,
                             basemap={"source": "overture"})).level == "admin_2"


def test_the_default_basemap_is_overture():
    raw = manifest(variable=None)
    del raw["basemap"]
    assert validate(raw).basemap_source == "overture"


# --- variable -------------------------------------------------------------

def test_variable_may_be_null_for_a_base_map():
    m = validate(manifest(variable=None))
    assert (m.variable_id, m.variable_source) == (None, None)


def test_rejects_unknown_variable_and_source():
    assert rejects(manifest(variable={"source": "natural_earth",
                                      "id": "GDP_PPP"})).field == "variable.id"
    assert rejects(manifest(variable={"source": "imf",
                                      "id": "GDP_MD"})).field == "variable.source"


def test_an_owid_id_asked_of_the_fixed_source_names_the_source():
    """natural_earth is enumerable, so a wrong id there is caught immediately."""
    error = rejects(manifest(variable={"source": "natural_earth",
                                       "id": "life-expectancy"}))
    assert error.field == "variable.id"


def test_the_reverse_cannot_be_caught_here_and_should_not_be():
    """owid is searchable, so validate has no catalogue to check GDP_MD against.
    It is accepted here and 404s at fetch, which is the only honest answer."""
    m = validate(manifest(variable={"source": "owid", "id": "GDP_MD"}))
    assert m.variable_id == "GDP_MD"


def test_a_searchable_source_accepts_an_id_the_validator_has_never_seen():
    """The id came from a live search; only the source can confirm it exists."""
    m = validate(manifest(variable={"source": "owid", "id": "some-new-chart-2026"}))
    assert m.variable_id == "some-new-chart-2026"


def test_a_searchable_source_still_needs_a_non_empty_id():
    assert rejects(manifest(variable={"source": "owid", "id": "  "})).field == "variable.id"


def test_owid_is_rejected_below_country_level():
    error = rejects(manifest(level="admin_1", basemap={"source": "natural_earth"},
                             variable={"source": "owid", "id": "life-expectancy"}))
    assert error.field == "level"


def test_rejects_a_misspelled_field_rather_than_silently_defaulting():
    assert rejects(manifest(bbbox=[1, 2, 3, 4])).field == "bbbox"


# --- cache key ------------------------------------------------------------

def test_data_key_covers_the_whole_manifest_now():
    """Nothing is render-time any more, so nothing is excluded."""
    a = validate(manifest())
    assert a.data_key() == {
        "bbox": [-5.0, 35.0, 40.0, 60.0], "level": "admin_0",
        "basemap": {"source": "natural_earth", "detail": "simplified"},
        "variable": {"source": "natural_earth", "id": "GDP_MD"},
    }
    assert validate(manifest(bbox=[0, 0, 1, 1])).data_key() != a.data_key()
