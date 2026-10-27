"""Projection, clipping, geometry drawing, and inset layout for SVG rendering."""

from __future__ import annotations

import math
from xml.sax.saxutils import escape, quoteattr

from mapsvc import colors, project
from mapsvc.models import Manifest

CANVAS_W = 960

PAD = 24

MAP_W = CANVAS_W - 2 * PAD

MAP_TOP = 76

MAP_H_MIN, MAP_H_MAX = 300, 700

COORD_DP = 2

SIMPLIFY_PX = 0.3

SPECK_PX2 = 0.25

WORLD = (-180.0, -90.0, 180.0, 90.0)

WORLD_MARGIN = 10.0

LEGEND_INSET = 12

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

MIN_VISIBLE_AREA = 4.0

MARKER_R = 2.6

SYMBOL_SCALE = 0.05

SYMBOL_MIN_R = 1.0

SYMBOL_MIN_SHARE = 0.10

class RenderError(ValueError):
    """Raised when the manifest cannot be honoured for this data."""

    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field

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
        self.core_h = self.map_h     # the map proper; insets below it add to map_h
        local = project.fit(bounds, MAP_W, self.map_h, margin)
        self.transform = lambda x, y: _shift(local(x, y))
        self.outline = (None if outline is None
                        else [self.transform(x, y) for x, y in outline])
        # Page pixels per projected unit. Every projection here is on the unit
        # sphere, so an inset drawn at this scale is at the same scale.
        self.scale = local(1.0, 0.0)[0] - local(0.0, 0.0)[0]
        self.insets: list = []
        self._visible: dict = {}
        self.legend_box: tuple | None = None    # a legend floated over the map
        self.table_box: tuple | None = None     # the ranked table, likewise

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return self.transform(*self.projector(lon, lat))

    @property
    def bottom(self) -> float:
        return MAP_TOP + self.map_h

    @property
    def core_bottom(self) -> float:
        """Where the map proper ends: an inset band below it is not map."""
        return MAP_TOP + self.core_h

    def visible(self, x: float, y: float) -> bool:
        if not (PAD <= x <= PAD + MAP_W and MAP_TOP <= y <= self.bottom):
            return False
        if self.outline is None:
            return True
        # Label placement asks this hundreds of thousands of times, at points
        # on a half-pixel grid: remember the answers.
        key = (round(x * 2.0), round(y * 2.0))
        seen = self._visible.get(key)
        if seen is None:
            seen = self._visible[key] = project.point_in_ring(
                key[0] / 2.0, key[1] / 2.0, self.outline)
        return seen

def _shift(point: tuple[float, float]) -> tuple[float, float]:
    return point[0] + PAD, point[1] + MAP_TOP

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
            if d and css_class == "sea":
                # Over the land and its borders: a water-coloured edge hides
                # the coarser outline running along the shore, then a thin
                # shoreline is drawn on the true coast.
                out.append(f'<path d="{d}" class="sea" fill="{colors.WATER}" '
                           f'stroke="{colors.WATER}" stroke-width="3" '
                           'stroke-linejoin="round" fill-rule="evenodd"/>')
                out.append(f'<path d="{d}" class="shore" fill="none" '
                           f'stroke="{colors.LAKE_STROKE}" stroke-width="0.6" '
                           'stroke-linejoin="round" fill-rule="evenodd"/>')
            elif d:
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

def _hierarchy(units: list, frame: Frame, land: list = ()) -> str:
    """Published parent outlines over detailed child polygons.

    A parent whose children went to an inset (the Canaries) is not on the
    main map: its outline would be drawn where nothing else is.
    """
    if not units:
        return ""
    drawn = _union(land) if land else None
    paths = []
    for unit in sorted(units, key=lambda item: str(item["id"])):
        parts = _project(unit["geometry"], frame)
        rings = [ring for part in parts for ring in part]
        if drawn is not None and rings:
            xs = [x for ring in rings for x, _ in ring]
            ys = [y for ring in rings for _, y in ring]
            if (max(xs) < drawn[0] - 4 or min(xs) > drawn[2] + 4
                    or max(ys) < drawn[1] - 4 or min(ys) > drawn[3] + 4):
                continue
        d = _d(rings)
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

INSET_INFLATION = 1.20

INSET_GAP = 0.05

INSET_MAX_AREA = 0.25

INSET_MAX = 12

INSET_MAX_W, INSET_MAX_H = 0.20, 0.30

INSET_MIN_W, INSET_MIN_H = 84.0, 60.0

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

def _enclave_ids(rows: list) -> set:
    """Ids of the units that sit in a hole of another unit (Kyiv city)."""
    holes = []
    for row in rows:
        for part in _parts(row["geometry"]):
            for hole in part[1:]:
                if len(hole) >= 4:
                    holes.append((_lonlat_box(hole), hole, row["id"]))
    found = set()
    if not holes:
        return found
    for row in rows:
        rings = [part[0] for part in _parts(row["geometry"]) if len(part[0]) >= 4]
        if not rings:
            continue
        ring = max(rings, key=project.planar_area)
        box = _lonlat_box(ring)
        lon, lat = ring[0]
        if any(owner != row["id"] and _box_within(box, hbox)
               and project.point_in_ring(lon, lat, hole)
               for hbox, hole, owner in holes):
            found.add(row["id"])
    return found

def _enclaves(rows: list, frame: Frame, encoding: Encoding, named: bool = True) -> str:
    """Outline and name units that sit in a hole of another unit.

    Kyiv city is an enclave in Kyiv oblast: a few pixels across, with the
    oblast all round it. Its own circle or colour is not enough to read as a
    separate unit, so its boundary is redrawn above the symbols and it gets a
    name. Only true enclaves qualify -- a unit inside another's hole -- so a
    small unit on a coast or a border is left alone.
    """
    inside_ids = _enclave_ids(rows)
    if not inside_ids:
        return ""
    out = []
    for row in rows:
        if row["id"] not in inside_ids:
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

def _least_covered(frame: Frame, land: list, units: list, w: float, h: float,
                   avoid: list = ()):
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
        if any(a and _overlaps((x, y, x + w, y + h), a) for a in avoid):
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
