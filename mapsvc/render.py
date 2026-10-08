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
import re
import textwrap
from xml.sax.saxutils import escape, quoteattr

from mapsvc import classify, colors, overlay_render, project, registry, usfips
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
                 centre_lon: float | None = None, min_height: int | None = None,
                 crop_window: bool = False):
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
            projected_window = [self.projector(lon, lat)
                                for lon, lat in project.window_outline(self.extent)]
            xs, ys = [p[0] for p in projected_window], [p[1] for p in projected_window]
            bounds = (min(xs), min(ys), max(xs), max(ys))
            # The globe's outline is a curve whose widest point is a single
            # vertex at the equator: fitted with a 1 px margin it sits on the
            # page gutter and reads as cut off flat. A bbox window is a
            # rectangle-ish frame and can go to the edge.
            margin = WORLD_MARGIN if self.is_world or crop_window else 1.0
            outline = None if crop_window else projected_window
        else:
            outline = None
            bounds = project.viewport_bounds(geometries, self.projector, self.extent)
            margin = 10.0

        span_x = max(bounds[2] - bounds[0], 1e-9)
        span_y = max(bounds[3] - bounds[1], 1e-9)
        ideal = (MAP_W - 2 * margin) * span_y / span_x + 2 * margin
        self.map_h = int(min(MAP_H_MAX, max(MAP_H_MIN, round(ideal))))
        if min_height is not None:
            self.map_h = min(MAP_H_MAX, max(self.map_h, min_height))
        local = project.fit(bounds, MAP_W, self.map_h, margin)
        self.transform = lambda x, y: _shift(local(x, y))
        self.outline = (None if outline is None
                        else [self.transform(x, y) for x, y in outline])
        # Page pixels per projected unit. Every projection here is on the unit
        # sphere, so an inset drawn at this scale is at the same scale.
        self.scale = local(1.0, 0.0)[0] - local(0.0, 0.0)[0]
        self.insets: list = []
        self.legend_box: tuple | None = None    # a legend floated over the map

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
    overlay_box = tuple(result.provenance.get("overlay_bbox") or ())
    measured_overlay = overlay_render.overlay_extent(result.overlays)
    if measured_overlay is not None:
        overlay_box = tuple(project.frame_box(measured_overlay))
    overlay_framed = bool(overlay_box) and overlay_box != WORLD
    if manifest.bbox is None and result.provenance.get("overlay_bbox"):
        # Framed on its overlays (no window was given): the harvest chose it.
        manifest = dataclasses.replace(
            manifest, bbox=tuple(result.provenance["overlay_bbox"]))
    if overlay_framed:
        # Even if an agent supplied a loose bbox, rendering remains derived:
        # centre and crop the projection on the data actually being drawn.
        manifest = dataclasses.replace(manifest, bbox=overlay_box)
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
    # A small atlas-style inset set needs the lower-left sea inside the map,
    # rather than a box dangling below its frame. A taller frame creates that
    # room without naming Alaska or Hawaii.
    inset_room = MAP_H_MAX if 0 < len(insets) <= 2 and not manifest.overlays else None
    frame = Frame(manifest, framed,
                  overlay_render.centre_lon(result.overlays, geometries), inset_room,
                  crop_window=overlay_framed)
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
    unit_stroke = _unit_stroke_width(len(rows), frame)
    body.append('<g clip-path="url(#map-clip)" stroke-linejoin="round">')
    markers: list[str] = []
    for row in rows:
        fill = encoding.fill(row["value"])
        parts = _project(row["geometry"], frame)
        boxes = _outer_boxes(parts)
        land.extend(boxes)
        units.append(_union(boxes))
        body.append(_path(row, parts, fill, border, unit_stroke,
                          plain=_visible_area(parts) < MIN_VISIBLE_AREA))
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
        body.append(_path(item, parts, f"url(#{colors.HATCH_ID})",
                          stroke_width=unit_stroke,
                          plain=_visible_area(parts) < MIN_VISIBLE_AREA))
    body.append("</g>")
    body.append(_hierarchy(result.hierarchy, frame))
    body.append(_physical_context(result.physical, frame))
    if frame.outline is not None:
        body.append(f'<path d="{_d([frame.outline])}" fill="none" '
                    f'stroke="{colors.FRAME}" stroke-width="0.8"/>')
    body.extend(markers)

    if encoding.mode == "symbols":
        encoding.r_max = SYMBOL_SCALE * math.sqrt(MAP_W * frame.map_h)
        svg, circles = _symbols(rows, frame, encoding)
        body.append(svg)
        units.extend(circles)
    body.append(_enclaves(rows, frame, encoding,
                          named=len(every_row) > LABEL_MAX_UNITS or has_overlays))
    inset_svg, inset_missing = _draw_insets(insets, frame, encoding, bool(result.context),
                                            land, units, unit_stroke)
    body.append(inset_svg)

    art = None
    if has_overlays:
        art = overlay_render.draw(result.overlays, result.places, frame, {
            "pad": PAD, "map_w": MAP_W, "map_top": MAP_TOP, "font": FONT},
                                  result.physical)
        body.append('<g clip-path="url(#map-clip)" stroke-linejoin="round">'
                    + "".join(art.svg) + "</g>")
        body.append(f'<g font-family="{FONT}">' + "".join(art.labels) + "</g>")

    legend_band = 0.0
    placer = art.placer if art is not None else None
    labels_svg = ""
    if encoding.mode != "basemap":
        encoding.missing_label = _missing_label(result, drawn_missing, insets)
        legend, legend_band = _legend(encoding, frame, land,
                                      [u for u in units if u],
                                      bool(drawn_missing) or inset_missing)
        if not has_overlays:
            placer = _placer(frame, encoding)
            labels_svg = _feature_labels(rows, insets, drawn_missing, frame, encoding,
                                         land, placer, len(every_row),
                                         bool(p.get("change")),
                                         lambda: _placer(frame, encoding))
        body.append(legend)
    if placer is None:
        placer = _placer(frame, encoding)
    context_svg, context_credits = _context_labels(result, rows, frame, encoding, manifest,
                                                   placer, land)
    body.append(context_svg)
    body.append(labels_svg)
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
    for y, size, line in _subtitle_lines(subtitle):
        head.append(f'<text x="{PAD}" y="{y}" font-size="{size}" '
                    f'fill="{colors.TEXT_MUTED}">{escape(line)}</text>')
    body.append(f'<g font-family="{FONT}">' + "".join(head) + "</g>")

    footer_top = frame.bottom + legend_band + 14
    footer, footer_h = _footer(manifest, frame, encoding, result, footer_top,
                               list(art.footer if art is not None else ()) + context_credits)
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
                 swatches: list[str] | None = None, vmax: float = 0.0,
                 value_scale: float = 1.0):
        self.mode = mode
        self.label = label
        self.unit = unit
        self.scheme = scheme
        self.swatches = swatches
        self.vmax = vmax
        self.value_scale = value_scale
        self.r_max = 0.0
        self.circles: list = []          # (x, y, r, row) of every drawn circle
        self.vmin = 0.0
        self.missing_label = "no data"   # what the hatch means, in the legend
        self.decimals: int | None = None    # the source's own precision, if it has one

    def fill(self, value) -> str:
        if self.mode == "basemap":
            return colors.BASEMAP_FILL
        if self.mode == "symbols":
            return colors.COUNT_LAND
        displayed = (float(value) * self.value_scale
                     if self.value_scale != 1.0 else value)
        return self.swatches[self.scheme.bin_of(displayed)]

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
        enc = Encoding("symbols", label, unit, vmax=max(float(v) for v in values))
        enc.vmin = min(float(v) for v in values)
        return enc

    if level == "ratio" and numeric:
        # Round breaks: log-spaced, either side of zero, a fixed 0-100 scale
        # or rounded natural breaks -- whichever the values call for.
        index_reference = provenance.get("index_reference") or {}
        reference_value = float(index_reference.get("value") or 0.0)
        value_scale = 100.0 / reference_value if reference_value > 0 else 1.0
        displayed = [float(v) * value_scale for v in values]
        if reference_value > 0:
            reference_label = str(index_reference.get("label") or "reference = 100")
            scheme = classify.anchored(displayed, 100.0, reference_label)
            year = index_reference.get("year")
            unit = f"index ({reference_label}{f', {year}' if year else ''})"
        else:
            scheme = classify.nice(displayed, percent=_is_share(unit))
        center_at = scheme.zero_at if scheme.zero_at is not None else scheme.center_at
        if center_at is not None:
            direction = provenance.get("diverging") or {}
            reverse = ((direction.get("negative_color"), direction.get("positive_color"))
                       == ("blue", "red"))
            swatches = colors.diverging(
                "RdBu", center_at, scheme.k - center_at, reverse=reverse)
        else:
            swatches = colors.colors(registry.RAMP_FOR_LEVEL["ratio"], scheme.k)
        enc = Encoding("choropleth", label, unit, scheme, swatches,
                       value_scale=value_scale)
        enc.decimals = _source_decimals(values) if value_scale == 1.0 else None
        return enc

    ramp = registry.RAMP_FOR_LEVEL.get(level, "YlGnBu")
    scheme = classify.build(values, level, registry.DEFAULT_METHOD, registry.DEFAULT_K)
    try:
        swatches = colors.colors(ramp, scheme.k)
    except colors.RampError as exc:
        # A nominal variable with more categories than any qualitative ramp
        # holds. There is nothing to reconfigure: the map cannot be drawn.
        raise RenderError(str(exc), "variable.id") from exc
    return Encoding("choropleth", label, unit, scheme, swatches)


def _is_share(unit: str) -> bool:
    """A percentage of a whole, which can only run 0-100 ("%", "percent").

    "% change 2000–2023" is a percentage but not a share: it is unbounded.
    """
    u = (unit or "").strip().lower()
    return u in ("%", "percent", "per cent") or u.startswith(("% of", "percent of"))


def _affixes(unit: str) -> tuple[str, str]:
    """What a legend label carries of the unit: '$' before, '%' after."""
    u = (unit or "").strip().lower()
    prefix = "$" if ("$" in u or "dollar" in u) else ""
    if "percentage point" in u:
        return prefix, " pp"
    suffix = "%" if u.startswith(("%", "percent", "per cent")) else ""
    return prefix, suffix


_MARGIN = re.compile(r"\(\s*([A-Za-z][\w .-]*?)\s+minus\s+([A-Za-z][\w .-]*?)\s*[;)]")


def _margin_sides(unit: str) -> tuple[str, str] | None:
    """('R', 'D') for "percentage points (Republican minus Democratic; ...)":
    the initials a margin label leads with, positive side first."""
    found = _MARGIN.search(unit or "")
    if "percentage point" not in (unit or "").lower() or not found:
        return None
    return found.group(1)[0].upper(), found.group(2)[0].upper()


