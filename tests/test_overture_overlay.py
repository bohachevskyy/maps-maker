"""Overture as the fallback for OSM layers when Overpass is down. Offline:
rows have the shape of the real queries; no S3, no Overpass."""

import json

import pytest

from mapsvc import overlay_render
from mapsvc.overlays import Window, overpass, overture

TOKYO = Window((139.55, 35.55, 139.95, 35.85), None, (139.55, 35.55, 139.95, 35.85),
               "Tokyo")
VOLYN = Window(None, ("UA-07",), (23.0, 50.0, 26.0, 52.0), "Volyn Oblast")


def _line(*points):
    return json.dumps({"type": "LineString", "coordinates": [list(p) for p in points]})


RAIL_ROWS = [
    ["b", "subway", _line((139.70, 35.65), (139.75, 35.68))],
    ["a", "subway", _line((139.60, 35.60), (139.70, 35.65))],
    ["c", "subway", json.dumps({"type": "MultiLineString", "coordinates": [
        [[139.80, 35.70], [139.82, 35.71]], [[139.82, 35.71], [139.84, 35.72]]]})],
]
PLACE_ROWS = [
    ["p2", "village", "Zastavne", "Заставне", 850, 25.1, 51.2],
    ["p1", "town", None, "Ковель", 66000, 24.7, 51.2],
    ["p3", None, "Somewhere", "Десь", None, 24.0, 51.0],
]


@pytest.fixture
def overture_on(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setenv("MAPSVC_OVERTURE_FALLBACK", "1")
    monkeypatch.setattr(overpass, "validate_tags", lambda pairs: None)
    monkeypatch.setattr(overpass, "_UNAVAILABLE_UNTIL", {})
    calls = []

    def down(ql):
        calls.append(ql)
        raise overpass.UpstreamUnavailable("all down", overpass.ENDPOINTS)

    monkeypatch.setattr(overpass, "_post", down)
    return calls


@pytest.mark.parametrize("raw,family", [
    ("railway=rail", "rail"), ("railway=subway|tram", "rail"),
    ("place=city|town|village|hamlet", "place"),
])
def test_faithful_families_are_mapped(raw, family):
    assert overture.plan(overpass.parse_filter(raw)).family == family


@pytest.mark.parametrize("raw", [
    "railway=rail;highspeed=yes",       # no speed attribute in Overture
    "railway=rail;electrified=contact_line",
    "route=subway;network=Tokyo Metro",  # relations do not exist
    "power=line", "railway=station", "railway=rail;name~Paris",
    "place=city|suburb",                 # suburb is not a locality class
])
def test_unmappable_filters_are_refused_not_guessed(raw):
    assert overture.plan(overpass.parse_filter(raw)) is None


def test_rail_sql_drops_dead_track_and_clips_to_within():
    plan = overture.plan(overpass.parse_filter("railway=rail"))
    sql = overture.rail_sql(plan, Window(None, ("FRA", "UA-07"), (-5, 41, 10, 52)),
                            "2026-10-21.0")
    assert "'standard_gauge', 'unknown'" in sql
    assert "is_abandoned" in sql and "is_under_construction" in sql
    assert "ST_Intersects(geometry, area.g)" in sql
    assert "region IN ('UA-07')" in sql and "country IN ('FR')" in sql
    assert "release/2026-10-21.0/theme=transportation" in sql
    box = overture.rail_sql(plan, TOKYO, "r")
    assert "area" not in box and "bbox.xmin <= 139.95" in box


def test_place_sql_uses_the_parent_region_for_within():
    plan = overture.plan(overpass.parse_filter("place=city|town|village|hamlet"))
    sql = overture.place_sql(plan, VOLYN, "r")
    assert "region IN ('UA-07')" in sql and "subtype = 'locality'" in sql


def test_rail_fallback_serves_the_layer_and_says_so(overture_on, monkeypatch):
    seen = []
    monkeypatch.setattr(overture, "_rows", lambda sql: seen.append(sql) or RAIL_ROWS)
    layer = overpass.load("railway=subway", TOKYO)
    again = overpass.load("railway=subway", TOKYO)
    assert len(overture_on) == 1, "the second call does not wait for Overpass again"
    assert layer["kind"] == "lines" and layer["served_by"] == "overture"
    assert [f["id"] for f in layer["features"]] == ["w1", "w2", "w3", "w4"]
    assert layer["features"][0]["coords"][0] == [139.6, 35.6], "sorted by Overture id"
    line = overlay_render._footer_line(layer, set())
    assert "via Overture Maps (Overpass unavailable)" in line
    assert "OpenStreetMap contributors" in line and "Overture Maps Foundation" in line
    assert "ODbL" in line and "route relations" in line
    assert "Overpass API" not in line
    assert json.dumps(layer, sort_keys=True) == json.dumps(again, sort_keys=True)


def test_settlement_fallback_counts_classless_localities(overture_on, monkeypatch):
    monkeypatch.setattr(overture, "_rows", lambda sql: PLACE_ROWS)
    layer = overpass.load("place=city|town|village|hamlet", VOLYN)
    assert layer["style"] == "settlements"
    assert layer["stats"]["count"] == 2 and layer["stats"]["count_town"] == 1
    assert {f["name"] for f in layer["features"]} == {"Zastavne", "Ковель"}
    assert layer["excluded"][-1]["count"] == 1
    assert "via Overture Maps (Overpass unavailable)" in layer["provenance"]["attribution"]


def test_unmappable_filter_keeps_the_overpass_error(overture_on, monkeypatch):
    monkeypatch.setattr(overture, "_rows", lambda sql: pytest.fail("must not query"))
    with pytest.raises(overpass.UpstreamUnavailable):
        overpass.load("railway=rail;highspeed=yes", VOLYN)
    # and the memo of the outage does not turn into a different error
    with pytest.raises(overpass.UpstreamUnavailable):
        overpass.load("railway=rail;highspeed=yes", VOLYN)


def test_overture_failure_keeps_the_overpass_error(overture_on, monkeypatch):
    def broken(sql):
        raise OSError("s3 unreachable")

    monkeypatch.setattr(overture, "_rows", broken)
    with pytest.raises(overpass.UpstreamUnavailable, match="all down"):
        overpass.load("railway=subway", TOKYO)


def test_the_fallback_is_off_in_the_suite_by_default(monkeypatch):
    monkeypatch.setenv("MAPSVC_OVERTURE_FALLBACK", "0")
    assert overture.load(overpass.parse_filter("railway=rail"), TOKYO) is None
