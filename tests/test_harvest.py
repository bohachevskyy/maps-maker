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
    assert min(values) == 100.0 * 1_000_000  # GDP_MD is converted from millions


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
    assert min(r["value"] for r in result.rows) == 100.0 * 1_000_000

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


def test_bbox_cannot_clip_units_selected_by_within(monkeypatch):
    from mapsvc.cartography import Boundaries

    def square(lon, lat):
        return {"type": "Polygon", "coordinates": [[
            [lon, lat], [lon + 1, lat], [lon + 1, lat + 1],
            [lon, lat + 1], [lon, lat],
        ]]}

    alaska = {"id": "US-AK", "name": "Alaska", "geometry": square(-150, 60)}
    hawaii = {"id": "US-HI", "name": "Hawaii", "geometry": square(-157, 20)}
    california = {"id": "US-CA", "name": "California",
                  "geometry": square(-120, 35)}

    def load(source, level, bbox, detail, within):
        units = [alaska, hawaii, california] if bbox is None else [california]
        return Boundaries(units=units)

    monkeypatch.setattr(H.cartography, "load", load)
    manifest = build_manifest(bbox=(-125, 24, -66, 50), within=("USA",),
                              level="admin_1", variable_source=None,
                              variable_id=None, basemap_source="census")
    with pytest.raises(H.HarvestError) as caught:
        H.validate_window(manifest)
    assert caught.value.field == "bbox"
    assert "Alaska" in str(caught.value) and "Hawaii" in str(caught.value)
    assert "drop the bbox or widen it" in str(caught.value)


def test_redundant_bbox_over_four_times_within_extent_is_dropped(monkeypatch, tmp_path):
    from mapsvc.cartography import Boundaries

    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    units = [
        {"id": "US-AK", "name": "Alaska", "geometry": {
            "type": "Polygon", "coordinates": [[[-170, 52], [-130, 52],
                [-130, 71], [-170, 71], [-170, 52]]]}},
        {"id": "US-HI", "name": "Hawaii", "geometry": {
            "type": "Polygon", "coordinates": [[[-160, 18], [-154, 18],
                [-154, 23], [-160, 23], [-160, 18]]]}},
        {"id": "US-CA", "name": "California", "geometry": {
            "type": "Polygon", "coordinates": [[[-125, 32], [-114, 32],
                [-114, 42], [-125, 42], [-125, 32]]]}}]
    boundaries = Boundaries(units=units, provenance={})
    monkeypatch.setattr(H.cartography, "load", lambda *args, **kwargs: boundaries)

    manifest = build_manifest(bbox=(-180, -90, 180, 90), within=("USA",),
                              level="admin_1", variable_source=None,
                              variable_id=None, basemap_source="census")
    result = H.harvest(manifest)

    assert result.provenance["bbox"] is None
    dropped = result.provenance["bbox_dropped"]
    assert dropped["bbox"] == [-180, -90, 180, 90]
    assert dropped["within_extent"] == [-170.0, 18.0, -114.0, 71.0]
    assert dropped["area_ratio"] == pytest.approx(21.833, abs=0.001)
    assert "clips no selected unit" in dropped["reason"]


def test_bbox_holding_every_within_unit_is_dropped_even_when_snug(monkeypatch, tmp_path):
    from mapsvc.cartography import Boundaries

    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    boundaries = Boundaries(units=[{"id": "AAA", "name": "A", "geometry": BOX}],
                            provenance={})
    monkeypatch.setattr(H.cartography, "load", lambda *args, **kwargs: boundaries)

    manifest = build_manifest(bbox=(-0.5, -0.5, 1.5, 1.5), within=("AAA",),
                              variable_source=None, variable_id=None)
    result = H.harvest(manifest)

    assert result.provenance["bbox"] is None
    assert result.effective.bbox is None
    assert "holds the within units' whole extent" in \
        result.provenance["bbox_dropped"]["reason"]


def test_bbox_zooming_into_a_within_unit_is_kept(monkeypatch, tmp_path):
    from mapsvc.cartography import Boundaries

    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    boundaries = Boundaries(units=[{"id": "AAA", "name": "A", "geometry": BOX}],
                            provenance={})
    monkeypatch.setattr(H.cartography, "load", lambda *args, **kwargs: boundaries)

    manifest = build_manifest(bbox=(0.2, 0.2, 0.8, 0.8), within=("AAA",),
                              variable_source=None, variable_id=None)
    result = H.harvest(manifest)

    assert result.provenance["bbox"] == [0.2, 0.2, 0.8, 0.8]
    assert "bbox_dropped" not in result.provenance