def _source_decimals(values) -> int | None:
    """How many decimals the source itself reports (0, 1 or 2), or None when
    the values are computed (a ratio, a change) and carry float noise. A label
    must not claim more precision than that -- or less: Starr County's 9.5%
    is not '10%'."""
    floats = [float(v) for v in values
              if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not floats:
        return None
    for places in (0, 1, 2):
        if all(abs(v - round(v, places)) < 1e-9 for v in floats):
            return places
    return None


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


def _unit_stroke_width(unit_count: int, frame: Frame) -> float:
    """A quiet boundary at dense page scales, stronger for sparse maps."""
    area_per_unit = MAP_W * frame.map_h / max(unit_count, 1)
    return max(0.18, min(0.35, 0.36 * math.sqrt(area_per_unit / 7600.0)))


def _path(item: dict, parts, fill: str, stroke: str = colors.BORDER,
          stroke_width: float = 0.35, plain: bool = False) -> str:
    d = _d([ring for part in parts for ring in part])
    if not d:
        return ""
    value = item.get("value")
    value_attr = ("" if value is None
                  else f" data-value={quoteattr(_raw_value(value))}")
    return (
        f'<path d="{d}" fill="{fill}" fill-rule="evenodd" '
        f'stroke="{"none" if plain else stroke}" '
        f'stroke-width="{_num(0 if plain else stroke_width)}" '
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
    piece = _piece(geometry, frame)
    return None if piece is None else (piece[0], piece[1])


def _piece(geometry, frame: Frame):
    """(anchor, share inside the window, largest visible ring, its holes), or None.

    The ring and holes are in page pixels, for testing whether a label fits
    inside the unit or where else in it a symbol could go.
    """
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
    return (x, y), share, best, best_holes


def _marker(item: dict, anchor, fill: str) -> str:
    x, y = anchor
    return (f'<circle cx="{_num(x)}" cy="{_num(y)}" r="{MARKER_R}" fill="{fill}" '
            f'class="small-unit" '
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
    placed = _separate(placed, frame)
    encoding.circles.extend((x, y, r, row) for _, _, (x, y), r, row in placed)
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


# Two circles overlapping by more than this share of the smaller radius read
# as one blob: Kyiv city's 2.95M (r 32 px) and Kyiv oblast's 1.80M (r 25) sat
# 48 px apart, 9 px deep in each other. The unit with more room moves its
# circle elsewhere inside itself.
SEPARATE_OVERLAP = 0.25
SEPARATE_STEP = 4.0


def _separate(placed: list, frame) -> list:
    """Move a circle off another whose centre it hides, staying inside its unit.

    Only pairs overlapping by more than SEPARATE_OVERLAP of the smaller
    radius are touched, and a circle moves only to a spot that overlaps less.
    Of the two, the unit with the larger visible piece moves: it has room,
    and the other (an enclave, a city) keeps its circle where it is. The new
    spot is the point inside the unit, on a grid, that overlaps every other
    circle least, then is nearest the original. Deterministic.
    """
    out = list(placed)
    for i in range(len(out)):
        for j in range(i):
            (xi, yi), ri = out[i][2], out[i][3]
            (xj, yj), rj = out[j][2], out[j][3]
            if ri + rj - math.hypot(xi - xj, yi - yj) <= SEPARATE_OVERLAP * min(ri, rj):
                continue
            pieces = {}
            for n in (i, j):
                piece = _piece(out[n][4]["geometry"], frame)
                pieces[n] = piece
            areas = {n: (project.planar_area(pieces[n][2]) if pieces[n] else 0.0)
                     for n in (i, j)}
            mover = i if (areas[i], -i) > (areas[j], -j) else j
            piece = pieces[mover]
            if piece is None:
                continue
            spot = _free_spot(out, mover, piece[2], piece[3])
            if spot is not None:
                item = out[mover]
                out[mover] = (item[0], item[1], spot, item[3], item[4])
    return out


def _free_spot(placed: list, mover: int, ring, holes):
    (x0, y0), r = placed[mover][2], placed[mover][3]

    def depth(x, y):
        return sum(max(0.0, r + other[3] - math.hypot(x - other[2][0], y - other[2][1]))
                   for n, other in enumerate(placed) if n != mover)

    ring = project._decimate(ring, 200)
    holes = [project._decimate(h, 200) for h in holes]
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    best = (depth(x0, y0), 0.0, x0, y0)
    step = max(SEPARATE_STEP, (max(xs) - min(xs) + max(ys) - min(ys)) / 80.0)
    y = min(ys)
    while y <= max(ys):
        x = min(xs)
        while x <= max(xs):
            # Inside the unit, with at least a third of the radius to spare.
            if project._signed_distance(x, y, ring, holes) >= r / 3.0:
                score = (round(depth(x, y), 3), round(math.hypot(x - x0, y - y0), 3), x, y)
                if score < best:
                    best = score
            x += step
        y += step
    return None if (best[2], best[3]) == (x0, y0) else (best[2], best[3])


# --------------------------------------------------------------- context ---

def _context(context: list, frame: Frame) -> str:
    """Neighbouring land, muted, under a `within` map; the sea tinted around it.

    Harvest fetched it; render only draws it. It is not land for the legend's
    purposes -- a legend may sit over Belarus -- and carries no data-id, so
    nothing reads it as a mapped unit.
    """
    if not context:
        return ""
    out = [f'<g clip-path="url(#map-clip)">']
    if not frame.windowed:
        # A windowed map has its water already.
        out.append(f'<rect x="{PAD}" y="{MAP_TOP}" width="{MAP_W}" height="{frame.map_h}" '
                   f'fill="{colors.WATER}"/>')
    out += [f'<g fill="{colors.CONTEXT_LAND}" stroke="{colors.CONTEXT_BORDER}" '
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


def _physical_context(layers: list, frame: Frame) -> str:
    """Draw physical geometry already fetched and selected by harvest."""
    if not layers:
        return ""
    out = ['<g clip-path="url(#map-clip)" class="physical-context">']
    for layer in layers:
        css_class = layer.get("class") or "physical"
        if layer.get("kind") == "polygons":
            paths = []
            for feature in layer.get("features", []):
                parts = _project(feature["geometry"], frame)
                paths.append(_d([ring for part in parts for ring in part]))
            d = "".join(paths)
            if d:
                out.append(f'<path d="{d}" class="{escape(css_class)}" '
                           f'fill="{colors.LAKE_FILL}" stroke="{colors.LAKE_STROKE}" '
                           'stroke-width="0.45" fill-rule="evenodd"/>')
        elif layer.get("kind") == "lines":
            paths = []
            for feature in layer.get("features", []):
                paths.extend(_physical_line(feature.get("coords") or [], frame))
            d = _line_d(paths)
            if not d:
                continue
            plate = css_class == "plate-boundaries"
            colour = colors.PLATE if plate else colors.RIVER
            width = 0.7 if plate else 0.75
            dash = ' stroke-dasharray="4 2.5"' if plate else ""
            out.append(f'<path d="{d}" class="{escape(css_class)}" fill="none" '
                       f'stroke="{colour}" stroke-width="{width}" '
                       f'stroke-linecap="round" stroke-linejoin="round"{dash}/>')
    out.append("</g>")
    return "".join(out)


def _hierarchy(units: list, frame: Frame) -> str:
    """Published parent outlines over detailed child polygons."""
    if not units:
        return ""
    paths = []
    for unit in sorted(units, key=lambda item: str(item["id"])):
        parts = _project(unit["geometry"], frame)
        d = _d([ring for part in parts for ring in part])
        if d:
            paths.append(f'<path d="{d}" data-parent-id={quoteattr(str(unit["id"]))}/>')
    if not paths:
        return ""
    return ('<g clip-path="url(#map-clip)" class="hierarchy-borders" fill="none" '
            'stroke="#4f5858" stroke-width="1.15" stroke-linejoin="round" '
            'pointer-events="none">' + "".join(paths) + "</g>")


def _physical_line(coords: list, frame: Frame) -> list:
    """Project a polyline, breaking it at the visible world's seam."""
    runs, run = [], []
    previous = None
    for lon, lat in coords:
        wrapped = frame.lon0 + project._shortest(float(lon) - frame.lon0)
        if previous is not None and abs(wrapped - previous) > 180.0:
            if len(run) >= 2:
                runs.append(project.simplify_ring(run, SIMPLIFY_PX))
            run = []
        run.append(frame.xy(wrapped, float(lat)))
        previous = wrapped
    if len(run) >= 2:
        runs.append(project.simplify_ring(run, SIMPLIFY_PX))
    return [run for run in runs if len(run) >= 2]


def _line_d(lines: list) -> str:
    return "".join("M" + "L".join(f"{_num(x)},{_num(y)}" for x, y in line)
                   for line in lines if len(line) >= 2)


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
    if len(insets) >= 3:
        # Geographic order makes a grid scan predictably west to east instead
        # of shuffling EU boxes by the land area of each outermost region.
        insets.sort(key=lambda f: ((f.extent[0] + f.extent[2]) / 2.0, f.label))
    else:
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
                 land: list, units: list, stroke_width: float = 0.35) -> tuple[str, bool]:
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
    grid = len(insets) >= 3
    if grid:
        # EU outermost regions read as one system: equal boxes in a west-to-east
        # grid, rather than unrelated boxes scattered around whatever sea fits.
        cell_w = max(w for w, _ in sizes)
        cell_h = max(h for _, h in sizes)
        sizes = [(cell_w, cell_h)] * len(insets)
    # Each inset, largest first, goes where it hides no unit and next to no
    # mapped land -- open sea or neighbouring land, bottom-left first, as on
    # an atlas page of the US. What fits nowhere goes to a band under the map.
    occupied = [u for u in units if u]
    placed: list = []
    layout: list = [None] * len(insets)
    if grid:
        columns = 2
        rows = math.ceil(len(insets) / columns)
        block_w = columns * cell_w + (columns - 1) * INSET_GAP_PX
        block_h = rows * cell_h + (rows - 1) * INSET_GAP_PX
        spot = _inset_spot(frame, land, occupied, [], block_w, block_h)
        if spot is not None:
            bx, by = spot
            for n in range(len(insets)):
                row, column = divmod(n, columns)
                x = bx + column * (cell_w + INSET_GAP_PX)
                y = by + row * (cell_h + INSET_GAP_PX)
                layout[n] = (x, y, cell_w, cell_h)
                placed.append((x, y, x + cell_w, y + cell_h))
    elif len(insets) == 2:
        # A paired national inset system reads as one composition. Reduce both
        # at the same stated factor until they fit the lower-left sea inside
        # the frame; only then consider an external band.
        for shrink in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4):
            trial_sizes = [(w * shrink, h * shrink) for w, h in sizes]
            trial_layout, trial_placed = [None] * len(insets), []
            for n, (w, h) in enumerate(trial_sizes):
                spot = _inset_spot(frame, land, occupied, trial_placed, w, h)
                if spot is None:
                    break
                x, y = spot
                trial_layout[n] = (x, y, w, h)
                trial_placed.append((x, y, x + w, y + h))
            if all(item is not None for item in trial_layout):
                sizes, layout, placed = trial_sizes, trial_layout, trial_placed
                if shrink != 1.0:
                    for inset in insets:
                        inset.factor *= shrink
                        inset.scale *= shrink
                break
    else:
        for n, (w, h) in enumerate(sizes):
            spot = _inset_spot(frame, land, occupied, placed, w, h)
            if spot is not None:
                x, y = spot
                layout[n] = (x, y, w, h)
                placed.append((x, y, x + w, y + h))
    for box in placed:
        x0, y0, x1, y1 = box
        land.append((box, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]))
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
            out.append(_path(row, parts, fill, stroke_width=stroke_width,
                             plain=_visible_area(parts) < MIN_VISIBLE_AREA))
            if encoding.mode == "choropleth" and _visible_area(parts) < MIN_VISIBLE_AREA:
                anchor = _anchor(row["geometry"], inset)
                if anchor:
                    markers.append(_marker(row, anchor, fill))
        for item in inset.missing:
            has_missing = True
            parts = _project(item["geometry"], inset)
            out.append(_path(item, parts, f"url(#{colors.HATCH_ID})",
                             stroke_width=stroke_width,
                             plain=_visible_area(parts) < MIN_VISIBLE_AREA))
        out.extend(markers)
        if encoding.mode == "symbols":
            svg, _ = _symbols(inset.rows, inset, encoding)
            out.append(svg.replace('clip-path="url(#map-clip)"', f'clip-path="url(#{clip})"'))
        out.append("</g>")
        out.append(_inset_scale_bar(inset, x0, y0, w, h))
        out.append(f'<text x="{_num(x0 + 5)}" y="{_num(y0 + 11)}" font-size="9.5" '
                   f'fill="{colors.TEXT_MUTED}" font-family="{FONT}">'
                   f'{escape(inset.caption)}</text></g>')
        units.append((x0, y0, x0 + w, y0 + h))
        if n in band:
            ring = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
            land.append(((x0, y0, x0 + w, y0 + h), ring))
    return "".join(out), has_missing


def _inset_scale_bar(inset: InsetFrame, x0: float, y0: float,
                     w: float, h: float) -> str:
    """A compact geodesic scale for an inset's unit-sphere projection."""
    target_km = max(1.0, 0.28 * w * 6371.0088 / max(inset.scale, 1e-9))
    exponent = 10 ** math.floor(math.log10(target_km))
    km = max(v * exponent for v in (1, 2, 5) if v * exponent <= target_km)
    px = km * inset.scale / 6371.0088
    right, base = x0 + w - 6, y0 + h - 6
    left = right - px
    return (f'<g class="inset-scale-bar" fill="{colors.TEXT_MUTED}" '
            f'stroke="{colors.TEXT_MUTED}" stroke-width="0.7">'
            f'<path d="M{_num(left)},{_num(base - 3)}V{_num(base)}H{_num(right)}'
            f'V{_num(base - 3)}" fill="none"/>'
            f'<text x="{_num((left + right) / 2)}" y="{_num(base - 5)}" '
            f'text-anchor="middle" stroke="none" font-size="8" '
            f'font-family="{FONT}">{km:g} km</text></g>')


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
        clearance = INSET_GAP_PX
        if any(_hidden(u, x - clearance, y - clearance,
                       w + 2 * clearance, h + 2 * clearance) for u in occupied):
            continue   # never hides a unit, a marker or a circle
        # A visible moat keeps Alaska's box from touching the conterminous US.
        # Sampling the expanded rectangle also catches an edge contact that
        # has zero covered area in the box itself.
        if _land_share(frame, land, x - clearance, y - clearance,
                       w + 2 * clearance, h + 2 * clearance) == 0.0:
            return x, y
    return None


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# -------------------------------------------------------------- enclaves ---

def _enclaves(rows: list, frame: Frame, encoding: Encoding, named: bool = True) -> str:
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
        if encoding.mode != "symbols" or not named:
            # In a choropleth the enclave carries its own colour; the outline
            # is enough. A circle, though, hides the shape under it -- unless
            # the feature labels name every unit anyway.
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


# ---------------------------------------------------------------- labels ---

# A map of at most this many units names them all; a bigger one (a world
# map, ~240 countries; EU NUTS 2, 244 regions) names only its extremes.
LABEL_MAX_UNITS = 60
# How many of the highest and of the lowest a big map names.
LABEL_EXTREMES = 2
# A big map names this many from each end, plus one per LABEL_PER_SIDE_STEP
# units (238 countries: 4 a side; 400 districts: 5), those beyond the first
# LABEL_EXTREMES being at least LABEL_MIN_AREA px2 on the page.
LABEL_PER_SIDE = 3
LABEL_PER_SIDE_STEP = 150
LABEL_MIN_AREA = 40.0
# ...and at least this far (px) from the one before: not five districts of one
# empty corner.
LABEL_APART = 45.0
NAME_SIZE, VALUE_SIZE = 9.5, 8.5
# A unit too small for the label at full size tries it this much smaller
# before it is called out: a short leader is better than a long one.
SMALL = 0.84
LABEL_INK = "#1f2a30"
# Every label is haloed, so it reads over any fill, border or leader.
LABEL_HALO = ('stroke="#ffffff" stroke-width="3" stroke-linejoin="round" '
              'stroke-opacity="0.9" paint-order="stroke"')
LEADER = "#5d6870"
# Where a callout may go, nearest first: distance from the unit (page px)
# and direction. Straight up and down are left out: a callout reads along
# its leader, and stacks are columns beside the land.
CALLOUT_REACH = (8.0, 14.0, 22.0, 32.0, 44.0, 58.0, 74.0, 92.0, 112.0, 136.0,
                 164.0, 196.0, 232.0, 272.0, 316.0)
CALLOUT_DIRECTIONS = ((1, 0), (-1, 0), (1, -0.5), (1, 0.5), (-1, -0.5), (-1, 0.5),
                      (1, -1), (1, 1), (-1, -1), (-1, 1), (0.5, -1), (0.5, 1),
                      (-0.5, -1), (-0.5, 1))
# A last-resort callout may cover this much mapped land; a normal one none.
CALLOUT_MAX_COVER = 0.25
# Callouts whose units lie within this distance of each other share a stack.
STACK_NEAR = 64.0
# A leader longer than this crosses too much of the map to be followed: the
# unit tries its code inside, and a code callout, before a longer leader.
LEADER_MAX = 90.0
# A name on a leader this short beats a code inside the unit.
LEADER_SHORT = 80.0
# No leader is ever longer than this: past it a middling unit goes unlabelled.
LEADER_LIMIT = 110.0
STACK_GAP = 1.5
# A stack's column moves out from the land in these steps, and the whole
# stack up or down by these, until every label is in free space.
STACK_STEP_X = 6.0
STACK_STEP_Y = 5.0
STACK_SHIFT_MAX = 260.0
# Words that say what kind of unit it is; dropped from a label when the rest
# is still unique ("Lviv Oblast" -> "Lviv", but "Kyiv Oblast" stays, because
# Kyiv city is "Kyiv").
_KIND_WORDS = ("Oblast", "County", "Province", "Region", "Governorate", "Prefecture",
               "Voivodeship", "Department", "District", "Raion", "Municipality",
               "Parish", "Borough", "Census Area", "City and Borough")
_KIND_PREFIXES = ("Autonomous Republic of ",)
# German district names carry their status after a comma: "München, Kreisfreie
# Stadt". A label wants the place.
_KIND_SUFFIXES = (", Kreisfreie Stadt", ", Stadtkreis", ", Landkreis", ", Kreis",
                  ", Landeshauptstadt", ", Universitätsstadt", ", Stadt", ", Hansestadt",
                  ", Wissenschaftsstadt", ", Große Kreisstadt", ", Regionalverband")
# What a reader calls a country, where Natural Earth's name is long.
_COUNTRY_SHORT = {"United Arab Emirates": "UAE", "United States of America": "United States"}
MISSING_TEXT = "no data"


_CITY_KINDS = ("Kreisfreie Stadt", "Stadtkreis", "Stadt", "Landeshauptstadt",
               "Hansestadt", "Universitätsstadt", "Wissenschaftsstadt")


def _label_names(rows: list) -> dict:
    full = {str(r["id"]): _short_name(r) for r in rows}
    stripped, kinds = {}, {}
    for gid, name in full.items():
        short = name
        for word in _KIND_WORDS:
            for form in (f" {word}", f" {word.lower()}"):
                if short.endswith(form) and len(short) > len(form) + 1:
                    short = short[: -len(form)]
        for prefix in _KIND_PREFIXES:
            if short.startswith(prefix) and len(short) > len(prefix) + 1:
                short = short[len(prefix):]
        for suffix in _KIND_SUFFIXES:
            if short.endswith(suffix) and len(short) > len(suffix) + 1:
                short = short[: -len(suffix)]
                kinds[gid] = suffix[2:]
        stripped[gid] = _COUNTRY_SHORT.get(short, short)
    counts: dict = {}
    for short in stripped.values():
        counts[short] = counts.get(short, 0) + 1
    out = {}
    for gid in full:
        short = stripped[gid]
        if counts[short] == 1:
            out[gid] = short
            continue
        # "München, Kreisfreie Stadt" beside "München, Landkreis": the city
        # keeps the plain name, the district says what it is.
        cities = [g for g in full if stripped[g] == short and kinds.get(g) in _CITY_KINDS]
        if len(cities) == 1 and cities[0] == gid:
            out[gid] = short
        elif len(cities) == 1 and gid in kinds:
            out[gid] = f"{short} ({kinds[gid]})"
        else:
            out[gid] = full[gid]
    return out


def _abbreviation(row) -> str | None:
    """The short form a crowded label falls back to: a subdivision's code
    (WY), a country's ISO 3166 alpha-3 (CZE); None when there is none."""
    code = _code(row)
    if code:
        return code
    gid = str(row["id"])
    if len(gid) == 3 and gid.isalpha() and gid.isupper():
        return gid
    return None


def _code(row) -> str | None:
    """A subdivision's own short code, from an ISO 3166-2 id: US-WY -> WY."""
    head, _, tail = str(row["id"]).partition("-")
    if len(head) == 2 and 2 <= len(tail) <= 3 and tail.isalpha() and tail.isupper():
        return tail
    return None


def _value_text(value, encoding: Encoding) -> str:
    """The value as a label line: '$164k', '2.95M', '+205%'. A category is
    already named by its colour in the legend, so it gets no line."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    v = float(value)
    if encoding.mode == "symbols":
        return _short_count(v)
    prefix, suffix = _affixes(encoding.unit)
    scheme = encoding.scheme
    signed = scheme is not None and scheme.zero_at is not None
    a = abs(v)
    if a >= 1000:
        text = classify.nice_label(classify.round_sig(a, 3))
    elif encoding.decimals is not None:
        text = f"{a:.{encoding.decimals}f}"          # as many as the source has
    elif a >= 10 or a == 0:
        text = f"{a:.0f}"
    elif a >= 1 or suffix in ("%", " pp"):
        text = f"{a:.1f}"
    else:
        text = f"{a:.2g}"
    shown = text.strip("0.") != ""
    sides = _margin_sides(encoding.unit) if signed else None
    if sides and shown:
        lead = sides[0] if v > 0 else sides[1]
        return f"{lead}+{prefix}{text}{suffix}"
    sign = "−" if v < 0 and shown else ("+" if signed and v > 0 else "")
    return f"{sign}{prefix}{text}{suffix}"


def _short_count(v: float) -> str:
    for size, word in ((1e9, "bn"), (1e6, "M"), (1e3, "k")):
        if v >= size:
            return f"{v / size:.3g}{word}" if v / size < 100 else f"{v / size:.0f}{word}"
    return f"{v:,.0f}"


class _Block:
    """A label of one or two lines, centred on x; (w, h) in page pixels."""

    def __init__(self, lines: list):
        self.lines = [(text, size, weight) for text, size, weight in lines if text]
        self.w = max(_text_w(text, size) for text, size, _ in self.lines)
        self.h = sum(size for _, size, _ in self.lines) + 1.5 * (len(self.lines) - 1)

    def box(self, cx: float, top: float) -> tuple:
        return (cx - self.w / 2, top, cx + self.w / 2, top + self.h)

    def svg(self, cx: float, top: float, gid: str) -> str:
        out, y = [], top
        for text, size, weight in self.lines:
            y += size * 0.86
            bold = ' font-weight="600"' if weight else ""
            out.append(f'<text x="{_num(cx)}" y="{_num(y)}" font-size="{size:g}"{bold} '
                       f'text-anchor="middle" fill="{LABEL_INK}" {LABEL_HALO}>'
                       f'{escape(text)}</text>')
            y += size * 0.14 + 1.5
        return (f'<g class="feature-label" data-placement="inside" data-id={quoteattr(gid)}>'
                + "".join(out) + "</g>")


class _Line:
    """A callout: the name in bold and the value after it, on one line.

    One line, so a stack of them is a compact column a reader runs down; the
    name is never without its value, nor the value without its name.
    """

    def __init__(self, name: str, value: str, scale: float = 1.0):
        self.name, self.value = name, value
        self.size, self.vsize = NAME_SIZE * scale, VALUE_SIZE * scale
        # Bold runs a little wider than _text_w's regular estimate.
        self.w = _text_w(name, self.size) * 1.05 + (
            3.0 + _text_w(value, self.vsize) if value else 0.0)
        self.h = self.size + 1.5

    def box(self, x: float, top: float, side: int) -> tuple:
        """side > 0: the text starts at x; side < 0: it ends there."""
        return (x, top, x + self.w, top + self.h) if side > 0 else (x - self.w, top, x, top + self.h)

    def svg(self, x: float, top: float, side: int, gid: str,
            placement: str = "callout") -> str:
        anchor = "start" if side > 0 else "end"
        y = top + self.size * 0.86 + 0.5
        spans = f'<tspan font-weight="600">{escape(self.name)}</tspan>'
        if self.value:
            spans += f'<tspan dx="3" font-size="{_num(self.vsize)}">{escape(self.value)}</tspan>'
        return (f'<g class="feature-label" data-placement="{placement}" '
                f'data-id={quoteattr(gid)}>'
                f'<text x="{_num(x)}" y="{_num(y)}" font-size="{_num(self.size)}" '
                f'text-anchor="{anchor}" fill="{LABEL_INK}" {LABEL_HALO}>{spans}</text></g>')


class _Inline:
    """A one-line label centred inside its unit: Los Angeles is wide, not tall."""

    def __init__(self, line: _Line):
        self.line, self.w, self.h = line, line.w, line.h

    def box(self, cx: float, top: float) -> tuple:
        return (cx - self.w / 2, top, cx + self.w / 2, top + self.h)

    def svg(self, cx: float, top: float, gid: str) -> str:
        return self.line.svg(cx - self.w / 2, top, 1, gid, placement="inside")


class _Target:
    """One unit to be labelled, wherever its label ends up."""

    def __init__(self, gid, x, y, ring, holes, name, value, code, rank,
                 inset=None, r=0.0):
        self.gid, self.x, self.y, self.ring, self.holes = gid, x, y, ring, holes
        self.name, self.value, self.code = name, value, code
        self.rank = rank            # sort key: smaller is more important
        self.inset = inset          # the inset frame it is drawn in, if any
        self.r = r                  # a circle's radius: leaders leave its edge
        self.area = project.planar_area(ring) if ring else 0.0

    def blocks(self, code: bool = False):
        """Inside labels, most legible first: the name (or the code) and
        value on two lines, then on one, then each a size smaller."""
        name = self.code if code else self.name
        if not name:
            return
        words = name.split(" ")
        for scale in (1.0, SMALL):
            yield _Block([(name, NAME_SIZE * scale, True), (self.value, VALUE_SIZE * scale, False)])
            yield _Inline(_Line(name, self.value, scale))
        # A two-word name on two lines ("West" / "Virginia") before a code.
        if len(words) == 2 and not code:
            for scale in (1.0, SMALL):
                yield _Block([(words[0], NAME_SIZE * scale, True),
                              (words[1], NAME_SIZE * scale, True),
                              (self.value, VALUE_SIZE * scale, False)])

    def lines(self):
        yield _Line(self.name, self.value)
        if self.code:
            yield _Line(self.code, self.value)


def _fits_inside(box: tuple, ring, holes) -> bool:
    x0, y0, x1, y1 = box
    xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
    for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1), (xm, y0), (xm, y1),
                 (x0, ym), (x1, ym), (xm, ym)):
        if not project.point_in_ring(x, y, ring) or any(
                project.point_in_ring(x, y, h) for h in holes):
            return False
    return True


class _Space:
    """Where mapped land is, scanline by scanline, for testing label boxes.

    A callout belongs in free space -- sea, neighbouring land, the margin --
    never over another mapped unit, where it would be read as that unit's.
    Each outer ring's crossings with a horizontal line give the land
    intervals on it; edges are bucketed by height so a line meets only the
    edges near it. Rings are kept apart, so an enclave inside another unit's
    hole is still land.
    """

    BUCKET = 8.0

    def __init__(self, land: list, frame):
        self.frame = frame
        self.edges: dict = {}
        for n, (_, ring) in enumerate(land):
            count = len(ring)
            for i in range(count):
                (ax, ay), (bx, by) = ring[i], ring[(i + 1) % count]
                if ay == by:
                    continue
                for b in range(int(min(ay, by) // self.BUCKET),
                               int(max(ay, by) // self.BUCKET) + 1):
                    self.edges.setdefault(b, []).append((n, ax, ay, bx, by))
        self.cache: dict = {}

    def intervals(self, y: float) -> list:
        key = round(y * 2.0) / 2.0
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        crossings: dict = {}
        for n, ax, ay, bx, by in self.edges.get(int(key // self.BUCKET), ()):
            if (ay <= key < by) or (by <= key < ay):
                crossings.setdefault(n, []).append(ax + (key - ay) * (bx - ax) / (by - ay))
        spans = []
        for xs in crossings.values():
            xs.sort()
            spans.extend((xs[i], xs[i + 1]) for i in range(0, len(xs) - 1, 2))
        spans.sort()
        out: list = []
        for a, b in spans:           # the union: an enclave is not land twice
            if out and a <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], b))
            else:
                out.append((a, b))
        self.cache[key] = out
        return out

    def _rows(self, box):
        y0, y1 = box[1], box[3]
        n = max(2, int(math.ceil((y1 - y0) / 2.5)))
        return [y0 + (y1 - y0) * i / n for i in range(n + 1)]

    def cover(self, box) -> float:
        """Share of the box over mapped land, by scanline."""
        x0, x1 = box[0], box[2]
        width = max(x1 - x0, 1e-6)
        rows = self._rows(box)
        total = 0.0
        for y in rows:
            for a, b in self.intervals(y):
                if b > x0 and a < x1:
                    total += min(b, x1) - max(a, x0)
        return total / (width * len(rows))

    def free(self, box, pad: float = 1.5) -> bool:
        x0, x1 = box[0] - pad, box[2] + pad
        for y in self._rows((x0, box[1] - pad, x1, box[3] + pad)):
            for a, b in self.intervals(y):
                if b > x0 and a < x1:
                    return False
        if self.frame.outline is not None:
            return all(self.frame.visible(x, y) for x, y in (
                (box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])))
        return True


def _placer(frame: Frame, encoding: Encoding):
    """The collision test every label on the page shares, with the legend
    and the circles already in it."""
    placer = overlay_render.Labels({"pad": PAD, "map_w": MAP_W, "map_top": MAP_TOP}, frame)
    if frame.legend_box:
        placer.block(*frame.legend_box)
    for x, y, r, _ in encoding.circles:
        placer.block(x - r, y - r, x + r, y + r)
    return placer


def _missing_label(result, drawn_missing: list, insets: list) -> str:
    """What the hatch means, for the legend: the one reason every hatched
    unit shares when it is specific ("not estimated by Ukrstat since 2014"),
    else "no data"."""
    hatched = {str(m["id"]) for m in drawn_missing}
    hatched |= {str(m["id"]) for inset in insets for m in inset.missing}
    reasons = {d.get("reason") for d in result.dropped if str(d["id"]) in hatched}
    meanings = result.provenance.get("drop_reasons") or {}
    if len(reasons) != 1:
        return MISSING_TEXT
    reason = next(iter(reasons))
    meaning = str(meanings.get(reason) or "")
    if not meaning or reason in _REASONS or meaning in _REASONS.values():
        return MISSING_TEXT
    meaning = meaning.split(" (")[0].strip()
    return meaning if len(meaning) <= 44 else MISSING_TEXT


_TRACE: list = []


def _feature_labels(rows: list, insets: list, missing: list, frame: Frame,
                    encoding: Encoding, land: list, placer, every: int,
                    change: bool = False, fresh=None) -> str:
    """Name the units, each with its value. Greedy, deterministic.

    Name and value always print together, never one without the other:

    1. inside the unit, at its pole of inaccessibility, when the block fits
       within the polygon -- the full name, else the unit's code (WY);
    2. otherwise a callout in free space (sea, neighbours, the margin) on a
       leader line. Callouts for units near each other are stacked in one
       column, in the order of the units on the ground, the leaders not
       crossing;
    3. failing that, the code and value in a callout; then, for the highest
       and lowest only, a callout allowed over a little mapped land;
    4. otherwise the unit goes unlabelled, least important first -- never
       the extremes while there is any spot for them.

    Circles: under the circle, else beside it, else a callout as above.
    A map with more than LABEL_MAX_UNITS units names only its LABEL_EXTREMES
    highest and lowest. Hatched units get their name and "no data".
    Nothing overlaps another label, a circle, a marker, an inset or the legend.
    """
    names = _label_names(rows + [r for inset in insets for r in inset.rows]
                         + missing + [m for inset in insets for m in inset.missing])
    space = _Space(land, frame)
    extremes_only = every > LABEL_MAX_UNITS

    def attempt(into, codes: bool):
        out: list[str] = []
        unlabelled: list[str] = []
        leaders: list = []
        if encoding.mode == "symbols":
            pending = _circle_labels(encoding, names, into, out, codes)
        else:
            pending = _choropleth_labels(rows, insets, frame, encoding, names, into,
                                         out, extremes_only, change, codes)
        if not extremes_only:
            pending += _missing_labels(missing, insets, frame, names, into, out)
        _callouts(pending, into, space, frame, out, leaders, unlabelled, insets, codes)
        return out, unlabelled

    # One name style per map. Codes (WV, MS, ARE) are the fallback of a
    # crowded label, and a map that mixes "Maryland" with "RI" reads as
    # careless: try it with names only, and keep that unless it must drop
    # units the mixed form could label.
    if fresh is not None:
        plain_placer, mixed_placer = fresh(), fresh()
        plain = attempt(plain_placer, False)
        mixed = attempt(mixed_placer, True)
        _TRACE.append((sorted(plain[1]), sorted(mixed[1])))
        if len(plain[1]) <= len(mixed[1]):
            (out, unlabelled), chosen = plain, plain_placer
        else:
            (out, unlabelled), chosen = mixed, mixed_placer
        placer.__dict__.update(chosen.__dict__)
    else:
        out, unlabelled = attempt(placer, True)
    if not out:
        return ""
    dropped = " ".join(sorted(unlabelled))
    return (f'<g font-family="{FONT}" class="feature-labels" '
            f'data-labels-dropped="{len(unlabelled)}" data-unlabelled={quoteattr(dropped)}>'
            + "".join(out) + "</g>")


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _readers_extremes(units, valued, encoding, change: bool) -> set:
    """Which units a big map names: the ones a reader goes looking for.

    The highest and lowest first -- but a unit too small to see (the Vatican,
    the Northern Marianas) is no story on a map of *change*: a percentage of
    a few thousand people is noise. Then more from either end, down to
    LABEL_FILLERS_MIN_AREA px2 (a country, not a dot), so a map of 400
    districts names Frankfurt and Stuttgart and not only Berlin and Munich.
    """
    area = {str(row["id"]): project.planar_area(piece[2]) for row, _, piece in units}
    at = {str(row["id"]): piece[0] for row, _, piece in units}
    per_side = LABEL_PER_SIDE + len(valued) // LABEL_PER_SIDE_STEP
    floor = LABEL_MIN_AREA
    # A skewed ratio (density, income) has its story at the long end: the
    # other end keeps its extremes and no more. A diverging one (growth and
    # decline) is told from both.
    low_n = high_n = per_side
    scheme = encoding.scheme
    if scheme is not None and scheme.zero_at is None and len(valued) >= 3:
        numbers = [v for v, _ in valued]
        median = numbers[len(numbers) // 2]
        if numbers[-1] - median > 2.0 * (median - numbers[0]):
            low_n = LABEL_EXTREMES
        elif median - numbers[0] > 2.0 * (numbers[-1] - median):
            high_n = LABEL_EXTREMES

    def side(order, limit):
        picked = []
        for _, gid in order:
            if len(picked) >= limit:
                break
            big = area.get(gid, 0.0) >= floor
            primary = len(picked) < LABEL_EXTREMES
            apart = primary or all(
                math.hypot(at[gid][0] - at[o][0], at[gid][1] - at[o][1]) >= LABEL_APART
                for o in picked if o in at and gid in at)
            if (big and apart) or (not change and primary):
                picked.append(gid)
        return picked

    keep = set(side(valued, low_n)) | set(side(valued[::-1], high_n))
    # When the extremes sit in insets (Mayotte, Guyane), the main map's own
    # highest and lowest are named too: Severozapaden, not only the outermost
    # regions.
    main = [(v, gid) for v, gid in valued
            if any(str(r["id"]) == gid and inset is None for r, inset, _ in units)
            and (not change or area.get(gid, 0.0) >= floor)]
    if main:
        keep |= {main[0][1], main[-1][1]}
    return keep


def _choropleth_labels(rows, insets, frame, encoding, names, placer, out,
                       extremes_only, change=False, codes=True) -> list:
    """Inside labels; returns the targets still to be called out."""
    units = []          # (row, inset or None, piece)
    for row in rows:
        piece = _piece(row["geometry"], frame)
        if piece is not None:
            units.append((row, None, piece))
    for inset in insets:
        for row in inset.rows:
            piece = _piece(row["geometry"], inset)
            if piece is not None:
                units.append((row, inset, piece))
    # Markers for units too small to see are obstacles too.
    for row, _, piece in units:
        if project.planar_area(piece[2]) < MIN_VISIBLE_AREA:
            (x, y) = piece[0]
            placer.block(x - MARKER_R, y - MARKER_R, x + MARKER_R, y + MARKER_R)
    for inset in insets:
        bx, by, bw, bh = inset.box
        placer.block(bx, by, bx + bw, by + bh)

    # Importance: the highest and lowest first, then by how far from the
    # median, then the larger unit. What is dropped, if anything, is the
    # middling and small.
    valued = sorted(((v, str(row["id"])) for row, _, _ in units
                     if (v := _number(row["value"])) is not None))
    order = {gid: i for i, (_, gid) in enumerate(valued)}
    last = max(1, len(valued) - 1)

    def rank(row, piece):
        gid = str(row["id"])
        if gid not in order:
            return (2, 1.0, 0.0, gid)
        i = order[gid]
        # On a big map every unit left was chosen on purpose: each may be
        # called out over a little land, as the extremes may.
        extreme = i == 0 or i == len(valued) - 1 or extremes_only
        return (0 if extreme else 1, -abs(i / last - 0.5),
                -project.planar_area(piece[2]), gid)

    if extremes_only:
        keep = _readers_extremes(units, valued, encoding, change)
        units = [u for u in units if str(u[0]["id"]) in keep]

    targets = []
    for row, inset, piece in units:
        (x, y), _, ring, holes = piece
        gid = str(row["id"])
        targets.append(_Target(gid, x, y, ring, holes, names[gid],
                               _value_text(row["value"], encoding),
                               _abbreviation(row) if codes else None,
                               rank(row, piece), inset=inset))
    pending = []
    insets_here = list(insets)
    for t in sorted(targets, key=lambda t: (-t.area, t.gid)):
        if not _inside_label(t.blocks(), t.x, t.y, t.ring, t.holes, placer, insets_here,
                             out, t.gid):
            pending.append(t)
    return pending


def _missing_labels(missing, insets, frame, names, placer, out) -> list:
    """Hatched units: their name and "no data", inside or called out.

    Their outline is redrawn over the hatch, so two adjacent units with no
    data (Crimea and Sevastopol) read as two.
    """
    pending = []
    for owner, items in [(None, missing)] + [(i, i.missing) for i in insets]:
        for item in sorted(items, key=lambda m: str(m["id"])):
            where = frame if owner is None else owner
            piece = _piece(item["geometry"], where)
            if piece is None:
                continue
            (x, y), _, ring, holes = piece
            gid = str(item["id"])
            parts = _project(item["geometry"], where)
            d = _d([part[0] for part in parts])
            if d:
                out.append(f'<path d="{d}" fill="none" stroke="{colors.TEXT_MUTED}" '
                           f'stroke-width="0.7" class="missing-outline" '
                           f'data-outline-of={quoteattr(gid)}/>')
            t = _Target(gid, x, y, ring, holes, names.get(gid, _short_name(item)),
                        MISSING_TEXT, None, (3, 0.0, -project.planar_area(ring), gid),
                        inset=owner)
            if not _inside_label(t.blocks(), x, y, ring, holes, placer, insets, out, gid):
                pending.append(t)
    return pending


def _circle_labels(encoding, names, placer, out, codes=True) -> list:
    """Under the circle, else beside it; returns the circles left to call out."""
    pending = []
    circles = sorted(encoding.circles, key=lambda c: (-c[2], str(c[3]["id"])))
    for n, (x, y, r, row) in enumerate(circles):
        gid = str(row["id"])
        block = _Block([(names.get(gid, _short_name(row)), NAME_SIZE, True),
                        (_value_text(row["value"], encoding), VALUE_SIZE, False)])
        spots = [(x, y + r + 2.0),                                  # under
                 (x + r + 3 + block.w / 2, y - block.h / 2),        # right
                 (x - r - 3 - block.w / 2, y - block.h / 2),        # left
                 (x, y - r - 2.0 - block.h)]                        # over
        for cx, top in spots:
            box = block.box(cx, top)
            if placer.fits(box):
                placer.block(*box)
                out.append(block.svg(cx, top, gid))
                break
        else:
            # Largest circle first is most important.
            pending.append(_Target(gid, x, y, None, [], names.get(gid, _short_name(row)),
                                   _value_text(row["value"], encoding),
                                   _code(row) if codes else None,
                                   (0 if n == 0 else 1, n, 0.0, gid), r=r))
    return pending


# --------------------------------------------------------------- callouts ---

def _callouts(targets, placer, space, frame, out, leaders, unlabelled, insets=(),
              codes=True) -> None:
    """Callouts in free space: stacked where units crowd, single where not.

    In order, each unit not yet labelled tries: its name on a short leader
    (stacked with its neighbours'); its code inside the unit (WV, HRV); its
    name on a leader up to LEADER_MAX; its code on one; its name, then its
    code, on one up to LEADER_LIMIT; a callout over a little mapped land;
    and, for the highest and lowest only, over as much land as it takes.
    A hatched unit, being no data, gets no more than LEADER_MAX.
    """
    targets = sorted(targets, key=lambda t: t.rank)
    rest = [t for t in targets if t.inset is not None]
    # A unit with a code falls back to it inside before a long leader; one
    # without goes straight to the longer leaders, so a cluster such as the
    # Bay Area stays one stack in one order.
    shed = _stack_phase([t for t in targets if t.inset is None],
                        lambda t: LEADER_SHORT if t.code else LEADER_MAX,
                        placer, space, frame, out, leaders)
    later = []
    for t in shed:
        if not (t.code and t.ring and _inside_label(
                t.blocks(code=True), t.x, t.y, t.ring, t.holes, placer, insets, out, t.gid)):
            later.append(t)
    rest += _stack_phase(later, lambda t: LEADER_MAX, placer, space, frame, out, leaders)

    for t in sorted(rest, key=lambda t: t.rank):
        if t.inset is not None:
            if not (_inside_label(t.blocks(code=True), t.x, t.y, t.ring, t.holes, placer,
                                  insets, out, t.gid) or _inset_corner(t, placer, out)):
                unlabelled.append(t.gid)
            continue
        hatched = t.rank[0] == 3
        placed = False
        attempts = [(True, LEADER_MAX)] if t.code else []
        if not hatched:
            attempts += [(False, LEADER_LIMIT)] + ([(True, LEADER_LIMIT)] if t.code else [])
        for use_code, limit in attempts:
            line = _Line(t.code, t.value) if use_code else _Line(t.name, t.value)
            spot = _nearest_spot(t, line, placer, space)
            if spot is not None and not _stack([t], spot[0], placer, space, frame, out,
                                               leaders, limit, use_code=use_code):
                placed = True
                break
        # The last resort, for the highest and lowest only: a callout over a
        # little mapped land, then over as much as it takes. A middling unit
        # goes unlabelled rather than have its name sit on a neighbour.
        # Without codes to fall back on (one name style), a middling unit may
        # also be called out over a little land.
        covers = ((CALLOUT_MAX_COVER, 1.0) if t.rank[0] == 0
                  else (CALLOUT_MAX_COVER,) if not codes else ())
        for cover in covers:
            for line in t.lines():
                if placed:
                    break
                placed = _radial(t, line, cover, placer, space, out, leaders)
        if not placed:
            unlabelled.append(t.gid)


def _stack_phase(targets, max_leader, placer, space, frame, out, leaders) -> list:
    """Stack every target that has free space within reach; return the rest."""
    rest, spots = [], {}
    for t in targets:
        spot = _nearest_spot(t, _Line(t.name, t.value), placer, space)
        if spot is None or spot[1] > max_leader(t):
            rest.append(t)
        else:
            spots[t.gid] = spot
    for group in _stack_groups([t for t in targets if t.gid in spots], spots):
        side = spots[group[0].gid][0]
        # Runs of one group start from one column, so its callouts align.
        start = (min(t.x + t.r for t in group) + 8.0 if side > 0
                 else max(t.x - t.r for t in group) - 8.0)
        for run in _runs(group):
            # One stack, one form: if the names do not all fit, codes for all.
            codes = all(t.code for t in run) and len(run) > 1 and bool(_stack(
                run, side, placer, space, frame, [], [], max_leader, dry=True, start=start))
            rest.extend(_stack(run, side, placer, space, frame, out, leaders, max_leader,
                               use_code=codes, start=start))
    return sorted(rest, key=lambda t: t.rank)


def _runs(group) -> list:
    """A group split where its stacked labels would not touch: each run gets
    its own column, hugging its own stretch of coast."""
    members = sorted(group, key=lambda t: (t.y, t.x, t.gid))
    h = _Line("x", "").h
    pitch = h + STACK_GAP
    tops = _stack_tops([t.y - h / 2 for t in members], pitch)
    runs, run = [], [members[0]]
    for i in range(1, len(members)):
        if tops[i] - tops[i - 1] > pitch + 0.01:
            runs.append(run)
            run = []
        run.append(members[i])
    runs.append(run)
    return runs


def _nearest_spot(t, line, placer, space):
    """(side, distance) of the nearest box in free space for this label."""
    for reach in CALLOUT_REACH:
        for dx, dy in CALLOUT_DIRECTIONS:
            if dx == 0:
                continue
            norm = math.hypot(dx, dy)
            ex, ey = t.x + dx / norm * (t.r + reach), t.y + dy / norm * (t.r + reach)
            side = 1 if dx > 0 else -1
            box = line.box(ex + side * 2.0, ey - line.h / 2, side)
            if placer.fits(box) and space.free(box):
                return side, reach
    return None


def _stack_groups(targets, spots) -> list:
    """Targets on the same side whose units are near each other, as groups.

    Single linkage over STACK_NEAR px, members in their sorted order; groups
    ordered by their most important member.
    """
    parent = list(range(len(targets)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(targets):
        for j in range(i):
            b = targets[j]
            if spots[a.gid][0] == spots[b.gid][0] and \
                    math.hypot(a.x - b.x, a.y - b.y) <= STACK_NEAR:
                parent[find(i)] = find(j)
    groups: dict = {}
    for i, t in enumerate(targets):
        groups.setdefault(find(i), []).append(t)
    return sorted(groups.values(), key=lambda g: min(t.rank for t in g))


def _stack_tops(desired: list, pitch: float) -> list:
    """Label tops as near their desired heights as a column without overlap
    allows: neighbours that collide merge into a block centred on them."""
    blocks: list = []
    for i, d in enumerate(desired):
        blocks.append([i, 1, d])
        while len(blocks) > 1:
            a, b = blocks[-2], blocks[-1]
            if a[2] + a[1] * pitch <= b[2]:
                break
            start, count = a[0], a[1] + b[1]
            top = sum(desired[j] - (j - start) * pitch
                      for j in range(start, start + count)) / count
            blocks[-2:] = [[start, count, top]]
    return [blk[2] + (j - blk[0]) * pitch for blk in blocks
            for j in range(blk[0], blk[0] + blk[1])]


def _leader_start(t, ex, ey):
    if t.r <= 0:
        return t.x, t.y
    norm = math.hypot(ex - t.x, ey - t.y) or 1.0
    return t.x + (ex - t.x) / norm * t.r, t.y + (ey - t.y) / norm * t.r


def _uncross(members, ends) -> list:
    """Slot order for the members such that no two leaders cross."""
    slots = list(range(len(members)))
    for _ in range(len(members) ** 2 + 1):
        segs = []
        for m, s in zip(members, slots):
            ex, ey = ends[s]
            sx, sy = _leader_start(m, ex, ey)
            segs.append((sx, sy, ex, ey))
        swap = None
        for i in range(len(segs)):
            for j in range(i):
                if _segments_cross(segs[i], segs[j]):
                    swap = (i, j)
                    break
            if swap:
                break
        if swap is None:
            return slots
        i, j = swap
        slots[i], slots[j] = slots[j], slots[i]
    return slots


def _stack(group, side, placer, space, frame, out, leaders, max_leader=None,
           use_code=False, dry=False, start=None) -> list:
    """Lay a group of callouts out as one column in free space.

    The column starts where the nearest member's label first fits and moves
    out from the land; at each column the stack, ordered as the units are
    from north to south, is tried at its natural height and then shifted up
    and down. The placement with the shortest total leader length wins. A
    group that fits nowhere sheds its least important member and tries
    again; the members shed are returned.
    """
    members = sorted(group, key=lambda t: (t.y, t.x, t.gid))

    def limit(t):
        return max_leader(t) if callable(max_leader) else max_leader
    lines = {t.gid: (_Line(t.code or t.name, t.value) if use_code else _Line(t.name, t.value))
             for t in members}
    h = max(line.h for line in lines.values())
    pitch = h + STACK_GAP
    base = _stack_tops([t.y - h / 2 for t in members], pitch)
    if side > 0:
        start = min(t.x + t.r for t in members) + 8.0 if start is None else start
        stop = placer.right
    else:
        start = max(t.x - t.r for t in members) - 8.0 if start is None else start
        stop = placer.left
    shifts = [0.0]
    k = 1
    while k * STACK_STEP_Y <= STACK_SHIFT_MAX:
        shifts += [-k * STACK_STEP_Y, k * STACK_STEP_Y]
        k += 1

    best = None
    cx = start
    while (cx <= stop) if side > 0 else (cx >= stop):
        reach = sum(abs(cx - t.x) for t in members)
        if best is not None and reach >= best[0]:
            break
        if max_leader is not None and any(abs(cx - t.x) > limit(t) for t in members):
            break
        for shift in shifts:
            tops = [top + shift for top in base]
            if tops[0] < placer.top or tops[-1] + h > placer.bottom:
                continue
            ends = [(cx - side * 1.5, top + h / 2) for top in tops]
            slots = _uncross(members, ends)
            boxes, segs, ok = [], [], True
            for t, s in zip(members, slots):
                box = lines[t.gid].box(cx, tops[s], side)
                if not (placer.fits(box) and space.free(box)):
                    ok = False
                    break
                ex, ey = ends[s]
                sx, sy = _leader_start(t, ex, ey)
                boxes.append(box)
                segs.append((sx, sy, ex, ey))
            if not ok:
                continue
            if max_leader is not None and any(
                    math.hypot(g[2] - g[0], g[3] - g[1]) > limit(t)
                    for g, t in zip(segs, members)):
                continue
            if any(_leader_blocked(seg, placer.taken + boxes, leaders) for seg in segs):
                continue
            score = sum(math.hypot(s[2] - s[0], s[3] - s[1]) for s in segs)
            if best is None or score < best[0] - 1e-9:
                best = (score, cx, tops, slots, boxes, segs)
            break       # the smallest shift that works at this column
        cx += side * STACK_STEP_X

    if best is None:
        if len(members) == 1:
            return members
        weakest = max(members, key=lambda t: t.rank)
        if dry:
            return [weakest]
        shed = _stack([t for t in members if t is not weakest], side, placer, space, frame,
                      out, leaders, max_leader, use_code, start=start)
        return shed + [weakest]

    if dry:
        return []
    _, cx, tops, slots, boxes, segs = best
    for t, s, box, seg in zip(members, slots, boxes, segs):
        placer.block(*box)
        leaders.append(seg)
        out.append(_leader_svg(seg, t))
        out.append(lines[t.gid].svg(cx, tops[s], side, t.gid))
    return []


def _leader_svg(seg, t) -> str:
    sx, sy, ex, ey = seg
    dot = ("" if t.r > 0 else
           f'<circle cx="{_num(sx)}" cy="{_num(sy)}" r="1.3" fill="{LEADER}" '
           'stroke="#ffffff" stroke-width="0.5" class="leader-end"/>')
    return (f'<line x1="{_num(sx)}" y1="{_num(sy)}" x2="{_num(ex)}" y2="{_num(ey)}" '
            f'stroke="{LEADER}" stroke-width="0.6" class="leader"/>' + dot)


def _radial(t, line, max_cover, placer, space, out, leaders) -> bool:
    """A single callout, nearest first, over at most `max_cover` mapped land."""
    for reach in (r for r in CALLOUT_REACH if r <= LEADER_LIMIT):
        for dx, dy in CALLOUT_DIRECTIONS:
            if dx == 0:
                continue
            norm = math.hypot(dx, dy)
            ex, ey = t.x + dx / norm * (t.r + reach), t.y + dy / norm * (t.r + reach)
            side = 1 if dx > 0 else -1
            x = ex + side * 1.5
            box = line.box(x, ey - line.h / 2, side)
            if not placer.fits(box) or space.cover(box) > max_cover:
                continue
            seg = (*_leader_start(t, ex, ey), ex, ey)
            if _leader_blocked(seg, placer.taken, leaders):
                continue
            placer.block(*box)
            leaders.append(seg)
            out.append(_leader_svg(seg, t))
            out.append(line.svg(x, ey - line.h / 2, side, t.gid))
            return True
    return False


def _inset_corner(t, placer, out) -> bool:
    """A unit in an inset that cannot hold its label: name and value along
    the inset's foot, inside its frame (the caption names the inset, not the
    value)."""
    bx, by, bw, bh = t.inset.box
    for line in t.lines():
        top = by + bh - line.h - 3.0
        box = line.box(bx + 5.0, top, 1)
        if box[2] > bx + bw - 3:
            continue
        frame_box = (bx, by, bx + bw, by + bh)
        saved = placer.taken
        placer.taken = [b for b in saved if b != frame_box]
        try:
            ok = placer.fits(box)
        finally:
            placer.taken = saved
        if ok:
            placer.block(*box)
            out.append(line.svg(bx + 5.0, top, 1, t.gid))
            return True
    return False


def _leader_blocked(segment, boxes, leaders) -> bool:
    """Whether a leader line would cross a placed label or another leader."""
    sx, sy, ex, ey = segment
    for box in boxes:
        if box[0] - 0.5 <= sx <= box[2] + 0.5 and box[1] - 0.5 <= sy <= box[3] + 0.5:
            continue    # the unit's own circle or marker, which the leader leaves
        if _segment_hits_box(sx, sy, ex, ey, box):
            return True
    return any(_segments_cross(segment, other) for other in leaders)


def _segment_hits_box(x0, y0, x1, y1, box) -> bool:
    bx0, by0, bx1, by1 = box
    if max(x0, x1) < bx0 or min(x0, x1) > bx1 or max(y0, y1) < by0 or min(y0, y1) > by1:
        return False
    for t in (i / 12.0 for i in range(13)):
        x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        if bx0 <= x <= bx1 and by0 <= y <= by1:
            return True
    return False


def _segments_cross(a, b) -> bool:
    def side(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1 = side(b[0], b[1], b[2], b[3], a[0], a[1])
    d2 = side(b[0], b[1], b[2], b[3], a[2], a[3])
    d3 = side(a[0], a[1], a[2], a[3], b[0], b[1])
    d4 = side(a[0], a[1], a[2], a[3], b[2], b[3])
    return (d1 * d2 < 0) and (d3 * d4 < 0)


# Where around a unit's pole of inaccessibility an inside label may be
# centred, as shares of the label's width and height: the pole is the centre
# of the largest inscribed circle, and a wide label often fits a little off it.
INSIDE_SHIFTS = ((0, 0), (0, -0.5), (0, 0.5), (-0.25, 0), (0.25, 0),
                 (-0.25, -0.5), (0.25, -0.5), (-0.25, 0.5), (0.25, 0.5))


def _inside_label(blocks, x, y, ring, holes, placer, insets, out, gid) -> bool:
    """The first block, at the first shift, that fits inside the unit and is clear."""
    ring = project._decimate(ring, 400)
    for block in blocks:
        for sx, sy in INSIDE_SHIFTS:
            cx, top = x + sx * block.w, y - block.h / 2 + sy * block.h
            box = block.box(cx, top)
            # Inside labels may sit on an inset's frame only in their own inset.
            if _fits_inside(box, ring, holes) and _clear_but_insets(placer, box, insets):
                placer.block(*box)
                out.append(block.svg(cx, top, gid))
                return True
    return False


def _clear_but_insets(placer, box, insets) -> bool:
    if placer.fits(box):
        return True
    # A unit drawn in an inset is labelled inside it: its own frame is not an
    # obstacle to it.
    saved = placer.taken
    frames = {(i.box[0], i.box[1], i.box[0] + i.box[2], i.box[1] + i.box[3]) for i in insets}
    placer.taken = [b for b in saved if b not in frames]
    try:
        return placer.fits(box) and any(
            f[0] <= box[0] and box[2] <= f[2] and f[1] <= box[1] and box[3] <= f[3]
            for f in frames)
    finally:
        placer.taken = saved


# ---------------------------------------------------------- context names ---

# Names that orient the reader -- seas, neighbouring countries, rivers --
# set muted and placed after every data label, so they never cover one.
CONTEXT_INK = "#7b8288"
SEA_INK = "#5b7d93"
RIVER_INK = "#4d7ea6"
CONTEXT_HALO = ('stroke="#ffffff" stroke-width="2.4" stroke-linejoin="round" '
                'stroke-opacity="0.7" paint-order="stroke"')
SEA_SIZE, COUNTRY_SIZE, RIVER_SIZE = 10.0, 8.5, 8.5
# The largest Natural Earth min_label (the zoom a name first appears at)
# named on a map whose ground span is at least this many degrees.
SEA_MIN_LABEL = ((100.0, 2.0), (45.0, 4.0), (25.0, 5.0), (0.0, 6.0))
SEA_LABELS, COUNTRY_LABELS, RIVER_LABELS = 5, 10, 5
# A sea or country shows less than this much of itself on the page: unnamed.
CONTEXT_MIN_PX2 = 1500.0
RIVER_MIN_PX = 70.0
RIVER_MAJOR = 5


def _ground_span(frame) -> float:
    west, south, east, north = frame.extent
    return max((east - west) * math.cos(math.radians((south + north) / 2.0)), north - south)


def _page_rings(geometry, frame) -> list:
    """Outer rings of a geometry, projected and clipped to the map's page box."""
    page = (PAD, MAP_TOP, PAD + MAP_W, frame.bottom)
    rings = []
    for part in _project(geometry, frame):
        ring = project.clip_to_extent(part[0], page)
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def _spiral(step: float, radius: float):
    """Offsets around a point, nearest first, in a fixed order."""
    n = int(radius // step)
    points = [(i * step, j * step) for i in range(-n, n + 1) for j in range(-n, n + 1)
              if i * i + j * j <= n * n]
    points.sort(key=lambda p: (p[0] * p[0] + p[1] * p[1], p[1], p[0]))
    return points


def _context_labels(result, rows, frame, encoding, manifest, placer, land) -> tuple:
    """Muted names for the seas, neighbouring countries and major rivers.

    Returns the SVG and any footnote credit it needs. Every name goes where
    no data label, leader, circle, legend or inset is, and seas only over
    open water.
    """
    out: list[str] = []
    credits: list[str] = []
    context_land = []
    for item in result.context:
        for ring in _page_rings(item["geometry"], frame):
            xs, ys = [p[0] for p in ring], [p[1] for p in ring]
            context_land.append(((min(xs), min(ys), max(xs), max(ys)), ring))
    water = _Space(land + context_land, frame)
    mapped = _Space(land, frame)

    seas = _sea_labels(result.names, frame, placer, water, out)
    if seas:
        credits.append(f"Sea names: {result.provenance.get('names_source') or 'Natural Earth'}.")
    _country_labels(result, rows, frame, encoding, manifest, placer, mapped, out)
    _river_labels(result.physical, frame, placer, out)
    if not out:
        return "", credits
    return (f'<g font-family="{FONT}" class="context-labels">' + "".join(out) + "</g>",
            credits)


def _sea_labels(names, frame, placer, water, out) -> int:
    span = _ground_span(frame)
    limit = next(m for floor, m in SEA_MIN_LABEL if span >= floor)
    placed = 0
    for sea in sorted(names, key=lambda n: (n.get("rank", 9), n["name"])):
        if placed >= SEA_LABELS:
            break
        if sea.get("kind") != "sea" or float(sea.get("rank", 9)) > limit:
            continue
        rings = _page_rings(sea["geometry"], frame)
        if not rings:
            continue
        ring = max(rings, key=project.planar_area)
        if project.planar_area(ring) < CONTEXT_MIN_PX2:
            continue
        name = sea["name"]
        words = name.split()
        lines = [name]
        if len(words) >= 3 or (len(words) == 2 and _text_w(name, SEA_SIZE) > 90):
            cut = (len(words) + 1) // 2
            lines = [" ".join(words[:cut]), " ".join(words[cut:])]
        w = max(_text_w(line, SEA_SIZE) for line in lines) * 1.04
        h = SEA_SIZE * len(lines) + 2.0 * (len(lines) - 1)
        thin = project._decimate(ring, 300)
        px, py = project.pole_of_inaccessibility(thin, 2.0)
        for dx, dy in _spiral(10.0, 220.0):
            cx, cy = px + dx, py + dy
            box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            if not all(project.point_in_ring(x, y, thin) for x, y in (
                    (box[0], box[1]), (box[2], box[1]), (box[0], box[3]),
                    (box[2], box[3]), (cx, cy))):
                continue
            if not (placer.fits(box) and water.free(box, pad=4.0)):
                continue
            placer.block(*box)
            spans = []
            for i, line in enumerate(lines):
                y = box[1] + SEA_SIZE * 0.86 + i * (SEA_SIZE + 2.0)
                spans.append(f'<text x="{_num(cx)}" y="{_num(y)}" font-size="{SEA_SIZE:g}" '
                             f'font-style="italic" text-anchor="middle" fill="{SEA_INK}" '
                             f'{CONTEXT_HALO}>{escape(line)}</text>')
            out.append(f'<g class="context-label" data-kind="sea">' + "".join(spans) + "</g>")
            placed += 1
            break
    return placed


def _country_labels(result, rows, frame, encoding, manifest, placer, mapped, out) -> int:
    """Neighbouring countries on a `within` map, and the countries of a plain
    country base map under an overlay, in small grey capitals."""
    candidates = []
    anchors = []
    for row in rows[:400]:
        piece = _piece(row["geometry"], frame)
        if piece is not None:
            anchors.append(piece[0])
    for item in result.context:
        name = str(item.get("name") or "")
        rings = _page_rings(item["geometry"], frame)
        if not name or not rings:
            continue
        # The country the mapped units belong to (the USA around California)
        # is not a neighbour.
        if any(project.point_in_ring(ax, ay, ring) for ring in rings for ax, ay in anchors[:5]):
            continue
        ring = max(rings, key=project.planar_area)
        label = item.get("label")
        candidates.append((-project.planar_area(ring), name, ring, label, False))
    if encoding.mode == "basemap" and manifest.level == "admin_0" and (
            result.overlays or result.places):
        for row in rows:
            rings = _page_rings(row["geometry"], frame)
            name = _short_name(row) if row.get("name") else ""
            if rings and name:
                ring = max(rings, key=project.planar_area)
                candidates.append((-project.planar_area(ring), name, ring, None, True))
    candidates.sort(key=lambda c: (c[0], c[1]))
    placed = 0
    for neg_area, name, ring, label, own in candidates:
        if placed >= COUNTRY_LABELS or -neg_area < CONTEXT_MIN_PX2:
            break
        text = name.upper()
        w = _text_w(text, COUNTRY_SIZE) * 1.18 + 2
        h = COUNTRY_SIZE
        thin = project._decimate(ring, 300)
        starts = []
        if label:
            lx, ly = frame.xy(project._shortest(label[0] - frame.lon0) + frame.lon0, label[1])
            if frame.visible(lx, ly) and project.point_in_ring(lx, ly, thin):
                starts.append((lx, ly))
        starts.append(project.pole_of_inaccessibility(thin, 2.0))
        done = False
        for px, py in starts:
            for dx, dy in _spiral(8.0, 120.0):
                cx, cy = px + dx, py + dy
                box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
                if not _fits_inside(box, thin, []) or not placer.fits(box):
                    continue
                if not own and not mapped.free(box, pad=2.0):
                    continue
                placer.block(*box)
                out.append(f'<g class="context-label" data-kind="country">'
                           f'<text x="{_num(cx)}" y="{_num(cy + h * 0.35)}" '
                           f'font-size="{COUNTRY_SIZE:g}" letter-spacing="1" '
                           f'text-anchor="middle" fill="{CONTEXT_INK}" {CONTEXT_HALO}>'
                           f'{escape(text)}</text></g>')
                placed += 1
                done = True
                break
            if done:
                break
    return placed


def _river_labels(layers, frame, placer, out) -> int:
    """Major rivers, named along a straight reach of each: those the overlay
    crosses first, then by Natural Earth's rank, then the longest on the page."""
    runs: dict = {}
    ranks: dict = {}
    for layer in layers or []:
        if layer.get("class") != "rivers" or layer.get("kind") != "lines":
            continue
        for feature in layer.get("features", []):
            name = str(feature.get("name") or "").strip()
            if not name:
                continue
            ranks[name] = min(ranks.get(name, 9.0), float(feature.get("rank", 9.0)))
            for run in _physical_line(feature.get("coords") or [], frame):
                visible = [p for p in run if frame.visible(*p)]
                if len(visible) >= 2:
                    runs.setdefault(name, []).append(visible)

    def length(run):
        return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(run, run[1:]))

    def served(rs):
        # A river the overlay crosses (the Niemen at Kowno, the Loire at
        # Tours) is part of its story; it is named before a longer one.
        inked = getattr(placer, "_inked", None)
        return inked is not None and any(inked((x - 4, y - 4, x + 4, y + 4))
                                         for r in rs for x, y in r[::2])

    ranked = sorted(((not served(rs), math.floor(ranks.get(name, 9.0)),
                      -sum(length(r) for r in rs), name) for name, rs in runs.items()))
    placed = 0
    for unserved, rank, neg_total, name in ranked:
        total = -neg_total
        if placed >= RIVER_LABELS:
            break
        # Major: Natural Earth shows it by zoom 5, or the overlay crosses a
        # long reach of it. The Mūša is neither.
        if total < RIVER_MIN_PX or (rank > RIVER_MAJOR and (unserved or total < 2 * RIVER_MIN_PX)):
            continue
        w = _text_w(name, RIVER_SIZE) * 1.02
        h = RIVER_SIZE
        spot = None
        for run in sorted(runs[name], key=lambda r: (-length(r), r[0])):
            spot = _river_spot(run, w, h, frame, placer)
            if spot:
                break
        if spot is None:
            continue
        cx, cy, angle, box = spot
        placer.block(*box)
        out.append(f'<g class="context-label" data-kind="river">'
                   f'<text x="{_num(cx)}" y="{_num(cy + h * 0.35)}" font-size="{RIVER_SIZE:g}" '
                   f'font-style="italic" text-anchor="middle" fill="{RIVER_INK}" '
                   f'transform="rotate({_num(angle)} {_num(cx)} {_num(cy)})" '
                   f'{CONTEXT_HALO}>{escape(name)}</text></g>')
        placed += 1
    return placed


def _river_spot(run, w, h, frame, placer):
    """A straight reach of the river at least as long as the name, middle
    first, with the name set just above it."""
    n = len(run)
    candidates = []
    for i in range(n - 1):
        j, chord = i + 1, 0.0
        while j < n:
            chord = math.hypot(run[j][0] - run[i][0], run[j][1] - run[i][1])
            if chord >= w + 6:
                break
            j += 1
        if j >= n:
            break
        (ax, ay), (bx, by) = run[i], run[j]
        bend = max((math.sqrt(project._segment_distance_sq(px, py, ax, ay, bx, by))
                    for px, py in run[i + 1:j]), default=0.0)
        if bend > 4.0:
            continue
        angle = math.degrees(math.atan2(by - ay, bx - ax))
        if angle > 90:
            angle -= 180
        elif angle <= -90:
            angle += 180
        if abs(angle) > 65:
            continue
        rad = math.radians(angle)
        # Beside the line, clear of its bends: above it first, then below.
        nx, ny = math.sin(rad), -math.cos(rad)
        for k, sign in enumerate((1, -1)):
            off = sign * (h / 2 + 1.5 + bend)
            cx = (ax + bx) / 2 + nx * off
            cy = (ay + by) / 2 + ny * off
            candidates.append((round(bend), abs(i - n / 2), k, i, cx, cy, angle))
    candidates.sort()
    for *_, cx, cy, angle in candidates:
        rad = math.radians(angle)
        hw = abs(math.cos(rad)) * w / 2 + abs(math.sin(rad)) * h / 2
        hh = abs(math.sin(rad)) * w / 2 + abs(math.cos(rad)) * h / 2
        box = (cx - hw, cy - hh, cx + hw, cy + hh)
        if not all(frame.visible(x, y) for x, y in ((box[0], box[1]), (box[2], box[3]))):
            continue
        if placer.fits(box):
            return cx, cy, angle, box
    return None


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
    water = (f'<path d="{_d([frame.outline])}" fill="{colors.WATER}" stroke="none"/>'
             if frame.outline is not None else
             f'<rect x="{PAD}" y="{MAP_TOP}" width="{MAP_W}" height="{frame.map_h}" '
             f'fill="{colors.WATER}"/>')
    return (
        water +
        f'<path d="{"".join(lines)}" fill="none" stroke="{colors.GRATICULE}" '
        'stroke-width="0.5" clip-path="url(#map-clip)"/>'
    )


# ---------------------------------------------------------------- legend ---

def _text_w(text: str, size: float) -> float:
    """Rough advance width; there are no font metrics in a bare SVG."""
    return len(text) * size * 0.56


def _wrap_words(text: str, size: float, width: float) -> list[str]:
    """Greedy wrap of `text` into lines no wider than `width`; a " · " that
    falls in the last 45% of a line is preferred to a plain space as the break."""
    lines: list[str] = []
    rest = text.strip()
    while rest:
        if _text_w(rest, size) <= width:
            lines.append(rest)
            break
        limit = max(10, int(width / (size * 0.56)))
        dot = rest.rfind(" · ", 0, limit + 1)
        space = rest.rfind(" ", 0, limit + 1)
        if dot >= limit * 0.55:
            first, rest = rest[:dot], rest[dot + 3:]
        elif space > 0:
            first, rest = rest[:space], rest[space + 1:]
        else:
            first, rest = rest[:limit], rest[limit:]
        lines.append(first.rstrip())
        rest = rest.lstrip()
    return lines


def _subtitle_lines(subtitle: str | None) -> list[tuple[float, float, str]]:
    """(baseline y, font size, text) for the subtitle, never wider than the map.

    One line at 12.5 px if it fits, else one at 11.5, else two or three lines
    wrapped at a separator or a space. A sentence is never cut short with an
    ellipsis: a subtitle exists to carry the caveats ("state popular vote, not
    electoral votes"), and the caveat is the part that would be lost.
    """
    if not subtitle:
        return []
    for size in (12.5, 11.5):
        if _text_w(subtitle, size) <= MAP_W:
            return [(58, size, subtitle)]
    two = _wrap_words(subtitle, 11.5, MAP_W)
    if len(two) <= 2:
        return [(54 + 15 * i, 11.5, line) for i, line in enumerate(two)]
    for size in (11.0, 10.5):
        lines = _wrap_words(subtitle, size, MAP_W)
        if len(lines) <= 3:
            return [(50 + 12.5 * i, size, line) for i, line in enumerate(lines)]
    # Four lines would run into the map: smallest type, three lines.
    return [(50 + 12.5 * i, 10.0, line)
            for i, line in enumerate(_wrap_words(subtitle, 10.0, MAP_W)[:3])]


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
            frame.legend_box = (spot[0], spot[1], spot[0] + w, spot[1] + h)
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
    entries = list(zip(encoding.swatches, encoding.scheme.labels(*_affixes(encoding.unit))))
    if has_missing:
        entries.append((f"url(#{colors.HATCH_ID})", encoding.missing_label))
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
        refs = _reference_values(encoding.vmax, encoding.vmin)
        big = encoding.radius(refs[0])
        labels = [_count_label(v) for v in refs]
        w = max(min_w, 20 + 2 * big + 10 + max(_text_w(s, 10.5) for s in labels))
        if has_missing:
            w = max(w, 20 + SWATCH_W + 8 + _text_w(encoding.missing_label, 10.5))
        h = 12 + head_h + 8 + 2 * big + (22 if has_missing else 0) + 10

        def draw(x, y):
            out = [f'<g font-family="{FONT}">', _box(x, y, w, h)]
            out += _heading(encoding, x + 10, y + 20)
            base = y + 12 + head_h + 8 + 2 * big
            out += _nested_circles(encoding, refs, labels, x + 10 + big, base)
            if has_missing:
                out.append(_hatch_swatch(x + 10, base + 9))
                out.append(f'<text x="{_num(x + 10 + SWATCH_W + 8)}" y="{_num(base + 19.5)}" '
                           f'font-size="10.5" fill="#333333">{escape(encoding.missing_label)}</text>')
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
        refs = _reference_values(encoding.vmax, encoding.vmin)
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
                           f'font-size="10.5" fill="#333333">{escape(encoding.missing_label)}</text>')
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
    if scheme.scheme == "quantile":
        edges = [classify.format_value(b) for b in scheme.breaks]
    else:
        affixes = _affixes(encoding.unit)
        edges = [scheme.edge_label(i, *affixes) for i in range(k + 1)]
        if scheme.open_ends:
            # The outer edges are the data's extremes, not class limits: the
            # end classes are open ("< $2k"), so their ends go unwritten.
            edges[0] = edges[-1] = ""
    h = head_h + 8 + SWATCH_H + 16

    def draw(x, y):
        out = [f'<g font-family="{FONT}">']
        out += _heading(encoding, x, y + 12)
        sy = y + head_h + 8
        for i, fill in enumerate(encoding.swatches):
            out.append(f'<rect x="{_num(x + i * cell)}" y="{_num(sy)}" width="{_num(cell)}" '
                       f'height="{SWATCH_H}" fill="{fill}" stroke="#ffffff" stroke-width="0.8"/>')
        for i, label in enumerate(edges):
            if not label or (i and label == edges[i - 1]):
                continue  # an open end, or a single-value class already written
            tx = x + i * cell
            anchor = "start" if i == 0 else ("end" if i == k else "middle")
            centre = scheme.zero_at if scheme.zero_at is not None else scheme.center_at
            weight = ' font-weight="600"' if centre == i else ""
            out.append(f'<line x1="{_num(tx)}" y1="{_num(sy + SWATCH_H)}" x2="{_num(tx)}" '
                       f'y2="{_num(sy + SWATCH_H + 3)}" stroke="#8c8c8c" stroke-width="0.6"/>')
            out.append(f'<text x="{_num(tx)}" y="{_num(sy + SWATCH_H + 14)}" '
                       f'font-size="10.5" text-anchor="{anchor}"{weight} '
                       f'fill="#333333">{escape(label)}</text>')
        if has_missing:
            mx = x + k * cell + 28
            out.append(_hatch_swatch(mx, sy, 30))
            out.append(f'<text x="{_num(mx + 38)}" y="{_num(sy + SWATCH_H - 2.5)}" '
                       f'font-size="10.5" fill="#333333">{escape(encoding.missing_label)}</text>')
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


