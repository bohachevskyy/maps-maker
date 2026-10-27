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


def test_auto_picks_an_equal_area_projection_by_window_shape():
    assert project.choose((-180, -90, 180, 90)) == "equal_earth"
    assert project.choose((-24.5, 35.8, 40.2, 71.1)) == "laea"     # Europe, by units
    assert project.choose((-25, 34, 45, 72)) == "laea"             # Europe, by bbox
    assert project.choose((-125, 24, -66, 50)) == "albers"         # conterminous US
    assert project.choose((22, 44, 40.5, 52.5)) == "laea"          # Ukraine
    assert project.choose((-124.5, 32.5, -114, 42)) == "laea"      # California
    assert project.choose((-60, -10, -35, 10)) == "laea"           # equatorial
    assert project.choose((-20, -35, 55, 38)) == "laea"            # Africa


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


def test_extent_includes_a_substantial_secondary_ring_like_newfoundland():
    labrador = [[-68, 52], [-56, 52], [-56, 61], [-68, 61], [-68, 52]]
    newfoundland = [[-59.5, 46], [-52.5, 46], [-52.5, 51.5], [-59.5, 51.5], [-59.5, 46]]
    geometry = {"type": "MultiPolygon", "coordinates": [[labrador], [newfoundland]]}
    extent = project.extent_of([geometry])
    assert extent == (-68, 46, -52.5, 61)
    assert extent[2] > -52.71 and extent[1] < 47.56, "St. John's is inside the frame"


def test_extent_keeps_a_nearby_small_ring_but_not_a_far_flung_one():
    mainland = [[0, 40], [10, 40], [10, 50], [0, 50], [0, 40]]
    nearby = [[10.5, 43], [11, 43], [11, 43.5], [10.5, 43.5], [10.5, 43]]
    far = [[-55, 3], [-52, 3], [-52, 6], [-55, 6], [-55, 3]]
    geometry = {"type": "MultiPolygon",
                "coordinates": [[mainland], [nearby], [far]]}
    assert project.extent_of([geometry]) == (0, 40, 11, 50)


def test_extent_area_is_latitude_adjusted_so_svalbard_does_not_frame_norway():
    mainland = [[5, 58], [31, 58], [31, 71], [5, 71], [5, 58]]
    # In square degrees this is >15% of mainland; geographically, at 78N, it
    # is smaller. It is also more than the five-degree proximity allowance away.
    svalbard = [[10, 76.5], [22, 76.5], [22, 80], [10, 80], [10, 76.5]]
    geometry = {"type": "MultiPolygon", "coordinates": [[mainland], [svalbard]]}
    extent = project.extent_of([geometry])
    assert extent == (5, 58, 31, 71)
    assert not (extent[0] <= 15.6 <= extent[2] and extent[1] <= 78.2 <= extent[3])


def test_extent_skips_features_clipped_at_the_antimeridian():
    europe = {"type": "Polygon",
              "coordinates": [[[0, 40], [10, 40], [10, 48], [0, 48], [0, 40]]]}
    russia = {"type": "Polygon",
              "coordinates": [[[30, 50], [180, 50], [180, 70], [30, 70], [30, 50]]]}
    assert project.extent_of([europe, russia])[2] == 10


def test_robust_feature_extent_drops_only_an_isolated_fringe_feature():
    cluster = [(i * 0.1, 50 + (i % 3) * 0.1, i * 0.1, 50 + (i % 3) * 0.1)
               for i in range(50)]
    assert project.robust_feature_extent(cluster + [(80, 0, 80, 0)]) == (0.0, 50.0, 4.9, 50.2)


def test_robust_feature_extent_never_drops_an_outlying_cluster():
    main = [(i * 0.1, 50, i * 0.1, 50) for i in range(48)]
    outlying_cluster = [(20, 52, 20, 52), (20.1, 52.1, 20.1, 52.1),
                        (20.2, 52, 20.2, 52)]
    assert project.robust_feature_extent(main + outlying_cluster)[2] == 20.2


def test_europe_viewport_ignores_russia_antimeridian_edge():
    """Russia is drawn in Europe, but its 180E clip edge must not set the fit."""
    extent = (-25.0, 34.0, 45.0, 72.0)
    europe = {"type": "Polygon",
              "coordinates": [[[-24, 36], [40, 36], [40, 71], [-24, 71], [-24, 36]]]}
    russia = {"type": "Polygon",
              "coordinates": [[[30, 50], [180, 50], [180, 70], [30, 70], [30, 50]]]}
    projector = project.make(project.choose(extent), extent)

    europe_bounds = project.viewport_bounds([europe], projector, extent)
    assert project.viewport_bounds([europe, russia], projector, extent) == europe_bounds


def test_extent_falls_back_when_every_feature_is_a_crosser():
    russia = {"type": "Polygon",
              "coordinates": [[[30, 50], [180, 50], [180, 70], [30, 70], [30, 50]]]}
    assert project.extent_of([russia]) == (30, 50, 180, 70)


def test_a_single_antimeridian_unit_frames_its_clipped_mainland():
    """Natural Earth's Russia mainland is clipped at 180E. It must frame a
    Russia-only map instead of being discarded in favour of small islands."""
    mainland = [[27, 54], [41, 41], [131.88, 43.11], [180, 65],
                [180, 78], [60, 78], [27, 54]]
    kaliningrad = [[19, 54], [23, 54], [23, 56], [19, 56], [19, 54]]
    kuril = [[145, 43], [146, 43], [146, 44], [145, 44], [145, 43]]
    russia = {"type": "MultiPolygon",
              "coordinates": [[mainland], [kaliningrad], [kuril]]}

    extent = project.extent_of([russia])
    projector = project.make(project.choose(extent), extent)
    bounds = project.viewport_bounds([russia], projector, extent)

    for place in ((20.5, 54.7), (105.0, 53.5), (131.88, 43.11)):
        x, y = projector(*place)
        assert bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]


