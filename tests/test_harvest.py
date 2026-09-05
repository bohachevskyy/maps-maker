import json

import pytest

from mapsvc import harvest as H
from mapsvc.manifest import validate
from tests.conftest import build_manifest

BOX = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
# A window that contains BOX, used wherever a test just needs "somewhere".
HERE = (-1.0, -1.0, 2.0, 2.0)
# A window that does not.
ELSEWHERE = (100.0, 60.0, 110.0, 70.0)


def feature(a3, continent="Europe", **props):
    base = {"ADM0_A3": a3, "CONTINENT": continent, "NAME": a3,
            "POP_EST": 1000, "POP_YEAR": 2019, "GDP_MD": 500, "GDP_YEAR": 2019,
            "POP_RANK": 5, "INCOME_GRP": "3. Upper middle income",
            "ECONOMY": "6. Developing region", "SUBREGION": "Western Europe"}
    base.update(props)
    return {"type": "Feature", "properties": base, "geometry": BOX}


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Point the cache at a temp dir and stub the fetch."""
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))

    def install(features):
        monkeypatch.setattr(H, "load_source", lambda level="admin_0": {"features": features})
    return install


def test_minus_99_and_zero_land_in_dropped_not_the_value_domain(offline):
    offline([
        feature("AAA", GDP_MD=100),
        feature("BBB", GDP_MD=-99),     # explicit no-data marker
        feature("CCC", GDP_MD=0),       # zero count is a placeholder row
        feature("DDD", GDP_MD=300),
    ])
    result = H.harvest(build_manifest(bbox=HERE, variable_id="GDP_MD"))
    assert sorted(r["id"] for r in result.rows) == ["AAA", "DDD"]
    assert {d["id"]: d["reason"] for d in result.dropped} == {"BBB": "no_data", "CCC": "no_data"}
    values = [r["value"] for r in result.rows]
    assert -99 not in values and 0 not in values
    assert min(values) == 100.0


def test_a_sentinel_would_otherwise_be_shaded_as_the_lowest_real_value(offline):
    """Why the sentinel filter matters, not just that it fires.

    Measured against the real 10m file, -99 and 0 barely move an
    equal_interval domain -- world GDP already spans 10 to 21,433,226, so the
    skew comes from the United States, not from the sentinel. What the filter
    actually prevents is a lie: unfiltered, the Vatican's -99 becomes the
    darkest-or-palest real value on the map instead of being marked no-data.
    """
    from mapsvc import classify
    offline([feature(f"C{i:02d}", GDP_MD=v)
             for i, v in enumerate([100, 200, 300, 400, 500, -99])])
    result = H.harvest(build_manifest(bbox=HERE))
    assert min(r["value"] for r in result.rows) == 100.0

    polluted = classify.build([100, 200, 300, 400, 500, -99], "count", "quantile", 5)
    clean = classify.build([100, 200, 300, 400, 500], "count", "quantile", 5)
    # Unfiltered, the sentinel occupies the bottom bin and shifts every real
    # country up a class.
    assert polluted.bin_of(-99) == 0
    assert polluted.bin_of(100) != clean.bin_of(100)


def test_zero_is_kept_for_ordinal_variables(offline):
    """A count of zero is a placeholder; a rank of zero could be real."""
    offline([feature("AAA", POP_RANK=0), feature("BBB", POP_RANK=5),
             feature("CCC", POP_RANK=-99)])
    result = H.harvest(build_manifest(bbox=HERE, variable_id="POP_RANK"))
    assert sorted(r["id"] for r in result.rows) == ["AAA", "BBB"]
    assert [d["id"] for d in result.dropped] == ["CCC"]


def test_dropped_rows_keep_their_geometry(offline):
    """`missing: "hatch"` has to draw the features it excludes."""
    offline([feature("AAA", GDP_MD=100), feature("BBB", GDP_MD=-99)])
    result = H.harvest(build_manifest(bbox=HERE))
    assert result.dropped[0]["geometry"] == BOX






def test_variables_without_a_year_column_report_none(offline):
    offline([feature("AAA")])
    result = H.harvest(build_manifest(bbox=HERE, variable_id="SUBREGION"))
    assert result.provenance["year"] is None


def test_cache_key_covers_every_manifest_field():
    """There are no render-time fields left, so nothing is excluded from the
    key -- unlike the old manifest, where ramp and classify deliberately were."""
    def key(**over):
        return H.cache_key(build_manifest(**over))
    base = key()
    assert base != key(bbox=(0.0, 0.0, 1.0, 1.0))
    assert base != key(level="admin_1")
    assert base != key(variable_id="POP_EST")
    assert base != key(basemap_source="overture")
    assert base == key()


def test_cache_key_includes_the_scale():
    """SCALE decides which geometry the rows carry, so it must key the cache."""
    from mapsvc import registry
    manifest = build_manifest()
    import pytest as _pytest
    monkey = _pytest.MonkeyPatch()
    try:
        monkey.setattr(registry, "SCALE", "110m")
        coarse = H.cache_key(manifest)
        monkey.setattr(registry, "SCALE", "50m")
        assert H.cache_key(manifest) != coarse
    finally:
        monkey.undo()


def test_second_harvest_reads_the_cache_and_matches(offline, monkeypatch):
    offline([feature("AAA", GDP_MD=100), feature("BBB", GDP_MD=-99)])
    manifest = build_manifest(bbox=HERE)
    first = H.harvest(manifest)

    def explode(level="admin_0"):
        raise AssertionError("second harvest must not re-read the source")
    monkeypatch.setattr(H, "load_source", explode)

    second = H.harvest(manifest)
    assert (second.rows, second.provenance, second.dropped) == \
           (first.rows, first.provenance, first.dropped)


def test_rows_and_dropped_come_back_in_a_stable_order(offline):
    offline([feature(a3, GDP_MD=v) for a3, v in
             [("DDD", 4), ("AAA", 1), ("CCC", -99), ("BBB", 2)]])
    result = H.harvest(build_manifest(bbox=HERE))
    assert [r["id"] for r in result.rows] == ["AAA", "BBB", "DDD"]
    assert [d["id"] for d in result.dropped] == ["CCC"]



def test_a_genuinely_empty_variable_still_blames_the_variable(offline):
    offline([feature("AAA", GDP_MD=-99), feature("BBB", GDP_MD=-99)])
    with pytest.raises(H.HarvestError) as excinfo:
        H.harvest(build_manifest(bbox=HERE))
    assert excinfo.value.field == "variable.id"




def test_admin_1_uses_its_own_scale_and_id(monkeypatch, tmp_path):
    """50m admin-1 carries only 294 units across nine large countries and no
    Ukrainian oblasts, so admin-1 is pinned to 10m regardless of SCALE."""
    from mapsvc import registry
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    assert registry.scale_for("admin_1") == "10m"
    assert registry.scale_for("admin_0") == registry.SCALE
    assert "admin_1_states_provinces" in registry.data_url("admin_1")
    assert H.source_path("admin_1").name.startswith("ne_10m_admin_1")



def test_cache_key_includes_a_format_version():
    """A change to the cached row shape must not be served from old entries."""
    manifest = build_manifest()
    before = H.cache_key(manifest)
    original = H.CACHE_VERSION
    try:
        H.CACHE_VERSION = original + 1
        assert H.cache_key(manifest) != before
    finally:
        H.CACHE_VERSION = original


# --- bbox selection replaces region matching ------------------------------

def _at(lon, lat, **props):
    """A one-degree square feature at a given place."""
    base = {"ADM0_A3": props.pop("a3", "XXX"), "CONTINENT": "Nowhere",
            "NAME": "X", "POP_EST": 1000, "POP_YEAR": 2019,
            "GDP_MD": 500, "GDP_YEAR": 2019, "POP_RANK": 5,
            "INCOME_GRP": "3. Upper middle income",
            "ECONOMY": "6. Developing region", "SUBREGION": "Western Europe"}
    base.update(props)
    return {"type": "Feature", "properties": base,
            "geometry": {"type": "Polygon", "coordinates": [[
                [lon, lat], [lon + 1, lat], [lon + 1, lat + 1],
                [lon, lat + 1], [lon, lat]]]}}


def test_only_units_intersecting_the_bbox_are_returned(offline):
    offline([_at(0, 0, a3="AAA"), _at(10, 10, a3="BBB"), _at(100, 60, a3="CCC")])
    result = H.harvest(build_manifest(bbox=(-1.0, -1.0, 12.0, 12.0)))
    assert sorted(r["id"] for r in result.rows) == ["AAA", "BBB"]


def test_a_unit_overlapping_the_edge_is_included_whole(offline):
    """Intersection, not containment: a unit half inside the window is in."""
    offline([_at(0, 0, a3="AAA")])
    result = H.harvest(build_manifest(bbox=(0.5, 0.5, 3.0, 3.0)))
    assert [r["id"] for r in result.rows] == ["AAA"]


def test_an_empty_window_names_the_bbox(offline):
    from mapsvc.cartography import CartographyError
    offline([_at(0, 0, a3="AAA")])
    with pytest.raises((H.HarvestError, CartographyError)) as excinfo:
        H.harvest(build_manifest(bbox=(100.0, 60.0, 110.0, 70.0)))
    assert excinfo.value.field == "bbox"


def test_a_unit_wrapping_the_antimeridian_is_not_returned_everywhere(offline):
    """Alaska's Unorganized Borough spans 358.9 degrees, so its bounding box
    intersects every window on earth. It must not appear on a map of Kyiv."""
    wrapper = {"type": "Feature",
               "properties": {"ADM0_A3": "WRP", "CONTINENT": "Nowhere", "NAME": "W",
                              "GDP_MD": 1, "GDP_YEAR": 2019, "POP_EST": 1,
                              "POP_YEAR": 2019, "POP_RANK": 1,
                              "INCOME_GRP": "5. Low income",
                              "ECONOMY": "6. Developing region",
                              "SUBREGION": "Western Europe"},
               "geometry": {"type": "Polygon", "coordinates": [[
                   [-179.5, 50], [179.5, 50], [179.5, 55], [-179.5, 55], [-179.5, 50]]]}}
    offline([_at(0, 0, a3="AAA"), wrapper])
    result = H.harvest(build_manifest(bbox=(-1.0, -1.0, 2.0, 2.0)))
    assert [r["id"] for r in result.rows] == ["AAA"]


def test_the_year_is_a_single_value_or_a_range(offline):
    # Distinct windows, because the same manifest would be served from cache
    # and the second stub would never be read.
    offline([_at(0, 0, a3="AAA", GDP_YEAR=2019), _at(1, 0, a3="BBB", GDP_YEAR=2019)])
    assert H.harvest(build_manifest(bbox=(-1.0, -1.0, 3.0, 3.0))).provenance["year"] == "2019"
    offline([_at(0, 0, a3="AAA", GDP_YEAR=1999), _at(1, 0, a3="BBB", GDP_YEAR=2019)])
    assert H.harvest(build_manifest(bbox=(-1.0, -1.0, 4.0, 4.0))).provenance["year"] == "1999–2019"
