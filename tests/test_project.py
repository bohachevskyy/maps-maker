import math

from mapsvc import project


def test_fit_uses_one_scale_factor_for_both_axes():
    # A wide, short box in a tall canvas: the shape must survive unstretched.
    transform = project.fit((0.0, 0.0, 10.0, 1.0), 400, 400, 0)
    x0, y0 = transform(0.0, 0.0)
    x1, y1 = transform(10.0, 1.0)
    scale_x = (x1 - x0) / 10.0
    scale_y = (y0 - y1) / 1.0
    assert math.isclose(scale_x, scale_y, rel_tol=1e-9)


def test_auto_picks_by_latitude_span():
    assert project.choose((-180, -90, 180, 90)) == "mollweide"
    assert project.choose((-24.5, 35.8, 40.2, 71.1)) == "albers"
    assert project.choose((-60, -10, -35, 10)) == "mercator"


def test_unwrap_keeps_a_ring_near_the_antimeridian_in_one_piece():
    # Natural Earth clips at 180; wrapping vertex-by-vertex would send the
    # 180 vertex to -180 and draw a spike across the whole map.
    ring = [(170.0, 66.0), (180.0, 66.0), (180.0, 70.0), (172.0, 70.0), (170.0, 66.0)]
    lons = [lon for lon, _ in project.unwrap_ring(ring, 0.0)]
    assert max(lons) - min(lons) == 10.0
    assert min(lons) > 0


def test_unwrap_makes_a_true_crosser_continuous():
    ring = [(178.0, 10.0), (-178.0, 10.0), (-178.0, 12.0), (178.0, 12.0), (178.0, 10.0)]
    lons = [lon for lon, _ in project.unwrap_ring(ring, 0.0)]
    assert lons == [178.0, 182.0, 182.0, 178.0, 178.0]


def test_extent_ignores_small_outlying_islands():
    # A mainland plus a distant speck: the speck must not define the viewport.
    mainland = {"type": "Polygon",
                "coordinates": [[[0, 40], [10, 40], [10, 48], [0, 48], [0, 40]]]}
    with_island = {"type": "MultiPolygon", "coordinates": [
        [[[0, 40], [10, 40], [10, 48], [0, 48], [0, 40]]],
        [[[-60, 4], [-59, 4], [-59, 5], [-60, 5], [-60, 4]]],
    ]}
    assert project.extent_of([mainland]) == project.extent_of([with_island])


def test_extent_skips_features_clipped_at_the_antimeridian():
    europe = {"type": "Polygon",
              "coordinates": [[[0, 40], [10, 40], [10, 48], [0, 48], [0, 40]]]}
    russia = {"type": "Polygon",
              "coordinates": [[[30, 50], [180, 50], [180, 70], [30, 70], [30, 50]]]}
    assert project.extent_of([europe, russia])[2] == 10


def test_extent_falls_back_when_every_feature_is_a_crosser():
    russia = {"type": "Polygon",
              "coordinates": [[[30, 50], [180, 50], [180, 70], [30, 70], [30, 50]]]}
    assert project.extent_of([russia]) == (30, 50, 180, 70)


def test_mollweide_maps_the_two_edges_of_the_world_to_opposite_sides():
    projector = project.make("mollweide", (-180, -90, 180, 90))
    east, _ = projector(180, 0)
    west, _ = projector(-180, 0)
    assert east > 0 > west
