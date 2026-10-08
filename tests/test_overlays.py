"""Overlays: grammars checked before I/O, layers drawn deterministically.

Offline: every source is exercised on a small recorded response.
"""

import io
import json
import pathlib
import urllib.error

import pytest

from mapsvc import colors, overlays
from mapsvc.manifest import ManifestError, validate
from mapsvc.models import HarvestResult
from mapsvc.overlays import usgs
from mapsvc.render import render

from tests.conftest import FIXTURE_BBOX, build_manifest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
QUAKES = "start=2025-01-01;end=2026-01-01;minmagnitude=6"


def _raw(**overrides):
    raw = {"level": "admin_0", "bbox": [-180, -90, 180, 90],
           "basemap": {"source": "natural_earth", "detail": "simplified"}}
    raw.update(overrides)
    return raw


# ------------------------------------------------------------------ usgs ---

def test_usgs_id_is_canonicalised():
    assert usgs.parse(" minmagnitude=6.0 ; start=2025-01-01;end=2026-01-01") == QUAKES


@pytest.mark.parametrize("bad", [
    "start=2025-01-01",                                   # no end
    "start=2025-01-01;end=2024-01-01",                    # backwards
    "start=2025-01-01;end=2026-01-01;limit=5",            # not whitelisted
    "start=2025-13-01;end=2026-01-01",                    # not a date
    "start=2025-01-01;end=2026-01-01;minmagnitude=big",   # not a number
    "start=2025-01-01;end=2026-01-01;minmagnitude=12",    # out of range
    "https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson",
])
def test_usgs_rejects_anything_outside_the_grammar(bad):
    with pytest.raises(ManifestError) as caught:
        validate(_raw(overlays=[{"source": "usgs", "id": bad}]))
    assert caught.value.field == "overlays[0].id"


def test_manifest_overlays_are_validated_before_io():
    manifest = validate(_raw(overlays=[{"source": "usgs", "id": QUAKES}]))
    assert manifest.overlays == (("usgs", QUAKES),)
    with pytest.raises(ManifestError, match="unknown overlay source"):
        validate(_raw(overlays=[{"source": "gdal", "id": "x"}]))
    with pytest.raises(ManifestError, match="derived"):
        validate(_raw(overlays=[{"source": "usgs", "id": QUAKES, "color": "red"}]))
    with pytest.raises(ManifestError, match="at most"):
        validate(_raw(overlays=[{"source": "usgs", "id": QUAKES}] * 4))


def test_a_manifest_without_overlays_hashes_as_before():
    manifest = validate(_raw())
    assert "overlays" not in manifest.data_key()


def _quake_layer():
    data = json.loads((FIXTURES / "usgs_2025_m6_sample.json").read_text())
    return usgs.layer_from_geojson(data, usgs._spec(QUAKES), "https://example/q")


def test_usgs_layer_shape():
    layer = _quake_layer()
    assert layer["kind"] == "points"
    assert layer["stats"]["count"] == 12
    assert layer["stats"]["max_magnitude"] == 8.8
    assert layer["title"] == "Every magnitude 6+ earthquake in 2025"
    names = {f["name"] for f in layer["features"]}
    assert "M8.8 Kamchatka Peninsula, Russia, 29 Jul" in names
    assert "M7.8 off Petropavlovsk-Kamchatsky, Russia, 18 Sep" in names
    assert layer["provenance"]["license"] == "public domain"


def test_usgs_window_becomes_query_box():
    spec = usgs._spec(QUAKES)
    world = usgs._params(spec, overlays.Window(None, None, (-180.0, -90.0, 180.0, 90.0)))
    assert "minlatitude" not in world
    boxed = usgs._params(spec, overlays.Window((20.0, 40.0, 30.0, 50.0), None,
                                               (20.0, 40.0, 30.0, 50.0)))
    assert boxed["minlongitude"] == "20" and boxed["maxlatitude"] == "50"


# --------------------------------------------------------------- render ---

