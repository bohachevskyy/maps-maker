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

from mapsvc import classify, colors, overlay_render, project, registry
from mapsvc.models import HarvestResult, Manifest

CANVAS_W = 960
PAD = 24                     # page gutter, left and right
MAP_W = CANVAS_W - 2 * PAD
MAP_TOP = 76                 # below the title block
# The map's height follows its projected aspect ratio, within these bounds, so
# a wide world map does not sit between two empty bands and a tall one does not
# run off the page.
MAP_H_MIN, MAP_H_MAX = 300, 700
COORD_DP = 2
# Douglas-Peucker tolerance in page pixels (the canvas is 960 px wide).
SIMPLIFY_PX = 0.3
# Rings smaller than this on the page (px^2) are not drawn, bar a unit's largest.
SPECK_PX2 = 0.25
WORLD = (-180.0, -90.0, 180.0, 90.0)
WORLD_MARGIN = 10.0

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
          "admin_2": "second-level subdivision", "admin_3": "third-level subdivision",
          "nuts_1": "NUTS 1 region", "nuts_2": "NUTS 2 region", "nuts_3": "NUTS 3 region"}
# Fallbacks for a result that predates provenance["drop_reasons"].
_REASONS = {"no_data": "no value reported by the source",
            "no_join": "no matching row in the statistics source"}
# Name the dropped units in the footnote when there are at most this many.
NAMED_DROPS = 4


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
            # The globe's outline is a curve whose widest point is a single
            # vertex at the equator: fitted with a 1 px margin it sits on the
            # page gutter and reads as cut off flat. A bbox window is a
            # rectangle-ish frame and can go to the edge.
            margin = WORLD_MARGIN if self.is_world else 1.0
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
        # Page pixels per projected unit. Every projection here is on the unit
        # sphere, so an inset drawn at this scale is at the same scale.
        self.scale = local(1.0, 0.0)[0] - local(0.0, 0.0)[0]
        self.insets: list = []

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
    # Far-flung units (Alaska, the Canaries) go to insets; the main frame is
    # fitted to the rest. Classification still sees every row.
    every_row = rows
    rows, drawn_missing, insets = _split_insets(manifest, rows, drawn_missing)

    geometries = [r["geometry"] for r in rows] + [d["geometry"] for d in drawn_missing]
    if not geometries:
        raise RenderError("no features to draw for this region", "region")

    framed = (overlay_render.focus(geometries, result.overlays)
              if result.overlays and manifest.bbox is None else geometries)
    frame = Frame(manifest, framed, overlay_render.centre_lon(result.overlays))
    p = result.provenance
    encoding = _encoding(manifest, every_row, p)

    body: list[str] = []
    land: list[tuple] = []          # (bbox, ring) of every drawn outer ring
    units: list[tuple] = []         # bbox of each unit's (or symbol's) ink
    if frame.windowed:
        body.append(_water(frame))
    body.append(_context(result.context, frame))

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
    body.append(_enclaves(rows, frame, encoding))
    inset_svg, inset_missing = _draw_insets(insets, frame, encoding, bool(result.context),
                                            land, units)
    body.append(inset_svg)

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
                                      [u for u in units if u],
                                      bool(drawn_missing) or inset_missing)
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


def _measurement(provenance: dict) -> str:
    """count | ratio | ordinal | nominal, of the quantity actually drawn.

    Harvest states it: it is the source's level for a raw variable, and ratio
    for a `per` or `change` result whatever the numerator was. Render cannot
    look it up itself -- that would be a catalogue read, and render does no I/O
    -- and must not guess, because the guess decides circles or choropleth.
    """
    level = provenance.get("measurement")
    if level not in ("count", "ratio", "ordinal", "nominal"):
        raise RenderError("the harvested data does not state its measurement level",
                          "variable.id")
    return level


