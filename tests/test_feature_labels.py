"""Feature labels and circle separation, on synthetic geometry. Offline."""

import math
import xml.etree.ElementTree as ET

from mapsvc import project, render as R
from mapsvc.models import HarvestResult
from mapsvc.render import render
from tests.conftest import build_manifest

SVG = "{http://www.w3.org/2000/svg}"


def _box(x0, y0, x1, y1, holes=()):
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return {"type": "Polygon", "coordinates": [ring, *holes]}


def _grid(n, value=lambda i: 10.0 + i, size=4.0):
    side = math.ceil(math.sqrt(n))
    return [{"id": f"U{i:03d}", "name": f"Unit {i}",
             "geometry": _box((i % side) * size, (i // side) * size,
                              (i % side) * size + size * 0.95, (i // side) * size + size * 0.95),
             "value": value(i)} for i in range(n)]


def _label_boxes(root):
    """Approximate page boxes of every feature label, as render sizes them."""
    out = []
    for group in root.iter(f"{SVG}g"):
        if group.get("class") != "feature-label":
            continue
        texts = group.findall(f"{SVG}text")
        widths = [R._text_w(t.text, float(t.get("font-size"))) for t in texts]
        x = float(texts[0].get("x"))
        top = float(texts[0].get("y")) - float(texts[0].get("font-size")) * 0.86
        bottom = float(texts[-1].get("y")) + 1
        w = max(widths)
        out.append((group.get("data-id"), (x - w / 2, top, x + w / 2, bottom)))
    return out


def _render(rows, measurement="ratio", bbox=None):
    xs = [p[0] for r in rows for p in r["geometry"]["coordinates"][0]]
    ys = [p[1] for r in rows for p in r["geometry"]["coordinates"][0]]
    manifest = build_manifest(bbox=bbox or (min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1))
    result = HarvestResult(rows=rows, provenance={"measurement": measurement}, dropped=[])
    return render(manifest, result)


def test_a_small_choropleth_names_its_units_without_overlap():
    text = _render(_grid(16))
    root = ET.fromstring(text)
    boxes = _label_boxes(root)
    labelled = {gid for gid, _ in boxes}
    assert len(labelled) >= 12, "most of 16 roomy units should carry a label"
    names = {t.text for t in root.iter(f"{SVG}text")}
    assert "Unit 0" in names
    for i, (_, a) in enumerate(boxes):
        for _, b in boxes[:i]:
            assert not (a[0] < b[2] - 0.5 and b[0] < a[2] - 0.5
                        and a[1] < b[3] - 0.5 and b[1] < a[3] - 0.5)


def test_a_world_sized_map_gets_no_feature_labels():
    text = _render(_grid(R.LABEL_MAX_UNITS + 1))
    assert 'class="feature-label"' not in text


def test_labels_are_deterministic():
    rows = _grid(20)
    assert _render(rows) == _render(list(reversed(rows)))


def test_a_kind_word_is_dropped_only_while_names_stay_unique():
    rows = [{"id": "A", "name": "Lviv Oblast"}, {"id": "B", "name": "Kyiv Oblast"},
            {"id": "C", "name": "Kyiv"}]
    names = R._label_names(rows)
    assert names == {"A": "Lviv", "B": "Kyiv Oblast", "C": "Kyiv"}


def test_a_us_state_code_comes_from_its_iso_id():
    assert R._code({"id": "US-WY"}) == "WY"
    assert R._code({"id": "UA-30"}) is None
    assert R._code({"id": "DEU"}) is None


def test_a_circle_stacked_on_an_enclave_moves_inside_its_own_unit():
    """Kyiv oblast surrounds Kyiv city; their circles must not read as one."""
    hole = [[4.0, 4.0], [4.0, 6.0], [6.0, 6.0], [6.0, 4.0], [4.0, 4.0]]
    rows = [
        {"id": "OBL", "name": "Ring Oblast", "geometry": _box(0, 0, 10, 10, [hole]),
         "value": 1_800_000},
        {"id": "CTY", "name": "Ring City", "geometry": _box(4, 4, 6, 6), "value": 2_950_000},
    ]
    root = ET.fromstring(_render(rows, measurement="count", bbox=(-1, -1, 11, 11)))
    circles = {c.get("data-id"): c for c in root.iter(f"{SVG}circle") if c.get("data-id")}
    (x1, y1, r1), (x2, y2, r2) = [(float(circles[k].get("cx")), float(circles[k].get("cy")),
                                   float(circles[k].get("r"))) for k in ("OBL", "CTY")]
    depth = r1 + r2 - math.hypot(x1 - x2, y1 - y2)
    assert depth <= R.SEPARATE_OVERLAP * min(r1, r2) + 1e-6
    # The oblast's circle stays on the oblast, outside the city.
    oblast = {p.get("data-id"): p for p in root.iter(f"{SVG}path")}["OBL"]
    rings = [[tuple(map(float, pt.split(","))) for pt in sub.rstrip("Z").split("L")]
             for sub in oblast.get("d").lstrip("M").split("M")]
    outer, inner = sorted(rings, key=project.planar_area, reverse=True)[:2]
    assert project.point_in_ring(x1, y1, outer) and not project.point_in_ring(x1, y1, inner)


def test_value_text_is_short_and_carries_the_unit():
    enc = R.Encoding("choropleth", "x", "2024 US dollars")
    assert R._value_text(164281, enc) == "$164k"
    enc = R.Encoding("choropleth", "x", "%")
    assert R._value_text(99.4, enc) == "99%"
    enc = R.Encoding("symbols", "x", "people")
    assert R._value_text(2952301, enc) == "2.95M"
    assert R._value_text("North", enc) == ""


class _PageFrame:
    """Geometry already in page pixels: the identity projection."""
    windowed = False
    is_world = False
    lon0 = 0.0

    def xy(self, lon, lat):
        return lon, lat

    def visible(self, x, y):
        return True


def test_separate_moves_the_roomier_unit_off_a_stacked_pair():
    hole = [[45.0, 45.0], [45.0, 55.0], [55.0, 55.0], [55.0, 45.0], [45.0, 45.0]]
    oblast = {"id": "OBL", "geometry": _box(0, 0, 100, 100, [hole]), "value": 1.8}
    city = {"id": "CTY", "geometry": _box(45, 45, 55, 55), "value": 2.95}
    placed = [(-2.95, "CTY", (50.0, 50.0), 30.0, city),
              (-1.8, "OBL", (50.0, 75.0), 25.0, oblast)]
    out = R._separate(placed, _PageFrame())
    assert out[0][2] == (50.0, 50.0), "the enclave keeps its circle"
    (x, y), r = out[1][2], out[1][3]
    assert (x, y) != (50.0, 75.0)
    assert 55.0 - math.hypot(x - 50.0, y - 50.0) <= R.SEPARATE_OVERLAP * 25.0
    assert 0 <= x <= 100 and 0 <= y <= 100
    assert not (45 <= x <= 55 and 45 <= y <= 55)
