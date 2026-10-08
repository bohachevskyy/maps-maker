"""What the renderer derives: encoding, projection, framing, layout, titles.

All offline, on synthetic geometry. Nothing here is in the manifest; every
assertion is about a choice the renderer made from the data and the window.
"""

import math
import re
import xml.etree.ElementTree as ET

from mapsvc import colors, render as R
from mapsvc.models import HarvestResult
from mapsvc.render import render
from tests.conftest import build_manifest, build_result

SVG = "{http://www.w3.org/2000/svg}"
WORLD = (-180.0, -90.0, 180.0, 90.0)


def _box(x0, y0, x1, y1):
    return {"type": "Polygon",
            "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def _rows(*specs):
    return [{"id": gid, "geometry": geom, "value": value} for gid, geom, value in specs]


def _features(root):
    return {p.get("data-id"): p for p in root.findall(f".//{SVG}path") if p.get("data-id")}


def _circles(root):
    return [c for c in root.findall(f".//{SVG}circle")
            if c.get("data-id") and c.get("class") != "small-unit"]


def _texts(root):
    return [t.text for t in root.findall(f".//{SVG}text") if t.text]


def _ratio(**extra):
    return {"measurement": "ratio", **extra}


# --------------------------------------------------------------- counts ---

def test_a_count_is_drawn_as_circles_over_neutral_polygons(mini_geojson):
    manifest = build_manifest()            # GDP_MD: a count
    result = build_result(mini_geojson, manifest)
    root = ET.fromstring(render(manifest, result))

    circles = {c.get("data-id"): c for c in _circles(root)}
    assert set(circles) == {r["id"] for r in result.rows}
    for row in result.rows:
        assert circles[row["id"]].get("data-value") == R._raw_value(row["value"])

    paths = _features(root)
    for row in result.rows:
        assert paths[row["id"]].get("fill") == colors.COUNT_LAND
        assert paths[row["id"]].get("data-value") is not None
    for item in result.dropped:
        # Missing: hatched, and no circle -- never a zero-sized one.
        assert paths[item["id"]].get("fill") == f"url(#{colors.HATCH_ID})"
        assert item["id"] not in circles


def test_circle_area_not_radius_is_proportional_to_value():
    rows = _rows(("A", _box(0, 40, 4, 44), 100.0), ("B", _box(6, 40, 10, 44), 400.0))
    root = ET.fromstring(render(build_manifest(bbox=(-2, 38, 12, 46)),
                                HarvestResult(rows=rows, provenance={"measurement": "count"}, dropped=[])))
    r = {c.get("data-id"): float(c.get("r")) for c in _circles(root)}
    assert math.isclose((r["B"] / r["A"]) ** 2, 4.0, rel_tol=0.02)


def test_largest_circle_is_drawn_first_so_small_ones_stay_visible():
    rows = _rows(("A", _box(0, 40, 4, 44), 5.0), ("B", _box(6, 40, 10, 44), 500.0),
                 ("C", _box(12, 40, 16, 44), 50.0))
    root = ET.fromstring(render(build_manifest(bbox=(-2, 38, 18, 46)),
                                HarvestResult(rows=rows, provenance={"measurement": "count"}, dropped=[])))
    assert [c.get("data-id") for c in _circles(root)] == ["B", "C", "A"]


def test_a_circle_sits_inside_its_unit_even_when_the_unit_is_a_c():
    """The centroid of a C is in its mouth; the circle must not be."""
    c_shape = {"type": "Polygon", "coordinates": [[
        [0, 40], [10, 40], [10, 42], [2, 42], [2, 48], [10, 48], [10, 50], [0, 50], [0, 40]]]}
    rows = _rows(("C", c_shape, 10.0))
    text = render(build_manifest(bbox=(-1, 39, 11, 51)),
                  HarvestResult(rows=rows, provenance={"measurement": "count"}, dropped=[]))
    root = ET.fromstring(text)
    circle = _circles(root)[0]
    cx = float(circle.get("cx"))
    d = _features(root)["C"].get("d")
    pts = [tuple(map(float, p.split(","))) for p in re.findall(r"-?[\d.]+,-?[\d.]+", d)]
    left = min(x for x, _ in pts)
    right = max(x for x, _ in pts)
    # The spine is the left fifth of the C.
    assert cx < left + (right - left) * 0.25


def test_the_size_legend_shows_nested_reference_circles(mini_geojson):
    manifest = build_manifest()
    root = ET.fromstring(render(manifest, build_result(mini_geojson, manifest)))
    refs = [c for c in root.findall(f".//{SVG}circle") if not c.get("data-id")]
    assert 2 <= len(refs) <= 3
    bottoms = {round(float(c.get("cy")) + float(c.get("r")), 2) for c in refs}
    assert len(bottoms) == 1, "nested circles share a baseline"


def test_size_legend_values_stay_inside_the_data_range():
    """Ukraine's oblasts run 0.89M-4.06M: 4M/2M/1M, not 2M/500k/50k."""
    assert R._reference_values(4_059_372, 890_457) == [4e6, 2e6, 1e6]
    # A range wider than 25x still shows its spread.
    assert R._reference_values(1.4e9, 1e4) == [1e9, 2e8, 4e7]


def test_a_ratio_keeps_the_choropleth():
    rows = _rows(("A", _box(0, 40, 4, 44), 1.0), ("B", _box(6, 40, 10, 44), 2.0),
                 ("C", _box(12, 40, 16, 44), 3.0))
    root = ET.fromstring(render(build_manifest(bbox=(-2, 38, 18, 46)),
                                HarvestResult(rows=rows, provenance=_ratio(), dropped=[])))
    assert not _circles(root)
    assert len({p.get("fill") for p in _features(root).values()}) == 3


# ------------------------------------------------------------ diverging ---

def _change_rows(values):
    return _rows(*[(f"U{i:02d}", _box(i * 2, 40, i * 2 + 1.5, 44), v)
                   for i, v in enumerate(values)])


def test_a_share_gets_a_0_100_legend_with_percent_labels():
    values = [4.8, 11.0, 17.0, 26.0, 33.0, 47.0, 56.0, 71.0, 84.0, 99.0]
    text = render(build_manifest(bbox=(-1, 38, 21, 46)),
                  HarvestResult(rows=_change_rows(values), provenance=_ratio(unit="%"),
                                dropped=[]))
    texts = _texts(ET.fromstring(text))
    assert "0–20%" in texts and "80–100%" in texts
    assert "fixed 0–100 scale" in text


def test_a_skewed_ratio_gets_round_log_breaks_and_open_ends():
    values = [1066.0, 1800.0, 3100.0, 6000.0, 9000.0, 14000.0, 26000.0, 42000.0,
              77000.0, 139592.0]
    text = render(build_manifest(bbox=(-1, 38, 21, 46)),
                  HarvestResult(rows=_change_rows(values),
                                provenance=_ratio(unit="international-$ in 2021 prices"),
                                dropped=[]))
    texts = _texts(ET.fromstring(text))
    # Floating panel ("< $2k") or band under the map ("$2k" on a tick).
    assert {"< $2k", "≥ $100k"} <= set(texts) or {"$2k", "$100k"} <= set(texts)
    assert "1,066" not in texts and "139,592" not in texts
    assert "rounded log breaks" in text


def test_values_straddling_zero_get_a_diverging_ramp_centred_on_zero():
    values = [-25.0, -12.0, -6.0, -2.0, 3.0, 8.0, 15.0, 40.0, 90.0, 180.0]
    text = render(build_manifest(bbox=(-1, 38, 21, 46)),
                  HarvestResult(rows=_change_rows(values), provenance=_ratio(), dropped=[]))
    root = ET.fromstring(text)
    fills = {k: p.get("fill") for k, p in _features(root).items()}
    rdbu = set().union(*(colors.colors("RdBu", k) for k in range(3, 12)))
    assert set(fills.values()) <= rdbu
    reds = {f for i, f in enumerate(fills[f"U{i:02d}"] for i in range(10)) if values[i] < 0}
    blues = {f for i, f in enumerate(fills[f"U{i:02d}"] for i in range(10)) if values[i] > 0}
    assert not reds & blues, "decline and growth must never share a colour"
    assert "either side of zero" in text
    assert "0" in _texts(root), "zero is a labelled class edge"


def test_a_lone_negative_outlier_does_not_trigger_a_diverging_ramp():
    values = [-1.0] + [float(v) for v in range(1, 20)]
    text = render(build_manifest(bbox=(-1, 38, 41, 46)),
                  HarvestResult(rows=_change_rows(values), provenance=_ratio(), dropped=[]))
    assert "diverging" not in text


def test_diverging_colours_meet_at_the_light_centre():
    below, above = colors.diverging("RdBu", 2, 3), colors.colors("RdBu", 6)
    assert below == ["#ef8a62", "#f6c4ad", "#b7d7e8", "#67a9cf", "#2166ac"]
    assert below[1:] == above[2:]
    reversed_asymmetric = colors.diverging("RdBu", 2, 4, reverse=True)
    assert reversed_asymmetric[:2] == ["#92c5de", "#b7d7e8"]
    assert reversed_asymmetric[2:] == ["#f6c4ad", "#f4a582", "#d6604d", "#b2182b"]


# ------------------------------------------------------- small features ---

def test_a_unit_too_small_to_see_gets_a_marker_in_its_class_colour():
    rows = _rows(("BIG", _box(-60, -30, 60, 30), 1.0),
                 ("BIG2", _box(70, -30, 150, 30), 3.0),
                 ("LUX", _box(6.0, 49.5, 6.2, 49.7), 2.0))
    root = ET.fromstring(render(build_manifest(bbox=WORLD),
                                HarvestResult(rows=rows, provenance=_ratio(), dropped=[])))
    markers = [c for c in root.findall(f".//{SVG}circle") if c.get("class") == "small-unit"]
    assert [m.get("data-id") for m in markers] == ["LUX"]
    assert markers[0].get("fill") == _features(root)["LUX"].get("fill")
    assert markers[0].get("data-value") == "2"
    assert markers[0].get("stroke") is None
    assert _features(root)["LUX"].get("stroke") == "none"


def test_diverging_subtitle_names_decline_and_growth_colours():
    values = [-25.0, -12.0, -6.0, -2.0, 3.0, 8.0, 15.0, 40.0, 90.0, 180.0]
    text = render(build_manifest(bbox=(-1, 38, 21, 46)),
                  HarvestResult(rows=_change_rows(values), provenance=_ratio(unit="% change"),
                                dropped=[]))
    assert "red = decline; blue = growth" in text


def test_election_margin_puts_republican_leads_in_red_and_names_both_signs():
    values = [-40.0, -20.0, -10.0, -3.0, 2.0, 8.0, 15.0, 30.0, 45.0, 60.0]
    direction = {"negative": "Democratic lead", "positive": "Republican lead",
                 "negative_color": "blue", "positive_color": "red"}
    text = render(build_manifest(bbox=(-1, 38, 21, 46)), HarvestResult(
        rows=_change_rows(values), provenance=_ratio(diverging=direction), dropped=[]))
    root = ET.fromstring(text)
    fills = {key: path.get("fill") for key, path in _features(root).items()}
    assert fills["U00"].lower().startswith(("#05", "#21", "#43", "#67", "#92", "#b7"))
    assert fills["U09"].lower().startswith(("#67", "#b2", "#d6", "#ef", "#f4", "#f6"))
    assert "blue = Democratic lead; red = Republican lead" in text


# --------------------------------------------------------- texts, frame ---

def test_title_and_legend_use_the_human_label_never_the_slug():
    rows = _rows(("A", _box(0, 40, 4, 44), 1.0), ("B", _box(6, 40, 10, 44), 2.0))
    manifest = build_manifest(variable_source="owid", variable_id="gdp-per-capita-worldbank")
    prov = _ratio(variable="GDP per capita", unit="international-$ in 2021 prices",
                  year="2024")
    text = render(manifest, HarvestResult(rows=rows, provenance=prov, dropped=[]))
    root = ET.fromstring(text)
    texts = _texts(root)
    assert "GDP per capita, 2024" in texts
    assert "GDP per capita" in texts                       # legend heading
    assert "international-$ in 2021 prices" in texts       # legend unit
    assert not any("gdp-per-capita-worldbank" in t for t in texts)
    assert root.get("aria-label") == "GDP per capita, 2024"


def test_a_world_map_uses_equal_earth_with_an_outline_and_a_graticule(mini_geojson):
    manifest = build_manifest(bbox=WORLD, variable_id="POP_RANK")
    text = render(manifest, build_result(mini_geojson, manifest))
    root = ET.fromstring(text)
    assert "Projection: Equal Earth" in text
    unlabelled = [p for p in root.findall(f".//{SVG}path") if not p.get("data-id")]
    assert any(p.get("fill") == colors.WATER for p in unlabelled)        # the sphere
    assert any(p.get("stroke") == colors.GRATICULE for p in unlabelled)  # 30° lines
    assert any(p.get("stroke") == colors.FRAME for p in unlabelled)      # its outline


def test_a_world_legend_sits_below_the_map_not_on_it(mini_geojson):
    manifest = build_manifest(bbox=WORLD, variable_id="POP_RANK")
    text = render(manifest, build_result(mini_geojson, manifest))
    root = ET.fromstring(text)
    frame = R.Frame(manifest, [f["geometry"] for f in mini_geojson["features"]])
    heading = next(t for t in root.findall(f".//{SVG}text") if t.text == "POP_RANK")
    assert float(heading.get("y")) > frame.bottom


def test_the_page_height_follows_the_map_shape(mini_geojson):
    wide = build_manifest(bbox=WORLD, variable_id="POP_RANK")
    tall = build_manifest(bbox=(0, 30, 10, 70), variable_id="POP_RANK")
    h_wide = float(ET.fromstring(render(wide, build_result(mini_geojson, wide))).get("height"))
    h_tall = float(ET.fromstring(render(tall, build_result(mini_geojson, tall))).get("height"))
    assert h_wide < h_tall


def test_a_bbox_frames_the_window_not_the_features_that_overlap_it():
    """Algeria overlapping a map of Europe must not widen it to the Sahara."""
    inside = ("IN", _box(2, 44, 6, 48), 1.0)
    huge = ("BIG", _box(-10, 10, 40, 41), 2.0)       # reaches far south of the box
    manifest = build_manifest(bbox=(0, 40, 10, 50))
    alone = render(manifest, HarvestResult(rows=_rows(inside), provenance=_ratio(),
                                           dropped=[]))
    both = render(manifest, HarvestResult(rows=_rows(inside, huge), provenance=_ratio(),
                                          dropped=[]))
    d_alone = _features(ET.fromstring(alone))["IN"].get("d")
    d_both = _features(ET.fromstring(both))["IN"].get("d")
    assert d_alone == d_both
    assert 'clip-path="url(#map-clip)"' in both


def test_within_frames_the_units_and_ignores_far_flung_parts():
    """France framed by its mainland, not stretched to French Guiana."""
    france = {"type": "MultiPolygon", "coordinates": [
        [[[-5, 42], [8, 42], [8, 51], [-5, 51], [-5, 42]]],
        [[[-54, 2], [-51, 2], [-51, 5], [-54, 5], [-54, 2]]],
    ]}
    rows = _rows(("FRA", france, 1.0))
    manifest = build_manifest(bbox=None, within=("FRA",))
    root = ET.fromstring(render(manifest, HarvestResult(rows=rows, provenance=_ratio(),
                                                        dropped=[])))
    frame = R.Frame(manifest, [france])
    mainland_w = frame.xy(8, 46.5)[0] - frame.xy(-5, 46.5)[0]
    assert mainland_w > 0.5 * R.MAP_W
    assert root.find(f".//{SVG}clipPath/{SVG}rect") is not None


def test_a_floating_legend_avoids_the_corner_with_land():
    """Land fills the west of the window; the legend must go east."""
    rows = _rows(("W", _box(0, 40, 5, 50), 1.0), ("W2", _box(5, 40, 9, 50), 2.0),
                 ("E", _box(16, 44, 17, 46), 3.0))
    text = render(build_manifest(bbox=(0, 40, 20, 50)),
                  HarvestResult(rows=rows, provenance=_ratio(), dropped=[]))
    root = ET.fromstring(text)
    box = next(r for r in root.findall(f".//{SVG}rect") if r.get("fill-opacity") == "0.94")
    assert float(box.get("x")) > R.CANVAS_W / 2


# ---------------------------------------------------------------- texts ---

def _footnote(root):
    return " ".join(_texts(root))


def test_drop_reasons_are_stated_in_words_with_the_units_named():
    box = _box(0, 40, 4, 44)
    prov = _ratio(variable="present population", unit="people", year="1 January 2022",
                  measurement="count",
                  drop_reasons={"not_estimated": "not estimated by Ukrstat since 2014 "
                                                 "(temporarily occupied)"})
    result = HarvestResult(
        rows=_rows(("UA-30", box, 2952301.0)), provenance=prov,
        dropped=[{"id": "UA-43", "name": "Crimea", "reason": "not_estimated",
                  "geometry": _box(6, 40, 10, 44)},
                 {"id": "UA-40", "name": "Sevastopol", "reason": "not_estimated",
                  "geometry": _box(6, 38, 7, 39)}])
    text = _footnote(ET.fromstring(render(build_manifest(bbox=(-2, 36, 12, 46)), result)))
    assert "not estimated by Ukrstat since 2014" in text
    assert "— Crimea, Sevastopol" in text
    assert "not_estimated" not in text


def test_title_and_footnote_of_a_per_capita_map_state_the_divisor():
    box = _box(0, 40, 4, 44)
    prov = _ratio(variable="Distillate fuel oil total consumption per person",
                  unit="barrels per person per year", year="2024", years_drawn=[2024],
                  per={"source": "eia", "source_name": "EIA SEDS", "id": "TPOPP",
                       "variable": "Resident population including Armed Forces",
                       "phrase": "person", "year": "2023"})
    root = ET.fromstring(render(build_manifest(bbox=(-2, 38, 6, 46)),
                                HarvestResult(rows=_rows(("US-TX", box, 6.2)),
                                              provenance=prov, dropped=[])))
    texts = _texts(root)
    title = root.get("aria-label")
    assert title == "Distillate fuel oil total consumption per person, 2024"
    assert "divided by resident population including Armed Forces (EIA SEDS, TPOPP, 2023)" \
        in " ".join(texts)


def test_a_change_names_both_years_and_its_formula():
    box = _box(0, 40, 4, 44)
    prov = _ratio(variable="change in population", unit="% change 2000–2023",
                  year="2000–2023",
                  change={"from": 2000, "to": 2023, "formula": "(2023 − 2000) / 2000 × 100"})
    root = ET.fromstring(render(build_manifest(bbox=(-2, 38, 6, 46)),
                                HarvestResult(rows=_rows(("AAA", box, 12.5)),
                                              provenance=prov, dropped=[])))
    assert root.get("aria-label") == "Change in population, 2000–2023"
    assert "(2023 − 2000) / 2000 × 100" in _footnote(root)


def test_title_is_concept_place_period_without_filler():
    box = _box(0, 40, 4, 44)
    one = _ratio(variable="median household income", year="2020–2024 (ACS 5-year)",
                 years_drawn=[2024], place="California")
    root = ET.fromstring(render(build_manifest(bbox=(-2, 38, 6, 46)),
                                HarvestResult(rows=_rows(("A", box, 1.0)),
                                              provenance=one, dropped=[])))
    assert root.get("aria-label") == \
        "Median household income, California, 2020–2024 (ACS 5-year)"
    many = _ratio(variable="share", year="2022–2025", years_drawn=[2022, 2024, 2025],
                  place="Europe", year_mode=2025,
                  year_exceptions={"2022": ["Ukraine"], "2024": ["Albania", "Iceland"]})
    root = ET.fromstring(render(build_manifest(bbox=(-2, 38, 6, 46)),
                                HarvestResult(rows=_rows(("A", box, 1.0)),
                                              provenance=many, dropped=[])))
    assert root.get("aria-label") == "Share, Europe, 2025"
    assert "latest data" not in " ".join(_texts(root))
    assert "latest year 2025 (Albania, Iceland 2024; Ukraine 2022)" in " ".join(_texts(root))


def test_a_query_string_id_never_reaches_the_page():
    box = _box(0, 40, 4, 44)
    manifest = build_manifest(bbox=(-2, 38, 6, 46), variable_source="eurostat",
                              variable_id="nama_10r_2gdp?freq=A&unit=PPS_EU27_2020_HAB")
    text = render(manifest, HarvestResult(rows=_rows(("A", box, 1.0)),
                                          provenance=_ratio(), dropped=[]))
    assert "?freq" not in text and "&amp;unit" not in text


def test_the_world_outline_does_not_touch_the_page_gutter(mini_geojson):
    """Its widest point is one equator vertex; at a 1 px margin it read as cut."""
    manifest = build_manifest(bbox=WORLD, variable_id="POP_RANK")
    frame = R.Frame(manifest, [f["geometry"] for f in mini_geojson["features"]])
    xs = [x for x, _ in frame.outline]
    assert min(xs) >= R.PAD + 8 and max(xs) <= R.PAD + R.MAP_W - 8


def test_sub_pixel_islets_are_dropped_but_a_unit_keeps_its_largest_part():
    islet = [[[20.0 + i * 0.01, 40.0], [20.001 + i * 0.01, 40.0],
              [20.001 + i * 0.01, 40.001], [20.0 + i * 0.01, 40.0]] for i in range(50)]
    main = [[[0.0, 40.0], [4.0, 40.0], [4.0, 44.0], [0.0, 44.0], [0.0, 40.0]]]
    country = {"type": "MultiPolygon", "coordinates": [main] + [[r] for r in islet]}
    speck = {"type": "Polygon", "coordinates": islet[:1]}
    root = ET.fromstring(render(build_manifest(bbox=(-2, 38, 22, 46)), HarvestResult(
        rows=_rows(("BIG", country, 1.0), ("TINY", speck, 2.0)),
        provenance=_ratio(), dropped=[])))
    paths = _features(root)
    assert paths["BIG"].get("d").count("M") == 1, "50 invisible islets not written"
    # A unit that is all speck still shows: as a marker in its class colour.
    markers = [c for c in root.iter(f"{SVG}circle") if c.get("class") == "small-unit"]
    assert [c.get("data-id") for c in markers] == ["TINY"]


def test_an_enclave_is_outlined_named_and_its_host_symbol_kept_off_it():
    """Kyiv city inside Kyiv oblast: two circles, the city's on the city."""
    host = {"type": "Polygon", "coordinates": [
        [[0, 40], [10, 40], [10, 50], [0, 50], [0, 40]],
        [[4, 44], [6, 44], [6, 46], [4, 46], [4, 44]]]}          # the hole
    city = {"type": "Polygon", "coordinates": [[[4, 44], [6, 44], [6, 46], [4, 46], [4, 44]]]}
    rows = [{"id": "UA-32", "name": "Kyiv Oblast", "geometry": host, "value": 1_800_000.0},
            {"id": "UA-30", "name": "Kyiv", "geometry": city, "value": 2_952_301.0}]
    root = ET.fromstring(render(build_manifest(bbox=(-1, 39, 11, 51)), HarvestResult(
        rows=rows, provenance={"measurement": "count"}, dropped=[])))
    enclaves = [p for p in root.iter(f"{SVG}path") if p.get("class") == "enclave"]
    assert [p.get("data-id") for p in enclaves] == ["UA-30"]
    assert "Kyiv" in _texts(root)
    circles = {c.get("data-id"): c for c in _circles(root)}
    path = _features(root)["UA-30"].get("d")
    pts = [tuple(map(float, p.split(","))) for p in re.findall(r"-?[\d.]+,-?[\d.]+", path)]
    x0, x1 = min(x for x, _ in pts), max(x for x, _ in pts)
    y0, y1 = min(y for _, y in pts), max(y for _, y in pts)
    def on_city(c):
        return x0 <= float(c.get("cx")) <= x1 and y0 <= float(c.get("cy")) <= y1
    assert on_city(circles["UA-30"])
    assert not on_city(circles["UA-32"]), "the oblast's circle avoids its hole"


# ---------------------------------------------------------------- insets ---

def _grid(prefix, lon0, lat0, nx, ny, size=4.0):
    return [(f"{prefix}{i}{j}", _box(lon0 + i * size, lat0 + j * size,
                                     lon0 + (i + 1) * size, lat0 + (j + 1) * size), 1.0 + i + j)
            for i in range(nx) for j in range(ny)]


def _within(**kw):
    from mapsvc.models import Manifest
    return Manifest(bbox=None, within=("USA",), level="admin_1", variable_source="eia",
                    variable_id="X", basemap_source="census", **kw)


def test_far_flung_units_go_to_insets_and_the_frame_fits_the_rest():
    """A conterminous block, an 'Alaska' to the north-west and a 'Hawaii' far
    south-west: both go to boxed insets, captioned, and the footnote says so."""
    core = _grid("C", -120, 30, 6, 3)                       # 24 x 12 degrees
    alaska = ("AK", _box(-160, 60, -148, 68), 5.0)            # ~19% of the core, far
    hawaii = ("HI", _box(-160, 19, -155, 22), 2.0)            # small, far
    rows = [{"id": i, "name": n, "geometry": g, "value": v}
            for (i, g, v), n in zip(core + [alaska, hawaii],
                                    [None] * len(core) + ["Alaska", "Hawaii"])]
    root = ET.fromstring(render(_within(), HarvestResult(rows=rows, provenance=_ratio(),
                                                         dropped=[])))
    insets = [g.get("data-inset") for g in root.iter(f"{SVG}g") if g.get("data-inset")]
    assert insets == ["Alaska", "Hawaii"]
    for group in (g for g in root.iter(f"{SVG}g") if g.get("data-inset")):
        box = group.find(f"{SVG}rect")
        assert float(box.get("y")) >= R.MAP_TOP
        assert float(box.get("y")) + float(box.get("height")) <= R.MAP_TOP + R.MAP_H_MAX
    assert len([g for g in root.iter(f"{SVG}g")
                if g.get("class") == "inset-scale-bar"]) == 2
    assert "Alaska at" in _footnote(root) and "Hawaii at" in _footnote(root)
    # Every unit is still drawn: nothing silently vanishes.
    assert set(_features(root)) == {r["id"] for r in rows}


def test_a_nearby_island_stays_in_the_main_frame():
    """Cyprus-like: separate, but inside the box the core already spans."""
    core = _grid("C", 0, 40, 6, 4)
    island = ("CY", _box(20, 41, 22, 42), 3.0)
    rows = [{"id": i, "geometry": g, "value": v} for i, g, v in core + [island]]
    root = ET.fromstring(render(_within(), HarvestResult(rows=rows, provenance=_ratio(),
                                                         dropped=[])))
    assert not [g for g in root.iter(f"{SVG}g") if g.get("data-inset")]


def test_a_bbox_map_never_moves_units_to_insets():
    core = _grid("C", -120, 30, 6, 3)
    far = ("HI", _box(-160, 19, -155, 22), 2.0)
    rows = [{"id": i, "geometry": g, "value": v} for i, g, v in core + [far]]
    root = ET.fromstring(render(build_manifest(bbox=(-170, 10, -90, 50)),
                                HarvestResult(rows=rows, provenance=_ratio(), dropped=[])))
    assert not [g for g in root.iter(f"{SVG}g") if g.get("data-inset")]