def _encoding(manifest: Manifest, rows: list, provenance: dict) -> Encoding:
    """Classification, colours, or symbol sizing -- all derived, none chosen.

    None of this is in the manifest. The measurement level comes from the
    source, and the encoding follows from it -- so a nominal variable cannot be
    shaded light-to-dark and a population cannot be a raw-count choropleth,
    because nothing offers the choice.
    """
    label = _label(manifest, provenance)
    unit = provenance.get("unit") or ""
    if manifest.variable_id is None:
        return Encoding("basemap", label, unit)
    if not rows:
        raise RenderError("every feature was dropped; nothing to classify", "variable")

    level = _measurement(provenance)
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
    """Stable machine-readable value: exact for whole numbers, else 10 digits.

    A population of 2,952,301 must read back as 2952301, not 2.9523e+06. Ten
    significant digits keep any real measurement while dropping the float noise
    a division leaves (0.1 + 0.2), so the bytes stay deterministic.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if not math.isfinite(value):
        return str(value)
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    text = format(value, ".10g")
    return "0" if text == "-0" else text


def _parts(geometry) -> list:
    """[[exterior, hole, ...], ...] for a Polygon or MultiPolygon."""
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    return list(geometry["coordinates"])


def _project(geometry, frame: Frame) -> list:
    """Every ring of every part, in page pixels, simplified to what shows.

    Holes survive. Overture rings carry metre-level detail -- a world map was
    25.9 MB -- and almost none of it survives at 960 px. Each ring is thinned
    in page space to SIMPLIFY_PX, below what the eye or a 2x raster can
    resolve; see project.simplify_ring.
    """
    if frame.is_world and frame.lon0 != 0:
        # A Pacific-centred world map cuts rings at the seam opposite lon0.
        rings_of = lambda part: [piece for ring in part
                                 for piece in project.split_at_seam(
                                     project.unwrap_ring(ring, frame.lon0), frame.lon0)]
    else:
        rings_of = lambda part: [project.unwrap_ring(ring, frame.lon0) for ring in part]
    parts = [[project.simplify_ring([frame.xy(lon, lat) for lon, lat in ring], SIMPLIFY_PX)
              for ring in rings_of(part)]
             for part in _parts(geometry)]
    return _drop_specks(parts)


def _drop_specks(parts) -> list:
    """Remove rings smaller than SPECK_PX2 on the page, keeping each unit's largest.

    Measured on the Overture world map: 42,946 of 43,453 subpaths were under a
    quarter of a pixel -- Arctic and Pacific islets -- and they were 2.27 MB of
    a 2.53 MB file while drawing nothing a reader can see. The largest part of
    a unit always stays, so a microstate keeps its polygon (and its marker).
    """
    if not parts:
        return parts
    areas = [project.planar_area(part[0]) if len(part[0]) >= 3 else 0.0 for part in parts]
    largest = max(range(len(parts)), key=lambda i: areas[i])
    kept = []
    for i, part in enumerate(parts):
        if i != largest and areas[i] < SPECK_PX2:
            continue
        holes = [ring for ring in part[1:]
                 if len(ring) >= 3 and project.planar_area(ring) >= SPECK_PX2]
        kept.append([part[0]] + holes)
    return kept


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
    best, best_area, best_holes = None, 0.0, []
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
            best_holes = [[frame.xy(lon, lat) for lon, lat in project.unwrap_ring(h, frame.lon0)]
                          for h in part[1:] if len(h) >= 3]
    if best is None:
        return None
    precision = max(0.5, math.sqrt(best_area) / 50.0)
    x, y = project.pole_of_inaccessibility(best, precision, holes=best_holes)
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


# --------------------------------------------------------------- context ---

def _context(context: list, frame: Frame) -> str:
    """Neighbouring land, muted, under a `within` map; the sea tinted around it.

    Harvest fetched it; render only draws it. It is not land for the legend's
    purposes -- a legend may sit over Belarus -- and carries no data-id, so
    nothing reads it as a mapped unit.
    """
    if not context or frame.windowed:
        return ""
    out = [f'<g clip-path="url(#map-clip)">',
           f'<rect x="{PAD}" y="{MAP_TOP}" width="{MAP_W}" height="{frame.map_h}" '
           f'fill="{colors.WATER}"/>',
           f'<g fill="{colors.CONTEXT_LAND}" stroke="{colors.CONTEXT_BORDER}" '
           'stroke-width="0.5" stroke-linejoin="round" class="context">']
    # Clipped to the page in pixels: most of a neighbour (all of Russia
    # beyond the Urals) is off the page, and its vertices would only add bytes.
    page = (PAD - 4.0, MAP_TOP - 4.0, PAD + MAP_W + 4.0, frame.bottom + 4.0)
    for item in context:
        rings = [project.clip_to_extent(part[0], page)
                 for part in _project(item["geometry"], frame)]
        d = _d([ring for ring in rings if len(ring) >= 3])
        if d:
            out.append(f'<path d="{d}"/>')
    out.append("</g></g>")
    return "".join(out)


# ---------------------------------------------------------------- insets ---

# A group of units goes to an inset when it is not part of the main cluster
# (no touching bounding boxes), would grow the main frame's projected area by
# INSET_INFLATION or more, is more than INSET_GAP of the main cluster's ground
# diagonal away from it, and holds less than INSET_MAX_AREA of its land.
# Measured growth against the main cluster, in its own projection:
#   EU NUTS 2: Cyprus 1.17, Madeira 1.26, Azores 1.41, Canaries 1.42,
#              Guadeloupe 2.59, Guyane 3.18, Reunion 4.84
#   US states: Puerto Rico 1.38, Hawaii 1.81, Alaska 2.87-2.91
#   Iceland on a map of European countries: under 1.02.
# 1.20 keeps Cyprus and Iceland in the main frame, as an atlas page of Europe
# does, and sends every outermost region and AK/HI/PR to an inset. The margin
# between Cyprus and Madeira is narrow; it is a measured threshold, not a law.
INSET_INFLATION = 1.20
INSET_GAP = 0.05
INSET_MAX_AREA = 0.25
INSET_MAX = 12
INSET_MAX_W, INSET_MAX_H = 0.20, 0.30      # of the map area, per inset
INSET_MIN_W, INSET_MIN_H = 84.0, 60.0
# An inset whose land would be smaller than its box at the main scale is
# enlarged to fill it, up to this factor: Mayotte at the scale of Europe is a
# speck. The caption states the factor either way.
INSET_MAX_ZOOM = 4.0
INSET_RING_SHARE = 0.08
INSET_PAD, INSET_LABEL_H, INSET_GAP_PX = 6.0, 14.0, 8.0
# The share of the inset block that may lie over mapped land before the insets
# move to a band under the map instead.
INSET_MAX_COVER = 0.04
KM_PER_DEGREE = 111.32


def _split_insets(manifest: Manifest, rows: list, missing: list):
    """(main rows, main missing, [InsetFrame]) -- far-flung groups set apart.

    Only for maps framed by their units: a bbox is a window the reader asked
    for, and moving part of it elsewhere would misrepresent it. Units are
    grouped by touching bounding boxes; the group with the most land is the
    main cluster, and any other group that is both far from it and small next
    to it becomes an inset. Nothing here names a place.
    """
    items = [("row", r) for r in rows] + [("missing", m) for m in missing]
    if manifest.bbox is not None or manifest.overlays or len(items) < 2:
        # An overlay map frames on the units near its features (Réunion is
        # not on a map of LGVs); boxing the far ones would bring them back.
        return rows, missing, []
    reps = []
    for _, item in items:
        rings = project.outer_rings(item["geometry"])
        rings = [r for r in rings if len(r) >= 4]
        if not rings:
            reps.append(None)
            continue
        ring = max(rings, key=project._ring_area)
        ring = project.unwrap_ring(ring, 0.0)
        reps.append((_lonlat_box(ring), ring, _ground_area(ring)))

    # Union-find over bounding boxes that touch (with a quarter-degree slack).
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if reps[i] and reps[j] and _boxes_touch(reps[i][0], reps[j][0], 0.25):
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)
    groups: dict = {}
    for i in range(len(items)):
        if reps[i]:
            groups.setdefault(find(i), []).append(i)
    if len(groups) < 2:
        return rows, missing, []
    area = {g: sum(reps[i][2] for i in members) for g, members in groups.items()}
    core = max(groups, key=lambda g: (area[g], -g))
    core_box = _union_box([reps[i][0] for i in groups[core]])
    diagonal = _ground_distance((core_box[0], core_box[1]), (core_box[2], core_box[3]))
    core_rings = [(reps[i][0], project._decimate(reps[i][1], 64)) for i in groups[core]]

    # Inflation is measured in the projection the main map would use, on the
    # core's own extent: Iceland sits inside the box Portugal and Norway
    # already span, the Canaries do not. Groups that do not inflate it join the
    # main frame, and the test repeats against the grown frame -- Crete is its
    # own group, and Cyprus must be judged against a frame that includes it.
    core_geoms = [items[i][1]["geometry"] for i in groups[core]]
    core_extent = project.extent_of(core_geoms)
    projector = project.make(project.choose(core_extent), core_extent)
    main = list(project.viewport_bounds(core_geoms, projector, core_extent))
    projected = {g: [projector(lon, lat) for i in members for lon, lat in reps[i][1]]
                 for g, members in groups.items() if g != core}
    pending = sorted(g for g in groups if g != core)
    changed = True
    while changed:
        changed = False
        for g in list(pending):
            xs = [x for x, _ in projected[g]]
            ys = [y for _, y in projected[g]]
            grown = [min(main[0], min(xs)), min(main[1], min(ys)),
                     max(main[2], max(xs)), max(main[3], max(ys))]
            before = max((main[2] - main[0]) * (main[3] - main[1]), 1e-12)
            after = (grown[2] - grown[0]) * (grown[3] - grown[1])
            if after / before < INSET_INFLATION or area[g] >= INSET_MAX_AREA * area[core]:
                main = grown
                pending.remove(g)
                changed = True

    far = []
    for g in pending:
        members = groups[g]
        gap = min(_gap_to(project._decimate(reps[i][1], 64), reps[i][0], core_rings)
                  for i in members)
        if gap > INSET_GAP * diagonal:
            far.append(members)
    if not far or len(far) > INSET_MAX:
        return rows, missing, []

    moved = {i for members in far for i in members}
    insets = []
    for members in far:
        member_items = [items[i] for i in sorted(members)]
        insets.append(InsetFrame([it for kind, it in member_items if kind == "row"],
                                 [it for kind, it in member_items if kind == "missing"],
                                 sum(reps[i][2] for i in members)))
    insets.sort(key=lambda f: (-f.area, f.label))
    keep_rows = [it for i, (kind, it) in enumerate(items) if kind == "row" and i not in moved]
    keep_missing = [it for i, (kind, it) in enumerate(items)
                    if kind == "missing" and i not in moved]
    return keep_rows, keep_missing, insets


def _ground_area(ring) -> float:
    lat = sum(p[1] for p in ring) / len(ring)
    return (project._ring_area(ring) * math.cos(math.radians(lat))
            * KM_PER_DEGREE * KM_PER_DEGREE)


def _ground_distance(a, b) -> float:
    lat = math.radians((a[1] + b[1]) / 2.0)
    dx = project._shortest(b[0] - a[0]) * math.cos(lat)
    dy = b[1] - a[1]
    return math.hypot(dx, dy) * KM_PER_DEGREE


def _boxes_touch(a, b, slack: float) -> bool:
    return not (a[2] + slack < b[0] or b[2] + slack < a[0]
                or a[3] + slack < b[1] or b[3] + slack < a[1])


def _union_box(boxes) -> tuple:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _box_gap(a, b) -> float:
    """A lower bound on the ground distance between two lon/lat boxes."""
    dx = max(0.0, b[0] - a[2], a[0] - b[2])
    dy = max(0.0, b[1] - a[3], a[1] - b[3])
    lat = math.radians(max(abs(a[1]), abs(a[3]), abs(b[1]), abs(b[3]), 0.0))
    return math.hypot(dx * math.cos(min(lat, math.radians(89))), dy) * KM_PER_DEGREE


def _gap_to(ring, box, core_rings) -> float:
    """Shortest vertex-to-vertex ground distance from a ring to the core."""
    best = float("inf")
    for core_box, core_ring in sorted(core_rings, key=lambda c: _box_gap(box, c[0])):
        if _box_gap(box, core_box) >= best:
            break
        for a in ring:
            for b in core_ring:
                d = _ground_distance(a, b)
                if d < best:
                    best = d
    return best


class InsetFrame:
    """A boxed inset: its own equal-area projection, placed on the map page.

    Drawn at the main map's scale when it fits, so areas stay comparable;
    shrunk to fit otherwise (Alaska), and the label says by how much. Offers
    the same interface as Frame, so the drawing helpers take either.
    """

    windowed = False
    is_world = False

    def __init__(self, rows: list, missing: list, area: float):
        self.rows, self.missing, self.area = rows, missing, area
        named = sorted({_short_name(i) for i in rows + missing})
        self.label = named[0] if len(named) == 1 else ", ".join(named[:2]) + (
            "…" if len(named) > 2 else "")
        self.geometries = [i["geometry"] for i in rows + missing]
        self.extent = project.extent_of(self.geometries)
        self.projection = project.choose(self.extent)
        self.projector = project.make(self.projection, self.extent)
        self.lon0 = (self.extent[0] + self.extent[2]) / 2.0
        self.lat0 = (self.extent[1] + self.extent[3]) / 2.0
        self.bounds = _inset_bounds(self.geometries, self.projector, self.lon0)
        self.factor = 1.0
        self.box = (0.0, 0.0, 0.0, 0.0)

    def size(self, scale: float, max_w: float, max_h: float) -> tuple:
        span_x = max(self.bounds[2] - self.bounds[0], 1e-9) * scale
        span_y = max(self.bounds[3] - self.bounds[1], 1e-9) * scale
        room_w, room_h = max_w - 2 * INSET_PAD, max_h - 2 * INSET_PAD - INSET_LABEL_H
        self.factor = min(1.0, room_w / span_x, room_h / span_y)
        min_w = max(INSET_MIN_W, _text_w(self.caption, 9.5) + 12)
        fill_w, fill_h = min_w - 2 * INSET_PAD, INSET_MIN_H - 2 * INSET_PAD - INSET_LABEL_H
        if self.factor == 1.0 and span_x < fill_w and span_y < fill_h:
            self.factor = min(INSET_MAX_ZOOM, fill_w / span_x, fill_h / span_y)
            self.factor = math.floor(self.factor * 10) / 10.0   # a round, stated factor
        min_w = max(INSET_MIN_W, _text_w(self.caption, 9.5) + 12)
        self.scale = scale * self.factor
        w = max(min_w, span_x * self.factor + 2 * INSET_PAD)
        h = max(INSET_MIN_H, span_y * self.factor + 2 * INSET_PAD + INSET_LABEL_H)
        return w, h

    def place(self, x: float, y: float, w: float, h: float) -> None:
        self.box = (x, y, w, h)
        inner_top = y + INSET_LABEL_H
        span_x = (self.bounds[2] - self.bounds[0]) * self.scale
        span_y = (self.bounds[3] - self.bounds[1]) * self.scale
        self._ox = x + (w - span_x) / 2.0
        self._oy = inner_top + (h - INSET_LABEL_H - span_y) / 2.0

    def xy(self, lon: float, lat: float) -> tuple:
        px, py = self.projector(lon, lat)
        return (self._ox + (px - self.bounds[0]) * self.scale,
                self._oy + (self.bounds[3] - py) * self.scale)

    def visible(self, x: float, y: float) -> bool:
        bx, by, bw, bh = self.box
        return bx <= x <= bx + bw and by <= y <= by + bh

    @property
    def caption(self) -> str:
        if abs(self.factor - 1.0) < 0.005:
            return self.label
        return f"{self.label} (×{self.factor:.2g})"

    @property
    def scale_note(self) -> str:
        if abs(self.factor - 1.0) < 0.005:
            return f"{self.label} at the main scale"
        return f"{self.label} at {self.factor:.2g}× the main scale"


def _inset_bounds(geometries, projector, lon0: float) -> tuple:
    """Projected bounds of every ring of at least INSET_RING_SHARE of its unit's largest.

    An inset is about one place, so the Canaries are all seven islands (El
    Hierro is 13% of Tenerife) -- the main map's largest-ring rule would show
    one. Outliers filed under the same unit do not stretch the box: Alaska's
    Aleutians, the Selvagens under Madeira, Saint-Martin (6% of Basse-Terre)
    under Guadeloupe. They are still drawn where they fall inside it.
    """
    xs: list = []
    ys: list = []
    for geometry in geometries:
        rings = [r for r in project.outer_rings(geometry) if len(r) >= 4]
        if not rings:
            continue
        rings = [project.unwrap_ring(r, lon0) for r in rings]
        largest = max(project._ring_area(r) for r in rings)
        for ring in rings:
            if project._ring_area(ring) >= INSET_RING_SHARE * largest:
                for lon, lat in ring:
                    x, y = projector(lon, lat)
                    xs.append(x)
                    ys.append(y)
    if not xs:
        return (0.0, 0.0, 1.0, 1.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _short_name(item: dict) -> str:
    """A unit's name for a caption: the first of a bilingual pair, trimmed."""
    name = str(item.get("name") or item["id"]).strip()
    return name.split("/")[0].strip() or name


