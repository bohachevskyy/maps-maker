"""SVG generation.

This module must not import anything that performs I/O -- no network, no
filesystem, no cache. It takes a validated manifest and an already-harvested
result and returns a string. `tests/test_no_io.py` enforces that by inspection.

Output is deterministic: rows are drawn in sorted id order, coordinates are
formatted to a fixed precision, and nothing carries a timestamp. The same
manifest twice must produce byte-identical bytes.

Nothing about the drawing is in the manifest. The projection follows from the
window, the encoding from the variable's measurement level (counts become
proportional circles, ratios a choropleth, ratios straddling zero a diverging
one), and the layout from the shape of the projected map.
"""

import dataclasses
import math
import textwrap
from xml.sax.saxutils import escape, quoteattr

from mapsvc import classify, colors, overlay_render, project, registry, statistics
from mapsvc.manifest import Manifest
from mapsvc.models import HarvestResult

CANVAS_W = 960
PAD = 24                     # page gutter, left and right
MAP_W = CANVAS_W - 2 * PAD
MAP_TOP = 76                 # below the title block
# The map's height follows its projected aspect ratio, within these bounds, so
# a wide world map does not sit between two empty bands and a tall one does not
# run off the page.
MAP_H_MIN, MAP_H_MAX = 300, 700
COORD_DP = 2
WORLD = (-180.0, -90.0, 180.0, 90.0)

FOOTER_SIZE, FOOTER_LEAD = 10.5, 15
# Characters that fit across the footer at FOOTER_SIZE in the chosen font.
FOOTER_CHARS = 160

SWATCH_W, SWATCH_H, SWATCH_GAP = 20, 13, 4
LEGEND_INSET = 12
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

# A polygon smaller than this on the page (px^2) cannot be seen, let alone have
# its colour read; it gets a marker in its class colour instead.
MIN_VISIBLE_AREA = 4.0
MARKER_R = 2.6

# The largest proportional circle, as a share of sqrt(map area).
SYMBOL_SCALE = 0.05
SYMBOL_MIN_R = 1.0
# A unit with less of its area than this inside the window gets no circle.
SYMBOL_MIN_SHARE = 0.10

_SCOPE = {"admin_0": "country", "admin_1": "first-level subdivision",
          "admin_2": "second-level subdivision", "admin_3": "third-level subdivision"}
_REASONS = {"no_data": "no value reported", "no_join": "absent from the statistics source"}


class RenderError(ValueError):
    """Raised when the manifest cannot be honoured for this data."""

    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


# ----------------------------------------------------------------- frame ---

class Frame:
    """The projected map and where it sits on the page.

    `windowed` maps were asked for by bbox (or cover the globe): the window
    itself is the frame, drawn as a projected outline, and everything is
    clipped to it. Otherwise the units define the frame, and only the page
    rectangle clips -- which is what keeps French Guiana from dragging a map of
    Europe across the Atlantic.
    """

    def __init__(self, manifest: Manifest, geometries: list,
                 centre_lon: float | None = None):
        self.extent = tuple(manifest.bbox or project.extent_of(geometries))
        self.is_world = self.extent == WORLD
        if self.is_world and centre_lon:
            # A world map centred where its overlay is: the same globe, the
            # seam moved. Rings crossing it are split in _project.
            self.extent = (centre_lon - 180.0, -90.0, centre_lon + 180.0, 90.0)
        self.windowed = manifest.bbox is not None or self.is_world
        self.projection = project.choose(self.extent)
        self.projector = project.make(self.projection, self.extent)
        self.lon0 = (self.extent[0] + self.extent[2]) / 2.0
        self.lat0 = (self.extent[1] + self.extent[3]) / 2.0

        if self.windowed:
            outline = [self.projector(lon, lat)
                       for lon, lat in project.window_outline(self.extent)]
            xs, ys = [p[0] for p in outline], [p[1] for p in outline]
            bounds = (min(xs), min(ys), max(xs), max(ys))
            margin = 1.0
        else:
            outline = None
            bounds = project.viewport_bounds(geometries, self.projector, self.extent)
            margin = 10.0

        span_x = max(bounds[2] - bounds[0], 1e-9)
        span_y = max(bounds[3] - bounds[1], 1e-9)
        ideal = (MAP_W - 2 * margin) * span_y / span_x + 2 * margin
        self.map_h = int(min(MAP_H_MAX, max(MAP_H_MIN, round(ideal))))
        local = project.fit(bounds, MAP_W, self.map_h, margin)
        self.transform = lambda x, y: _shift(local(x, y))
        self.outline = (None if outline is None
                        else [self.transform(x, y) for x, y in outline])

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return self.transform(*self.projector(lon, lat))

    @property
    def bottom(self) -> float:
        return MAP_TOP + self.map_h

    def visible(self, x: float, y: float) -> bool:
        if not (PAD <= x <= PAD + MAP_W and MAP_TOP <= y <= self.bottom):
            return False
        return self.outline is None or project.point_in_ring(x, y, self.outline)


def _shift(point: tuple[float, float]) -> tuple[float, float]:
    return point[0] + PAD, point[1] + MAP_TOP


# ---------------------------------------------------------------- render ---

