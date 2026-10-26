"""The Natural Earth de facto Morocco boundary is corrected without Shapely."""

import json

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
                        lambda level="admin_0": {
                            "features": natural_earth._western_sahara(_sample())})
    boundaries = natural_earth.load("admin_0", None)
    units = {unit["key"]: unit for unit in boundaries.units}
    assert set(units) == {"MAR", "ESH"}
    assert units["ESH"]["geometry"]["type"] == "MultiPolygon"
    assert boundaries.provenance["note"] == natural_earth.WESTERN_SAHARA_NOTE


def test_admin0_source_gateway_applies_split_for_every_consumer(monkeypatch, tmp_path):
    """Context/statistics/cartography all read this one normalised gateway."""
    from mapsvc import harvest

    path = tmp_path / "admin0.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": _sample()}))
    monkeypatch.setattr(harvest, "source_path", lambda level="admin_0": path)

    features = harvest.load_source("admin_0")["features"]
    marocco, sahara = features
    assert marocco["geometry"]["type"] == "MultiPolygon"
    assert len(sahara["geometry"]["coordinates"]) == 2


def test_boundary_note_is_absent_when_marocco_and_sahara_are_not_drawn(monkeypatch):
    france = _feature("FRA", "FRA", "France", [
        [-5, 42], [8, 42], [8, 51], [-5, 51], [-5, 42],
    ])
    monkeypatch.setattr("mapsvc.harvest.load_source",
                        lambda level="admin_0": {"features": [france]})
    boundaries = natural_earth.load("admin_0", None)
    assert boundaries.provenance["note"] is None


def test_sahara_is_named_neutral_context_when_marocco_is_the_data(monkeypatch):
    from mapsvc import harvest
    from mapsvc.manifest import Manifest
    from mapsvc.models import HarvestResult

    marocco, sahara = natural_earth._western_sahara(_sample())
    marocco["properties"].update({"LABEL_X": -12, "LABEL_Y": 32})
    sahara["properties"].update({"LABEL_X": -13, "LABEL_Y": 24})
    monkeypatch.setattr(harvest, "context_countries", lambda: [marocco, sahara])
    result = HarvestResult(
        rows=[{"id": "MAR", "name": "Morocco",
               "geometry": marocco["geometry"], "value": 1}],
        provenance={"vintage": "v5.1.1"},
    )

    harvest._add_context(Manifest(bbox=None, within=("MAR",), level="admin_0",
                                  variable_source=None, variable_id=None), result)

    assert [item["id"] for item in result.context] == ["ESH"]
    assert result.context[0]["name"] == "W. Sahara"
    assert natural_earth.WESTERN_SAHARA_NOTE in result.provenance["vintage"]


def test_within_accepts_iso3_where_adm0_a3_differs():
    from mapsvc.cartography import natural_earth as ne

    sudan = {"properties": {"ADM0_A3": "SDS", "ISO_A3": "SSD", "ISO_A2": "SS"}}
    sahara = {"properties": {"ADM0_A3": "SAH", "ISO_A3": "ESH", "ISO_A2": "EH"}}
    assert ne._belongs(sudan, {"SSD"}, "admin_0")
    assert ne._belongs(sudan, {"SDS"}, "admin_0")
    assert ne._belongs(sahara, {"ESH"}, "admin_0")


def test_crimea_moves_from_russia_to_ukraine():
    from mapsvc.cartography import natural_earth as ne

    square = lambda x, y: [[[x, y], [x + 1, y], [x + 1, y + 1], [x, y + 1], [x, y]]]
    russia = {"properties": {"ADM0_A3": "RUS"},
              "geometry": {"type": "MultiPolygon",
                           "coordinates": [square(40, 55), square(33.5, 44.7)]}}
    ukraine = {"properties": {"ADM0_A3": "UKR"},
               "geometry": {"type": "Polygon", "coordinates": square(30, 49)}}
    fixed = {f["properties"]["ADM0_A3"]: f for f in ne._crimea_admin0([russia, ukraine])}
    assert fixed["RUS"]["geometry"]["coordinates"] == [square(40, 55)]
    assert square(33.5, 44.7) in fixed["UKR"]["geometry"]["coordinates"]


def test_crimea_admin1_units_belong_to_ukraine():
    from mapsvc.cartography import natural_earth as ne

    unit = {"properties": {"iso_3166_2": "UA-43", "adm0_a3": "RUS", "admin": "Russia"}}
    other = {"properties": {"iso_3166_2": "RU-MOW", "adm0_a3": "RUS"}}
    fixed = ne._crimea_admin1([unit, other])
    assert fixed[0]["properties"]["adm0_a3"] == "UKR"
    assert fixed[1]["properties"]["adm0_a3"] == "RUS"