def _draw_insets(insets: list, frame: Frame, encoding: Encoding, water: bool,
                 land: list, units: list) -> tuple[str, bool]:
    """Lay the insets out in a block where they hide the least, and draw them.

    The block is a row (wrapping) of boxes, tried in the legend's candidate
    spots; the chosen boxes are then added to `land` and `units` so the legend
    keeps clear of them. Returns the SVG and whether any inset has no-data.
    """
    frame.insets = insets
    if not insets:
        return "", False
    max_w, max_h = INSET_MAX_W * MAP_W, INSET_MAX_H * frame.map_h
    sizes = [inset.size(frame.scale, max_w, max_h) for inset in insets]
    # Each inset, largest first, goes where it hides no unit and next to no
    # mapped land -- open sea or neighbouring land, bottom-left first, as on
    # an atlas page of the US. What fits nowhere goes to a band under the map.
    occupied = [u for u in units if u]
    placed: list = []
    layout: list = [None] * len(insets)
    for n, (w, h) in enumerate(sizes):
        spot = _inset_spot(frame, land, occupied, placed, w, h)
        if spot is not None:
            x, y = spot
            layout[n] = (x, y, w, h)
            placed.append((x, y, x + w, y + h))
            land.append(((x, y, x + w, y + h), [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]))
    band = [n for n, item in enumerate(layout) if item is None]
    if band:
        x, y, row_h = PAD + LEGEND_INSET, frame.bottom + INSET_GAP_PX, 0.0
        top = y
        for n in band:
            w, h = sizes[n]
            if x > PAD + LEGEND_INSET and x + w > PAD + MAP_W - LEGEND_INSET:
                x, y, row_h = PAD + LEGEND_INSET, y + row_h + INSET_GAP_PX, 0.0
            layout[n] = (x, y, w, h)
            x += w + INSET_GAP_PX
            row_h = max(row_h, h)
        frame.map_h += int(math.ceil(y + row_h - top + 2 * INSET_GAP_PX))
    out = []
    has_missing = False
    background = colors.WATER if water else "#ffffff"
    for n, (inset, (x0, y0, w, h)) in enumerate(zip(insets, layout)):
        inset.place(x0, y0, w, h)
        clip = f"inset-clip-{n}"
        out.append(f'<g class="inset" data-inset={quoteattr(inset.label)}>'
                   f'<clipPath id="{clip}"><rect x="{_num(x0)}" y="{_num(y0)}" '
                   f'width="{_num(w)}" height="{_num(h)}"/></clipPath>'
                   f'<rect x="{_num(x0)}" y="{_num(y0)}" width="{_num(w)}" height="{_num(h)}" '
                   f'fill="{background}" stroke="{colors.FRAME}" stroke-width="0.8"/>'
                   f'<g clip-path="url(#{clip})" stroke-linejoin="round">')
        markers = []
        for row in inset.rows:
            fill = encoding.fill(row["value"])
            parts = _project(row["geometry"], inset)
            out.append(_path(row, parts, fill))
            if encoding.mode == "choropleth" and _visible_area(parts) < MIN_VISIBLE_AREA:
                anchor = _anchor(row["geometry"], inset)
                if anchor:
                    markers.append(_marker(row, anchor, fill))
        for item in inset.missing:
            has_missing = True
            out.append(_path(item, _project(item["geometry"], inset),
                             f"url(#{colors.HATCH_ID})"))
        out.extend(markers)
        if encoding.mode == "symbols":
            svg, _ = _symbols(inset.rows, inset, encoding)
            out.append(svg.replace('clip-path="url(#map-clip)"', f'clip-path="url(#{clip})"'))
        out.append("</g>")
        out.append(f'<text x="{_num(x0 + 5)}" y="{_num(y0 + 11)}" font-size="9.5" '
                   f'fill="{colors.TEXT_MUTED}" font-family="{FONT}">'
                   f'{escape(inset.caption)}</text></g>')
        units.append((x0, y0, x0 + w, y0 + h))
        if n in band:
            ring = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
            land.append(((x0, y0, x0 + w, y0 + h), ring))
    return "".join(out), has_missing


