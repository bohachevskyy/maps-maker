"""The same manifest must always produce the same bytes.

A map that changes between identical requests cannot be cited, diffed or
cached, and there is no reason for it to change.
"""

import copy

import pytest

from mapsvc import harvest as H
from mapsvc.pipeline import build_map

BOX = {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 3], [0, 3], [0, 0]]]}

RAW = {
    "bbox": [-5.0, 35.0, 40.0, 60.0],
    "level": "admin_0",
    # Pinned: these tests stub the Natural Earth loader and never touch S3.
    "basemap": {"source": "natural_earth", "detail": "simplified"},
    "variable": {"source": "natural_earth", "id": "GDP_MD"},
}


def _features():
    out = []
    for i, (gdp, pop) in enumerate([(100, 10), (400, 10), (900, 10), (-99, 10), (250, 0)]):
        lon = i * 5
        out.append({
            "type": "Feature",
            "properties": {"ADM0_A3": f"C{i:02d}", "CONTINENT": "Europe", "NAME": f"C{i}",
                           "GDP_MD": gdp, "GDP_YEAR": 2019, "POP_EST": pop, "POP_YEAR": 2019,
                           "POP_RANK": 5, "INCOME_GRP": "3. Upper middle income",
                           "ECONOMY": "6. Developing region", "SUBREGION": "Western Europe"},
            "geometry": {"type": "Polygon", "coordinates": [
                [[lon, 40], [lon + 4, 40], [lon + 4, 44], [lon, 44], [lon, 40]]]},
        })
    return out


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setattr(H, "load_source", lambda level="admin_0": {"features": _features()})
    return tmp_path


def test_same_manifest_twice_is_byte_identical(offline):
    first = build_map(copy.deepcopy(RAW))
    second = build_map(copy.deepcopy(RAW))
    assert first.encode() == second.encode()


def test_a_cached_harvest_renders_the_same_bytes_as_a_fresh_one(offline, monkeypatch):
    """Round-tripping through the cache's JSON must not perturb any float."""
    fresh = build_map(copy.deepcopy(RAW))
    assert list((offline / "harvest").glob("*.json")), "expected a cache entry"

    def explode(level="admin_0"):
        raise AssertionError("should have been served from cache")
    monkeypatch.setattr(H, "load_source", explode)

    assert build_map(copy.deepcopy(RAW)).encode() == fresh.encode()





def test_output_carries_no_timestamp_or_random_identifier(offline):
    svg = build_map(copy.deepcopy(RAW))
    import datetime
    assert str(datetime.date.today().year) not in svg.replace("2019", "")


def test_changing_the_window_refetches(offline):
    build_map(copy.deepcopy(RAW))
    before = {p.name for p in (offline / "harvest").glob("*.json")}
    build_map({**copy.deepcopy(RAW), "bbox": [0.0, 39.0, 12.0, 45.0]})
    assert {p.name for p in (offline / "harvest").glob("*.json")} > before


def test_every_manifest_field_is_data_relevant_now(offline):
    """The old manifest had render-time fields that deliberately did not
    invalidate a fetch. There are none left, so any change is a new entry."""
    build_map(copy.deepcopy(RAW))
    one = {p.name for p in (offline / "harvest").glob("*.json")}
    build_map({**copy.deepcopy(RAW), "bbox": [-4.0, 39.0, 39.0, 59.0]})
    two = {p.name for p in (offline / "harvest").glob("*.json")}
    assert len(two) == len(one) + 1