def _overlay_map(mini_geojson, layers, places=(), physical=()):
    manifest = build_manifest(variable_source=None, variable_id=None,
                              overlays=(("usgs", QUAKES),))
    rows = [{"id": f["properties"]["ADM0_A3"], "geometry": f["geometry"], "value": None}
            for f in mini_geojson["features"]]
    provenance = {"source": "Natural Earth", "scale": "50m", "vintage": "v5.1.1"}
    result = HarvestResult(rows=rows, provenance=provenance, dropped=[],
                           overlays=list(layers), places=list(places),
                           physical=list(physical))
    return render(manifest, result)


def test_points_render_sized_coloured_and_credited(mini_geojson):
    layer = _quake_layer()
    # Put every event inside the fixture window so they are drawn.
    for i, feature in enumerate(layer["features"]):
        feature["lon"], feature["lat"] = 0.0 + i * 3, 40.0 + (i % 5) * 3
    svg = _overlay_map(mini_geojson, [layer])
    assert 'data-overlay="usgs"' in svg and 'data-stat-count="12"' in svg
    assert svg.count("data-fid=") == 12
    assert "U.S. Geological Survey" in svg and "public domain" in svg
    assert "Every magnitude 6+ earthquake in 2025" in svg
    assert "Depth of focus" in svg
    # Deterministic, byte for byte.
    assert svg == _overlay_map(mini_geojson, [layer])


def test_overlay_only_footer_omits_pipeline_boilerplate_and_urls(mini_geojson):
    layer = _quake_layer()
    layer["provenance"]["url"] = "https://example.test/raw-query"
    svg = _overlay_map(mini_geojson, [layer])
    assert "Boundaries: Natural Earth (public domain)" in svg
    assert "Variable: none" not in svg
    assert "Classification: boundaries only" not in svg
    assert "https://" not in svg


def test_physical_context_is_drawn_and_credited(mini_geojson):
    river = {"kind": "lines", "class": "rivers",
             "features": [{"id": "r", "coords": [[0, 40], [5, 45]]}],
             "provenance": {"name": "Natural Earth rivers",
                            "publisher": "Natural Earth", "license": "public domain"}}
    plates = {"kind": "lines", "class": "plate-boundaries",
              "features": [{"id": "p", "coords": [[10, 40], [15, 45]]}],
              "provenance": {"name": "PB2002 tectonic plate boundaries",
                             "publisher": "Bird 2003 / Hugo Ahlenius",
                             "license": "ODC-By 1.0"}}
    svg = _overlay_map(mini_geojson, [_quake_layer()], physical=[river, plates])
    assert 'class="physical-context"' in svg
    assert 'class="rivers"' in svg and 'class="plate-boundaries"' in svg
    assert "Natural Earth rivers" in svg and "PB2002 tectonic plate boundaries" in svg
    assert "Bird 2003 / Hugo Ahlenius" in svg and "ODC-By 1.0" in svg
    assert f'stroke="{colors.PLATE}"' in svg and 'stroke-dasharray="4 2.5"' in svg
    assert "Plate boundaries (Bird 2003)" in svg


def test_overlay_render_module_does_no_io():
    from tests.test_no_io import _reachable_from

    assert "overlay_render" in _reachable_from("render")


# -------------------------------------------------------------- overpass ---

from mapsvc.overlays import geo, overpass, web  # noqa: E402

LGV = "railway=rail;highspeed=yes"


@pytest.mark.parametrize("bad", [
    'way["railway"="rail"](area.a);out geom;',   # free QL
    "railway=rail;railway=tram",                  # repeated key
    "Railway=Rail",                               # upper case
    "railway=*",                                  # matches far too much
    "a=1;b=2;c=3;d=4;e=5",                        # too many pairs
    "railway",                                    # not key=value
])
def test_overpass_rejects_anything_outside_the_grammar(bad):
    with pytest.raises(ManifestError):
        validate(_raw(overlays=[{"source": "overpass", "id": bad}]))


def test_overpass_query_scopes_by_country_area_and_box():
    pairs = overpass._pairs(LGV)
    window = overlays.Window((-5.5, 41.2, 10.0, 51.2), ("FR",), (-5.5, 41.2, 10.0, 51.2))
    ql = overpass.query(pairs, window)
    assert 'area["ISO3166-1"="FR"][admin_level=2]->.a;' in ql
    assert 'way["railway"="rail"]["highspeed"="yes"](area.a)(41.2,-5.5,51.2,10)' in ql
    assert ql.endswith("out tags geom;")
    sub = overpass.query(pairs, overlays.Window(None, ("FR-IDF",), (0, 0, 1, 1)))
    assert 'area["ISO3166-2"="FR-IDF"]' in sub