def render(manifest: Manifest, result: HarvestResult) -> str:
    if manifest.bbox is None and result.provenance.get("overlay_bbox"):
        # Framed on its overlays (no window was given): the harvest chose it.
        manifest = dataclasses.replace(
            manifest, bbox=tuple(result.provenance["overlay_bbox"]))
    rows = sorted(result.rows, key=lambda r: r["id"])
    dropped = sorted(result.dropped, key=lambda r: r["id"])
    show_missing = registry.DEFAULT_MISSING != "exclude"
    drawn_missing = [d for d in dropped if d.get("geometry")] if show_missing else []

    geometries = [r["geometry"] for r in rows] + [d["geometry"] for d in drawn_missing]
    if not geometries:
        raise RenderError("no features to draw for this region", "region")

    framed = (overlay_render.focus(geometries, result.overlays)
              if result.overlays and manifest.bbox is None else geometries)
    frame = Frame(manifest, framed, overlay_render.centre_lon(result.overlays))
    p = result.provenance
    encoding = _encoding(manifest, rows, p)

    body: list[str] = []
    land: list[tuple] = []          # (bbox, ring) of every drawn outer ring
    units: list[tuple] = []         # bbox of each unit's (or symbol's) ink
    if frame.windowed:
        body.append(_water(frame))

    # Feature layers carry the message when there is no variable: the base map
    # recedes to quiet land with white borders.
    has_overlays = bool(result.overlays or result.places)
    border = (overlay_render.MUTED_BORDER if has_overlays and encoding.mode == "basemap"
              else colors.BORDER)
    body.append('<g clip-path="url(#map-clip)" stroke-linejoin="round">')
    markers: list[str] = []
    for row in rows:
        fill = encoding.fill(row["value"])
        parts = _project(row["geometry"], frame)
        boxes = _outer_boxes(parts)
        land.extend(boxes)
        units.append(_union(boxes))
        body.append(_path(row, parts, fill, border))
        if encoding.mode == "choropleth" and _visible_area(parts) < MIN_VISIBLE_AREA:
            anchor = _anchor(row["geometry"], frame)
            if anchor:
                markers.append(_marker(row, anchor, fill))
                units.append((anchor[0] - MARKER_R, anchor[1] - MARKER_R,
                              anchor[0] + MARKER_R, anchor[1] + MARKER_R))
    for item in drawn_missing:
        parts = _project(item["geometry"], frame)
        boxes = _outer_boxes(parts)
        land.extend(boxes)
        units.append(_union(boxes))
        body.append(_path(item, parts, f"url(#{colors.HATCH_ID})"))
    body.append("</g>")
    if frame.outline is not None:
        body.append(f'<path d="{_d([frame.outline])}" fill="none" '
                    f'stroke="{colors.FRAME}" stroke-width="0.8"/>')
    body.extend(markers)

    if encoding.mode == "symbols":
        encoding.r_max = SYMBOL_SCALE * math.sqrt(MAP_W * frame.map_h)
        svg, circles = _symbols(rows, frame, encoding)
        body.append(svg)
        units.extend(circles)

    art = None
    if has_overlays:
        art = overlay_render.draw(result.overlays, result.places, frame, {
            "pad": PAD, "map_w": MAP_W, "map_top": MAP_TOP, "font": FONT})
        body.append('<g clip-path="url(#map-clip)" stroke-linejoin="round">'
                    + "".join(art.svg) + "</g>")
        body.append(f'<g font-family="{FONT}">' + "".join(art.labels) + "</g>")

    legend_band = 0.0
    if encoding.mode != "basemap":
        legend, legend_band = _legend(encoding, frame, land,
                                      [u for u in units if u], bool(drawn_missing))
        body.append(legend)
    if art is not None and art.legend_h:
        body.append(art.legend(PAD, frame.bottom + legend_band + 16))
        legend_band += art.legend_h + 16

    title, subtitle = _title(manifest, p), _subtitle(manifest, p, encoding)
    if art is not None and manifest.variable_id is None and art.title:
        title, subtitle = art.title, art.subtitle
    head = [
        f'<text x="{PAD}" y="36" font-size="21" font-weight="600" '
        f'fill="{colors.TEXT}">{escape(title)}</text>',
    ]
    if subtitle:
        head.append(f'<text x="{PAD}" y="58" font-size="12.5" '
                    f'fill="{colors.TEXT_MUTED}">{escape(subtitle)}</text>')
    body.append(f'<g font-family="{FONT}">' + "".join(head) + "</g>")

    footer_top = frame.bottom + legend_band + 14
    footer, footer_h = _footer(manifest, frame, encoding, result, footer_top,
                               art.footer if art is not None else ())
    body.append(footer)
    height = int(math.ceil(footer_top + footer_h + 14))

    # Declared explicitly so a saved .svg is self-describing about its encoding
    # (the footnote carries en dashes and a division sign).
    head_svg = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS_W}" '
        f'height="{height}" viewBox="0 0 {CANVAS_W} {height}" '
        f'role="img" aria-label={quoteattr(title)}>',
        _defs(frame),
        f'<rect width="{CANVAS_W}" height="{height}" fill="{colors.OCEAN}"/>',
    ]
    return "\n".join(head_svg + [b for b in body if b] + ["</svg>"]) + "\n"


