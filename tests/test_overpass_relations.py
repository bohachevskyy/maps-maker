"""Overpass route relations, multi-value tags, names and settlements. Offline."""

import pytest

from mapsvc import overlays
from mapsvc.manifest import ManifestError, validate
from mapsvc.overlays import geo, overpass
from tests.test_overlays import _overlay_map, _raw

WINDOW = overlays.Window((0.0, 40.0, 10.0, 50.0), None, (0.0, 40.0, 10.0, 50.0))


# ---------------------------------------------------------------- grammar ---

@pytest.mark.parametrize("raw, canonical", [
    ("route=subway;network=東京メトロ|都営地下鉄",
     "relation:route=subway;network=東京メトロ|都営地下鉄"),
    ("relation:route=railway;name~Trans-Siberian",
     "relation:route=railway;name~Trans-Siberian"),
    ("place=city|town|village|hamlet", "node:place=city|town|village|hamlet"),
    ("railway=rail;highspeed=yes", "railway=rail;highspeed=yes"),
    ("node:railway=station", "node:railway=station"),
])
def test_the_element_is_derived_from_the_tags(raw, canonical):
    assert overpass.parse(raw) == canonical


@pytest.mark.parametrize("bad, message", [
    ('route=subway;name~a"];out;', "letters, digits"),     # closes the QL string
    ("route=subway;name~Lin.*e", "letters, digits"),         # regex metacharacters
    ("route=subway;network=a\\b", "letters, digits"),
    ("name~Trans-Siberian", "at least one vocabulary tag"),  # names only
    ("route~subway", "only names"),                           # ~ on vocabulary
    ("place=City|town", "outside"),                           # vocabulary is lower case
    ("place=a|b|c|d|e|f|g|h|i", "at most 8"),
    ("way:route=subway", "drop the 'way:' prefix"),
    ("route=subway;name~ab", "3-60"),
])
def test_names_and_lists_are_validated(bad, message):
    with pytest.raises(overlays.OverlayError, match=message):
        overpass.parse(bad)
    with pytest.raises(ManifestError):
        validate(_raw(overlays=[{"source": "overpass", "id": bad}]))


def test_relation_query_fetches_members_stops_and_masters():
    window = overlays.Window(None, ("UA-07",), (23, 50, 27, 52))
    ql = overpass.query(overpass.parse_filter("route=railway;name~Trans-Siberian"), window)
    assert 'area["ISO3166-2"="UA-07"]->.a;.a out tags;' in ql
    assert 'relation["route"="railway"][~"^name(:[a-z_]+)?$"~"Trans-Siberian",i](area.a)->.r;' in ql
    assert ".r out geom;node(r.r)->.s;.s out tags geom;" in ql
    assert 'rel(br.r)["type"="route_master"];out body;' in ql


def test_value_lists_become_an_anchored_alternation():
    ql = overpass.query(overpass.parse_filter("place=city|town|village"), WINDOW)
    assert 'node["place"~"^(city|town|village)$"](40,0,50,10);out tags geom;' in ql
    # A country's area is unchanged, so a cached France response is reused.
    plain = overpass.query(overpass._pairs("railway=rail;highspeed=yes"),
                           overlays.Window(None, ("FR",), (0, 0, 1, 1)))
    assert ".a out tags" not in plain


def test_taginfo_checks_each_vocabulary_value_not_names():
    spec = overpass.parse_filter("route=subway;network=東京メトロ|都営地下鉄")
    assert spec.vocabulary() == [("route", "subway")]
    assert overpass.parse_filter("place=town|village").vocabulary() == [
        ("place", "town"), ("place", "village")]


# ----------------------------------------------------------------- routes ---

def _way(i, coords, role=""):
    return {"type": "way", "ref": i, "role": role,
            "geometry": [{"lon": x, "lat": y} for x, y in coords]}


def _stop(ref, lon, lat):
    return {"type": "node", "ref": ref, "role": "stop", "lon": lon, "lat": lat}


def _relation(rid, tags, members):
    return {"type": "relation", "id": rid, "tags": {"type": "route", **tags},
            "members": members}