def _reference_values(vmax: float, vmin: float = 0.0) -> list[float]:
    """Up to three round values for the size legend, inside the data's range.

    Round means 1, 2, 4 or 5 times a power of ten. When the data span more
    than 25x, the circles are the largest round value, about a quarter of it
    and about a twenty-fifth, so the legend shows the range. When they do
    not -- Ukraine's oblasts run 0.89M to 4.06M -- they halve from the top,
    4M, 2M, 1M, and none is smaller than the smallest unit: a 50,000 circle
    that no unit approaches only misleads.
    """
    def nice_below(v: float) -> float:
        if v <= 0:
            return 0.0
        exp = math.floor(math.log10(v))
        for m in (5, 4, 2, 1):
            candidate = m * 10.0 ** exp
            if candidate <= v * (1 + 1e-9):
                return candidate
        return 10.0 ** exp

    top = nice_below(vmax)
    out = [top]
    wide = vmin <= 0 or vmax / vmin > 25
    for share in ((0.25, 0.04) if wide else (0.5, 0.25)):
        v = nice_below(top * share)
        if 0 < v < out[-1] and (wide or v >= vmin * (1 - 1e-9)):
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


def _period(provenance: dict) -> str | None:
    """The period of the values drawn: a change's two years, the year most
    units share, or the source's own vintage label."""
    change = provenance.get("change")
    if change:
        return f"{change['from']}–{change['to']}"
    if provenance.get("year_mode") is not None:
        return str(provenance["year_mode"])
    year = provenance.get("year")
    return str(year) if year else None


