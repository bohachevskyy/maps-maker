"""The Natural Earth de facto Morocco boundary is corrected without Shapely."""

import pytest

from mapsvc.cartography import natural_earth


def _feature(a3, iso3, name, coordinates):
    return {
        "type": "Feature",
        "properties": {"ADM0_A3": a3, "ISO_A3": iso3, "NAME": name,
                       "CONTINENT": "Africa"},
        "geometry": {"type": "Polygon", "coordinates": [coordinates]},
    }


def _sample():
    # Morocco crosses the recognised border; NE Western Sahara is represented
    # by a separate eastern strip. The shapes have deliberately unequal widths.
    marocco = _feature("MAR", "MAR", "Morocco", [
        [-17, 21], [-9, 21], [-9, 36], [-17, 36], [-17, 21],
    ])
    sahara = _feature("SAH", "ESH", "W. Sahara", [
        [-9, 21], [-8, 21], [-8, natural_earth.WESTERN_SAHARA_BORDER],
        [-9, natural_earth.WESTERN_SAHARA_BORDER], [-9, 21],
    ])
    return [marocco, sahara]


def test_marocco_is_split_and_southern_part_joins_esh_as_multipolygon():
    corrected = natural_earth._western_sahara(_sample())
    marocco, sahara = corrected
    assert marocco["geometry"]["type"] == "MultiPolygon"
    assert sahara["geometry"]["type"] == "MultiPolygon"
    assert len(sahara["geometry"]["coordinates"]) == 2
    marocco_lats = [point[1] for polygon in marocco["geometry"]["coordinates"]
                    for ring in polygon for point in ring]
    moved_lats = [point[1] for ring in sahara["geometry"]["coordinates"][1]
                  for point in ring]
    assert min(marocco_lats) == pytest.approx(natural_earth.WESTERN_SAHARA_BORDER)
    assert max(moved_lats) == pytest.approx(natural_earth.WESTERN_SAHARA_BORDER)


def test_western_sahara_split_conserves_spherical_area():
    original = _sample()
    before = sum(natural_earth._spherical_area_km2(f["geometry"]) for f in original)
    corrected = natural_earth._western_sahara(original)
    after = sum(natural_earth._spherical_area_km2(f["geometry"]) for f in corrected)
    assert after == pytest.approx(before, rel=1e-12)


def test_natural_earth_load_exposes_esh_key_and_boundary_note(monkeypatch):
    monkeypatch.setattr("mapsvc.harvest.load_source",
                        lambda level="admin_0": {"features": _sample()})
    boundaries = natural_earth.load("admin_0", None)
    units = {unit["key"]: unit for unit in boundaries.units}
    assert set(units) == {"MAR", "ESH"}
    assert units["ESH"]["geometry"]["type"] == "MultiPolygon"
    assert boundaries.provenance["note"] == natural_earth.WESTERN_SAHARA_NOTE