def _inset_spot(frame: Frame, land: list, occupied: list, placed_insets: list,
                w: float, h: float):
    """The first spot, bottom-left outward, that hides nothing mapped."""
    left, right = PAD + LEGEND_INSET, PAD + MAP_W - w - LEGEND_INSET
    top, bottom = MAP_TOP + LEGEND_INSET, frame.bottom - h - LEGEND_INSET
    if right < left or bottom < top:
        return None
    steps = 16
    xs = [left + (right - left) * i / steps for i in range(steps + 1)]
    ys = [bottom - (bottom - top) * i / steps for i in range(steps + 1)]
    candidates = ([(x, bottom) for x in xs] + [(left, y) for y in ys[1:]]
                  + [(right, y) for y in ys[1:]] + [(x, top) for x in xs])
    for x, y in candidates:
        if any(_overlaps((x - INSET_GAP_PX, y - INSET_GAP_PX, x + w + INSET_GAP_PX,
                          y + h + INSET_GAP_PX), box) for box in placed_insets):
            continue   # never on, or touching, another inset
        if any(_hidden(u, x, y, w, h) for u in occupied):
            continue   # never hides a unit, a marker or a circle
        if _land_share(frame, land, x, y, w, h) <= INSET_MAX_COVER:
            return x, y
    return None


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# -------------------------------------------------------------- enclaves ---