def test_boolean_wildcard_is_rejected_with_yes_suggestion():
    with pytest.raises(overlays.OverlayError, match="highspeed=yes"):
        overpass._pairs("railway=rail;highspeed=*")


def test_taginfo_rejects_a_near_unused_tag_with_cached_suggestions(monkeypatch,
                                                                  tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if "/tag/stats?" in url:
            return json.dumps({"data": [{"type": "all", "count": 19}]}).encode()
        return json.dumps({"data": [
            {"value": "driveway"}, {"value": "yard"}, {"value": "spur"},
        ]}).encode()

    monkeypatch.setattr(overpass.fetch, "get", get)
    for _ in range(2):
        with pytest.raises(overlays.OverlayError) as caught:
            overpass.validate_tags([("service", "highspeed")])
        assert "only 19 uses worldwide" in str(caught.value)
        assert "service=driveway" in str(caught.value)
    assert len(calls) == 2, "the stats and suggestion responses are disk-cached"
    assert all(call[1]["timeout"] == overpass.TAGINFO_TIMEOUT_S for call in calls)


def test_taginfo_failure_does_not_block_overpass(monkeypatch):
    def unavailable(*args, **kwargs):
        raise overpass.fetch.FetchError("offline")

    monkeypatch.setattr(overpass.fetch, "cached", unavailable)
    overpass.validate_tags([("railway", "rail")])


def test_an_empty_overpass_result_is_an_error():
    window = overlays.Window(None, ("FRA",), (-5, 41, 10, 52), "France")
    with pytest.raises(overlays.OverlayError, match="matched 0 features; check the tag"):
        overpass.layer_from_overpass({"elements": []}, overpass._pairs(LGV),
                                    window, "ql")


def test_an_overbroad_overpass_filter_is_refused():
    window = overlays.Window(None, ("FRA",), (-5, 41, 10, 52), "France")
    way = {"type": "way", "id": 1,
           "geometry": [{"lon": 1, "lat": 45}, {"lon": 2, "lat": 46}]}
    with pytest.raises(overlays.OverlayError, match="40,001 OpenStreetMap ways"):
        overpass.layer_from_overpass({"elements": [way] * 40_001},
                                    [("usage", "main")], window, "ql")


class _HTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self):
        return self.payload


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _http_error(request, status):
    return urllib.error.HTTPError(
        request.full_url, status, "test response", {}, io.BytesIO(b"busy"))


