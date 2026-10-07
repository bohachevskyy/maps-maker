"""Overlays: grammars checked before I/O, layers drawn deterministically.

Offline: every source is exercised on a small recorded response.
"""

import json
import pathlib

import pytest

from mapsvc import overlays
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

def _overlay_map(mini_geojson, layers, places=()):
    manifest = build_manifest(variable_source=None, variable_id=None,
                              overlays=(("usgs", QUAKES),))
    rows = [{"id": f["properties"]["ADM0_A3"], "geometry": f["geometry"], "value": None}
            for f in mini_geojson["features"]]
    provenance = {"source": "Natural Earth", "scale": "50m", "vintage": "v5.1.1"}
    result = HarvestResult(rows=rows, provenance=provenance, dropped=[],
                           overlays=list(layers), places=list(places))
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


def test_overlay_render_module_does_no_io():
    from tests.test_no_io import _reachable_from

    assert "overlay_render" in _reachable_from("render")