_US_STATES = frozenset(usfips.NAMES.values())

# Natural Earth's formal names, as a reader would say them in a title.
_PLACE_ALIASES = {
    "United States of America": "United States",
    "Russian Federation": "Russia",
    "Republic of Korea": "South Korea",
    "Dem. Rep. Korea": "North Korea",
}


def _title(manifest: Manifest, provenance: dict) -> str:
    """Concept, place, period: 'Present population, Ukraine, 1 January 2022'.

    From the drawn data, never the slug; no "latest data" filler -- when units
    disagree on the year, the title gives the year most share and the subtitle
    names the exceptions.
    """
    if manifest.variable_id is None:
        head = _human(f"{_SCOPE.get(manifest.level, manifest.level)} boundaries")
        place = provenance.get("place")
        return f"{head}, {place}" if place else head
    place = provenance.get("place")
    parts = [_label(manifest, provenance), _PLACE_ALIASES.get(place, place),
             _period(provenance)]
    return ", ".join(str(part) for part in parts if part)


# What a reader calls the unit, where the place makes it unambiguous.
_UNIT_NAMES = {
    ("United States of America", "admin_1"): "state",
    ("United States", "admin_1"): "state",
    ("Canada", "admin_1"): "province or territory",
    ("Ukraine", "admin_1"): "oblast",
    ("Germany", "admin_1"): "state (Land)",
}