def _route_data():
    a = [(1.0, 45.0), (1.1, 45.0)]
    a2 = [(1.1, 45.0), (1.2, 45.0)]
    b = [(1.1, 44.9), (1.1, 45.1)]
    up = _relation(1, {"route": "subway", "name": "Red Line (A --> B)",
                       "colour": "red", "ref": "R"},
                   [_way(10, a), _way(11, a2), _way(90, a, "platform"),
                    _stop(100, 1.0, 45.0), _stop(101, 1.1, 45.0)])
    down = _relation(2, {"route": "subway", "name": "Red Line (B --> A)", "ref": "R"},
                     [_way(11, a2), _way(10, a), _stop(102, 1.1, 45.0001)])
    blue = _relation(3, {"route": "subway", "name": "Blue Line", "colour": "#00F"},
                     [_way(20, b), _stop(103, 1.1, 45.0002)])
    through = _relation(4, {"route": "subway", "name": "Blue-Express"},
                        [_way(21, [(1.1, 45.1), (1.3, 45.3)])])
    master = {"type": "relation", "id": 50,
              "tags": {"type": "route_master", "route_master": "train"},
              "members": [{"type": "relation", "ref": 4, "role": ""}]}
    nodes = [{"type": "node", "id": i, "lat": 45.0, "lon": lon,
              "tags": {"name": name, **({"name:en": en} if en else {})}}
             for i, lon, name, en in ((100, 1.0, "Ouest", "West"),
                                      (101, 1.1, "Centre", None),
                                      (102, 1.1, "Centre", "Centre"),
                                      (103, 1.1, "Centre", None))]
    return {"elements": [up, down, blue, through, master, *nodes]}


def test_relations_are_one_line_each_with_validated_colours():
    layer = overpass.layer_from_relations(_route_data(), "route=subway", WINDOW)
    classes = {c["label"]: c for c in layer["category"]["classes"]}
    assert set(classes) == {"Red Line", "Blue Line"}   # directions merged
    assert classes["Red Line"]["colour"] == "#ff0000"   # CSS name
    assert classes["Blue Line"]["colour"] == "#0000ff"  # short hex
    assert classes["Red Line"]["relations"] == 2
    assert layer["stats"]["count"] == 2
    assert layer["excluded"][0]["count"] == 1           # the through service
    assert "through-service" in layer["excluded"][0]["reason"]
    # The two ways of the red line chain into one polyline; the platform is not track.
    red = [f for f in layer["features"] if f["category"] == classes["Red Line"]["value"]]
    assert len(red) == 1 and len(red[0]["coords"]) == 3
    centre = next(s for s in layer["stations"] if s["name"] == "Centre")
    assert centre["lines"] == 2


def test_cross_operator_route_master_is_excluded_as_one_through_service():
    first = _relation(1, {"route": "subway", "network": "Metro",
                          "operator": "Metro;Other", "name": "Airport A"},
                      [_way(10, [(1, 45), (2, 45)])])
    second = _relation(2, {"route": "subway", "network": "Metro",
                           "operator": "Metro", "name": "Airport B"},
                       [_way(11, [(2, 45), (3, 45)])])
    master = {"type": "relation", "id": 50,
              "tags": {"type": "route_master", "route_master": "subway",
                       "network": "Metro", "operator": "Metro"},
              "members": [{"type": "relation", "ref": 1},
                          {"type": "relation", "ref": 2}]}
    regular = _relation(3, {"route": "subway", "network": "Metro",
                            "operator": "Metro", "name": "Regular"},
                        [_way(12, [(1, 46), (2, 46)])])
    layer = overpass.layer_from_relations(
        {"elements": [first, second, regular, master]},
        "route=subway;network=Metro", WINDOW)
    assert layer["stats"]["count"] == 1
    assert layer["category"]["classes"][0]["label"] == "Regular"
    assert layer["excluded"] == [{"count": 1, "reason":
        "through-service relations (another network's trains running over these lines)"}]


def test_a_bad_colour_tag_is_ignored():
    assert overpass.osm_colour("#C1A470") == "#c1a470"
    assert overpass.osm_colour("url(javascript:x)") is None
    assert overpass.osm_colour("#12345") is None


def test_zero_relations_hint_at_route_railway():
    with pytest.raises(overlays.OverlayError, match="route=railway is the physical"):
        overpass.layer_from_relations({"elements": []}, "route=train;name~Nowhere", WINDOW)


def test_line_names_lose_their_direction():
    assert overpass.line_name({"name": "Tozai Line Local: Nakano => Nishi-Funabashi"}) \
        == "Tozai Line Local"
    assert overpass.line_name({"name:en": "Trans-Siberian Railway (even direction)"}) \
        == "Trans-Siberian Railway"