def test_mollweide_maps_the_two_edges_of_the_world_to_opposite_sides():
    projector = project.make("mollweide", (-180, -90, 180, 90))
    east, _ = projector(180, 0)
    west, _ = projector(-180, 0)
    assert east > 0 > west


def test_equal_earth_maps_edges_and_poles_to_a_finite_outline():
    projector = project.make("equal_earth", (-180, -90, 180, 90))
    west, equator = projector(-180, 0)
    east, _ = projector(180, 0)
    pole_x, north = projector(180, 90)
    assert west < 0 < east
    assert abs(pole_x) < east
    assert math.isfinite(north) and north > equator


def test_equal_earth_matches_the_published_outline():
    """Šavrič et al. (2018): half-width 2.7066, half-height 1.3174 on the unit sphere."""
    projector = project.make("equal_earth", (-180, -90, 180, 90))
    x, _ = projector(180, 0)
    _, y = projector(0, 90)
    assert math.isclose(x, 2.70663, abs_tol=1e-4)
    assert math.isclose(y, 1.31736, abs_tol=1e-4)


def test_equal_earth_is_equal_area():
    """A 10x10 degree cell at the equator and at 60N keep the true area ratio."""
    projector = project.make("equal_earth", (-180, -90, 180, 90))

    def cell_area(lat):
        ring = [projector(lon, la) for lon, la in
                project.window_outline((0, lat, 10, lat + 10), 0.5)]
        return project.planar_area(ring)

    truth = (math.sin(math.radians(70)) - math.sin(math.radians(60))) / math.sin(math.radians(10))
    assert math.isclose(cell_area(60) / cell_area(0), truth, rel_tol=1e-3)


def test_laea_is_centred_on_the_extent():
    extent = (-24.5, 35.8, 40.2, 71.1)
    projector = project.make("laea", extent)
    assert projector((extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2) == (0.0, 0.0)


def test_a_near_global_extent_snaps_to_the_whole_globe():
    """Otherwise a world map takes its southern limit from Tierra del Fuego and
    clips Antarctica, which the antimeridian filter had set aside."""
    west = {"type": "Polygon",
            "coordinates": [[[-176, -54], [-170, -54], [-170, -40], [-176, -40], [-176, -54]]]}
    east = {"type": "Polygon",
            "coordinates": [[[170, 20], [179, 20], [179, 40], [170, 40], [170, 20]]]}
    assert project.extent_of([west, east]) == (-180.0, -90.0, 180.0, 90.0)


def test_a_regional_extent_is_not_snapped():
    europe = {"type": "Polygon",
              "coordinates": [[[-24, 36], [40, 36], [40, 71], [-24, 71], [-24, 36]]]}
    assert project.extent_of([europe]) == (-24, 36, 40, 71)


def test_window_outline_is_closed_and_densified():
    ring = project.window_outline((-10, 30, 10, 50), step=5)
    assert ring[0] == ring[-1]
    assert len(ring) == 4 * 4 + 1
    assert (0.0, 50) in ring           # a parallel is sampled, not just its ends


def test_graticule_lines_stay_inside_the_window():
    extent = (-25, 34, 45, 72)
    lines = project.graticule(extent, project.graticule_interval(extent))
    assert lines
    for line in lines:
        for lon, lat in line:
            assert extent[0] <= lon <= extent[2] and extent[1] <= lat <= extent[3]


def test_clip_to_extent_keeps_only_the_inside():
    ring = [(-5, -5), (5, -5), (5, 5), (-5, 5)]
    clipped = project.clip_to_extent(ring, (0, 0, 10, 10))
    assert sorted(clipped) == [(0, 0), (0, 5), (5, 0), (5, 5)]
    assert project.clip_to_extent(ring, (20, 20, 30, 30)) == []


def test_pole_of_inaccessibility_stays_inside_a_c_shape():
    """A C's centroid lies in its mouth; a symbol there belongs to no one."""
    c = [(0, 0), (10, 0), (10, 2), (2, 2), (2, 8), (10, 8), (10, 10), (0, 10)]
    x, y = project.pole_of_inaccessibility(c, precision=0.1)
    assert project.point_in_ring(x, y, c)
    assert x < 2.5          # in the spine, not the mouth


def test_pole_of_inaccessibility_of_a_square_is_its_centre():
    x, y = project.pole_of_inaccessibility([(0, 0), (10, 0), (10, 10), (0, 10)], 0.01)
    assert math.isclose(x, 5, abs_tol=0.05) and math.isclose(y, 5, abs_tol=0.05)


def test_simplify_ring_keeps_shape_within_tolerance_and_stays_closed():
    import math
    # A circle of radius 100 px with 10,000 vertices: far more than shows.
    ring = [(100 * math.cos(2 * math.pi * i / 10000), 100 * math.sin(2 * math.pi * i / 10000))
            for i in range(10000)]
    ring.append(ring[0])
    out = project.simplify_ring(ring, 0.3)
    assert out[0] == out[-1], "a closed ring stays closed"
    assert 20 < len(out) < 200
    # Every dropped vertex lies within tolerance of the simplified outline.
    for x, y in ring[::97]:
        nearest = min(project._segment_distance_sq(x, y, *out[i], *out[i + 1])
                      for i in range(len(out) - 1))
        assert nearest <= 0.3 ** 2 + 1e-9
    assert project.simplify_ring(ring, 0.3) == out, "deterministic"


def test_simplify_ring_leaves_tiny_rings_alone():
    tri = [(0, 0), (1, 0), (0, 1), (0, 0)]
    assert project.simplify_ring(tri, 0.3) == tri