def _enclaves(rows: list, frame: Frame, encoding: Encoding) -> str:
    """Outline and name units that sit in a hole of another unit.

    Kyiv city is an enclave in Kyiv oblast: a few pixels across, with the
    oblast all round it. Its own circle or colour is not enough to read as a
    separate unit, so its boundary is redrawn above the symbols and it gets a
    name. Only true enclaves qualify -- a unit inside another's hole -- so a
    small unit on a coast or a border is left alone.
    """
    holes = []
    for row in rows:
        for part in _parts(row["geometry"]):
            for hole in part[1:]:
                if len(hole) >= 4:
                    holes.append((_lonlat_box(hole), hole, row["id"]))
    if not holes:
        return ""
    out = []
    for row in rows:
        rings = [part[0] for part in _parts(row["geometry"]) if len(part[0]) >= 4]
        if not rings:
            continue
        ring = max(rings, key=project.planar_area)
        box = _lonlat_box(ring)
        lon, lat = ring[0]
        inside = any(owner != row["id"] and _box_within(box, hbox)
                     and project.point_in_ring(lon, lat, hole)
                     for hbox, hole, owner in holes)
        if not inside:
            continue
        parts = _project(row["geometry"], frame)
        d = _d([part[0] for part in parts])
        if not d:
            continue
        out.append(f'<path d="{d}" fill="none" stroke="{colors.TEXT}" stroke-width="0.9" '
                   f'class="enclave" data-id={quoteattr(str(row["id"]))}/>')
        if encoding.mode != "symbols":
            # In a choropleth the enclave carries its own colour; the outline
            # is enough. A circle, though, hides the shape under it.
            continue
        placed = _placement(row["geometry"], frame)
        if placed is None:
            continue
        (x, y), _ = placed
        reach = encoding.radius(float(row["value"])) if (
            encoding.mode == "symbols" and isinstance(row["value"], (int, float))) else 0.0
        reach = max(reach, MARKER_R, max(b[0][2] - b[0][0] for b in _outer_boxes(parts)) / 2)
        name = _short_name(row)
        out.append(f'<text x="{_num(x + reach + 4)}" y="{_num(y + 4)}" font-size="11" '
                   f'font-weight="600" fill="{colors.TEXT}" stroke="#ffffff" '
                   'stroke-width="3" stroke-linejoin="round" paint-order="stroke" '
                   f'font-family="{FONT}">{escape(name)}</text>')
    return "".join(out)


