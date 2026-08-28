import copy

import pytest

from mapsvc.manifest import ManifestError, validate

BASE = {
    "region": "europe",
    "level": "admin_0",
    # Pinned: these tests stub the Natural Earth loader and never touch S3.
    "basemap": {"source": "natural_earth", "detail": "simplified"},
    "variable": {"source": "natural_earth", "id": "GDP_MD"},
    "normalize": "POP_EST",
    "classify": {"method": "quantile", "k": 5},
    "ramp": "YlGnBu",
    "projection": "auto",
    "missing": "hatch",
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
    assert m.variable_id == "GDP_MD"
    assert m.normalize == "POP_EST"
    assert m.k == 5


def test_rejects_unknown_variable():
    assert rejects(manifest(variable={"source": "natural_earth", "id": "GDP_PPP"})).field \
        == "variable.id"


def test_rejects_k_of_12():
    assert rejects(manifest(classify={"method": "quantile", "k": 12})).field == "classify.k"


def test_rejects_k_below_three():
    assert rejects(manifest(classify={"method": "quantile", "k": 2})).field == "classify.k"


def test_rejects_nominal_variable_with_a_sequential_ramp():
    """SUBREGION shaded light-to-dark would imply one region is 'more'."""
    error = rejects(manifest(variable={"source": "natural_earth", "id": "SUBREGION"},
                             ramp="YlGnBu"))
    assert error.field == "ramp"
    assert "qualitative" in str(error)


def test_rejects_nominal_variable_with_a_diverging_ramp():
    """Diverging ramps imply a meaningful midpoint, which is just as wrong."""
    assert rejects(manifest(variable={"source": "natural_earth", "id": "SUBREGION"},
                            ramp="RdBu")).field == "ramp"


def test_accepts_nominal_variable_with_a_qualitative_ramp():
    assert validate(manifest(variable={"source": "natural_earth", "id": "SUBREGION"},
                             ramp="Set2")).ramp == "Set2"


def test_rejects_an_unknown_level():
    assert rejects(manifest(level="admin_9")).field == "level"


def test_the_default_basemap_is_overture():
    m = validate({"region": "UKR", "level": "admin_2", "variable": None})
    assert m.basemap_source == "overture"


def test_natural_earth_must_be_asked_for_explicitly():
    m = validate({"region": "europe", "level": "admin_0", "variable": None,
                  "basemap": {"source": "natural_earth"}})
    assert m.basemap_source == "natural_earth"


def test_admin_1_is_valid_with_an_admin_1_variable():
    m = validate(manifest(level="admin_1", region="UKR", normalize=None,
                          basemap={"source": "natural_earth"},
                          variable={"source": "natural_earth", "id": "type"},
                          ramp="Set2"))
    assert (m.level, m.variable_id) == ("admin_1", "type")


def test_a_variable_from_the_wrong_level_is_rejected():
    """GDP_MD exists, but not on sub-national units."""
    error = rejects(manifest(level="admin_1", normalize=None,
                             basemap={"source": "natural_earth"},
                             variable={"source": "natural_earth", "id": "GDP_MD"}))
    assert error.field == "variable.id"
    assert "admin_0 variable" in str(error)

    error = rejects(manifest(level="admin_0", normalize=None,
                             variable={"source": "natural_earth", "id": "type"},
                             ramp="Set2"))
    assert error.field == "variable.id"
    assert "admin_1 variable" in str(error)


def test_the_nominal_rule_applies_at_admin_1_too():
    assert rejects(manifest(level="admin_1", region="UKR", normalize=None,
                            basemap={"source": "natural_earth"},
                            variable={"source": "natural_earth", "id": "type"},
                            ramp="YlGnBu")).field == "ramp"


def test_rejects_a_second_source_until_one_exists():
    """The shape is right so stored manifests survive; the value is not valid yet."""
    assert rejects(manifest(variable={"source": "world_bank", "id": "GDP_MD"})).field \
        == "variable.source"


def test_rejects_a_bare_string_variable():
    assert rejects(manifest(variable="GDP_MD")).field == "variable"


def test_rejects_unknown_enum_values():
    assert rejects(manifest(classify={"method": "head_tail", "k": 5})).field == "classify.method"
    assert rejects(manifest(ramp="Viridis")).field == "ramp"
    assert rejects(manifest(projection="robinson")).field == "projection"
    assert rejects(manifest(missing="skip")).field == "missing"


def test_rejects_a_misspelled_field_rather_than_silently_defaulting():
    raw = manifest()
    raw["projeciton"] = "mercator"
    assert rejects(raw).field == "projeciton"


def test_rejects_missing_required_fields():
    for field in ("region", "level"):
        raw = manifest()
        del raw[field]
        assert rejects(raw).field == field


def test_classify_and_ramp_are_required_only_alongside_a_variable():
    for field in ("classify", "ramp"):
        raw = manifest()
        del raw[field]
        assert rejects(raw).field == field

    # With no variable there is nothing to classify or colour.
    base = validate({"region": "europe", "level": "admin_0", "variable": None,
                     "basemap": {"source": "natural_earth"}})
    assert (base.variable_id, base.ramp) == (None, None)


def test_a_base_map_rejects_normalize():
    error = rejects({"region": "europe", "level": "admin_0", "variable": None,
                     "normalize": "POP_EST"})
    assert error.field == "normalize"


def test_optional_fields_default():
    raw = manifest()
    for field in ("normalize", "projection", "missing"):
        del raw[field]
    m = validate(raw)
    assert (m.normalize, m.projection, m.missing) == (None, "auto", "hatch")


def test_k_must_be_an_integer_not_a_bool():
    assert rejects(manifest(classify={"method": "quantile", "k": True})).field == "classify.k"
    assert rejects(manifest(classify={"method": "quantile", "k": 5.5})).field == "classify.k"


def test_normalize_may_be_null_and_is_not_second_guessed():
    """Both POP_EST and GDP_MD are counts; dividing one by the other is the
    caller's judgement to make, and the validator stays out of it."""
    assert validate(manifest(normalize=None)).normalize is None
    assert validate(manifest(normalize="POP_EST")).normalize == "POP_EST"
    assert rejects(manifest(normalize="POPULATION")).field == "normalize"


def test_region_must_be_a_non_empty_string():
    assert rejects(manifest(region="")).field == "region"
    assert rejects(manifest(region=None)).field == "region"


def test_data_key_excludes_render_time_fields():
    """ramp and classify must not invalidate a cached fetch."""
    a = validate(manifest(ramp="YlGnBu", classify={"method": "quantile", "k": 5}))
    b = validate(manifest(ramp="Blues", classify={"method": "jenks", "k": 7}))
    assert a.data_key() == b.data_key()
    c = validate(manifest(region="africa"))
    assert c.data_key() != a.data_key()