def _scope_word(manifest: Manifest, provenance: dict) -> str:
    place = str(provenance.get("place") or "")
    if (place, manifest.level) in _UNIT_NAMES:
        return _UNIT_NAMES[(place, manifest.level)]
    if manifest.level == "admin_2" and place in _US_STATES:
        return "county"
    return _SCOPE.get(manifest.level, manifest.level)


# Source caveats about a count of values ("180 values provisional or estimated")
# belong to the footnote: in the subtitle they crowd out what the map shows.
_FOOTNOTE_ONLY = re.compile(r"\bprovisional\b|\bestimated\b", re.I)


def _subtitle(manifest: Manifest, provenance: dict, encoding: Encoding) -> str:
    if encoding.mode == "basemap":
        return ""
    head = f"By {_scope_word(manifest, provenance)}"
    if encoding.mode == "symbols":
        head += "; circle area is proportional to the value"
    unit = provenance.get("unit")
    if unit == "%":
        unit = "percent"
    period = _period(provenance)
    if unit and period and unit.endswith(f" {period}"):
        unit = unit[: -len(period) - 1]     # the title already says when
    pieces = [head] + ([unit] if unit else [])
    note = provenance.get("statistics_note")
    if note and not _FOOTNOTE_ONLY.search(str(note)):
        pieces.append(str(note))
    if encoding.scheme is not None and encoding.scheme.zero_at is not None:
        direction = provenance.get("diverging") or {}
        if direction.get("negative") and direction.get("positive"):
            pieces.append(
                f"{direction.get('negative_color', 'red')} = {direction['negative']}; "
                f"{direction.get('positive_color', 'blue')} = {direction['positive']}")
        else:
            pieces.append("red = decline; blue = growth")
    exceptions = provenance.get("year_exceptions")
    if exceptions:
        named = sum(len(v) for v in exceptions.values())
        if named <= 8:
            detail = "; ".join(f"{', '.join(names)} {year}"
                               for year, names in sorted(exceptions.items(), reverse=True))
            pieces.append(f"latest year {provenance['year_mode']} ({detail})")
        else:
            noun = _SCOPE.get(manifest.level, "unit")
            noun = "countries" if noun == "country" else f"{noun}s"
            span = (min(exceptions) if min(exceptions) == max(exceptions)
                    else f"{min(exceptions)}–{max(exceptions)}")
            pieces.append(f"latest year available per {_SCOPE.get(manifest.level, 'unit')}: "
                          f"{provenance['year_mode']}, except {named} {noun} ({span})")
    return " · ".join(pieces)