def _lonlat_box(ring) -> tuple:
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return min(lons), min(lats), max(lons), max(lats)


def _box_within(inner: tuple, outer: tuple, slack: float = 1e-6) -> bool:
    return (inner[0] >= outer[0] - slack and inner[1] >= outer[1] - slack
            and inner[2] <= outer[2] + slack and inner[3] <= outer[3] + slack)


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

def _label(manifest: Manifest, provenance: dict) -> str:
    """The quantity's human name. Never an id: a Eurostat id is a query string."""
    label = provenance.get("variable")
    if not label:
        label = str(manifest.variable_id or "").split("?")[0].replace("_", " ")
    return _human(label)


def _title(manifest: Manifest, provenance: dict) -> str:
    """'GDP per inhabitant, 2024' -- from the drawn data, never the slug.

    The year is that of the values drawn. When each unit shows its latest
    observation and they disagree, the title says so rather than implying one
    vintage; a change names its two years.
    """
    if manifest.variable_id is None:
        return _human(f"{_SCOPE.get(manifest.level, manifest.level)} boundaries")
    label = _label(manifest, provenance)
    change = provenance.get("change")
    if change:
        return f"{label}, {change['from']}–{change['to']}"
    year = provenance.get("year")
    if not year:
        return label
    drawn = provenance.get("years_drawn")
    spread = len(drawn) > 1 if drawn is not None else "–" in str(year)
    if spread:
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

    scale = p.get("scale") or registry.scale_for(manifest.level)
    vintage = p.get("vintage") or registry.SOURCE_VINTAGE
    source = f"{p.get('source') or registry.SOURCE_NAME} {vintage}, {scale}"
    # ODbL requires attribution wherever the data is shown; the footnote is the
    # only place this map has to say it.
    license_note = p.get("license")
    if license_note and license_note != "public domain":
        source += f". {p.get('attribution') or ''} ({license_note})".rstrip()
    if p.get("context_source") and result.context:
        source += f". Neighbouring land: {p['context_source']}"

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

    variable = _variable_line(manifest, p)
    excluded = _exclusions(result)

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


