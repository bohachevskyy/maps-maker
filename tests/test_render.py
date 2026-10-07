import xml.etree.ElementTree as ET

import pytest

from mapsvc import colors, registry
from mapsvc.render import RenderError, render
from tests.conftest import build_manifest, build_result

SVG = "{http://www.w3.org/2000/svg}"
# Natural Earth publishes no ratio variables; harvest passes the measurement
# level through provenance, so a synthetic result can declare one.
RATIO = {"measurement": "ratio"}


def _features(text):
    """The unit polygons -- not the water, the frame or the graticule."""
    return [p for p in ET.fromstring(text).findall(f".//{SVG}path") if p.get("data-id")]


def _svg(geojson, **overrides):
    manifest = build_manifest(**overrides)
    result = build_result(geojson, manifest)
    return render(manifest, result), manifest, result


def test_render_produces_wellformed_svg(mini_geojson):
    text, _, _ = _svg(mini_geojson)
    root = ET.fromstring(text)  # raises if malformed
    assert root.tag == f"{SVG}svg"


def test_feature_count_matches_rows_plus_drawn_missing(mini_geojson):
    text, _, result = _svg(mini_geojson)
    ids = {p.get("data-id") for p in _features(text)}
    assert ids == {r["id"] for r in result.rows} | {d["id"] for d in result.dropped}


def test_shaded_features_expose_raw_values_but_missing_features_do_not(mini_geojson):
    manifest = build_manifest()
    result = build_result(mini_geojson, manifest)
    result.rows[0]["value"] = 1234567.89
    text = render(manifest, result)
    paths = {p.get("data-id"): p for p in _features(text)}
    assert paths[result.rows[0]["id"]].get("data-value") == "1234567.89"
    for row in result.rows:
        assert float(paths[row["id"]].get("data-value")) == row["value"]
    assert all(paths[item["id"]].get("data-value") is None
               for item in result.dropped)


def test_data_values_are_exact_for_whole_numbers_and_ten_digits_otherwise():
    """Kyiv's 2,952,301 people must not come back as 2.9523e+06."""
    from mapsvc.render import _raw_value
    assert _raw_value(2952301.0) == "2952301"
    assert _raw_value(2952301) == "2952301"
    assert _raw_value(0.1 + 0.2) == "0.3"
    assert _raw_value(15.284937261) == "15.28493726"
    assert _raw_value(-0.0) == "0"
    assert _raw_value("Low income") == "Low income"


def test_missing_values_never_render_as_the_bottom_bin(mini_geojson):
    text, _, result = _svg(mini_geojson)
    paths = {p.get("data-id"): p.get("fill") for p in _features(text)}
    ramp = set(colors.colors("YlGnBu", 3))
    assert result.dropped, "the fixture carries both sentinels"
    for item in result.dropped:
        assert paths[item["id"]] == f"url(#{colors.HATCH_ID})"
        assert paths[item["id"]] not in ramp


def test_holes_and_multipolygons_become_multiple_subpaths(mini_geojson):
    text, _, _ = _svg(mini_geojson)
    d = {p.get("data-id"): p.get("d") for p in _features(text)}
    assert d["AAA"].count("M") == 2   # outer ring plus a hole
    assert d["CCC"].count("M") == 2   # mainland plus an island
    assert d["BBB"].count("M") == 1
    assert 'fill-rule="evenodd"' in text


def test_footnote_states_everything_required(mini_geojson):
    text, _, _ = _svg(mini_geojson)
    footnote = " ".join(
        e.text for e in ET.fromstring(text).findall(f".//{SVG}text") if e.text
    )
    assert "Natural Earth" in footnote                  # source
    assert f"1:{registry.scale_for('admin_0')}" in footnote   # scale
    assert "Lambert azimuthal equal-area" in footnote   # projection
    assert "proportional circles" in footnote           # GDP_MD is a count
    assert "2015" in footnote and "2019" in footnote     # variable vintage
    assert "2 of 5 features excluded" in footnote        # dropped count


def test_footnote_states_the_classification_of_a_choropleth(mini_geojson):
    text, _, _ = _svg(mini_geojson, variable_id="POP_RANK")
    footnote = " ".join(
        e.text for e in ET.fromstring(text).findall(f".//{SVG}text") if e.text)
    assert "quantile" in footnote and "k=" in footnote


def test_year_range_is_shown_when_features_disagree(mini_geojson):
    text, _, _ = _svg(mini_geojson)
    assert "2015–2019" in text


def test_nominal_with_a_qualitative_ramp_renders(mini_geojson):
    text, _, _ = _svg(mini_geojson, variable_id="SUBREGION")
    fills = {p.get("data-id"): p.get("fill") for p in _features(text)}
    # Three distinct subregions in the fixture, three distinct colours.
    assert len(set(fills.values())) == 3