def _line_layer(coords):
    return {"kind": "lines", "features": [{"coords": coords}], "provenance": {}}


def test_oversized_bbox_on_an_overlay_map_frames_on_the_overlay(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    layers = [_line_layer([[10.0, 40.0], [14.0, 44.0]])]
    monkeypatch.setattr(H, "_load_overlay_layers", lambda specs, window: layers)
    monkeypatch.setattr(H, "_add_physical_context", lambda m, r, extent: None)
    monkeypatch.setattr(H, "_base", lambda m, b=None: H.HarvestResult(provenance={}))
    monkeypatch.setattr(H, "context_places", lambda *a, **k: [])
    wide = build_manifest(bbox=(-180, -90, 180, 90), variable_source=None,
                          variable_id=None, overlays=(("web", "https://x/y.csv"),))
    bare = build_manifest(bbox=None, variable_source=None, variable_id=None,
                          overlays=(("web", "https://x/y.csv"),))

    a, b = H.harvest(wide), H.harvest(bare)

    assert a.provenance["overlay_bbox"] == b.provenance["overlay_bbox"]
    assert a.effective.bbox is None


def test_bbox_clipping_an_overlay_feature_is_kept(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    layers = [_line_layer([[10.0, 40.0], [14.0, 44.0]])]
    monkeypatch.setattr(H, "_load_overlay_layers", lambda specs, window: layers)
    assert not H._bbox_swamps_overlays((11.0, 39.0, 20.0, 50.0), layers)
    assert not H._bbox_swamps_overlays((9.0, 39.0, 15.0, 45.0), layers)   # under 4x
    assert H._bbox_swamps_overlays((-180, -90, 180, 90), layers)


def test_us_units_derive_the_census_basemap_and_say_so():
    manifest = build_manifest(within=("USA",), level="admin_1",
                              variable_source=None, variable_id=None,
                              basemap_source="overture")
    derived, notes = H.derive_manifest(manifest)
    assert derived.basemap_source == "census"
    assert "replaced by census" in notes[0]
    state = build_manifest(within=("US-TX",), level="admin_2", variable_source=None,
                           variable_id=None, basemap_source="overture")
    assert H.derive_manifest(state)[0].basemap_source == "census"


def test_non_us_or_unsupported_level_keeps_the_chosen_basemap():
    other = build_manifest(within=("CAN",), level="admin_1", variable_source=None,
                           variable_id=None, basemap_source="overture")
    assert H.derive_manifest(other) == (other, ())
    country = build_manifest(within=("USA",), level="admin_0", variable_source=None,
                             variable_id=None, basemap_source="natural_earth")
    assert H.derive_manifest(country) == (country, ())


def test_single_country_overlay_map_gets_admin_1_interior_lines():
    for level in ("admin_0", "admin_1"):
        manifest = build_manifest(within=("RUS",), level=level, variable_source=None,
                                  variable_id=None, basemap_source="natural_earth",
                                  overlays=(("overpass", "route=railway"),))
        derived, _, notes = H._interior_context_level(manifest, None)
        assert derived.level == "admin_1"
        assert bool(notes) == (level == "admin_0")
    several = build_manifest(within=("RUS", "UKR"), level="admin_0",
                             variable_source=None, variable_id=None,
                             overlays=(("overpass", "route=railway"),))
    assert H._interior_context_level(several, None)[0].level == "admin_0"


def test_pipeline_renders_without_a_bbox_that_harvest_dropped(monkeypatch):
    from mapsvc import pipeline

    result = H.HarvestResult(
        provenance={"bbox_dropped": {"bbox": [-180, -90, 180, 90]}})
    seen = {}
    monkeypatch.setattr(pipeline, "harvest", lambda manifest: result)
    monkeypatch.setattr(
        pipeline, "render",
        lambda manifest, harvested: seen.setdefault("bbox", manifest.bbox) or "svg",
    )

    actual = pipeline.draw(build_manifest(bbox=(-180, -90, 180, 90),
                                          within=("USA",)))

    assert actual == "svg"
    assert seen["bbox"] is None


def test_wide_admin_0_rule_is_carried_to_the_footnote_provenance(monkeypatch):
    result = H.HarvestResult(rows=[], provenance={"vintage": "v5.1.1"}, dropped=[])
    monkeypatch.setattr(H, "_base", lambda manifest: result)
    manifest = build_manifest(bbox=(-180, -90, 180, 90), variable_source=None,
                              variable_id=None, basemap_source="natural_earth")
    actual = H.harvest(manifest)
    assert "Natural Earth required for admin_0 windows wider than 90°" in \
        actual.provenance["vintage"]


def test_boundary_note_is_carried_to_the_footnote_provenance(monkeypatch):
    from mapsvc.cartography import Boundaries

    boundaries = Boundaries(
        units=[{"id": "ESH", "key": "ESH", "name": "W. Sahara",
                "geometry": BOX}],
        keys=("ISO3",),
        provenance={
            "source": "Natural Earth", "release": "v5.1.1", "scale": "1:50m",
            "note": "Western Sahara shown as a non-self-governing territory (UN)",
        },
    )
    monkeypatch.setattr(H, "_check_granularity", lambda manifest: None)
    monkeypatch.setattr(H.cartography, "load", lambda *args, **kwargs: boundaries)
    result = H._build(build_manifest(variable_source=None, variable_id=None))
    assert result.provenance["vintage"] == (
        "v5.1.1; Western Sahara shown as a non-self-governing territory (UN)"
    )


def test_rows_and_dropped_come_back_in_a_stable_order(offline):
    offline([feature(a3, GDP_MD=v) for a3, v in
             [("DDD", 4), ("AAA", 1), ("CCC", -99), ("BBB", 2)]])
    result = H.harvest(build_manifest(bbox=HERE))
    assert [r["id"] for r in result.rows] == ["AAA", "BBB", "DDD"]
    assert [d["id"] for d in result.dropped] == ["CCC"]


def test_every_explicit_within_country_is_drawn_or_hatched(monkeypatch):
    from mapsvc.cartography import Boundaries
    from mapsvc.statistics import Values

    units = [
        {"id": "AAA", "keys": {"ISO3": "AAA"}, "geometry": BOX},
        {"id": "BBB", "keys": {"ISO3": "BBB"}, "geometry": BOX},
    ]
    boundaries = Boundaries(units=units, keys=("ISO3",), provenance={})
    monkeypatch.setattr(H, "_check_granularity", lambda manifest: None)
    monkeypatch.setattr(H.cartography, "load", lambda *args, **kwargs: boundaries)
    monkeypatch.setattr(H.derive, "values", lambda manifest: Values(
        values={"AAA": 42.0}, provenance={"level": "ratio"}))
    manifest = build_manifest(within=("AAA", "BBB"), bbox=None,
                              variable_source="owid", variable_id="x")
    result = H._build(manifest)
    assert [row["id"] for row in result.rows] == ["AAA"]
    assert [(row["id"], row["reason"]) for row in result.dropped] == [("BBB", "no_join")]
    assert len(result.rows) + len(result.dropped) == len(manifest.within)
    from mapsvc.render import render
    svg = render(manifest, result)
    assert 'data-id="AAA"' in svg and 'data-id="BBB"' in svg
    assert "1 of 2 features excluded" in svg


def test_missing_explicit_within_country_is_an_error(monkeypatch):
    from mapsvc.cartography import Boundaries

    boundaries = Boundaries(
        units=[{"id": "AAA", "keys": {"ISO3": "AAA"}, "geometry": BOX}],
        keys=("ISO3",), provenance={})
    monkeypatch.setattr(H, "_check_granularity", lambda manifest: None)
    monkeypatch.setattr(H.cartography, "load", lambda *args, **kwargs: boundaries)
    manifest = build_manifest(within=("AAA", "BBB"), bbox=None,
                              variable_source=None, variable_id=None)
    with pytest.raises(H.HarvestError, match="BBB") as caught:
        H._build(manifest)
    assert caught.value.field == "within"



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


def test_the_year_is_that_of_the_drawn_units_not_the_whole_source(offline):
    """A 16-country map of Europe said 2009-2025 because the world file does."""
    offline([_at(0, 0, a3="AAA", GDP_YEAR=2019), _at(50, 50, a3="FAR", GDP_YEAR=2009)])
    result = H.harvest(build_manifest(bbox=(-1.0, -1.0, 3.0, 3.0)))
    assert [r["id"] for r in result.rows] == ["AAA"]
    assert result.provenance["year"] == "2019"
    assert result.provenance["years_drawn"] == [2019]


def test_dropped_units_carry_their_name_and_reasons_their_meaning(offline):
    offline([_at(0, 0, a3="AAA", NAME="Alpha"), _at(1, 0, a3="BBB", NAME="Beta", GDP_MD=-99)])
    result = H.harvest(build_manifest(bbox=(-1.0, -1.0, 5.0, 3.0)))
    assert result.dropped[0]["name"] == "Beta"
    assert result.provenance["drop_reasons"] == {"no_data": "no value reported by the source"}


def test_antarctica_is_plain_land_not_an_exclusion(offline):
    offline([_at(0, 0, a3="AAA"), _at(1, 0, a3="ATA", NAME="Antarctica", GDP_MD=-99)])
    result = H.harvest(build_manifest(bbox=(-1.0, -1.0, 5.0, 3.0)))
    assert result.dropped == []
    assert result.provenance["plain_land"] == ["Antarctica"]
    assert [c["id"] for c in result.context] == ["ATA"]


def test_the_modal_year_and_its_exceptions_are_named(offline):
    offline([_at(0, 0, a3="AAA", NAME="Alpha", GDP_YEAR=2019),
             _at(1, 0, a3="BBB", NAME="Beta", GDP_YEAR=2019),
             _at(2, 0, a3="CCC", NAME="Gamma", GDP_YEAR=2015)])
    p = H.harvest(build_manifest(bbox=(-1.0, -1.0, 6.0, 3.0))).provenance
    assert p["year_mode"] == 2019
    assert p["year_exceptions"] == {"2015": ["Gamma"]}


def test_sea_names_are_clipped_to_the_maps_neighbourhood(monkeypatch):
    from mapsvc import harvest as H
    from mapsvc.models import HarvestResult as HR

    near = {"type": "Feature", "properties": {"name": "Near Sea", "featurecla": "sea",
                                              "min_label": 4},
            "geometry": {"type": "Polygon", "coordinates": [
                [[0, 0], [60, 0], [60, 10], [0, 10], [0, 0]]]}}
    far = {"type": "Feature", "properties": {"name": "Far Sea", "featurecla": "sea",
                                             "min_label": 2},
           "geometry": {"type": "Polygon", "coordinates": [
               [[150, -60], [160, -60], [160, -50], [150, -50], [150, -60]]]}}
    small = {"type": "Feature", "properties": {"name": "Tiny Bay", "featurecla": "bay",
                                               "min_label": 7.6},
             "geometry": {"type": "Polygon", "coordinates": [
                 [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]]}}
    monkeypatch.setattr(H, "marine_areas", lambda: [near, far, small])
    manifest = build_manifest(bbox=(0.0, 0.0, 20.0, 20.0))
    result = HR(rows=[], provenance={}, dropped=[])
    H._add_names(manifest, result)
    assert [n["name"] for n in result.names] == ["Near Sea"]
    ring = result.names[0]["geometry"]["coordinates"][0][0]
    assert max(p[0] for p in ring) <= 20.0, "clipped to the window"
    assert result.provenance["names_source"] == H.MARINE_CREDIT


def test_city_scale_routes_derive_municipal_context():
    city = validate({
        "bbox": [139.5, 35.55, 139.95, 35.85], "level": "admin_1",
        "basemap": {"source": "natural_earth"}, "variable": None,
        "overlays": [{"source": "overpass", "id":
                      "route=subway;network=東京メトロ|都営地下鉄"}],
    })
    derived = H._overlay_context_manifest(city)
    assert derived.level == "admin_2"
    assert derived.basemap_source == "overture"
    assert derived.basemap_detail == "full"

    regional = validate({
        "bbox": [0, 40, 20, 55], "level": "admin_1", "variable": None,
        "basemap": {"source": "natural_earth"},
        "overlays": [{"source": "overpass", "id": "route=train;network=Rail"}],
    })
    assert H._overlay_context_manifest(regional) == regional


def test_bbox_overlay_window_is_named_by_its_largest_place(monkeypatch):
    from mapsvc.models import HarvestResult

    manifest = validate({
        "bbox": [139.5, 35.55, 139.95, 35.85], "level": "admin_1",
        "basemap": {"source": "natural_earth"}, "variable": None,
        "overlays": [{"source": "overpass", "id": "route=subway;network=Metro"}],
    })
    seen = {}
    monkeypatch.setattr(H, "context_places", lambda *args, **kwargs: [
        {"name": "Tokyo", "lon": 139.7, "lat": 35.68, "pop": 37_000_000}])

    def layers(specs, window):
        seen["window"] = window
        return [{"kind": "lines", "style": "routes", "features": []}]

    monkeypatch.setattr(H, "_load_overlay_layers", layers)
    monkeypatch.setattr(H, "_add_physical_context", lambda *args: None)
    result = HarvestResult(rows=[{"id": "x", "geometry": BOX, "value": None}],
                           provenance={}, dropped=[])
    H._add_overlays(manifest, result)
    assert seen["window"].name == "Tokyo"