def _footer(manifest, frame: Frame, encoding: Encoding, result, top: float,
            extra: tuple | list = ()):
    """The mandatory footnote. Every rendered map states its own provenance."""
    p = result.provenance

    scale = p.get("scale") or registry.scale_for(manifest.level)
    vintage = p.get("vintage") or registry.SOURCE_VINTAGE
    source = f"{_boundary_credit(p)}, {vintage}, {scale}"
    context_source = p.get("context_source")
    if (context_source and result.context
            and str(p.get("source") or "").casefold() not in context_source.casefold()):
        source += f". Neighbouring land: {context_source}"

    scheme = encoding.scheme
    if encoding.mode == "basemap":
        method = f"boundaries only, no variable ({len(result.rows)} units)"
    elif encoding.mode == "symbols":
        method = ("none; a count is drawn as proportional circles, area "
                  f"proportional to value (largest {_count_label(encoding.vmax)})")
    elif scheme.kind == "categorical":
        method = f"one class per category ({scheme.k} categories)"
    elif scheme.scheme != "quantile":
        method = f"{scheme.scheme}, {scheme.k} classes"
        if scheme.zero_at is not None:
            method += f" ({scheme.zero_at} below zero, {scheme.k - scheme.zero_at} above)"
        if scheme.open_ends:
            method += ", the first and last open-ended"
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

    overlay_only = manifest.variable_id is None and bool(result.overlays)
    lines = [f"Boundaries: {source}."]
    if overlay_only:
        lines.append(f"Projection: {_projection_label(frame)}.")
    else:
        lines.extend([
            f"Variable: {variable}.",
            f"Projection: {_projection_label(frame)}. Classification: {method}. {excluded}."
            + (f" {_plain_land_note(p)}" if _plain_land_note(p) else ""),
        ])
    lines.extend(_physical_credits(result.physical))
    lines.extend(_merge_label_credits(extra))

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