def _variable_line(manifest: Manifest, p: dict) -> str:
    """What is drawn, its unit and year, how it was derived, and who measured it."""
    if manifest.variable_id is None:
        return f"none -- {manifest.level} boundaries"
    variable = _label(manifest, p)
    if p.get("unit"):
        variable += f" ({p['unit']})"
    if p.get("year") and not p.get("change"):
        variable += f", {p['year']}"
    per = p.get("per")
    if per:
        divisor = per.get("variable") or per.get("id")
        detail = ", ".join(str(x) for x in (per.get("source_name") or per.get("source"),
                                            per.get("id"), per.get("year")) if x)
        variable += f"; divided by {_lower_first(divisor)} ({detail})"
    change = p.get("change")
    if change:
        variable += ("; computed per unit as "
                     f"{change.get('formula', '(end − start) / start × 100')}")
    if p.get("statistics_source") and p.get("statistics_source") != p.get("source"):
        # Boundaries and numbers came from different providers, possibly under
        # different licences. Credit the data as well as the map.
        credit = p.get("statistics_attribution") or p["statistics_source"]
        vintage = p.get("statistics_vintage")
        if vintage and vintage != p.get("year"):
            credit += f"; {vintage}"
        variable += f" [{credit}]"
    return variable


def _exclusions(result) -> str:
    """How many units are hatched, and why, in words -- naming them when few."""
    dropped = result.dropped
    if not dropped:
        return "0 features excluded"
    meanings = {**_REASONS, **(result.provenance.get("drop_reasons") or {})}
    by_reason: dict[str, list] = {}
    for item in dropped:
        by_reason.setdefault(item.get("reason", "unknown"), []).append(item)
    parts = []
    for reason, items in sorted(by_reason.items()):
        part = f"{len(items)} {meanings.get(reason, reason.replace('_', ' '))}"
        names = sorted({str(i["name"]) for i in items if i.get("name")})
        if 0 < len(items) <= NAMED_DROPS and len(names) == len(items):
            part += f" — {', '.join(names)}"
        parts.append(part)
    total = len(dropped) + len(result.rows)
    return (f"{len(dropped)} of {total} features excluded, hatched as no data: "
            + "; ".join(parts))


def _lower_first(text: str) -> str:
    text = str(text)
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def _projection_label(frame: Frame) -> str:
    label = _main_projection_label(frame)
    if getattr(frame, "insets", None):
        label += ("; insets, each in its own equal-area projection: "
                  + ", ".join(inset.scale_note for inset in frame.insets))
    return label


def _main_projection_label(frame: Frame) -> str:
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