def test_more_categories_than_any_qualitative_ramp_holds_is_an_error():
    """Cycling colours would give two distinct categories the same fill."""
    from mapsvc.models import HarvestResult
    manifest = build_manifest(variable_id="ECONOMY")
    box = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    rows = [{"id": f"X{i:02d}", "geometry": box, "value": f"{i}. Category {i}"}
            for i in range(1, 13)]  # 12 categories, YlGnBu tops out at 9
    with pytest.raises(RenderError) as excinfo:
        render(manifest, HarvestResult(rows=rows, provenance={}, dropped=[]))
    # There is no ramp field to blame any more: the ramp is derived, so the
    # variable is what cannot be drawn.
    assert excinfo.value.field == "variable.id"


def test_aspect_ratio_is_preserved(mini_geojson):
    """One scale factor: a square in the middle of the map stays square."""
    square = {"type": "Feature",
              "properties": {"ADM0_A3": "SQR", "GDP_MD": 15, "GDP_YEAR": 2019,
                             "POP_EST": 1, "POP_YEAR": 2019, "SUBREGION": "West Test",
                             "INCOME_GRP": "5. Low income", "ECONOMY": "6. Developing region",
                             "POP_RANK": 1, "CONTINENT": "Testland", "NAME": "Square"},
              "geometry": {"type": "Polygon",
                           "coordinates": [[[14, 47], [16, 47], [16, 49], [14, 49], [14, 47]]]}}
    geojson = {"type": "FeatureCollection", "features": mini_geojson["features"] + [square]}
    text, _, _ = _svg(geojson)
    d = {p.get("data-id"): p.get("d") for p in _features(text)}
    import re
    nums = [float(n) for n in re.findall(r"-?\d+\.?\d*", d["SQR"])]
    xs, ys = nums[0::2], nums[1::2]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    # Under Mercator a 2x2 degree box at 48N is taller than wide by 1/cos(lat);
    # what matters is that the two axes share a scale, not that they are equal.
    assert 1.0 < height / width < 1.8


def test_same_manifest_twice_is_byte_identical(mini_geojson):
    first, _, _ = _svg(mini_geojson)
    second, _, _ = _svg(mini_geojson)
    assert first.encode() == second.encode()


def test_empty_region_is_an_error():
    from mapsvc.models import HarvestResult
    with pytest.raises(RenderError) as excinfo:
        render(build_manifest(), HarvestResult(rows=[], provenance={}, dropped=[]))
    assert excinfo.value.field == "region"


def test_a_window_with_one_feature_still_renders():
    """region can be a single ADM0_A3 code, which leaves one distinct value.

    ColorBrewer ramps start at three classes; k clamps below that.
    """
    from mapsvc.models import HarvestResult
    box = {"type": "Polygon", "coordinates": [[[0, 40], [8, 40], [8, 48], [0, 48], [0, 40]]]}
    result = HarvestResult(rows=[{"id": "FRA", "geometry": box, "value": 2716000.0}],
                           provenance={"variable": "GDP_MD", "unit": "million USD",
                                       "measurement": "ratio"},
                           dropped=[])
    text = render(build_manifest(), result)
    paths = _features(text)
    assert len(paths) == 1
    assert paths[0].get("fill").startswith("#")


def test_two_distinct_values_render_as_two_bins():
    from mapsvc.models import HarvestResult
    box = {"type": "Polygon", "coordinates": [[[0, 40], [8, 40], [8, 48], [0, 48], [0, 40]]]}
    rows = [{"id": "AAA", "geometry": box, "value": 1.0},
            {"id": "BBB", "geometry": box, "value": 2.0}]
    text = render(build_manifest(),
                  HarvestResult(rows=rows, provenance=RATIO, dropped=[]))
    fills = {p.get("fill") for p in _features(text)}
    assert len(fills) == 2


def test_footnote_reports_a_collapsed_k_in_readable_english():
    from mapsvc.models import HarvestResult
    box = {"type": "Polygon", "coordinates": [[[0, 40], [8, 40], [8, 48], [0, 48], [0, 40]]]}
    one = render(build_manifest(),
                 HarvestResult(rows=[{"id": "FRA", "geometry": box, "value": 1.0}],
                               provenance=RATIO, dropped=[]))
    assert "1 distinct value)" in one
    two = render(build_manifest(), HarvestResult(
        rows=[{"id": "A", "geometry": box, "value": 1.0},
              {"id": "B", "geometry": box, "value": 2.0}], provenance=RATIO, dropped=[]))
    assert "2 distinct values)" in two
