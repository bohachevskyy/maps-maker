"""Offline regressions for Natural Earth's circular longitude filtering."""

from mapsvc.cartography import natural_earth


def _polygon(points):
    return {"type": "Polygon", "coordinates": [[*points, points[0]]]}


def _multipolygon(*polygons):
    return {"type": "MultiPolygon",
            "coordinates": [[[*points, points[0]]] for points in polygons]}


def _feature(identifier, geometry):
    return {"type": "Feature", "properties": {"ADM0_A3": identifier},
            "geometry": geometry}


def _ids(features):
    return {feature["properties"]["ADM0_A3"] for feature in features}


def test_world_window_keeps_wide_and_antimeridian_features():
    """The old >=180-degree planar bbox guard discarded every one of these."""
    features = [
        _feature("USA", _multipolygon(
            [(-125, 25), (-67, 25), (-67, 49), (-125, 49)],
            [(179, 51), (-179, 51), (-179, 55), (179, 55)],
        )),
        _feature("RUS", _multipolygon(
            [(30, 50), (170, 50), (170, 70), (30, 70)],
            [(179, 60), (-180, 60), (-180, 68), (179, 68)],
        )),
        _feature("NZL", _multipolygon(
            [(166, -47), (178, -47), (178, -34), (166, -34)],
            [(-177, -45), (-176, -45), (-176, -43), (-177, -43)],
        )),
        _feature("FJI", _polygon(
            [(178, -19), (-179, -19), (-179, -16), (178, -16)])),
        _feature("KIR", _multipolygon(
            [(172, 1), (174, 1), (174, 3), (172, 3)],
            [(-158, 1), (-156, 1), (-156, 3), (-158, 3)],
        )),
        _feature("ATA", _polygon(
            [(-180, -85), (-90, -89), (0, -85), (90, -89), (180, -85)])),
    ]

    selected = natural_earth._intersecting(features, (-180, -90, 180, 90))

    assert _ids(selected) == {"USA", "RUS", "NZL", "FJI", "KIR", "ATA"}


def test_dateline_feature_does_not_intersect_kyiv_window():
    alaska_like = _feature("US-AK", _polygon(
        [(-179.5, 50), (179.5, 50), (179.5, 55), (-179.5, 55)]))
    kyiv_like = _feature("UA-30", _polygon(
        [(30.0, 50.0), (31.0, 50.0), (31.0, 51.0), (30.0, 51.0)]))

    selected = natural_earth._intersecting(
        [alaska_like, kyiv_like], (29.2, 49.2, 32.2, 51.6))

    assert _ids(selected) == {"UA-30"}


def test_europe_window_keeps_russias_true_overlap():
    russia = _feature("RUS", _multipolygon(
        [(30, 50), (170, 50), (170, 70), (30, 70)],
        [(179, 60), (-180, 60), (-180, 68), (179, 68)],
    ))

    selected = natural_earth._intersecting([russia], (-25, 34, 45, 72))

    assert _ids(selected) == {"RUS"}


def test_longitude_intersection_handles_arcs_wrapping_zero():
    feature = _feature("WRP", _polygon(
        [(-10, 0), (10, 0), (10, 1), (-10, 1)]))

    assert natural_earth._intersecting([feature], (-20, -1, 0, 2))
    assert natural_earth._intersecting([feature], (0, -1, 20, 2))
    assert not natural_earth._intersecting([feature], (40, -1, 50, 2))
