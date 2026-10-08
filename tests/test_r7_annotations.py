"""Round-7 annotation rules, on small synthetic inputs. Offline."""

import types

from mapsvc import overlay_render, render as R
from mapsvc.models import HarvestResult
from mapsvc.overlays import web
from mapsvc.statistics import elections


def _enc(unit="Persons per square kilometre", mode="choropleth", scale=1.0):
    enc = R.Encoding(mode, "Density", unit, value_scale=scale)
    return enc


def _square(x, y, side):
    ring = [[x, y], [x + side, y], [x + side, y + side], [x, y + side], [x, y]]
    return {"type": "Polygon", "coordinates": [ring]}


def test_density_map_names_the_populous_dense_units_not_the_densest_n():
    # 100 units; the five densest are tiny, the big dense ones hold the people.
    rows, units = [], []
    for i in range(100):
        side = 0.1 if i >= 95 else 1.0
        value = 1000.0 + i if i >= 85 else 50.0 + i
        row = {"id": f"U{i:03d}", "geometry": _square(i * 2.0, 50.0, side), "value": value}
        rows.append(row)
        units.append((row, None, None))
    valued = sorted((r["value"], r["id"]) for r in rows)
    picked = R._populous_dense(units, valued, _enc(), 4)
    assert len(picked) == 4
    assert picked <= {f"U{i:03d}" for i in range(85, 95)}      # large and dense
    assert not picked & {f"U{i:03d}" for i in range(95, 100)}  # small, though densest


def test_populous_dense_ignores_what_is_not_per_area():
    rows = [{"id": f"U{i}", "geometry": _square(i, 0, 1), "value": float(i)}
            for i in range(80)]
    units = [(r, None, None) for r in rows]
    valued = sorted((r["value"], r["id"]) for r in rows)
    assert R._populous_dense(units, valued, _enc(unit="percent"), 4) == set()


def test_aggregate_note_needs_a_prefix_every_drawn_id_shares():
    prov = {"aggregates": {"48": {"label": "Texas statewide", "value": 4.07}},
            "unit": "%"}
    rows = [{"id": "48201"}, {"id": "48113"}]
    assert R._aggregate_note(rows, prov, _enc(unit="%")) == "Texas statewide 4.1%"
    assert R._aggregate_note(rows + [{"id": "35001"}], prov, _enc(unit="%")) == ""


def test_aggregate_note_of_a_change_is_signed():
    prov = {"aggregates": {"CA-": {"label": "Canada overall", "value": 5.24}},
            "change": {"from": 2016, "to": 2021}, "unit": "% change 2016–2021"}
    rows = [{"id": "CA-ON"}, {"id": "CA-QC"}]
    assert R._aggregate_note(rows, prov, _enc(unit="% change")) == "Canada overall +5.2%"


def test_index_callouts_use_the_legend_unit_with_the_source_value_in_brackets():
    enc = _enc(unit="index (EU27 = 100)", scale=100.0 / 40000.0)
    assert R._value_text(107000.0, enc) == "268 (107k)"


def test_outside_keys_name_neighbours_and_antarctica():
    prov = {"context_source": "Natural Earth", "plain_land": ["Antarctica"]}
    result = HarvestResult(rows=[], provenance=prov, dropped=[], context=[{"id": "ATA"}])
    assert R._outside_keys(result)[0][1] == "Outside the set, or uninhabited"
    result.provenance = {"plain_land": ["Antarctica", "Heard I."]}
    assert R._outside_keys(result)[0][1] == "Uninhabited (Antarctica)"
    result.context = []
    assert R._outside_keys(result) == []


def test_diverging_end_captions_name_the_candidates():
    direction = {"negative": "Democratic lead", "positive": "Republican lead",
                 "negative_candidate": "Harris", "positive_candidate": "Trump"}
    assert R._side_caption(direction, "negative") == "Harris (D) ahead"
    assert R._side_caption(direction, "positive") == "Trump (R) ahead"
    assert R._side_caption({"negative": "Decline", "positive": "Growth"}, "positive") == "Growth"


def test_nominees_are_the_party_names_with_most_votes(monkeypatch):
    series = {2024: {
        "TX": {"valid": 1.0, "candidates": {"REPUBLICAN": {"TRUMP, DONALD J.": 9.0},
                                            "DEMOCRAT": {"HARRIS, KAMALA D.": 5.0}}},
        "CA": {"valid": 1.0, "candidates": {"REPUBLICAN": {"TRUMP, DONALD J.": 4.0, "WRITE-IN": 1.0},
                                            "DEMOCRAT": {"HARRIS, KAMALA D.": 8.0}}}}}
    monkeypatch.setattr(elections, "_series", lambda: series)
    assert elections.nominees(2024) == {"REPUBLICAN": "Trump", "DEMOCRAT": "Harris"}