def test_overpass_budget_is_shared_by_all_endpoints(monkeypatch):
    clock = _Clock()
    calls = []

    def urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        if request.data is None:
            return _HTTPResponse(b"2 slots available now.")
        clock.advance(5)
        raise _http_error(request, 504)

    monkeypatch.setenv("MAPSVC_OVERPASS_BUDGET", "5")
    monkeypatch.setattr(overpass.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(overpass.fetch.urllib.request, "urlopen", urlopen)

    with pytest.raises(overpass.UpstreamUnavailable) as caught:
        overpass._post("ql")

    message = str(caught.value)
    assert "wall-clock budget of 5s exhausted" in message
    assert f"{overpass.ENDPOINTS[0]}: status 504, 5.0s" in message
    assert caught.value.endpoints_tried == (overpass.ENDPOINTS[0],)
    posts = [(url, timeout) for url, timeout in calls if url.endswith("interpreter")]
    assert posts == [(overpass.ENDPOINTS[0], 5.0)]


def test_overpass_falls_back_after_504(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(request)
        if request.data is None:
            return _HTTPResponse(b"1 slot available now.")
        if request.full_url == overpass.ENDPOINTS[0]:
            raise _http_error(request, 504)
        return _HTTPResponse(b'{"elements": []}')

    monkeypatch.setattr(overpass.fetch.urllib.request, "urlopen", urlopen)

    assert overpass._post("ql") == b'{"elements": []}'
    assert [r.full_url for r in calls if r.data is not None] == list(overpass.ENDPOINTS[:2])


def test_overpass_health_skips_busy_endpoint_and_sends_user_agent(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(request)
        if request.data is None:
            slots = b"0 slots available now." if "overpass-api.de" in request.full_url \
                else b"2 slots available now."
            return _HTTPResponse(slots)
        return _HTTPResponse(b'{"elements": []}')

    monkeypatch.setattr(overpass.fetch.urllib.request, "urlopen", urlopen)

    assert overpass._post("ql") == b'{"elements": []}'
    posts = [r for r in calls if r.data is not None]
    assert [r.full_url for r in posts] == [overpass.ENDPOINTS[1]]
    assert all(r.get_header("User-agent").startswith("mapsvc/") for r in calls)
    assert all("github.com/bohachevskyy/maps-maker" in r.get_header("User-agent")
               for r in calls)


def test_overpass_uses_stale_cache_after_every_endpoint_fails(monkeypatch, tmp_path):
    from mapsvc import overlay_render

    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setattr(overpass, "validate_tags", lambda pairs: None)
    window = overlays.Window(None, ("FRA",), (-5, 41, 10, 52), "France")
    spec = overpass.parse_filter(LGV)
    ql = overpass.query(spec, window)
    digest = overpass.hashlib.sha256(ql.encode()).hexdigest()[:16]
    target = overpass.fetch.path("overlays", "overpass", f"{digest}.json")
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "osm3s": {"timestamp_osm_base": "2026-09-30T12:34:56Z"},
        "elements": [{"type": "way", "id": 1,
                      "geometry": [{"lon": 1, "lat": 45},
                                   {"lon": 2, "lat": 46}],
                      "tags": {"railway": "rail", "highspeed": "yes"}}],
    }))
    old = overpass.time.time() - overpass.CACHE_FRESH_S - 1
    overpass.os.utime(target, (old, old))

    unavailable = overpass.UpstreamUnavailable("all down", overpass.ENDPOINTS)
    posts = 0

    def post(ql):
        nonlocal posts
        posts += 1
        raise unavailable

    monkeypatch.setattr(overpass, "_post", post)

    layer = overpass.load(LGV, window)
    second = overpass.load(LGV, window)
    assert ("OpenStreetMap data as of 2026-09-30; Overpass was unavailable"
            in layer["provenance"]["vintage"])
    assert ("OpenStreetMap data as of 2026-09-30; Overpass was unavailable"
            in overlay_render._footer_line(layer, set()))
    assert second["provenance"]["vintage"] == layer["provenance"]["vintage"]
    assert posts == 1, "preflight and render share the Retry-After outage result"


