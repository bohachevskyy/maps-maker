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


def test_overture_uses_true_circular_extent_for_wrapped_bounding_boxes():
    """A stored 358-degree box needs its vertices to locate it on the globe."""
    sql = overture._intersects((29.2, 49.2, 32.2, 51.6))
    assert f"bbox.xmax - bbox.xmin < {overture.MAX_BBOX_WIDTH}" in sql
    assert f"bbox.xmax - bbox.xmin >= {overture.MAX_BBOX_WIDTH}" in sql
    assert "ST_Points(geometry)" in sql
    assert "list_transform" in sql


def test_overture_world_window_keeps_wrapped_units_without_reading_geometry():
    sql = overture._intersects((-180, -90, 180, 90))
    assert "bbox.ymin <= 90" in sql and "bbox.ymax >= -90" in sql
    assert "geometry" not in sql


def test_overture_largest_gap_arc_overlap_with_synthetic_vertices():
    """Core DuckDB exercises the circular SQL offline, without spatial/S3."""
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE features(id VARCHAR, longitudes DOUBLE[])")
    con.executemany("INSERT INTO features VALUES (?, ?)", [
        ("US-AK", [179, -179, -170, -130]),
        ("RUS", [19, 180, -180, -170]),
        ("UKR", [22, 40]),
    ])
    vertices = "SELECT longitude FROM unnest(longitudes) AS points(longitude)"

    def ids(window):
        predicate = overture._smallest_arc_intersects(window, vertices)
        return {row[0] for row in con.execute(
            f"SELECT id FROM features WHERE {predicate}"
        ).fetchall()}

    assert "US-AK" not in ids((29.2, 49.2, 32.2, 51.6))
    assert ids((-180, -90, 180, 90)) == {"US-AK", "RUS", "UKR"}
    assert "RUS" in ids((-25, 34, 45, 72))


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


def test_iso2_to_iso3_prefers_the_country_that_owns_the_code(monkeypatch):
    """Natural Earth gives AU (in ISO_A2_EH) to two Australian territories too."""
    from mapsvc.cartography import iso
    features = [{"properties": p} for p in (
        {"ADM0_A3": "AUS", "ISO_A3": "AUS", "ISO_A2": "AU", "ISO_A2_EH": "AU"},
        {"ADM0_A3": "IOA", "ISO_A3": "-99", "ISO_A2": "-99", "ISO_A2_EH": "AU"},
        {"ADM0_A3": "ATC", "ISO_A3": "-99", "ISO_A2": "-99", "ISO_A2_EH": "AU"},
        {"ADM0_A3": "FRA", "ISO_A3": "FRA", "ISO_A2": "-99", "ISO_A2_EH": "FR"},
    )]
    monkeypatch.setattr("mapsvc.harvest.load_source", lambda level="admin_0": {"features": features})
    iso._tables.cache_clear()
    try:
        assert iso.to_alpha3("AU") == "AUS"
        assert iso.to_alpha3("FR") == "FRA"
        assert iso.to_alpha2("ATC") == "AU"
    finally:
        iso._tables.cache_clear()
