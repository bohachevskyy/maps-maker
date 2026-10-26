"""The sea from coastline ways: pure geometry, offline."""

from mapsvc.overlays import coast
from mapsvc import overlay_render


def _area(ring):
    return abs(coast._signed_area([tuple(p) for p in ring]))


def test_open_coastline_closes_along_the_box_into_the_sea():
    box = (0.0, 0.0, 10.0, 10.0)
    # Land on the left of travel: heading east along y=4 puts land to the north,
    # so the sea is the southern 4 degrees.
    rings = coast.sea_rings([[(-1.0, 4.0), (11.0, 4.0)]], box)
    assert len(rings) == 1
    assert abs(_area(rings[0]) - 40.0) < 1e-6
    assert all(0 <= x <= 10 and 0 <= y <= 4 for x, y in rings[0])


def test_island_is_a_hole_in_an_all_sea_box():
    box = (0.0, 0.0, 10.0, 10.0)
    island = [(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0), (4.0, 4.0)]   # counter-clockwise
    rings = coast.sea_rings([island], box)
    assert sorted(round(_area(r)) for r in rings) == [4, 100]


def test_no_coastline_says_nothing_about_sea():
    assert coast.sea_rings([], (0, 0, 1, 1)) == []


def test_ways_are_joined_head_to_tail():
    chains = coast._chains([[(0, 0), (1, 0)], [(1, 0), (2, 0)]])
    assert chains == [[(0, 0), (1, 0), (2, 0)]]


def test_operator_groups_name_the_shared_words():
    classes = [{"value": str(i), "label": f"Tokyo Metro {n} Line", "network": "A", "km": 1.0}
               for i, n in enumerate(("Ginza", "Hibiya"))]
    classes += [{"value": "9", "label": "Toei Mita Line", "network": "B", "km": 2.0},
                {"value": "8", "label": "Toei Oedo Line", "network": "B", "km": 3.0}]
    groups = overlay_render._operator_groups({"classes": classes}, {c["value"] for c in classes})
    assert [g[0] for g in groups] == ["Tokyo Metro", "Toei"]
    assert [r[1] for r in groups[0][1]] == ["Ginza Line", "Hibiya Line"]