def test_overpass_without_a_cache_surfaces_upstream_unavailability(monkeypatch,
                                                                   tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setattr(overpass, "validate_tags", lambda pairs: None)
    unavailable = overpass.UpstreamUnavailable("all down", overpass.ENDPOINTS)
    monkeypatch.setattr(overpass, "_post", lambda ql: (_ for _ in ()).throw(unavailable))
    window = overlays.Window(None, ("FRA",), (-5, 41, 10, 52), "France")
    with pytest.raises(overpass.UpstreamUnavailable):
        overpass.load(LGV, window)


def _lgv_layer():
    data = json.loads((FIXTURES / "overpass_lgv_lyon.json").read_text())
    window = overlays.Window(None, ("FRA",), (4.0, 45.0, 6.0, 46.0), "France")
    return overpass.layer_from_overpass(data, overpass._pairs(LGV), window, "ql")


def test_overpass_route_length_merges_parallel_tracks():
    layer = _lgv_layer()
    stats = layer["stats"]
    # 73 kept ways around Lyon: a double-track line, so route ≈ half of track.
    assert stats["count"] == 73
    assert 0.45 <= stats["total_km"] / stats["track_km"] <= 0.6
    assert layer["excluded"][0]["reason"].startswith("service tracks")
    assert layer["title"] == "High-speed railway lines in France"
    assert layer["category"]["field"] == "maxspeed"
    assert layer["provenance"]["license"] == "ODbL 1.0"


def test_route_length_counts_a_double_track_once():
    a = [(0.0, 45.0), (0.1, 45.0)]
    b = [(0.0, 45.00004), (0.1, 45.00004)]        # 4.4 m away: same route
    c = [(0.0, 45.01), (0.1, 45.01)]              # 1.1 km away: another route
    one = geo.line_m(a)
    assert geo.route_m([a, b]) == pytest.approx(one, rel=0.01)
    assert geo.route_m([a, c]) == pytest.approx(2 * one, rel=0.01)


# ------------------------------------------------------------------- web ---

TROOPS = web._RDATASETS + "HistData/Minard.troops.csv"
CITIES = web._RDATASETS + "HistData/Minard.cities.csv"
TEMPS = web._RDATASETS + "HistData/Minard.temp.csv"
NAPOLEON = overlays.Window((23.0, 53.3, 38.5, 56.4), None, (23.0, 53.3, 38.5, 56.4))


@pytest.mark.parametrize("bad", [
    "http://example.com/data.csv",
    "https://user:pw@example.com/data.csv",
    "https://127.0.0.1/data.csv",
    "https://10.0.0.8/data.csv",
    "https://localhost/data.csv",
    "file:///etc/passwd",
    "https://example.com/a b.csv",
])
def test_web_accepts_only_public_https_urls(bad):
    with pytest.raises(ManifestError):
        validate(_raw(overlays=[{"source": "web", "id": bad}]))


def test_minard_is_a_flow_with_the_three_numbers():
    layer = web.layer_from_bytes((FIXTURES / "minard_troops.csv").read_bytes(),
                                 TROOPS, NAPOLEON)
    assert layer["kind"] == "flow"
    assert layer["stats"]["start_survivors"] == 422000
    assert layer["stats"]["turn_survivors"] == 100000
    # Groups 1 and 3 return to the Niemen; group 2 merged back mid-route.
    assert layer["stats"]["end_survivors"] == 10000
    assert [c["label"] for c in layer["category"]["classes"]] == [
        "Advance, June–Sept.", "Retreat, Oct.–Dec."]
    assert "Minard" in layer["provenance"]["attribution"]


def test_minard_cities_are_places():
    layer = web.layer_from_bytes((FIXTURES / "minard_cities.csv").read_bytes(),
                                 CITIES, NAPOLEON)
    assert layer["kind"] == "places" and len(layer["features"]) == 20
    assert {f["name"] for f in layer["features"] if f["major"]} == {
        "Kowno", "Wilna", "Smolensk", "Moscou"}


def test_minard_temperatures_are_converted_and_drawn_below_the_map(mini_geojson):
    layer = web.layer_from_bytes((FIXTURES / "minard_temp.csv").read_bytes(),
                                 TEMPS, NAPOLEON)
    assert layer["kind"] == "temperature"
    assert layer["features"][3]["temperature_c"] == -26.25
    troops = web.layer_from_bytes((FIXTURES / "minard_troops.csv").read_bytes(),
                                  TROOPS, NAPOLEON)
    svg = _overlay_map(mini_geojson, [troops, layer])
    assert "temperature-panel" in svg
    assert "Réaumur readings converted to °C" in svg
    assert "18 Oct" in svg and "7 Dec" in svg


def test_an_unknown_url_is_read_by_column_names_and_says_licence_unknown():
    csv_bytes = b"name,latitude,longitude,count\nA,50.1,30.2,10\nB,50.3,30.6,40\n"
    layer = web.layer_from_bytes(csv_bytes, "https://data.example.org/x.csv",
                                 overlays.Window(None, None, (29.0, 49.0, 31.0, 51.0)))
    assert layer["kind"] == "points"
    assert layer["provenance"]["license"] == overlays.UNKNOWN_LICENSE
    assert [f["size"] for f in layer["features"]] == [10.0, 40.0]


def test_a_file_without_coordinates_is_refused():
    with pytest.raises(overlays.OverlayError, match="longitude/latitude"):
        web.layer_from_bytes(b"a,b\n1,2\n", "https://data.example.org/x.csv",
                             NAPOLEON)


def test_catalogue_search_finds_minard():
    found = [c.id for c in web.search("napoleon 1812 russia army")]
    assert found[:2] == [TROOPS, CITIES] or set(found[:2]) == {TROOPS, CITIES}
    assert web.companions(TROOPS) == (TEMPS,)


def test_flow_and_lines_render(mini_geojson):
    troops = web.layer_from_bytes((FIXTURES / "minard_troops.csv").read_bytes(),
                                  TROOPS, NAPOLEON)
    # Shift the route into the fixture window.
    for f in troops["features"]:
        f["lon"] -= 15
        f["lat"] -= 5
    for note in troops["notes"]:
        note["lon"] -= 15
        note["lat"] -= 5
    svg = _overlay_map(mini_geojson, [troops])
    assert 'data-stat-start_survivors="422000"' in svg
    assert "Band width = soldiers" in svg
    assert "Retreat, Oct.–Dec." in svg
    assert svg == _overlay_map(mini_geojson, [troops])


# --------------------------------------------------------------- framing ---

from mapsvc import overlay_render, project  # noqa: E402


def test_world_map_centres_on_concentrated_overlays():
    pacific = [150.0, 160.0, 170.0, -170.0, 140.0, 120.0, -75.0]
    assert project.central_meridian(pacific) == 150.0
    assert project.central_meridian([-10.0, 10.0, 100.0, -100.0]) is None   # spread out
    assert project.central_meridian([1.0, -2.0, 3.0]) is None               # already 0


def test_rings_crossing_a_moved_seam_are_split_not_smeared():
    greenland = [(-70.0, 60.0), (-15.0, 60.0), (-15.0, 80.0), (-70.0, 80.0), (-70.0, 60.0)]
    pieces = project.split_at_seam(project.unwrap_ring(greenland, 150.0), 150.0)
    assert len(pieces) == 2
    for piece in pieces:
        lons = [lon for lon, _ in piece]
        assert -30.0 - 1e-9 <= min(lons) and max(lons) <= 330.0 + 1e-9
    inside = [(10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (10.0, 0.0)]
    assert project.split_at_seam(inside, 150.0) == [inside]


def test_within_maps_frame_on_units_near_the_overlay():
    def square(lon, lat):
        return {"type": "Polygon", "coordinates": [[(lon, lat), (lon + 1, lat), (lon + 1, lat + 1),
                                                    (lon, lat + 1), (lon, lat)]]}
    mainland, overseas = square(2.0, 46.0), square(-53.0, 4.0)
    layer = {"kind": "points", "features": [{"lon": 2.5, "lat": 46.5}]}
    assert overlay_render.focus([mainland, overseas], [layer]) == [mainland]
    assert overlay_render.focus([mainland, overseas], []) == [mainland, overseas]


def test_a_map_with_overlays_may_omit_the_window():
    manifest = validate({"level": "admin_0", "overlays": [
        {"source": "web", "id": TROOPS}]})
    assert manifest.bbox is None and manifest.within is None
    with pytest.raises(ManifestError, match="bbox"):
        validate({"level": "admin_0"})


def test_frame_box_pads_a_thin_route_into_a_readable_window():
    from mapsvc.harvest import frame_box

    west, south, east, north = frame_box((24.0, 53.9, 37.7, 55.8))
    assert west < 24.0 and east > 37.7
    assert north - south >= (east - west) * 0.57 / 3 - 1e-6   # cos 55° ≈ 0.57


def test_overlay_labels_keep_off_lines_and_bands():
    """Nimes sat on the Nimes-Montpellier line; Minard's towns on the bands."""
    from mapsvc import overlay_render as O

    class Frame:
        map_h = 400

    labels = O.Labels({"pad": 0, "map_w": 400, "map_top": 0}, Frame())
    labels.ink((100.0, 200.0), (300.0, 200.0), 6.0)
    assert not labels.fits((150, 195, 190, 205))       # on the band
    assert not labels.fits((150, 188, 190, 197))       # within its half-width
    assert labels.fits((150, 180, 190, 190))           # clear of it
    # A town on the band is labelled on a leader, off the ink.
    assert labels.callout("Smolensk", 200.0, 200.0, 2.2)
    line, text = labels.out[0], labels.out[1]
    assert line.startswith("<line") and "Smolensk" in text
    box = labels.taken[-1]
    assert not labels._inked(box)
