"""Closed OSM ways are places, drawn at their centre; duplicates merge. Offline."""

import json
import math

from mapsvc import overlays
from mapsvc.overlays import overpass

WINDOW = overlays.Window((-123.2, 36.9, -121.2, 38.6), None, (-123.2, 36.9, -121.2, 38.6))
SPEC = overpass.parse_filter("brand=Costco;shop=wholesale")


def _square(lon, lat, half_m=40.0):
    dlat = math.degrees(half_m / 6371008.8)
    dlon = dlat / math.cos(math.radians(lat))
    ring = [(lon - dlon, lat - dlat), (lon + dlon, lat - dlat),
            (lon + dlon, lat + dlat), (lon - dlon, lat + dlat)]
    return [{"lon": x, "lat": y} for x, y in ring + ring[:1]]


def _way(i, lon, lat, name="Costco"):
    return {"type": "way", "id": i, "tags": {"building": "retail", "name": name,
                                              "brand": "Costco"},
            "geometry": _square(lon, lat)}


def _node(i, lon, lat, name="Costco", tags=None):
    return {"type": "node", "id": i, "lon": lon, "lat": lat,
            "tags": {"name": name, **(tags or {})}}


def _mixed():
    return {"elements": [
        _node(1, -122.0, 37.0), _node(2, -122.1, 37.5),
        _way(10, -122.2, 37.1), _way(11, -122.3, 37.2), _way(12, -122.4, 37.3),
        # the same store as way 12, a node 3 m from its centre
        _node(3, -122.4, 37.30003),
    ]}


def test_nodes_and_outlines_become_points_and_duplicates_merge():
    layer = overpass.layer_from_overpass(_mixed(), SPEC, WINDOW, "ql")
    assert layer["kind"] == "points"
    assert len(layer["features"]) == 5
    assert layer["excluded"] == [{"count": 1, "reason": layer["excluded"][0]["reason"]}]
    assert "duplicate features merged" in layer["excluded"][0]["reason"]
    assert "areas drawn at their centre" in layer["encoding"]
    assert layer == overpass.layer_from_overpass(_mixed(), SPEC, WINDOW, "ql")


def test_the_element_with_most_tags_represents_a_merged_place():
    layer = overpass.layer_from_overpass(_mixed(), SPEC, WINDOW, "ql")
    # way 12 has three tags, node 3 one: the way is kept, at its centre.
    assert "w12" in {f["id"] for f in layer["features"]}
    assert "n3" not in {f["id"] for f in layer["features"]}


def test_distinct_places_a_few_km_apart_stay_distinct():
    data = {"elements": [_way(1, -122.40, 37.65), _way(2, -122.40, 37.69)]}
    assert len(overpass.layer_from_overpass(data, SPEC, WINDOW, "ql")["features"]) == 2


def test_different_names_close_together_stay_distinct_but_unnamed_merge():
    data = {"elements": [_node(1, -122.0, 37.0, "Costco"), _node(2, -122.0, 37.0001, "Safeway")]}
    assert len(overpass.layer_from_overpass(data, SPEC, WINDOW, "ql")["features"]) == 2
    data = {"elements": [_node(1, -122.0, 37.0, "Costco"),
                         {"type": "node", "id": 2, "lon": -122.0, "lat": 37.0001}]}
    assert len(overpass.layer_from_overpass(data, SPEC, WINDOW, "ql")["features"]) == 1


def test_open_way_majority_stays_a_lines_layer():
    line = lambda i: {"type": "way", "id": i, "tags": {"railway": "rail"},
                      "geometry": [{"lon": 5.0, "lat": 45.0 + i * 0.01},
                                   {"lon": 5.1, "lat": 45.0 + i * 0.01}]}
    data = {"elements": [line(1), line(2), line(3), _node(9, 5.0, 45.0),
                         _way(20, 5.0, 45.5)]}
    layer = overpass.layer_from_overpass(
        data, overpass._pairs("railway=rail"), WINDOW, "ql")
    assert layer["kind"] == "lines" and len(layer["features"]) == 3
    reasons = {e["reason"]: e["count"] for e in layer["excluded"]}
    assert reasons == {"points in a line layer": 1, "outlines in a line layer": 1}


def test_ring_centroid_at_60_north():
    lon, lat = 24.9, 60.0
    ring = [(p["lon"], p["lat"]) for p in _square(lon, lat, 500.0)]
    # an off-centre start and winding direction do not matter
    rot = ring[2:-1] + ring[:2]
    for r in (ring, ring[::-1], rot + rot[:1]):
        x, y = overpass.ring_centroid(r)
        assert abs(x - lon) < 1e-6 and abs(y - lat) < 1e-6


def test_degenerate_ring_falls_back_to_the_vertex_mean():
    x, y = overpass.ring_centroid([(1.0, 2.0), (3.0, 2.0), (2.0, 2.0), (1.0, 2.0)])
    assert (round(x, 9), y) == (2.0, 2.0)


def test_result_is_json_serialisable():
    json.dumps(overpass.layer_from_overpass(_mixed(), SPEC, WINDOW, "ql"))
