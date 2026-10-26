"""Round 8: callouts capped and kept off other units, the ranked table,
legend keys on the page, context names off the inset band. Offline."""

import math
import re
import xml.etree.ElementTree as ET

from mapsvc import project, render as R
from mapsvc.models import HarvestResult
from mapsvc.render import render
from tests.conftest import build_manifest
from tests.test_feature_labels import _box, _cluster, _feature_groups
from tests.test_render_encoding import _change_rows, _grid, _ratio, _within

SVG = "{http://www.w3.org/2000/svg}"


def _leaders(root):
    return [tuple(float(l.get(k)) for k in ("x1", "y1", "x2", "y2"))
            for l in root.iter(f"{SVG}line") if l.get("class") == "leader"]


def _mosaic():
    """A 7 x 7 block of units, the middle one small: no free space within reach."""
    rows = []
    for i in range(7):
        for j in range(7):
            gid = f"M{i}{j}"
            if (i, j) == (3, 3):
                geometry = _box(i * 4 + 1.7, j * 4 + 1.7, i * 4 + 2.3, j * 4 + 2.3)
            else:
                geometry = _box(i * 4, j * 4, i * 4 + 4, j * 4 + 4)
            rows.append({"id": gid, "name": f"Mosaic {i}{j}", "geometry": geometry,
                         "value": 50.0 + (i * 7 + j) * 0.01 if (i, j) != (3, 3) else 52.0})
    return rows


def _render(rows, bbox):
    return render(build_manifest(bbox=bbox),
                  HarvestResult(rows=rows, provenance={"measurement": "ratio"}, dropped=[]))


def test_no_leader_is_longer_than_the_cap():
    for rows, bbox in ((_cluster(n=26, small=0.9), (-2, -2, 50, 42)),
                       (_cluster(), (-2, -2, 70, 42))):
        root = ET.fromstring(_render(rows, bbox))
        assert _leaders(root)
        for x1, y1, x2, y2 in _leaders(root):
            assert math.hypot(x2 - x1, y2 - y1) <= R.LEADER_LIMIT + 1e-6


def test_a_unit_that_only_a_leader_across_a_neighbour_could_reach_goes_unlabelled():
    root = ET.fromstring(_render(_mosaic(), (-1, -1, 30, 30)))
    record = root.find(".//*[@data-unlabelled]")
    assert "M33" in record.get("data-unlabelled").split()
    # ...and no leader runs over another unit's land.
    units = {p.get("data-id"): [tuple(map(float, q.split(","))) for q in
                                re.findall(r"-?[\d.]+,-?[\d.]+", p.get("d"))]
             for p in root.iter(f"{SVG}path") if p.get("data-id")}
    for x1, y1, x2, y2 in _leaders(root):
        for k in range(3, 20):
            x, y = x1 + (x2 - x1) * k / 20, y1 + (y2 - y1) * k / 20
            assert sum(project.point_in_ring(x, y, ring) for ring in units.values()) <= 1


def test_unlabelled_units_bring_a_ranked_highest_and_lowest_table():
    root = ET.fromstring(_render(_mosaic(), (-1, -1, 30, 30)))
    tables = [g for g in root.iter(f"{SVG}g") if g.get("class") == "ranked-table"]
    assert len(tables) == 1
    texts = [t.text for t in tables[0].iter(f"{SVG}text")]
    assert "Highest" in texts and "Lowest" in texts
    # Five rows a side, values beside the names, the top one first.
    assert "Mosaic 66" in texts and "Mosaic 00" in texts
    assert texts.index("Mosaic 66") < texts.index("Mosaic 65")


def test_a_map_that_names_everything_has_no_table():
    root = ET.fromstring(_render(_cluster(n=3), (-2, -2, 70, 42)))
    assert not [g for g in root.iter(f"{SVG}g") if g.get("class") == "ranked-table"]


def test_a_signed_ranked_table_names_the_ends_of_the_scale():
    rows = _mosaic()
    for i, r in enumerate(rows):
        r["value"] = (i - 24) / 2.0
    root = ET.fromstring(render(
        build_manifest(bbox=(-1, -1, 30, 30)),
        HarvestResult(rows=rows, provenance={
            "measurement": "ratio", "diverging": {
                "negative": "Democratic lead", "positive": "Republican lead"}},
            dropped=[])))
    texts = [t.text for g in root.iter(f"{SVG}g") if g.get("class") == "ranked-table"
             for t in g.iter(f"{SVG}text")]
    assert "Republican lead" in texts and "Democratic lead" in texts
    assert "Highest" not in texts


def test_a_wide_graded_legend_keeps_every_key_on_the_page():
    values = [-30.0, -18.0, -11.0, -6.0, -2.0, 3.0, 12.0, 30.0, 70.0, 140.0, 260.0, 360.0]
    root = ET.fromstring(render(
        build_manifest(bbox=(-180, -90, 180, 90)),
        HarvestResult(rows=_change_rows(values), provenance=_ratio(unit="% change"),
                      dropped=[], context=[],
                      )))
    for t in root.iter(f"{SVG}text"):
        x = float(t.get("x") or 0)
        width = R._text_w("".join(t.itertext()), float(t.get("font-size") or 10))
        anchor = t.get("text-anchor", "start")
        right = x + (width if anchor == "start" else width / 2 if anchor == "middle" else 0)
        assert right <= R.CANVAS_W, "".join(t.itertext())


def test_the_western_sahara_note_needs_a_variable_on_morocco_or_the_sahara():
    note = R._SAHARA
    vintage = f"v5.1.1; {note}"
    with_data = HarvestResult(rows=[{"id": "MAR"}], provenance={}, dropped=[])
    elsewhere = HarvestResult(rows=[{"id": "FRA"}], provenance={}, dropped=[],
                              context=[{"id": "MAR"}])
    assert note in R._without_idle_notes(vintage, with_data, True)
    assert note not in R._without_idle_notes(vintage, with_data, False)   # no variable
    assert note not in R._without_idle_notes(vintage, elsewhere, True)    # MAR is grey context


def test_the_map_proper_ends_where_the_inset_band_begins():
    """Context names are kept above `core_bottom`: an inset band under the
    map grows the frame, but not the map (EGYPT beside the Canaries)."""
    core = _grid("C", -120, 30, 6, 3)
    far = [(f"F{k}", _box(-170 + k * 3, 10 + (k % 3) * 20, -168 + k * 3, 12 + (k % 3) * 20),
            2.0 + k) for k in range(8)]
    rows = [{"id": i, "name": f"Far {i}" if i.startswith("F") else None, "geometry": g,
             "value": v} for i, g, v in core + far]
    root = ET.fromstring(render(_within(), HarvestResult(rows=rows, provenance=_ratio(),
                                                         dropped=[])))
    band_top = min(float(g.find(f"{SVG}rect").get("y"))
                   for g in root.iter(f"{SVG}g") if g.get("data-inset"))
    frame_h = float(root.find(f".//{SVG}clipPath/{SVG}rect").get("height"))
    assert R.MAP_TOP + frame_h > band_top, "the band is inside the frame's page height"
    manifest = _within()
    frame = R.Frame(manifest, [r["geometry"] for r in rows[:18]])
    core_bottom = frame.core_bottom
    frame.map_h += 150
    assert frame.core_bottom == core_bottom and frame.bottom == core_bottom + 150