_HEADER = ("SID,SEASON,NUMBER,BASIN,NAME,ISO_TIME,NATURE,LAT,LON,WMO_WIND,TRACK_TYPE,"
           "LANDFALL,USA_AGENCY,USA_RECORD,USA_WIND,USA_SSHS")


def _storm(rows):
    text = "\n".join([_HEADER, " ,Year, , , , , , , , , , , , , ,1"] + rows) + "\n"
    spec = web.parse(f"{web._IBTRACS}ibtracs.NA.list.v04r01.csv#season=2024")
    from mapsvc.overlays import Window
    window = Window(bbox=None, within=None, extent=(-180, -90, 180, 90))
    return web.layer_from_bytes(text.encode(), spec, window)


def test_landfalls_are_the_nhc_landfall_records_not_distance_to_land(monkeypatch):
    monkeypatch.setattr(web, "_landfall_region", lambda lon, lat: "Grenada")
    rows = [
        "X1,2024,1,NA,BERYL,2024-07-01 12:00:00,TS,12.0,-60.6,110,main,149,hurdat_atl, ,115,4",
        "X1,2024,1,NA,BERYL,2024-07-01 15:20:00,TS,12.5,-61.5,115,main,201,hurdat_atl,L,120,4",
        "X1,2024,1,NA,BERYL,2024-07-01 18:00:00,TS,12.8,-62.3,115,main,235,hurdat_atl, ,115,4",
    ]
    layer = _storm(rows)
    assert layer["features"][0]["landfalls"] == [
        [-61.5, 12.5, 4, "2024-07-01", 120.0, "Grenada"]]


def test_major_hurricane_landfalls_get_date_category_and_place_callouts():
    layer = {"kind": "lines", "along": True, "category": {"classes": [
        {"value": "cat4", "label": "Category 4"}]},
        "features": [{"id": "A", "name": "Helene", "peak": 4, "peak_wind": 120.0,
                      "peak_at": [-84.0, 29.0], "category": "cat4",
                      "coords": [[-86.0, 26.0], [-83.7, 29.9]], "classes": ["cat4"],
                      "landfalls": [[-83.7, 29.9, 4, "2024-09-27", 120.0, "Florida"]]}]}

    class Frame:
        lon0, map_h = -84.0, 400
        def xy(self, lon, lat):
            return 400 + (lon + 84) * 40, 200 - (lat - 28) * 40
        def visible(self, x, y):
            return True
    labels = overlay_render.Labels({"pad": 0, "map_w": 800, "map_top": 0}, Frame())
    overlay_render._tracks(layer, Frame(), labels, [])
    text = labels.svg()
    assert "Helene landfall, 27 Sep" in text and "Cat 4, 120 kt" in text and "Florida" in text


def test_a_station_with_one_line_but_a_town_name_outranks_a_plain_interchange():
    stations = [{"name": "Tokyo", "lines": 1, "rail": False, "wikidata": False},
                {"name": "Kasuga", "lines": 2, "rail": False, "wikidata": False},
                {"name": "Shinjuku", "lines": 2, "rail": False, "wikidata": True},
                {"name": "Shinjuku-sanchome", "lines": 3, "rail": False, "wikidata": True}]
    standing = overlay_render._station_standing(stations, [{"name": "Tokyo", "pop": 1}])
    assert standing["Tokyo"] > standing["Kasuga"]
    assert standing["Shinjuku"] > standing["Kasuga"]          # namesake of another station
    assert standing["Tokyo"] >= overlay_render.INTERCHANGE_STANDING


def test_western_sahara_note_only_where_it_is_drawn():
    vintage = "v5.1.1; Western Sahara shown as a non-self-governing territory (UN)"
    europe = HarvestResult(rows=[{"id": "FRA"}], provenance={}, dropped=[])
    assert R._without_idle_notes(vintage, europe) == "v5.1.1"
    world = HarvestResult(rows=[{"id": "ESH"}], provenance={}, dropped=[])
    assert R._without_idle_notes(vintage, world) == vintage


def test_repeated_credit_is_not_written_as_source_as_above():
    layer = {"kind": "places", "title": "Towns", "provenance": {"attribution": "Minard"},
             "features": [1, 2], "encoding": "Labelled points"}
    credited = {"Minard"}
    line = overlay_render._footer_line(layer, credited)
    assert "as above" not in line and line.startswith("Also drawn: Towns")
