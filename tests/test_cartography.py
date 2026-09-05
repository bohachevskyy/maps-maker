"""Cartography returns boundaries and nothing else."""

import pytest

from mapsvc import cartography
from mapsvc.cartography import CartographyError, iso, overture


def test_providers_declare_the_levels_they_serve():
    assert cartography.levels("natural_earth") == ("admin_0", "admin_1")
    assert cartography.levels("overture") == ("admin_0", "admin_1", "admin_2", "admin_3")


def test_an_unknown_provider_names_the_basemap_field():
    with pytest.raises(CartographyError) as excinfo:
        cartography.levels("gadm")
    assert excinfo.value.field == "basemap.source"


def test_a_provider_refuses_a_level_it_does_not_serve():
    with pytest.raises(CartographyError) as excinfo:
        cartography.load("natural_earth", "admin_2", (22.0, 44.0, 41.0, 53.0))
    assert excinfo.value.field == "level"
    assert "overture" in str(excinfo.value)


def test_iso_translation_handles_the_natural_earth_minus_99s():
    """ISO_A2 is -99 for eight countries, France and Norway among them."""
    assert iso.to_alpha2("FRA") == "FR"
    assert iso.to_alpha2("NOR") == "NO"
    assert iso.to_alpha2("UKR") == "UA"
    assert iso.to_alpha3("UA") == "UKR"
    assert iso.to_alpha2("UA") == "UA", "an ISO2 code passes straight through"


def test_overture_maps_canonical_levels_onto_its_own_subtypes():
    assert overture.SUBTYPES["admin_1"] == "region"
    assert overture.SUBTYPES["admin_2"] == "county"


def test_overture_excludes_antimeridian_wrapped_bounding_boxes():
    """A unit spanning 358 degrees intersects every window on earth."""
    sql = overture._intersects((29.2, 49.2, 32.2, 51.6))
    assert f"bbox.xmax - bbox.xmin < {overture.MAX_BBOX_WIDTH}" in sql


def test_overture_tests_intersection_not_corner_containment():
    """`bbox.xmin BETWEEN ...` asks whether the unit's corner is inside the
    window, so a box drawn inside Ukraine would return no country at all."""
    sql = overture._intersects((29.2, 49.2, 32.2, 51.6))
    assert "bbox.xmin <= 32.2" in sql and "bbox.xmax >= 29.2" in sql
    assert "BETWEEN" not in sql


def test_overture_carries_its_licence_so_the_footnote_can_state_it():
    assert overture.LICENSE == "ODbL 1.0"
    assert "OpenStreetMap" in overture.ATTRIBUTION


def test_natural_earth_returns_geometry_and_keys_but_no_values(monkeypatch, tmp_path):
    from mapsvc import harvest as H
    from mapsvc.cartography import natural_earth
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    box = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    monkeypatch.setattr(H, "load_source", lambda level="admin_0": {"features": [
        {"type": "Feature", "geometry": box,
         "properties": {"ADM0_A3": "FRA", "CONTINENT": "Europe", "NAME": "France",
                        "GDP_MD": 2715518}}]})

    boundaries = natural_earth.load("admin_0", (-1.0, -1.0, 2.0, 2.0))
    unit = boundaries.units[0]
    assert unit["id"] == "FRA" and unit["key"] == "FRA"
    assert unit["geometry"] == box
    assert "value" not in unit, "cartography must not carry statistics"
    assert boundaries.provenance["license"] == "public domain"