# -------------------------------------------------------------- encoding ---

class Encoding:
    """How values become ink: one of basemap, choropleth or symbols."""

    def __init__(self, mode: str, label: str, unit: str,
                 scheme: classify.Classification | None = None,
                 swatches: list[str] | None = None, vmax: float = 0.0):
        self.mode = mode
        self.label = label
        self.unit = unit
        self.scheme = scheme
        self.swatches = swatches
        self.vmax = vmax
        self.r_max = 0.0

    def fill(self, value) -> str:
        if self.mode == "basemap":
            return colors.BASEMAP_FILL
        if self.mode == "symbols":
            return colors.COUNT_LAND
        return self.swatches[self.scheme.bin_of(value)]

    def radius(self, value: float) -> float:
        if self.vmax <= 0 or value <= 0:
            return 0.0
        return max(SYMBOL_MIN_R, self.r_max * math.sqrt(value / self.vmax))


def _measurement(manifest: Manifest, provenance: dict) -> str:
    """count | ratio | ordinal | nominal, preferably from the harvested data."""
    level = provenance.get("measurement")
    if level:
        return level
    return statistics.describe(manifest.variable_source, manifest.variable_id).level


def _encoding(manifest: Manifest, rows: list, provenance: dict) -> Encoding:
    """Classification, colours, or symbol sizing -- all derived, none chosen.

    None of this is in the manifest. The measurement level comes from the
    source, and the encoding follows from it -- so a nominal variable cannot be
    shaded light-to-dark and a population cannot be a raw-count choropleth,
    because nothing offers the choice.
    """
    label = _human(provenance.get("variable") or manifest.variable_id or "")
    unit = provenance.get("unit") or ""
    if manifest.variable_id is None:
        return Encoding("basemap", label, unit)
    if not rows:
        raise RenderError("every feature was dropped; nothing to classify", "variable")

    level = _measurement(manifest, provenance)
    values = [r["value"] for r in rows]
    numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool)
                  for v in values)

    if level == "count" and numeric:
        # A count shaded by value paints the largest *places* darkest: a big
        # empty unit outweighs a small crowded one. Circles sized by value do
        # not carry that bias.
        return Encoding("symbols", label, unit, vmax=max(float(v) for v in values))

    if level == "ratio" and numeric and classify.substantially_diverging(
            [float(v) for v in values]):
        scheme = classify.diverging([float(v) for v in values],
                                    registry.DEFAULT_METHOD, registry.DEFAULT_K)
        swatches = colors.diverging("RdBu", scheme.zero_at, scheme.k - scheme.zero_at)
        return Encoding("choropleth", label, unit, scheme, swatches)

    ramp = registry.RAMP_FOR_LEVEL.get(level, "YlGnBu")
    scheme = classify.build(values, level, registry.DEFAULT_METHOD, registry.DEFAULT_K)
    try:
        swatches = colors.colors(ramp, scheme.k)
    except colors.RampError as exc:
        # A nominal variable with more categories than any qualitative ramp
        # holds. There is nothing to reconfigure: the map cannot be drawn.
        raise RenderError(str(exc), "variable.id") from exc
    return Encoding("choropleth", label, unit, scheme, swatches)


def _human(text: str) -> str:
    """'population' -> 'Population'; leaves 'GDP' and 'iPhone' alone."""
    return text[:1].upper() + text[1:] if text else text


# -------------------------------------------------------------- geometry ---

def _num(v: float) -> str:
    s = f"{v:.{COORD_DP}f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0", "-") else s