def _boundary_credit(p: dict) -> str:
    """A stable, explicit boundary credit for each cartography provider."""
    source = str(p.get("source") or registry.SOURCE_NAME)
    low = source.casefold()
    attribution = str(p.get("attribution") or "")
    license_note = str(p.get("license") or "")
    if "natural earth" in low:
        return "Natural Earth (public domain)"
    if "overture" in low:
        detail = attribution or "© Overture Maps Foundation"
        return f"Overture Maps (ODbL 1.0; {detail})"
    if "gisco" in low:
        return f"Eurostat GISCO ({attribution or '© EuroGeographics'})"
    if "census" in low:
        detail = f"; {attribution}" if attribution else ""
        return f"U.S. Census Bureau (public domain{detail})"
    if license_note:
        return f"{source} ({license_note})"
    return source


def _physical_credits(layers) -> list[str]:
    """One "Physical context" line for all the layers that share a publisher
    and licence (lakes and rivers from one Natural Earth release), one more
    for each that does not."""
    groups: dict = {}
    for layer in layers:
        p = layer.get("provenance") or {}
        name = str(p.get("name") or layer.get("class") or "physical context")
        details = ", ".join(str(x) for x in (p.get("publisher"), p.get("license")) if x)
        names = groups.setdefault(details, [])
        if name not in names:
            names.append(name)
    out = []
    for details, names in groups.items():
        # "Natural Earth ne_50m_lakes", "Natural Earth ne_50m_rivers": the
        # publisher is said once.
        head = names[0].split(" ")[0] if len(names) > 1 else ""
        if head and all(n.startswith(head + " ") for n in names):
            names = [names[0]] + [n[len(head) + 1:] for n in names[1:]]
        out.append("Physical context: " + "; ".join(names)
                   + (f" ({details})." if details else "."))
    return out


def _merge_label_credits(extra) -> list[str]:
    """"Place labels: ..." and "Sea names: ..." are one paragraph, not two."""
    labels = [e for e in extra if str(e).startswith(("Place labels:", "Sea names:"))]
    rest = [e for e in extra if e not in labels]
    return rest + ([" ".join(labels)] if labels else [])


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
        licence = p.get("statistics_license")
        if licence and str(licence).casefold() not in credit.casefold():
            credit += f" ({licence})"
        vintage = p.get("statistics_vintage")
        if vintage and vintage != p.get("year"):
            credit += f"; {vintage}"
        variable += f" [{credit}]"
    note = p.get("statistics_note")
    if note and _FOOTNOTE_ONLY.search(str(note)):
        variable += f"; {note}"
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


def _plain_land_note(p: dict) -> str:
    names = p.get("plain_land")
    if not names:
        return ""
    return (f"{', '.join(names)} drawn as plain land: no permanent population, "
            "so no statistics.")


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