def test_chain_joins_reversed_ways_deterministically():
    lines = [[(0, 0), (1, 0)], [(2, 0), (1, 0)], [(5, 5), (6, 6)]]
    assert geo.chain(lines) == [[(0, 0), (1, 0), (2, 0)], [(5, 5), (6, 6)]]


def test_routes_render_in_their_colours_with_a_legend(mini_geojson):
    layer = overpass.layer_from_relations(_route_data(), "route=subway", WINDOW)
    svg = _overlay_map(mini_geojson, [layer])
    assert 'stroke="#ff0000"' in svg and 'stroke="#0000ff"' in svg
    assert ">Red Line<" in svg and ">Blue Line<" in svg
    assert 'data-stat-count="2"' in svg
    assert svg == _overlay_map(mini_geojson, [layer])


# ------------------------------------------------------------ settlements ---

def _places():
    rows = [(1, "city", "Lutsk", "217000"), (2, "town", "Kovel", "68000"),
            (3, "village", "Ozero", "900")] + [
        (10 + i, "village", f"V{i}", str(i)) for i in range(40)] + [
        (99, "hamlet", "Khutir", None)]
    return {"elements": [{"type": "node", "id": i, "lon": 2 + (i % 9) * 0.6,
                          "lat": 42 + (i // 9) * 0.9,
                          "tags": {"place": p, "name": n,
                                   **({"population": pop} if pop else {})}}
                         for i, p, n, pop in rows]}


def test_place_nodes_are_a_settlement_hierarchy(mini_geojson):
    layer = overpass.layer_from_overpass(
        _places(), overpass.parse_filter("place=city|town|village|hamlet"), WINDOW, "ql")
    assert layer["style"] == "settlements" and layer["own_labels"]
    assert [c["value"] for c in layer["category"]["classes"]] == [
        "city", "town", "village", "hamlet"]
    assert layer["stats"] == {"count": 44, "count_city": 1, "count_town": 1,
                              "count_village": 41, "count_hamlet": 1}
    assert layer["title"].startswith("Cities, towns, villages and hamlets")
    svg = _overlay_map(mini_geojson, [layer])
    assert ">Lutsk<" in svg and ">Kovel<" in svg       # always named
    assert ">Khutir<" not in svg                        # hamlets never
    assert svg == _overlay_map(mini_geojson, [layer])


def test_ukrainian_place_nodes_use_official_katotth_categories(monkeypatch):
    from mapsvc.statistics import katotth

    monkeypatch.setattr(katotth, "load", lambda: {
        "UA07000000000000001": {"category": "city", "name": "A", "raion": "R"},
        "UA07000000000000002": {"category": "selyshche", "name": "B", "raion": "R"},
    })
    data = {"elements": [
        {"type": "node", "id": 1, "lon": 1, "lat": 45,
         "tags": {"place": "town", "name": "A", "katotth": "UA07000000000000001"}},
        {"type": "node", "id": 2, "lon": 2, "lat": 45,
         "tags": {"place": "town", "name": "B", "katotth": "UA07000000000000002"}},
        {"type": "node", "id": 3, "lon": 3, "lat": 45,
         "tags": {"place": "hamlet", "name": "Extra"}},
    ]}
    layer = overpass.layer_from_overpass(
        data, overpass.parse_filter("place=city|town|village|hamlet"), WINDOW, "ql")
    assert [feature["category"] for feature in layer["features"]] == [
        "city", "selyshche", "hamlet"]
    assert layer["stats"]["count_city"] == 1
    assert layer["stats"]["count_selyshche"] == 1
    assert "KATOTTH" in layer["category"]["label"]
    assert layer["excluded"][0]["count"] == 1
    assert "unmatched" in layer["excluded"][0]["reason"]


def test_places_along_a_route_keep_its_stops():
    from mapsvc import overlay_render

    layer = {"features": [{"coords": [[30.0, 55.0], [40.0, 55.0]]}]}
    places = [{"name": "On", "lon": 35.0, "lat": 55.05}, {"name": "Off", "lon": 35.0, "lat": 56.0}]
    # Vertices are tested, not segments: OSM track has a vertex every few
    # hundred metres, so a two-point line is not what this is for.
    assert [p["name"] for p in overlay_render.places_along(places, [layer])] == []
    layer["features"][0]["coords"] = [[30.0 + i * 0.05, 55.0] for i in range(201)]
    assert [p["name"] for p in overlay_render.places_along(places, [layer])] == ["On"]