def _raw_value(value) -> str:
    """Stable machine-readable value, with numeric noise capped at 6 digits."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format(value, ".6g")
    return str(value)


def _parts(geometry) -> list:
    """[[exterior, hole, ...], ...] for a Polygon or MultiPolygon."""
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    return list(geometry["coordinates"])


def _project(geometry, frame: Frame) -> list:
    """Every ring of every part, in page pixels. Holes survive."""
    if frame.is_world and frame.lon0 != 0:
        return [[[frame.xy(lon, lat) for lon, lat in piece]
                 for ring in part
                 for piece in project.split_at_seam(project.unwrap_ring(ring, frame.lon0),
                                                    frame.lon0)]
                for part in _parts(geometry)]
    return [[[frame.xy(lon, lat) for lon, lat in project.unwrap_ring(ring, frame.lon0)]
             for ring in part]
            for part in _parts(geometry)]


def _d(rings) -> str:
    subpaths: list[str] = []
    for ring in rings:
        points: list[str] = []
        previous = None
        for x, y in ring:
            point = (_num(x), _num(y))
            if point != previous:
                points.append(f"{point[0]},{point[1]}")
                previous = point
        if len(points) < 3:
            continue  # collapsed to a sliver at this scale
        subpaths.append("M" + "L".join(points) + "Z")
    return "".join(subpaths)


def _path(item: dict, parts, fill: str, stroke: str = colors.BORDER) -> str:
    d = _d([ring for part in parts for ring in part])
    if not d:
        return ""
    value = item.get("value")
    value_attr = ("" if value is None
                  else f" data-value={quoteattr(_raw_value(value))}")
    return (
        f'<path d="{d}" fill="{fill}" fill-rule="evenodd" '
        f'stroke="{stroke}" stroke-width="0.4" '
        f'data-id={quoteattr(str(item["id"]))}{value_attr}/>'
    )


def _outer_boxes(parts) -> list[tuple]:
    out = []
    for part in parts:
        ring = part[0]
        if len(ring) < 3:
            continue
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        out.append(((min(xs), min(ys), max(xs), max(ys)), ring))
    return out


def _union(boxes) -> tuple | None:
    if not boxes:
        return None
    return (min(b[0][0] for b in boxes), min(b[0][1] for b in boxes),
            max(b[0][2] for b in boxes), max(b[0][3] for b in boxes))


def _visible_area(parts) -> float:
    return sum(project.planar_area(part[0]) for part in parts if len(part[0]) >= 3)


def _anchor(geometry, frame: Frame) -> tuple[float, float] | None:
    """Where a unit's symbol goes: inside its largest *visible* piece.

    A unit cut by the window carries its symbol on the part that is shown --
    Russia's circle on a map of Europe sits west of the Urals, not in Siberia,
    where it would be clipped away.
    """
    placed = _placement(geometry, frame)
    return None if placed is None else placed[0]


def _placement(geometry, frame: Frame):
    """(anchor, share of the unit's area inside the window), or None."""
    best, best_area = None, 0.0
    total = visible = 0.0
    for part in _parts(geometry):
        ring = project.unwrap_ring(part[0], frame.lon0)
        if len(ring) < 3:
            continue
        clipped = ring
        if frame.windowed and not frame.is_world:
            total += project.planar_area([frame.xy(lon, lat) for lon, lat in ring])
            clipped = project.clip_to_extent(ring, frame.extent)
            if len(clipped) < 3:
                continue
        pts = [frame.xy(lon, lat) for lon, lat in clipped]
        area = project.planar_area(pts)
        visible += area
        if best is None or area > best_area:
            best, best_area = pts, area
    if best is None:
        return None
    precision = max(0.5, math.sqrt(best_area) / 50.0)
    x, y = project.pole_of_inaccessibility(best, precision)
    if not frame.visible(x, y):
        return None
    share = visible / total if total > 0 else 1.0
    return (x, y), share


def _marker(item: dict, anchor, fill: str) -> str:
    x, y = anchor
    return (f'<circle cx="{_num(x)}" cy="{_num(y)}" r="{MARKER_R}" fill="{fill}" '
            f'stroke="{colors.BORDER}" stroke-width="0.6" class="small-unit" '
            f'data-id={quoteattr(str(item["id"]))} '
            f'data-value={quoteattr(_raw_value(item["value"]))}/>')


def _symbols(rows: list, frame: Frame, encoding: Encoding) -> str:
    """Proportional circles, largest first so the small ones stay on top.

    Returns the SVG and each circle's bounding box, so the legend can avoid
    them.
    """
    placed = []
    for row in rows:
        r = encoding.radius(float(row["value"]))
        if r <= 0:
            continue
        where = _placement(row["geometry"], frame)
        # A unit barely inside the window would hang its whole total on a
        # sliver -- all of Kazakhstan's people on the edge of a map of Europe.
        if where is None or where[1] < SYMBOL_MIN_SHARE:
            continue
        placed.append((-float(row["value"]), str(row["id"]), where[0], r, row))
    placed.sort(key=lambda t: (t[0], t[1]))
    # Clipped like the polygons: a circle near the frame is cut by it, as the
    # land under it is, rather than spilling into the margin.
    out = [f'<g clip-path="url(#map-clip)" fill="{colors.SYMBOL_FILL}" fill-opacity="0.72" '
           f'stroke="{colors.SYMBOL_STROKE}" stroke-width="0.7">']
    for _, _, (x, y), r, row in placed:
        out.append(f'<circle cx="{_num(x)}" cy="{_num(y)}" r="{_num(r)}" '
                   f'data-id={quoteattr(str(row["id"]))} '
                   f'data-value={quoteattr(_raw_value(row["value"]))}/>')
    out.append("</g>")
    return "".join(out), [(x - r, y - r, x + r, y + r) for _, _, (x, y), r, _ in placed]


# ------------------------------------------------------------ background ---

def _defs(frame: Frame) -> str:
    if frame.outline is not None:
        clip = f'<path d="{_d([frame.outline])}"/>'
    else:
        clip = f'<rect x="{PAD}" y="{MAP_TOP}" width="{MAP_W}" height="{frame.map_h}"/>'
    return (
        "<defs>"
        f'<pattern id="{colors.HATCH_ID}" width="6" height="6" '
        'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        f'<rect width="6" height="6" fill="{colors.HATCH_BG}"/>'
        f'<line x1="0" y1="0" x2="0" y2="6" stroke="{colors.HATCH_STROKE}" stroke-width="2"/>'
        "</pattern>"
        f'<clipPath id="map-clip">{clip}</clipPath>'
        "</defs>"
    )


def _water(frame: Frame) -> str:
    """The window tinted as water, with a faint graticule behind the land."""
    interval = 30.0 if frame.is_world else project.graticule_interval(frame.extent)
    lines = []
    for line in project.graticule(frame.extent, interval):
        pts = [frame.xy(lon, lat) for lon, lat in line]
        lines.append("M" + "L".join(f"{_num(x)},{_num(y)}" for x, y in pts))
    return (
        f'<path d="{_d([frame.outline])}" fill="{colors.WATER}" stroke="none"/>'
        f'<path d="{"".join(lines)}" fill="none" stroke="{colors.GRATICULE}" '
        'stroke-width="0.5" clip-path="url(#map-clip)"/>'
    )


# ---------------------------------------------------------------- legend ---

def _text_w(text: str, size: float) -> float:
    """Rough advance width; there are no font metrics in a bare SVG."""
    return len(text) * size * 0.56


def _legend(encoding: Encoding, frame: Frame, land: list, units: list,
            has_missing: bool) -> tuple[str, float]:
    """The legend, and how much page height it adds below the map.

    On a regional map it floats wherever it hides no unit and the least land.
    On a world map -- and on any map where every spot would hide something --
    it sits in a band under the map instead, so it never covers a country.
    """
    if not frame.is_world:
        w, h, draw = _panel(encoding, has_missing)
        spot, hidden, covered = _least_covered(frame, land, units, w, h)
        if hidden == 0 and covered <= 0.02:
            return draw(*spot), 0.0
    h, draw = _band(encoding, has_missing)
    return draw(PAD, frame.bottom + 12), h + 12


def _least_covered(frame: Frame, land: list, units: list, w: float, h: float):
    """The candidate spot hiding the fewest units, then the least land.

    Land is sampled on a grid inside each candidate box rather than by counting
    vertices: a box in the middle of Russia covers no vertex but plenty of land.
    Counting hidden units as well keeps a small island state from vanishing
    under the legend while barely registering as land. Candidates -- corners,
    then edge midpoints, then points along the bottom and top edges -- are
    tried in a fixed order, so ties resolve the same way every time.
    """
    left, right = PAD + LEGEND_INSET, PAD + MAP_W - w - LEGEND_INSET
    top, bottom = MAP_TOP + LEGEND_INSET, frame.bottom - h - LEGEND_INSET
    middle = MAP_TOP + (frame.map_h - h) / 2.0
    candidates = [(left, bottom), (right, bottom), (left, top), (right, top),
                  (left, middle), (right, middle)]
    for i in (1, 2, 3):
        x = left + (right - left) * i / 4.0
        candidates += [(x, bottom), (x, top)]
    best, best_score = candidates[0], None
    for x, y in candidates:
        if y < MAP_TOP or x < PAD:
            continue
        hidden = sum(1 for u in units if _hidden(u, x, y, w, h))
        score = (hidden, round(_land_share(frame, land, x, y, w, h), 6))
        if best_score is None or score < best_score:
            best, best_score = (x, y), score
        if best_score == (0, 0.0):
            break
    if best_score is None:
        return best, 1, 1.0
    return best, best_score[0], best_score[1]


def _hidden(unit: tuple, x0: float, y0: float, w: float, h: float) -> bool:
    """Whether at least half of a unit's on-page extent lies under the box."""
    ux0, uy0 = max(unit[0], PAD), max(unit[1], MAP_TOP)
    ux1, uy1 = min(unit[2], PAD + MAP_W), unit[3]
    if ux1 < ux0 or uy1 < uy0:
        return False
    ix = max(0.0, min(ux1, x0 + w) - max(ux0, x0))
    iy = max(0.0, min(uy1, y0 + h) - max(uy0, y0))
    area = max(ux1 - ux0, 1e-6) * max(uy1 - uy0, 1e-6)
    if ux1 - ux0 < 1e-6 or uy1 - uy0 < 1e-6:
        cx, cy = (ux0 + ux1) / 2, (uy0 + uy1) / 2
        return x0 <= cx <= x0 + w and y0 <= cy <= y0 + h
    return ix * iy >= 0.5 * area


def _land_share(frame: Frame, land: list, x0: float, y0: float, w: float, h: float) -> float:
    step = 6.0
    near = [ring for (bx0, by0, bx1, by1), ring in land
            if bx1 >= x0 and bx0 <= x0 + w and by1 >= y0 and by0 <= y0 + h]
    total = hit = 0
    y = y0 + step / 2
    while y < y0 + h:
        x = x0 + step / 2
        while x < x0 + w:
            total += 1
            if near and frame.visible(x, y) and any(
                    project.point_in_ring(x, y, ring) for ring in near):
                hit += 1
            x += step
        y += step
    return hit / total if total else 0.0


def _heading(encoding: Encoding, x: float, y: float) -> list[str]:
    out = [f'<text x="{_num(x)}" y="{_num(y)}" font-size="12" font-weight="600" '
           f'fill="{colors.TEXT}">{escape(encoding.label)}</text>']
    if encoding.unit:
        out.append(f'<text x="{_num(x)}" y="{_num(y + 14)}" font-size="10.5" '
                   f'fill="{colors.TEXT_MUTED}">{escape(encoding.unit)}</text>')
    return out


def _heading_h(encoding: Encoding) -> float:
    return 14.0 + (14.0 if encoding.unit else 0.0)


def _entries(encoding: Encoding, has_missing: bool) -> list[tuple[str, str]]:
    entries = list(zip(encoding.swatches, encoding.scheme.labels()))
    if has_missing:
        entries.append((f"url(#{colors.HATCH_ID})", "no data"))
    return entries


def _hatch_swatch(x: float, y: float, w: float = SWATCH_W) -> str:
    return (f'<rect x="{_num(x)}" y="{_num(y)}" width="{_num(w)}" height="{SWATCH_H}" '
            f'fill="url(#{colors.HATCH_ID})" stroke="{colors.BORDER}" stroke-width="0.4"/>')


def _box(x, y, w, h) -> str:
    return (f'<rect x="{_num(x)}" y="{_num(y)}" width="{_num(w)}" height="{_num(h)}" '
            'fill="#ffffff" fill-opacity="0.94" stroke="#bfbfbf" stroke-width="0.7" rx="3"/>')


def _panel(encoding: Encoding, has_missing: bool):
    """A boxed legend floated over the map: (width, height, draw(x, y))."""
    head_h = _heading_h(encoding)
    min_w = max(20 + _text_w(encoding.label, 12), 20 + _text_w(encoding.unit, 10.5))
    if encoding.mode == "symbols":
        refs = _reference_values(encoding.vmax)
        big = encoding.radius(refs[0])
        labels = [_count_label(v) for v in refs]
        w = max(min_w, 20 + 2 * big + 10 + max(_text_w(s, 10.5) for s in labels))
        h = 12 + head_h + 8 + 2 * big + (22 if has_missing else 0) + 10

        def draw(x, y):
            out = [f'<g font-family="{FONT}">', _box(x, y, w, h)]
            out += _heading(encoding, x + 10, y + 20)
            base = y + 12 + head_h + 8 + 2 * big
            out += _nested_circles(encoding, refs, labels, x + 10 + big, base)
            if has_missing:
                out.append(_hatch_swatch(x + 10, base + 9))
                out.append(f'<text x="{_num(x + 10 + SWATCH_W + 8)}" y="{_num(base + 19.5)}" '
                           f'font-size="10.5" fill="#333333">no data</text>')
            out.append("</g>")
            return "".join(out)
        return w, h, draw

    entries = _entries(encoding, has_missing)
    w = max(min_w, 20 + SWATCH_W + 8 + max(_text_w(label, 11) for _, label in entries))
    h = 12 + head_h + len(entries) * (SWATCH_H + SWATCH_GAP) + 6

    def draw(x, y):
        out = [f'<g font-family="{FONT}">', _box(x, y, w, h)]
        out += _heading(encoding, x + 10, y + 20)
        top = y + 12 + head_h
        for i, (fill, label) in enumerate(entries):
            sy = top + i * (SWATCH_H + SWATCH_GAP)
            out.append(
                f'<rect x="{_num(x + 10)}" y="{_num(sy)}" width="{SWATCH_W}" '
                f'height="{SWATCH_H}" fill="{fill}" stroke="{colors.BORDER}" '
                'stroke-width="0.4"/>')
            out.append(
                f'<text x="{_num(x + 10 + SWATCH_W + 8)}" y="{_num(sy + SWATCH_H - 2.5)}" '
                f'font-size="11" fill="#333333">{escape(label)}</text>')
        out.append("</g>")
        return "".join(out)
    return w, h, draw


def _band(encoding: Encoding, has_missing: bool):
    """A legend laid out in a row under the map: (height, draw(x, y))."""
    head_h = _heading_h(encoding)
    if encoding.mode == "symbols":
        refs = _reference_values(encoding.vmax)
        big = encoding.radius(refs[0])
        labels = [_count_label(v) for v in refs]
        h = head_h + 10 + 2 * big

        def draw(x, y):
            out = [f'<g font-family="{FONT}">']
            out += _heading(encoding, x, y + 12)
            base = y + head_h + 10 + 2 * big
            out += _nested_circles(encoding, refs, labels, x + big, base)
            if has_missing:
                mx = x + 2 * big + 10 + max(_text_w(s, 10.5) for s in labels) + 36
                out.append(_hatch_swatch(mx, base - SWATCH_H))
                out.append(f'<text x="{_num(mx + SWATCH_W + 8)}" y="{_num(base - 2.5)}" '
                           f'font-size="10.5" fill="#333333">no data</text>')
            out.append("</g>")
            return "".join(out)
        return h, draw

    scheme = encoding.scheme
    if scheme.kind == "categorical":
        entries = _entries(encoding, has_missing)
        rows, line, used = [], [], 0.0
        for fill, label in entries:
            width = SWATCH_W + 6 + _text_w(label, 11) + 18
            if line and used + width > MAP_W:
                rows.append(line)
                line, used = [], 0.0
            line.append((fill, label, used))
            used += width
        rows.append(line)
        h = head_h + 8 + len(rows) * (SWATCH_H + 8)

        def draw(x, y):
            out = [f'<g font-family="{FONT}">']
            out += _heading(encoding, x, y + 12)
            for i, line in enumerate(rows):
                sy = y + head_h + 8 + i * (SWATCH_H + 8)
                for fill, label, offset in line:
                    out.append(f'<rect x="{_num(x + offset)}" y="{_num(sy)}" '
                               f'width="{SWATCH_W}" height="{SWATCH_H}" fill="{fill}" '
                               f'stroke="{colors.BORDER}" stroke-width="0.4"/>')
                    out.append(f'<text x="{_num(x + offset + SWATCH_W + 6)}" '
                               f'y="{_num(sy + SWATCH_H - 2.5)}" font-size="11" '
                               f'fill="#333333">{escape(label)}</text>')
            out.append("</g>")
            return "".join(out)
        return h, draw

    # Graded: a continuous strip of swatches with each break written under the
    # edge it marks -- the conventional classed-choropleth legend.
    k = scheme.k
    cell = min(84.0, (MAP_W - 160) / k)
    edges = [classify.format_value(b) for b in scheme.breaks]
    h = head_h + 8 + SWATCH_H + 16

    def draw(x, y):
        out = [f'<g font-family="{FONT}">']
        out += _heading(encoding, x, y + 12)
        sy = y + head_h + 8
        for i, fill in enumerate(encoding.swatches):
            out.append(f'<rect x="{_num(x + i * cell)}" y="{_num(sy)}" width="{_num(cell)}" '
                       f'height="{SWATCH_H}" fill="{fill}" stroke="#ffffff" stroke-width="0.8"/>')
        for i, label in enumerate(edges):
            if i and label == edges[i - 1]:
                continue  # a single-value class: its one value is already written
            tx = x + i * cell
            anchor = "start" if i == 0 else ("end" if i == k else "middle")
            weight = ' font-weight="600"' if scheme.zero_at == i else ""
            out.append(f'<line x1="{_num(tx)}" y1="{_num(sy + SWATCH_H)}" x2="{_num(tx)}" '
                       f'y2="{_num(sy + SWATCH_H + 3)}" stroke="#8c8c8c" stroke-width="0.6"/>')
            out.append(f'<text x="{_num(tx)}" y="{_num(sy + SWATCH_H + 14)}" '
                       f'font-size="10.5" text-anchor="{anchor}"{weight} '
                       f'fill="#333333">{escape(label)}</text>')
        if has_missing:
            mx = x + k * cell + 28
            out.append(_hatch_swatch(mx, sy, 30))
            out.append(f'<text x="{_num(mx + 38)}" y="{_num(sy + SWATCH_H - 2.5)}" '
                       f'font-size="10.5" fill="#333333">no data</text>')
        out.append("</g>")
        return "".join(out)
    return h, draw


def _nested_circles(encoding: Encoding, refs, labels, cx: float, base: float) -> list[str]:
    """Reference circles sharing a baseline, each labelled by a leader line."""
    big = encoding.radius(refs[0])
    lx = cx + big + 10
    out = []
    for value, label in zip(refs, labels):
        r = encoding.radius(value)
        top = base - 2 * r
        out.append(f'<circle cx="{_num(cx)}" cy="{_num(base - r)}" r="{_num(r)}" '
                   f'fill="{colors.SYMBOL_FILL}" fill-opacity="0.18" '
                   f'stroke="{colors.SYMBOL_FILL}" stroke-width="0.8"/>')
        out.append(f'<line x1="{_num(cx)}" y1="{_num(top)}" x2="{_num(lx - 3)}" '
                   f'y2="{_num(top)}" stroke="#8c8c8c" stroke-width="0.5" '
                   'stroke-dasharray="2,1.5"/>')
        out.append(f'<text x="{_num(lx)}" y="{_num(top + 3.5)}" font-size="10.5" '
                   f'fill="#333333">{escape(label)}</text>')
    return out


def _reference_values(vmax: float) -> list[float]:
    """Up to three round values for the size legend, none above vmax."""
    def nice_below(v: float) -> float:
        if v <= 0:
            return 0.0
        exp = math.floor(math.log10(v))
        for m in (5, 2, 1):
            candidate = m * 10.0 ** exp
            if candidate <= v * (1 + 1e-9):
                return candidate
        return 10.0 ** exp

    top = nice_below(vmax)
    out = [top]
    for share in (0.25, 0.04):
        v = nice_below(top * share)
        if 0 < v < out[-1]:
            out.append(v)
    return out


def _count_label(v: float) -> str:
    for size, word in ((1e9, "billion"), (1e6, "million")):
        if v >= size:
            return f"{v / size:.3g} {word}"
    return f"{v:,.0f}" if v >= 1 else f"{v:g}"


# ----------------------------------------------------------------- texts ---

def _title(manifest: Manifest, provenance: dict) -> str:
    """'GDP per capita, latest data (2022–2025)' -- from the variable, never the slug."""
    if manifest.variable_id is None:
        return _human(f"{_SCOPE.get(manifest.level, manifest.level)} boundaries")
    label = _human(provenance.get("variable") or manifest.variable_id)
    if provenance.get("normalize"):
        label += f" per {provenance['normalize']}"
    year = provenance.get("year")
    if not year:
        return label
    if "–" in str(year):
        # Each unit's most recent observation, and they disagree on the year.
        return f"{label}, latest data ({year})"
    return f"{label}, {year}"


def _subtitle(manifest: Manifest, provenance: dict, encoding: Encoding) -> str:
    if encoding.mode == "basemap":
        return ""
    head = f"By {_SCOPE.get(manifest.level, manifest.level)}"
    if encoding.mode == "symbols":
        head += "; circle area is proportional to the value"
    unit = provenance.get("unit")
    if unit == "%":
        unit = "percent"
    return f"{head} · {unit}" if unit else head


def _footer(manifest, frame: Frame, encoding: Encoding, result, top: float,
            extra: tuple | list = ()):
    """The mandatory footnote. Every rendered map states its own provenance."""
    p = result.provenance
    dropped = result.dropped

    scale = p.get("scale") or registry.scale_for(manifest.level)
    vintage = p.get("vintage") or registry.SOURCE_VINTAGE
    source = f"{p.get('source') or registry.SOURCE_NAME} {vintage}, {scale}"
    # ODbL requires attribution wherever the data is shown; the footnote is the
    # only place this map has to say it.
    license_note = p.get("license")
    if license_note and license_note != "public domain":
        source += f". {p.get('attribution') or ''} ({license_note})".rstrip()

    scheme = encoding.scheme
    if encoding.mode == "basemap":
        method = f"boundaries only, no variable ({len(result.rows)} units)"
    elif encoding.mode == "symbols":
        method = ("none; a count is drawn as proportional circles, area "
                  f"proportional to value (largest {_count_label(encoding.vmax)})")
    elif scheme.kind == "categorical":
        method = f"one class per category ({scheme.k} categories)"
    else:
        method = f"{registry.DEFAULT_METHOD}, k={scheme.k}"
        if scheme.zero_at is not None:
            method += (f", diverging at zero ({scheme.zero_at} below, "
                       f"{scheme.k - scheme.zero_at} above)")
        elif scheme.k != registry.DEFAULT_K:
            plural = "" if scheme.k == 1 else "s"
            method += f" ({scheme.k} distinct value{plural})"

    variable = p.get("variable") or manifest.variable_id
    if variable is None:
        variable = f"none -- {manifest.level} boundaries"
    elif p.get("unit"):
        variable += f" ({p['unit']})"
    if p.get("year"):
        variable += f", {p['year']}"
    if p.get("statistics_source") and p.get("statistics_source") != p.get("source"):
        # Boundaries and numbers came from different providers, possibly under
        # different licences. Credit the data as well as the map.
        credit = p.get("statistics_attribution") or p["statistics_source"]
        variable += f" [{credit}]"

    if dropped:
        reasons: dict[str, int] = {}
        for item in dropped:
            reason = item.get("reason", "unknown")
            reasons[reason] = reasons.get(reason, 0) + 1
        detail = ", ".join(f"{count} {_REASONS.get(reason, reason)}"
                           for reason, count in sorted(reasons.items()))
        excluded = (f"{len(dropped)} of {len(dropped) + len(result.rows)} features "
                    f"excluded ({detail}), shown hatched as no data")
    else:
        excluded = "0 features excluded"

    lines = [
        f"Source: {source}.",
        f"Variable: {variable}.",
        f"Projection: {_projection_label(frame)}. Classification: {method}. {excluded}.",
        *extra,
    ]

    # Attribution can make a line longer than the canvas. There is no text
    # measurement in a bare SVG, so wrap on a character budget tuned to the
    # font size; overflowing silently off the right edge is worse than a guess.
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(textwrap.wrap(line, width=FOOTER_CHARS) or [""])

    out = [f'<g font-family="{FONT}">',
           f'<line x1="{PAD}" y1="{_num(top)}" x2="{CANVAS_W - PAD}" y2="{_num(top)}" '
           'stroke="#d9d9d9" stroke-width="1"/>']
    for i, line in enumerate(wrapped):
        out.append(
            f'<text x="{PAD}" y="{_num(top + 18 + i * FOOTER_LEAD)}" '
            f'font-size="{FOOTER_SIZE}" fill="#595959">{escape(line)}</text>'
        )
    out.append("</g>")
    return "".join(out), 18 + (len(wrapped) - 1) * FOOTER_LEAD


def _projection_label(frame: Frame) -> str:
    if frame.is_world and frame.lon0 != 0:
        lon = frame.lon0
        return (f"{_PROJECTION_LABELS.get(frame.projection, frame.projection)}, "
                f"centred on {abs(lon):.0f}°{'E' if lon > 0 else 'W'}")
    if frame.projection == "laea":
        lat, lon = frame.lat0, frame.lon0
        return ("Lambert azimuthal equal-area, centred "
                f"{abs(lat):.0f}°{'N' if lat >= 0 else 'S'} "
                f"{abs(lon):.0f}°{'E' if lon >= 0 else 'W'}")
    return _PROJECTION_LABELS.get(frame.projection, frame.projection)


_PROJECTION_LABELS = {
    "albers": "Albers equal-area conic",
    "equal_earth": "Equal Earth",
    "mercator": "Mercator",
    "mollweide": "Mollweide equal-area",
}
