"""A local Overpass server is tried first, but only inside its coverage. Offline."""

import json

import pytest

from mapsvc import overlays
from mapsvc.overlays import overpass

LOCAL = "http://127.0.0.1:12345/api/interpreter"
KYIV = (30.2, 50.2, 30.8, 50.6)
PARIS = (2.2, 48.8, 2.5, 48.95)
SPEC = overpass.parse_filter("amenity=hospital")
ANSWER = json.dumps({"osm3s": {"timestamp_osm_base": "2026-11-01T00:00:00Z"},
                     "elements": [
                         {"type": "node", "id": 1, "lon": 30.5, "lat": 50.4,
                          "tags": {"amenity": "hospital", "name": "H"}}]}).encode()


@pytest.fixture(autouse=True)
def _local(monkeypatch, tmp_path):
    coverage = tmp_path / "coverage.json"
    coverage.write_text(json.dumps({"extracts": [
        {"name": "ukraine", "bbox": [22.0, 44.0, 40.5, 52.5], "countries": ["UA"]}]}))
    monkeypatch.setenv("MAPSVC_OVERPASS_COVERAGE", str(coverage))
    monkeypatch.setenv("MAPSVC_OVERPASS_LOCAL", LOCAL)
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(overpass, "validate_tags", lambda pairs: None)


def _window(bbox, within=None):
    return overlays.Window(bbox, within, bbox)


def _record(monkeypatch, local_error=None):
    posted = []

    def post(url, body, timeout=0, **kw):
        posted.append(url)
        if url == LOCAL and local_error is not None:
            raise overpass.fetch.FetchError("down", local_error or None)
        return ANSWER

    monkeypatch.setattr(overpass.fetch, "post", post)
    monkeypatch.setattr(overpass.fetch, "get", lambda *a, **k: b"1 slot available now.")
    return posted


def test_local_is_used_first_inside_coverage(monkeypatch):
    posted = _record(monkeypatch)
    layer = overpass.load("amenity=hospital", _window(KYIV))
    assert posted == [LOCAL]
    assert "2026-11-01" in json.dumps(layer["provenance"])


def test_local_is_used_for_a_covered_country(monkeypatch):
    posted = _record(monkeypatch)
    overpass.load("amenity=hospital", _window(None, ("UKR",)))
    assert posted == [LOCAL]


def test_local_is_skipped_outside_coverage(monkeypatch):
    posted = _record(monkeypatch)
    overpass.load("amenity=hospital", _window(PARIS))
    assert posted and LOCAL not in posted


def test_local_is_skipped_for_an_unloaded_country(monkeypatch):
    posted = _record(monkeypatch)
    overpass.load("amenity=hospital", _window(KYIV, ("FRA",)))
    assert LOCAL not in posted


def test_local_is_off_without_the_env_var(monkeypatch):
    monkeypatch.delenv("MAPSVC_OVERPASS_LOCAL")
    posted = _record(monkeypatch)
    overpass.load("amenity=hospital", _window(KYIV))
    assert LOCAL not in posted


@pytest.mark.parametrize("status", [500, 504, 0])
def test_public_endpoints_take_over_when_local_errors(monkeypatch, status):
    posted = _record(monkeypatch, local_error=status)
    overpass.load("amenity=hospital", _window(KYIV))
    assert posted[0] == LOCAL and posted[1] in overpass.ENDPOINTS


def test_runtime_error_remark_falls_back(monkeypatch):
    posted = []

    def post(url, body, timeout=0, **kw):
        posted.append(url)
        if url == LOCAL:
            return b'{"remark": "runtime error: x", "elements": []}'
        return ANSWER

    monkeypatch.setattr(overpass.fetch, "post", post)
    monkeypatch.setattr(overpass.fetch, "get", lambda *a, **k: b"1 slot available now.")
    overpass.load("amenity=hospital", _window(KYIV))
    assert posted[0] == LOCAL and posted[1] in overpass.ENDPOINTS


def test_missing_coverage_file_means_never_local(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_OVERPASS_COVERAGE", str(tmp_path / "none.json"))
    assert not overpass.local_covers(_window(KYIV))
