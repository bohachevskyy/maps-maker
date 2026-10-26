"""Overlay drawing: points, lines, flows and place labels, as SVG.

Pure, like render.py, which calls it: layers arrive already harvested and
leave as strings. Kept in its own module so the choropleth renderer and the
overlay renderer can change independently.

Nothing here is chosen by the manifest. A layer's kind comes from its data;
size and colour come from the attributes its source declares:

* points  -- circle area by `size` (magnitude: 10^(M/2), so 3.2x per step;
             anything else: area proportional to value), translucent and
             smallest first so clusters read; colour by `category`.
* lines   -- constant width, colour by `category`.
* flow    -- Minard: consecutive rows of a group joined into a band whose
             width is linear in `size`, colour by `category`.
* places  -- labelled dots that came with the data (Minard's cities).

Context places (Natural Earth) are labelled by a greedy, deterministic
collision test: largest first, the first of eight positions that overlaps no
earlier label or dot, otherwise not at all.
"""

import math
import re
from xml.sax.saxutils import escape, quoteattr

from mapsvc import colors, project

# Ordered categories (depth classes, speed classes): strong to faint.
ORDERED = ("#d0312d", "#f0962c", "#4a3f9c", "#9a9a9a", "#6a3d9a")
# Unordered categories. A two-class flow is Minard's: tan out, black back.
NOMINAL = ("#1b9e77", "#d95f02", "#7570b3", "#e7298a", "#66a61e", "#e6ab02",
           "#8c8c8c")
# Saffir-Simpson along a storm track: grey outside the tropical stages, blue
# below hurricane strength, then yellow to deep red to purple, one hue step
# per category -- ordered, so the eye reads the strongest segments first.
SSHS = {"other": "#b3b8bb", "td": "#8cb6d6", "ts": "#3a7fb8", "cat1": "#f2c84b",
        "cat2": "#f08a24", "cat3": "#dd4b1a", "cat4": "#a50f15", "cat5": "#5c1a6b"}
TRACK_W = (1.0, 1.2, 1.5, 2.0, 2.4, 2.8, 3.2, 3.6)   # thin to thick, same order
TRACK_DASH = "3 2.5"          # a track outside its tropical life
FLOW = ("#d6b27c", "#25292c", "#7d9fbf", "#b3546b")
# Text in a band's colour must still read on paper: the tan is darkened.
NOTE_INK = {"#d6b27c": "#8a6630"}
# Polygon borders under overlays: the base map is orientation, not message.
MUTED_BORDER = "#9fa8a5"
LINE_W = 1.9
POINT_R = 2.6                 # a point with no size attribute
DENSE_POINT_R = 1.65          # categorical point clouds above DENSE_POINT_COUNT
DENSE_POINT_COUNT = 500
MAG_R0 = 2.6                  # radius of the smallest magnitude drawn
SQRT_R_MAX = 0.035            # largest sqrt-scaled circle, share of sqrt(area)
FLOW_W_MAX = 0.075            # widest band, share of map height...
FLOW_W_MAX_W = 0.027          # ...or of map width, whichever is larger
FLOW_W_MIN = 1.2
SIMPLIFY_PX = 0.7
TEXT = "#1f2a30"
MUTED = "#5d6870"
LABEL_SIZE = 10.0
LABEL_GAP = 3.0               # px kept between two labels side by side
HALO = ('paint-order="stroke" stroke="#ffffff" stroke-width="3" '
        'stroke-linejoin="round" stroke-opacity="0.9"')
CONTEXT_LABELS = 16
CONTEXT_BOLD = 7
SERVED_PX = 9.0
SERVED_KM = 16.0
# A city of this size within this far of a line is one it serves: a line
# stops short of a big city, at a station on its edge.
NEAR_KM = 42.0
NEAR_POPULATION = 150_000
# A served place is set in bold from this population: the cities of a network
# form two ranks by size, not a cut at the n-th of them.
BOLD_POPULATION = 400_000


class Art:
    """Everything an overlay contributes to the page."""

    def __init__(self):
        self.svg: list[str] = []          # clipped to the map
        self.labels: list[str] = []       # above everything on the map
        self.legend_h = 0.0
        self.legend = lambda x, y: ""
        self.title = ""
        self.subtitle = ""
        self.footer: list[str] = []
        self.ink: list[tuple] = []        # boxes a floating legend must avoid
        self.placer = None                # the Labels every overlay label went into


def _num(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0", "-") else s


def _text_w(text: str, size: float) -> float:
    return len(text) * size * 0.56


def _wrap_lon(lon: float, lon0: float) -> float:
    return lon0 + ((lon - lon0 + 180.0) % 360.0) - 180.0


def _xy(frame, lon: float, lat: float) -> tuple[float, float]:
    return frame.xy(_wrap_lon(lon, frame.lon0), lat)


def _palette(category: dict | None, kind: str) -> dict:
    if not category:
        return {}
    values = [c["value"] for c in category["classes"]]
    if kind == "flow":
        cycle = FLOW
    elif category.get("ordered"):
        cycle = ORDERED
    else:
        cycle = NOMINAL
    return {v: cycle[i % len(cycle)] for i, v in enumerate(values)}


# ------------------------------------------------------------------- draw ---

FOCUS_PAD = 0.25


def overlay_extent(layers: list) -> tuple | None:
    """lon/lat box of every overlay feature, or None (none, or wraps the globe)."""
    lons, lats = [], []
    for layer in layers or []:
        if layer["kind"] == "lines":
            for f in layer["features"]:
                coords = f["coords"]
                # Storm files carry long pre-genesis and post-tropical tails.
                # They remain drawn, but the named tropical lifecycle defines
                # the basin frame rather than a few grey fixes near Iceland.
                if layer.get("along") and f.get("classes"):
                    active = []
                    for i, category in enumerate(f["classes"]):
                        if category != "other" and i + 1 < len(coords):
                            active.extend((coords[i], coords[i + 1]))
                    coords = active or coords
                lons.extend(c[0] for c in coords)
                lats.extend(c[1] for c in coords)
        elif layer["kind"] in ("points", "places", "flow"):
            lons.extend(f["lon"] for f in layer["features"])
            lats.extend(f["lat"] for f in layer["features"])
    if not lons or max(lons) - min(lons) > 180.0:
        return None
    return (min(lons), min(lats), max(lons), max(lats))


def focus(geometries: list, layers: list) -> list:
    """The polygons a `within` map should be framed on, when it has overlays.

    `within ["FRA"]` brings French Guiana and Réunion; framed on every unit,
    the high-speed lines of metropolitan France shrink to a corner. The units
    near the overlay -- its own extent padded by a quarter -- define the frame
    instead; the rest are still drawn, clipped by the page.
    """
    box = overlay_extent(layers)
    if box is None:
        return geometries
    dx = (box[2] - box[0]) * FOCUS_PAD + 0.5
    dy = (box[3] - box[1]) * FOCUS_PAD + 0.5
    west, south, east, north = box[0] - dx, box[1] - dy, box[2] + dx, box[3] + dy
    keep = []
    for geometry in geometries:
        for ring in project.outer_rings(geometry):
            lons = [p[0] for p in ring]
            lats = [p[1] for p in ring]
            if (max(lons) - min(lons) <= 180.0 and min(lons) <= east and max(lons) >= west
                    and min(lats) <= north and max(lats) >= south):
                keep.append(geometry)
                break
    return keep or geometries


def centre_lon(layers: list, geometries: list = ()) -> float | None:
    """A world map's central meridian, derived from where its overlays are.

    Earthquakes ring the Pacific; centred on Greenwich, the ring is cut in
    two at the map's edges. Only points and lines count; None keeps 0°.
    """
    lons = []
    for layer in layers or []:
        if layer["kind"] in ("points", "places"):
            lons.extend(f["lon"] for f in layer["features"])
        elif layer["kind"] == "lines":
            lons.extend(f["coords"][0][0] for f in layer["features"] if f["coords"])
        elif layer["kind"] == "flow":
            lons.extend(f["lon"] for f in layer["features"])
    centre = project.central_meridian(lons)
    if centre is None and len(lons) >= SEAM_MIN_POINTS:
        centre = _emptiest_seam(lons, geometries)
    return centre


# Points spread round the whole globe (1,214 Holocene volcanoes: resultant
# 0.09) have no centre, but they do have an emptiest meridian. Centred on
# Greenwich the seam at 180° cuts the Ring of Fire through 75 volcanoes;
# centred on 120°W it runs through 7.
SEAM_MIN_POINTS = 200
SEAM_BAND = 10.0


def _emptiest_seam(lons: list, geometries: list = ()) -> float | None:
    def near(centre):
        seam = centre + 180.0
        return sum(1 for v in lons if abs((v - seam + 180.0) % 360.0 - 180.0) <= SEAM_BAND)

    def land_cut(centre):
        seam = centre + 180.0
        cost = 0.0
        for geometry in geometries:
            for ring in project.outer_rings(geometry):
                unwrapped = project.unwrap_ring(ring, 0.0)
                if not unwrapped:
                    continue
                xs = [point[0] for point in unwrapped]
                if not any(min(xs) < branch < max(xs)
                           for branch in (seam - 360.0, seam, seam + 360.0)):
                    continue
                ys = [point[1] for point in unwrapped]
                mean_lat = sum(ys) / len(ys)
                cost += (max(ys) - min(ys)) * max(
                    0.1, math.cos(math.radians(mean_lat)))
        return cost

    candidates = range(-180, 180, 30)
    counts = {c: near(c) for c in candidates}
    # One degree of substantial land cut costs as much as two points near the
    # seam. For the Holocene volcanoes this chooses 150E (37 points, only three
    # small Atlantic land crossings) over 120W (7 points, Eurasia cut at 60E).
    scores = {c: counts[c] + 2.0 * land_cut(c) for c in candidates}
    best = min(candidates, key=lambda c: (scores[c], counts[c], abs(c), c))
    if scores[best] * 2 > scores[0]:
        return None
    return None if best == 0 else float(best)


def draw(layers: list, places: list, frame, layout: dict, physical: list = ()) -> Art:
    """Draw every layer, then the labels, then lay out the legend band."""
    art = Art()
    labels = Labels(layout, frame)
    blocks: list = []
    order = {"flow": 0, "lines": 1, "points": 2, "places": 3, "temperature": 4}
    indexed = sorted(enumerate(layers), key=lambda t: (
        -1 if t[1].get("area") else order.get(t[1]["kind"], 9), t[0]))
    anchors: list[tuple] = []         # pixel points the overlays pass through
    # Placed after every layer's own labels: numbers written beside a flow
    # must not sit on the flow, but town names along it come first.
    deferred: list = []

    for index, layer in indexed:
        kind = layer["kind"]
        stats = "".join(f" data-stat-{k}={quoteattr(_stat(v))}"
                        for k, v in sorted((layer.get("stats") or {}).items()))
        head = (f'<g data-overlay={quoteattr(layer.get("source", ""))} '
                f'data-layer="{index}"{stats}>')
        if kind == "points" and layer.get("style") == "settlements":
            body, legend = _settlements(layer, frame, labels, anchors)
        elif kind == "lines" and layer.get("style") == "routes":
            body, legend = _routes(layer, frame, labels, anchors, places)
        elif kind == "points" and layer.get("rank"):
            body, legend = _ranked_points(layer, frame, labels, anchors, layout)
        elif kind == "points":
            body, legend = _points(layer, frame, labels, anchors, layout)
        elif kind == "lines" and layer.get("along"):
            body, legend = _tracks(layer, frame, labels, anchors)
        elif kind == "lines":
            body, legend = _lines(layer, frame, anchors, labels)
        elif kind == "flow":
            body, legend = _flow(layer, frame, labels, anchors, deferred, layout)
        elif kind == "places" and layer.get("area"):
            body, legend = _area_labels(layer, frame, labels)
        elif kind == "places":
            body, legend = _dataset_places(layer, frame, labels)
        elif kind == "temperature":
            body, legend = "", [_temperature_block(layer, frame, layout)]
        else:
            continue
        if body:
            art.svg.append(head + body + "</g>")
        blocks.extend(legend)

    for obstacle in (d for d in deferred if d[0] == "block"):
        labels.block(*obstacle[1])
    for note in (d for d in deferred if d[0] == "note"):
        labels.note(*note[1])
    if places:
        labels.context(places, anchors)

    art.labels.append(labels.svg())
    blocks.extend(_physical_legend_blocks(physical))
    art.placer = labels
    _legend(art, blocks, layout)

    main = [layer for layer in layers if layer["kind"] not in ("places", "temperature")] or layers
    if main:
        art.title = main[0].get("map_title") or main[0].get("title") or ""
        art.subtitle = " · ".join(s for s in (main[0].get("summary"),) if s)
    credited: set = set()
    for layer in layers:
        art.footer.append(_footer_line(layer, credited))
    if places and labels.context_count:
        art.footer.append("Place labels: Natural Earth populated places 1:10m "
                          "(public domain), largest places near the overlay first.")
    return art


def _stat(value) -> str:
    if isinstance(value, float):
        return format(value, ".6g")
    return str(value)


def _footer_line(layer: dict, credited: set) -> str:
    p = layer.get("provenance") or {}
    credit = p.get("attribution") or p.get("name") or layer.get("source")
    licence = p.get("license") or "licence not stated by source"
    again = credit in credited
    if again:
        # Same publisher and licence as a layer already credited: say what
        # this one is, not that its source is "as above".
        head = f"Also drawn: {layer.get('title') or layer.get('label')}"
        parts = [head]
    else:
        credited.add(credit)
        parts = [f"Overlay: {layer.get('title') or layer.get('label')}. "
                 f"Source: {credit} ({licence})"]
        if p.get("vintage"):
            parts.append(p["vintage"])
    line = "; ".join(parts) + "."
    if layer.get("encoding"):
        line += f" {layer['encoding']}."
    drawn = layer.get("drawn", len(layer.get("features") or []))
    noun = layer.get("noun") or {"flow": "rows", "lines": "lines", "places": "places",
                                 "temperature": "observations"}.get(layer["kind"],
                                                                    "features")
    excluded = layer.get("excluded") or []
    if excluded:
        detail = ", ".join(f"{e['count']:,} {e['reason']}" for e in excluded)
        line += f" {drawn:,} {noun} drawn; excluded: {detail}."
    else:
        line += f" {drawn:,} {noun} drawn" + ("." if again else "; none excluded.")
    return line


# ----------------------------------------------------------------- points ---

def _radius_fn(layer: dict, frame, layout):
    size = layer.get("size")
    values = [f["size"] for f in layer["features"] if f.get("size") is not None]
    if not size or not values:
        return (lambda v: POINT_R), None
    if size.get("scale") == "magnitude":
        floor = math.floor(min(values) * 2) / 2.0

        def radius(v):
            return MAG_R0 * 10 ** (0.25 * (float(v) - floor))
        return radius, floor
    vmax = max(values)
    r_max = SQRT_R_MAX * math.sqrt(layout["map_w"] * frame.map_h)

    def radius(v):
        return 0.0 if vmax <= 0 or v <= 0 else max(1.2, r_max * math.sqrt(v / vmax))
    return radius, None


def _points(layer, frame, labels, anchors, layout):
    radius, floor = _radius_fn(layer, frame, layout)
    colours = _palette(layer.get("category"), "points")
    default = ORDERED[0]
    placed = []
    dense = len(layer["features"]) >= DENSE_POINT_COUNT and not layer.get("size")
    for f in layer["features"]:
        x, y = _xy(frame, f["lon"], f["lat"])
        if not frame.visible(x, y):
            continue
        r = radius(f["size"]) if f.get("size") is not None else (
            DENSE_POINT_R if dense else POINT_R)
        if r <= 0:
            continue
        placed.append((r, f["id"], x, y, f))
    # Smallest first: the large ones are translucent, the small ones stay
    # visible through them rather than vanishing underneath.
    placed.sort(key=lambda t: (t[0], t[1]))
    opacity = "0.58" if dense else "0.78"
    stroke_width = "0.35" if dense else "0.6"
    out = [f'<g fill-opacity="{opacity}" stroke="#ffffff" '
           f'stroke-width="{stroke_width}">']
    for r, fid, x, y, f in placed:
        fill = colours.get(f.get("category"), default)
        value = "" if f.get("size") is None else f' data-value={quoteattr(_stat(f["size"]))}'
        out.append(f'<circle cx="{_num(x)}" cy="{_num(y)}" r="{_num(r)}" fill="{fill}" '
                   f'data-fid={quoteattr(str(fid))}{value} '
                   f'data-lon="{f["lon"]}" data-lat="{f["lat"]}"/>')
        anchors.append((x, y))
        labels.block(x - r, y - r, x + r, y + r)
    out.append("</g>")

    # Name the largest few: the M8.8 a reader looks for should not need a
    # lookup. Largest first, so they win the space.
    top = layer.get("label_top") or 0
    if any(t[4].get("notable") is not None for t in placed):
        _name_notable(placed, labels, top)
    else:
        named = sorted((t for t in placed if t[4].get("name")),
                       key=lambda t: (-t[0], t[1]))[:top]
        for rank, (r, _, x, y, f) in enumerate(named):
            labels.callout(f["name"], x, y, r, bold=rank < 3)

    legend = []
    size = layer.get("size")
    if size and placed:
        legend.append(_size_block(layer, radius, floor))
    if layer.get("category"):
        present = {t[4].get("category") for t in placed}
        legend.append(_swatch_block(layer["category"], colours, present, "dot"))
    elif not size:
        legend.append(_swatch_block({"label": layer.get("label") or "", "classes": [
            {"value": None, "label": layer.get("label") or "Feature"}]},
            {None: default}, {None}, "dot"))
    return "".join(out), legend


NOTABLE_APART = 60.0          # px between two named points of a crowded layer


def _name_notable(placed, labels, top) -> None:
    """Name the points the data itself marks as worth naming (a volcano whose
    record reports deaths and destruction), the best first, each at least
    NOTABLE_APART px from the last so the names spread over the map rather
    than pile up on one arc. A name that finds no room is skipped for the next."""
    ranked = sorted((t for t in placed if t[4].get("name") and t[4].get("notable") is not None),
                    key=lambda t: (-t[4]["notable"], t[4]["name"], t[1]))
    done: list = []
    for r, _, x, y, f in ranked:
        if len(done) >= top:
            break
        if any(math.hypot(x - ox, y - oy) < NOTABLE_APART for ox, oy in done):
            continue
        if labels.callout(f["name"], x, y, r, bold=len(done) < 3,
                          reaches=(8.0, 18.0, 30.0, 46.0)):
            done.append((x, y))


def _short(v: float) -> str:
    """A population on a label: 10.4M, 873k."""
    if v >= 1e7:
        return f"{v / 1e6:.1f}M"
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}k"
    return f"{v:,.0f}"


def _ranked_points(layer, frame, labels, anchors, layout):
    """The N largest, as proportional circles, each named with its value.

    Largest drawn first so a small neighbour stays on top of a big one; every
    circle is blocked before any label is placed, then labels go largest
    first: beside the circle if there is room, otherwise on a leader.
    """
    radius, _ = _radius_fn(layer, frame, layout)
    placed = []
    for f in layer["features"]:
        x, y = _xy(frame, f["lon"], f["lat"])
        if not frame.visible(x, y):
            continue
        placed.append((radius(f["size"]), f["rank"], x, y, f))
    placed.sort(key=lambda t: (-t[0], t[1]))
    out = ['<g fill="#c4553a" fill-opacity="0.62" stroke="#ffffff" stroke-width="0.8">']
    for r, _, x, y, f in placed:
        out.append(f'<circle cx="{_num(x)}" cy="{_num(y)}" r="{_num(r)}" '
                   f'data-fid={quoteattr(str(f["id"]))} '
                   f'data-value={quoteattr(_stat(f["size"]))} '
                   f'data-lon="{f["lon"]}" data-lat="{f["lat"]}"/>')
        anchors.append((x, y))
        labels.block(x - r, y - r, x + r, y + r)
    out.append("</g>")
    missed = 0
    for r, rank, x, y, f in sorted(placed, key=lambda t: t[1]):
        text = f"{f['name']} {_short(f['size'])}"
        if not (_beside(labels, text, x, y, r, rank <= 5)
                or labels.callout(text, x, y, r, bold=rank <= 5)):
            missed += 1
    if missed:
        out.append(f'<desc data-unlabelled="{missed}">{missed} labels did not fit</desc>')
    legend = [_size_block(layer, radius, None)] if placed else []
    return "".join(out), legend


def _beside(labels, text, x, y, r, bold) -> bool:
    """A label touching its circle's edge: right, left, above, below, then the
    diagonals -- never across a gap where it could be read as a neighbour's."""
    extra = ' font-weight="600"' if bold else ""
    for dx, dy in ((1, 0), (-1, 0), (0, -1), (0, 1), (1, -1), (-1, -1), (1, 1), (-1, 1)):
        norm = math.hypot(dx, dy)
        ax, ay = x + dx / norm * (r + 1.5), y + dy / norm * (r + 1.5)
        if labels._try_at(text, ax, ay, dx, dy, LABEL_SIZE, extra):
            return True
    return False


def _tracks(layer, frame, labels, anchors):
    """Storm tracks: one line per storm, each segment in its category's colour.

    Segments of one class are drawn as one path, weakest first, so a
    hurricane's red runs over the blue of its own tropical-storm stages.
    Every named storm is labelled, strongest first, majors in bold; hurricane
    landfalls are marked.
    """
    category = layer.get("category") or {"classes": []}
    order = [c["value"] for c in category["classes"]]
    rank = {v: i for i, (v, _) in enumerate(SSHS.items())}
    paths: dict = {v: [] for v in order}
    for f in sorted(layer["features"], key=lambda f: f["id"]):
        coords = project.unwrap_ring([tuple(c) for c in f["coords"]], frame.lon0)
        pts = [frame.xy(lon, lat) for lon, lat in coords]
        classes = f.get("classes") or []
        run, current = [], None
        for i in range(len(pts) - 1):
            cls = classes[i] if i < len(classes) else "other"
            if cls != current and run:
                paths.setdefault(current, []).append(run)
                run = []
            if not run:
                run = [pts[i]]
            run.append(pts[i + 1])
            current = cls
            labels.ink(pts[i], pts[i + 1], 1.5)
        if run:
            paths.setdefault(current, []).append(run)
        anchors.extend(pts[:: max(1, len(pts) // 8)])
    out = []
    for value in sorted(paths, key=lambda v: rank.get(v, -1)):
        runs = paths[value]
        if not runs:
            continue
        d = "".join("M" + "L".join(f"{_num(x)},{_num(y)}" for x, y in _thin(run))
                    for run in runs)
        width = TRACK_W[rank.get(value, 0)]
        dash = f' stroke-dasharray="{TRACK_DASH}"' if value == "other" else ""
        out.append(f'<path d="{d}" fill="none" stroke="{SSHS.get(value, ORDERED[0])}" '
                   f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"'
                   f'{dash} data-class={quoteattr(str(value))}/>')
    # Landfalls first, so no name is written over one.
    landfall_svg, landfall_n, told = [], 0, []
    for f in layer["features"]:
        for fall in f.get("landfalls") or ():
            lon, lat, cat, when = fall[:4]
            wind = fall[4] if len(fall) > 4 else None
            place = fall[5] if len(fall) > 5 else ""
            x, y = _xy(frame, lon, lat)
            if not frame.visible(x, y):
                continue
            landfall_svg.append(
                f'<circle cx="{_num(x)}" cy="{_num(y)}" r="3.4" fill="#ffffff" '
                f'stroke="{TEXT}" stroke-width="1.3" class="landfall" '
                f'data-storm={quoteattr(f["name"])} data-date={quoteattr(str(when))} '
                f'data-category="{int(cat)}"/>')
            labels.block(x - 4.5, y - 4.5, x + 4.5, y + 4.5)
            anchors.append((x, y))
            landfall_n += 1
            if f["peak"] >= MAJOR_STORM and f.get("name"):
                told.append((-f["peak"], str(when), f["name"], x, y, cat, wind, place))
    out.extend(landfall_svg)
    # The landfalls of the major hurricanes are the story of a season: each
    # says who, when, how strong and where (the NHC's own record).
    for _, when, name, x, y, cat, wind, place in sorted(told):
        first = f"{name} landfall, {_day(when)}"
        strength = f"Cat {int(cat)}" + (f", {wind:.0f} kt" if wind else "")
        second = f"{strength} \u00b7 {place}" if place else strength
        labels.callout_lines([(first, True), (second, False)], x, y, 4.0)
    # Every named storm, strongest first, majors in bold. A storm tries the
    # place it peaked, then the rest of its track: in the Gulf of Mexico the
    # peak is under six other storms.
    named = sorted((f for f in layer["features"]
                    if f.get("name") and f["name"].upper() != "UNNAMED"),
                   key=lambda f: (-f["peak"], -f.get("peak_wind", 0), f["id"]))
    for f in named:
        coords = project.unwrap_ring([tuple(c) for c in f["coords"]], frame.lon0)
        spots = [tuple(f["peak_at"])]
        n = len(coords)
        for k in (1.0, 0.5, 0.75, 0.25, 0.0, 0.62, 0.38, 0.88, 0.12):
            spots.append(coords[min(n - 1, int(k * (n - 1)))])
        for lon, lat in spots:
            x, y = _xy(frame, lon, lat)
            if frame.visible(x, y) and labels.callout(
                    f["name"], x, y, 2.0, bold=f["peak"] >= 3,
                    reaches=(8.0, 18.0, 30.0, 46.0, 64.0, 84.0)):
                break
    present = {v for v, runs in paths.items() if runs}
    colours = {v: SSHS.get(v, ORDERED[0]) for v in order}
    legend = [_swatch_block(category, colours, present, "line",
                            dashed=frozenset({"other"}), columns=2)]
    if landfall_n:
        legend.append(_landfall_block(landfall_n))
    return "".join(out), legend


MAJOR_STORM = 3          # Saffir-Simpson category 3: a "major" hurricane
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
           "Nov", "Dec")


def _day(iso: str) -> str:
    """'2024-07-08' -> '8 Jul'; anything else is shown as given."""
    try:
        _, month, day = iso[:10].split("-")
        return f"{int(day)} {_MONTHS[int(month) - 1]}"
    except (ValueError, IndexError):
        return iso


def _landfall_block(count: int):
    text = "Hurricane landfall"
    w, h = 30 + _text_w(text, 10), 18 + 16

    def draw(x, y):
        return (f'<circle cx="{_num(x + 9)}" cy="{_num(y + 12)}" r="3.4" fill="#ffffff" '
                f'stroke="{TEXT}" stroke-width="1.3"/>'
                f'<text x="{_num(x + 28)}" y="{_num(y + 15.5)}" font-size="10" '
                f'fill="{TEXT}">{escape(text)} ({count})</text>')
    return w + 30, h, draw


def _thin(run):
    out = [run[0]]
    for i, (x, y) in enumerate(run[1:], 1):
        if i < len(run) - 1 and math.hypot(x - out[-1][0], y - out[-1][1]) < SIMPLIFY_PX:
            continue
        out.append((x, y))
    return out


def _size_block(layer, radius, floor):
    size = layer["size"]
    values = [f["size"] for f in layer["features"] if f.get("size") is not None]
    vmax = max(values)
    if size.get("scale") == "magnitude":
        refs = [m for m in range(int(math.ceil(floor)), int(math.floor(vmax)) + 1)]
        if vmax - (refs[-1] if refs else floor) >= 0.3:
            refs.append(vmax)
        labels = [f"M{v:g}" for v in refs]
        heading = f"{size['label']} (circle area)"
    else:
        refs = _nice_refs(vmax)
        labels = [_count(v) for v in refs]
        heading = f"{size['label']} (circle area)"
    radii = [radius(v) for v in refs]

    def width():
        return max(_text_w(heading, 11.5), sum(2 * r + 10 + _text_w(t, 10) + 14
                                                for r, t in zip(radii, labels)))
    height = 18 + 2 * max(radii) + 6

    def draw(x, y):
        out = [_heading(heading, x, y + 11)]
        cy = y + 18 + max(radii)
        cx = x
        for r, text in zip(radii, labels):
            out.append(f'<circle cx="{_num(cx + r)}" cy="{_num(cy)}" r="{_num(r)}" '
                       'fill="#9aa3a8" fill-opacity="0.8" stroke="#5f686e" stroke-width="0.7"/>')
            out.append(f'<text x="{_num(cx + 2 * r + 5)}" y="{_num(cy + 3.5)}" '
                       f'font-size="10" fill="{TEXT}">{escape(text)}</text>')
            cx += 2 * r + 10 + _text_w(text, 10) + 14
        return "".join(out)
    return width(), height, draw


def _nice_refs(vmax: float) -> list[float]:
    if vmax <= 0:
        return [0.0]
    exp = math.floor(math.log10(vmax))
    top = next(m * 10.0 ** exp for m in (5, 2, 1) if m * 10.0 ** exp <= vmax * (1 + 1e-9))
    refs = [top]
    for share in (0.3, 0.06):
        v = top * share
        e = math.floor(math.log10(v))
        v = next(m * 10.0 ** e for m in (5, 2, 1) if m * 10.0 ** e <= v * (1 + 1e-9))
        if v < refs[-1]:
            refs.append(v)
    return refs


def _count(v: float) -> str:
    if v >= 1e6:
        return f"{v / 1e6:.3g} million"
    return f"{v:,.0f}" if v >= 1 else f"{v:g}"


def _heading(text: str, x: float, y: float) -> str:
    return (f'<text x="{_num(x)}" y="{_num(y)}" font-size="11.5" font-weight="600" '
            f'fill="{TEXT}">{escape(text)}</text>')


def _swatch_block(category, colours, present, mark, dashed=frozenset(), columns=1):
    """The classes of a category, one under another -- or in `columns` columns
    filled down, which is how a ladder of storm categories stays short."""
    classes = [c for c in category["classes"] if c["value"] in present]
    heading = category.get("label") or ""
    rows = [(colours.get(c["value"], ORDERED[0]), c["label"], c["value"]) for c in classes]
    per_column = -(-len(rows) // columns) if rows else 1
    widths = [max([0.0] + [30 + _text_w(t, 10) for _, t, _ in rows[c * per_column:
                                                                  (c + 1) * per_column]])
              for c in range(columns)]
    starts = [sum(widths[:c]) + 26.0 * c for c in range(columns)]
    offsets = [(starts[i // per_column], i % per_column) for i in range(len(rows))]
    w = max(_text_w(heading, 11.5), starts[-1] + widths[-1])
    h = 18 + 16 * per_column

    def draw(x0, y):
        out = [_heading(heading, x0, y + 11)] if heading else []
        for i, (colour, text, value) in enumerate(rows):
            cy = y + 18 + 16 * offsets[i][1] + 6
            x = x0 + offsets[i][0]
            if mark == "dot":
                out.append(f'<circle cx="{_num(x + 6)}" cy="{_num(cy)}" r="5" '
                           f'fill="{colour}" fill-opacity="0.85"/>')
            elif mark == "line":
                dash = (f' stroke-dasharray="{TRACK_DASH}"'
                        if value in dashed else "")
                out.append(f'<line x1="{_num(x)}" y1="{_num(cy)}" x2="{_num(x + 20)}" '
                           f'y2="{_num(cy)}" stroke="{colour}" stroke-width="{LINE_W + 0.6}" '
                           f'stroke-linecap="round"{dash}/>')
            else:
                out.append(f'<rect x="{_num(x)}" y="{_num(cy - 5)}" width="20" height="10" '
                           f'fill="{colour}"/>')
            out.append(f'<text x="{_num(x + 28)}" y="{_num(cy + 3.5)}" font-size="10" '
                       f'fill="{TEXT}">{escape(text)}</text>')
        return "".join(out)
    return w, h, draw


# ------------------------------------------------------------------ lines ---

def _line_pixels(coords, frame) -> list[tuple[float, float]]:
    """A line in page pixels, unwrapped and thinned to what the eye can see."""
    ring = project.unwrap_ring([tuple(c) for c in coords], frame.lon0)
    out: list[tuple[float, float]] = []
    for i, (lon, lat) in enumerate(ring):
        x, y = frame.xy(lon, lat)
        if out and i < len(ring) - 1 and math.hypot(x - out[-1][0], y - out[-1][1]) < SIMPLIFY_PX:
            continue
        out.append((x, y))
    return out


def _lines(layer, frame, anchors, labels=None):
    category = layer.get("category")
    colours = _palette(category, "lines")
    classes = [c["value"] for c in category["classes"]] if category else [None]
    paths: dict = {c: [] for c in classes}
    for f in layer["features"]:
        pts = _line_pixels(f["coords"], frame)
        if len(pts) < 2:
            continue
        key = f.get("category") if category else None
        paths.setdefault(key, []).append(
            "M" + "L".join(f"{_num(x)},{_num(y)}" for x, y in pts))
        if labels is not None:
            for a, b in zip(pts, pts[1:]):
                labels.ink(a, b, LINE_W / 2 + 1.0)
        anchors.extend(pts[:: max(1, len(pts) // 8)] + [pts[-1]])

    out = []
    # Faintest first, so the class the layer is about is drawn on top.
    for value in reversed(classes):
        if not paths.get(value):
            continue
        colour = colours.get(value, ORDERED[0])
        out.append(f'<path d="{"".join(paths[value])}" fill="none" stroke="{colour}" '
                   f'stroke-width="{LINE_W}" stroke-linecap="round" '
                   f'stroke-linejoin="round" data-class={quoteattr(str(value))}/>')
    legend = []
    if category:
        present = {v for v, d in paths.items() if d}
        block = _swatch_block(category, colours, present, "line")
    else:
        block = _swatch_block({"label": "", "classes": [
            {"value": None, "label": layer.get("label") or "Line"}]},
            {None: ORDERED[0]}, {None}, "line")
    legend.append(block)
    if layer.get("facts"):
        legend.append(_facts_block(layer.get("facts_label") or "", layer["facts"]))
    return "".join(out), legend


def _facts_block(heading: str, facts: list):
    """A small table of numbers: [(label, value), ...]."""
    w = max([_text_w(heading, 11.5)] + [_text_w(a, 10) + _text_w(b, 10) + 24
                                         for a, b in facts])
    h = 18 + 15 * len(facts)

    def draw(x, y):
        out = [_heading(heading, x, y + 11)] if heading else []
        for i, (a, b) in enumerate(facts):
            ty = y + 18 + 15 * i + 9
            out.append(f'<text x="{_num(x)}" y="{_num(ty)}" font-size="10" '
                       f'fill="{TEXT}">{escape(a)}</text>')
            out.append(f'<text x="{_num(x + w)}" y="{_num(ty)}" font-size="10" '
                       f'text-anchor="end" fill="{TEXT}">{escape(b)}</text>')
        return "".join(out)
    return w, h, draw


# ----------------------------------------------------------------- routes ---

# Lines whose OSM colour tag is missing or invalid. Twelve, dark enough to read
# as a line on the pale base map.
ROUTE_PALETTE = ("#1f78b4", "#e31a1c", "#33a02c", "#ff7f00", "#6a3d9a", "#b15928",
                 "#a6cee3", "#fb9a99", "#b2df8a", "#fdbf6f", "#cab2d6", "#8c8c8c")
ROUTE_W = 2.6                 # a network's lines
ROUTE_W_SINGLE = 3.6          # one route, the whole message of the map
STATION_LABELS = 30


def route_colours(classes: list) -> dict:
    """Each line's own colour; the palette, in class order, for the rest."""
    out, spare = {}, 0
    for c in classes:
        if c.get("colour"):
            out[c["value"]] = c["colour"]
        else:
            out[c["value"]] = ROUTE_PALETTE[spare % len(ROUTE_PALETTE)] if len(classes) > 1 \
                else ORDERED[0]
            spare += 1
    return out


def _routes(layer, frame, labels, anchors, places=()):
    """Route relations: one colour per line, cased in white so lines that run
    side by side stay apart; stations as white dots, interchanges larger and
    named. A single route is drawn bold over the muted base map."""
    classes = layer["category"]["classes"]
    colours = route_colours(classes)
    width = ROUTE_W_SINGLE if len(classes) == 1 else ROUTE_W
    paths: dict = {c["value"]: [] for c in classes}
    for f in layer["features"]:
        pts = _line_pixels(f["coords"], frame)
        if len(pts) < 2:
            continue
        paths.setdefault(f["category"], []).append(
            "M" + "L".join(f"{_num(x)},{_num(y)}" for x, y in pts))
        for a, b in zip(pts, pts[1:]):
            labels.ink(a, b, width / 2 + 1.0)
        # Every vertex: a long route's stops must be found along all of it.
        anchors.extend(pts)
    out = []
    # Later classes underneath, so the legend's first lines are on top.
    for c in reversed(classes):
        d = "".join(paths.get(c["value"]) or ())
        if not d:
            continue
        out.append(f'<path d="{d}" fill="none" stroke="#ffffff" '
                   f'stroke-width="{_num(width + 2.2)}" stroke-linecap="round" '
                   'stroke-linejoin="round"/>')
        out.append(f'<path d="{d}" fill="none" stroke="{colours[c["value"]]}" '
                   f'stroke-width="{_num(width)}" stroke-linecap="round" '
                   f'stroke-linejoin="round" data-class={quoteattr(str(c["value"]))}/>')

    stations = []
    for st in layer.get("stations") or ():
        x, y = _xy(frame, st["lon"], st["lat"])
        if frame.visible(x, y):
            stations.append((st, x, y))
    standing = _station_standing([t[0] for t in stations], places)
    stations.sort(key=lambda t: (-standing[t[0]["name"]], t[0]["name"]))
    dots = []
    for st, x, y in reversed(stations):
        big = standing[st["name"]] >= INTERCHANGE_STANDING
        r = min(3.8, 2.4 + 0.45 * (st["lines"] - 2)) if big else 1.5
        dots.append(f'<circle cx="{_num(x)}" cy="{_num(y)}" r="{_num(r)}" '
                    f'stroke-width="{1.0 if big else 0.6}"/>')
        if big:
            # A one-line stop's 1.5 px dot may sit under an interchange's
            # name; in the core of a network there is no room otherwise.
            labels.block(x - r, y - r, x + r, y + r)
    if dots:
        out.append(f'<g fill="#ffffff" stroke="{TEXT}">' + "".join(dots) + "</g>")
    named = 0
    named_hubs = 0
    for st, x, y in stations:            # most important first
        if standing[st["name"]] < INTERCHANGE_STANDING or named >= STATION_LABELS:
            break
        bold = standing[st["name"]] >= 3.0
        extra = ' font-weight="600"' if bold else ""
        # A station's name may cross another line, halo first, as on any
        # transit map; it may not cover another name or a station.
        if labels.place(st["name"], x, y, bold=bold) or _place_over_lines(
                labels, st["name"], x, y, 4.0, LABEL_SIZE, extra):
            named += 1
        elif named_hubs < HUB_CALLOUTS and labels.callout_lines(
                [(st["name"], True)], x, y, 3.0,
                reaches=(10.0, 18.0, 28.0, 40.0, 54.0, 70.0), size=LABEL_SIZE):
            named += 1
        named_hubs += 1

    present = {v for v, d in paths.items() if d}
    legend = [_swatch_block(layer["category"], colours, present, "line")]
    if layer.get("facts"):
        legend.append(_facts_block(layer.get("facts_label") or "", layer["facts"]))
    if stations and any(standing[st["name"]] >= INTERCHANGE_STANDING for st, _, _ in stations):
        legend.append(_station_block())
    return "".join(out), legend


INTERCHANGE_STANDING = 2.0
# The most important stations are called out on a leader when they cannot be
# named beside their dot: a map of Tokyo without Tokyo is not read.
HUB_CALLOUTS = 12


def _station_standing(stations: list, places: list = ()) -> dict:
    """How much a station matters, from what the data says about it: the lines
    it serves, whether OSM marks it as also served by trains or as a
    Wikidata entity, whether other stations are named after it (Shinjuku,
    Nishi-shinjuku: the district's own station), and whether it carries the
    name of a populated place. Not how many lines alone: Tokyo Station has
    one subway line and is the one a reader looks for."""
    names = [st["name"].casefold() for st in stations]
    town = {str(p.get("name") or "").casefold() for p in places}
    out = {}
    for st in stations:
        me = st["name"].casefold()
        stem = me.replace("-", " ")
        kin = sum(1 for other in names if other != me and (
            other.replace("-", " ").endswith(" " + stem)
            or other.replace("-", " ").startswith(stem + " ")))
        out[st["name"]] = (st["lines"] + 1.2 * bool(st.get("rail"))
                           + 0.6 * bool(st.get("wikidata")) + min(1.4, 0.7 * kin)
                           + 3.0 * (me in town))
    return out


def _place_over_lines(labels, text, x, y, gap, size, extra) -> bool:
    """Labels' own placement, except that line ink does not block a position."""
    w, h = _text_w(text, size), size
    for dx, dy in labels.OFFSETS:
        x0 = x + gap if dx > 0 else x - gap - w if dx < 0 else x - w / 2
        tx = x + gap if dx > 0 else x - gap if dx < 0 else x
        anchor = "start" if dx > 0 else "end" if dx < 0 else "middle"
        ty = y - gap - 1 if dy < 0 else y + gap + h - 1 if dy > 0 else y + h * 0.35
        box = (x0, ty - h * 0.85, x0 + w, ty + h * 0.2)
        if labels._free(box, ink=False):
            labels.taken.append(box)
            labels.out.append(
                f'<text x="{_num(tx)}" y="{_num(ty)}" font-size="{size:g}" '
                f'text-anchor="{anchor}" fill="{TEXT}" {HALO}{extra}>{escape(text)}</text>')
            return True
    return False


ALONG_KM = 12.0


def places_along(places: list, layers: list, km: float = ALONG_KM) -> list:
    """The places within `km` of a route's line, in the order given.

    For a route too long for general context labels: Novosibirsk and Irkutsk
    are its stops, Murmansk is not. Vertices are bucketed by tenth of a degree
    so each place is tested only against the line near it.
    """
    cell = 0.1
    grid: dict = {}
    for layer in layers:
        for f in layer["features"]:
            for lon, lat in f["coords"]:
                grid.setdefault((int(lon // cell), int(lat // cell)), []).append((lon, lat))
    out = []
    for p in places:
        k = math.cos(math.radians(p["lat"]))
        reach_lat = km / 111.2
        reach_lon = reach_lat / max(k, 0.05)
        cx0, cx1 = int((p["lon"] - reach_lon) // cell), int((p["lon"] + reach_lon) // cell)
        cy0, cy1 = int((p["lat"] - reach_lat) // cell), int((p["lat"] + reach_lat) // cell)
        near = any(math.hypot((lon - p["lon"]) * k, lat - p["lat"]) * 111.2 <= km
                   for i in range(cx0, cx1 + 1) for j in range(cy0, cy1 + 1)
                   for lon, lat in grid.get((i, j), ()))
        if near:
            out.append(p)
    return out


def _station_block():
    rows = [(1.5, 0.6, "Station"), (2.85, 1.0, "Major interchange (named)")]
    w = max(30 + _text_w(t, 10) for _, _, t in rows)
    h = 16 * len(rows) + 4

    def draw(x, y):
        out = []
        for i, (r, sw, text) in enumerate(rows):
            cy = y + 8 + 16 * i
            out.append(f'<circle cx="{_num(x + 6)}" cy="{_num(cy)}" r="{r}" fill="#ffffff" '
                       f'stroke="{TEXT}" stroke-width="{sw}"/>')
            out.append(f'<text x="{_num(x + 28)}" y="{_num(cy + 3.5)}" font-size="10" '
                       f'fill="{TEXT}">{escape(text)}</text>')
        return "".join(out)
    return w, h, draw


# ------------------------------------------------------------ settlements ---

# place tag -> (radius, fill, stroke, label size, label weight, label ink).
# Rank reads twice, by size and by darkness; hamlets are dots, never named.
SETTLEMENT_STYLE = {
    "city": (4.6, "#ffffff", 1.7, 12.0, "700", TEXT),
    "town": (3.0, TEXT, 0.0, 10.0, "600", TEXT),
    # Ukraine's selyshche (former urban-type settlement): an open circle,
    # between a city's ring and a village's dot, named in italics.
    "selyshche": (2.7, "#ffffff", 1.1, 8.5, None, TEXT),
    "suburb": (2.0, "#46525a", 0.0, 8.5, None, MUTED),
    "village": (1.55, "#5d6870", 0.0, 7.5, None, MUTED),
    "hamlet": (1.0, "#98a2a8", 0.0, 0.0, None, MUTED),
}
SETTLEMENT_RANK = ("city", "town", "selyshche", "suburb", "village", "hamlet")
# Villages are tried for a label in order of population, only this many.
VILLAGE_LABEL_TRIES = 400
DENSE_POINTS = 100            # more symbols than this and villages go unnamed
DENSE_VILLAGE_LABELS = 10


def _settlement_style(place):
    return SETTLEMENT_STYLE.get(place, SETTLEMENT_STYLE["hamlet"])


def _settlements(layer, frame, labels, anchors):
    placed = []
    for f in layer["features"]:
        x, y = _xy(frame, f["lon"], f["lat"])
        if frame.visible(x, y):
            placed.append((f, x, y))

    def rank(place):
        return SETTLEMENT_RANK.index(place) if place in SETTLEMENT_RANK else len(SETTLEMENT_RANK)

    out = []
    for place in sorted({f["category"] for f, _, _ in placed}, key=rank, reverse=True):
        r, fill, stroke, *_ = _settlement_style(place)
        group = [t for t in placed if t[0]["category"] == place]
        ring = (f' stroke="{TEXT}" stroke-width="{stroke}"' if stroke
                else ' stroke="#ffffff" stroke-width="0.5"' if r >= 2.5 else "")
        out.append(f'<g fill="{fill}"{ring} data-class={quoteattr(str(place))}>')
        for f, x, y in group:
            out.append(f'<circle cx="{_num(x)}" cy="{_num(y)}" r="{r}" '
                       f'data-fid={quoteattr(f["id"])}/>')
        out.append("</g>")

    def major(f):
        return f["category"] in ("city", "town")

    def block(rows):
        for f, x, y in rows:
            r = _settlement_style(f["category"])[0]
            labels.block(x - r, y - r, x + r, y + r)
            if r >= 2.5:
                anchors.append((x, y))

    # Cities and towns are named first and may sit over a village's dot (the
    # halo keeps both legible); a village's name may not cover anything.
    order = sorted((t for t in placed if t[0].get("name")),
                   key=lambda t: (rank(t[0]["category"]), -(t[0].get("population") or 0),
                                  t[0]["name"], t[0]["id"]))
    block([t for t in placed if major(t[0])])
    for f, x, y in (t for t in order if major(t[0])):
        r, _, _, size, weight, _ = _settlement_style(f["category"])
        if not labels._try(f["name"], x, y, r + 1.5, size, f' font-weight="{weight}"'):
            labels.callout(f["name"], x, y, r, bold=True)
    # Selyshcha are named in italics, like cities before the villages' dots
    # fill the page; a name may sit over a village's dot (the halo keeps both
    # legible) but not over another name.
    block([t for t in placed if t[0]["category"] == "selyshche"])
    for f, x, y in (t for t in order if t[0]["category"] == "selyshche"):
        r, _, _, size, _, _ = _settlement_style("selyshche")
        if not labels._try(f["name"], x, y, r + 1.5, size, ' font-style="italic"'):
            labels.callout(f["name"], x, y, r, italic=True)
    block([t for t in placed if not major(t[0]) and t[0]["category"] != "selyshche"])
    # A page of a thousand villages named is a page of text: past
    # DENSE_POINTS symbols only the cities and towns are named, and the few
    # villages the data ranks (by population) -- none if it ranks none.
    minor = [t for t in order if not major(t[0]) and t[0]["category"] != "selyshche"]
    tries = VILLAGE_LABEL_TRIES
    if len(placed) > DENSE_POINTS:
        minor = [t for t in minor if t[0].get("population")]
        tries = DENSE_VILLAGE_LABELS
    for f, x, y in minor[:tries]:
        r, _, _, size, weight, ink = _settlement_style(f["category"])
        if size:
            extra = (f' font-weight="{weight}"' if weight else "") + f' style="fill:{ink}"'
            labels._try(f["name"], x, y, r + 1.5, size, extra)

    present = {f["category"] for f, _, _ in placed}
    return "".join(out), [_settlement_block(layer["category"], present)]


def _settlement_block(category, present):
    rows = [(c["value"], c["label"]) for c in category["classes"] if c["value"] in present]
    heading = category.get("label") or ""
    w = max([_text_w(heading, 11.5)] + [30 + _text_w(t, 10) for _, t in rows])
    h = 18 + 16 * len(rows)

    def draw(x, y):
        out = [_heading(heading, x, y + 11)] if heading else []
        for i, (place, text) in enumerate(rows):
            r, fill, stroke, *_ = _settlement_style(place)
            r = max(r, 2.2)         # a key must be visible even where the map's dot is a speck
            cy = y + 18 + 16 * i + 6
            ring = f' stroke="{TEXT}" stroke-width="{stroke}"' if stroke else ""
            out.append(f'<circle cx="{_num(x + 6)}" cy="{_num(cy)}" r="{r}" '
                       f'fill="{fill}"{ring}/>')
            out.append(f'<text x="{_num(x + 28)}" y="{_num(cy + 3.5)}" font-size="10" '
                       f'fill="{TEXT}">{escape(text)}</text>')
        return "".join(out)
    return w, h, draw


# ------------------------------------------------------------------- flow ---

def _flow(layer, frame, labels, anchors, deferred, layout):
    category = layer.get("category")
    colours = _palette(category, "flow")
    classes = [c["value"] for c in category["classes"]] if category else [None]
    sizes = [f["size"] for f in layer["features"] if f.get("size") is not None]
    vmax = max(sizes) if sizes else 1.0
    w_max = max(FLOW_W_MAX * frame.map_h, FLOW_W_MAX_W * layout["map_w"])

    def width(v):
        return max(FLOW_W_MIN, w_max * float(v) / vmax) if vmax > 0 else FLOW_W_MIN

    groups: dict = {}
    for f in layer["features"]:
        groups.setdefault(f["group"], []).append(f)
    segments: dict = {c: [] for c in classes}
    for group in sorted(groups, key=str):
        rows = sorted(groups[group], key=lambda f: f["order"])
        pts = [_xy(frame, r["lon"], r["lat"]) for r in rows]
        anchors.extend(pts)
        for i in range(len(rows) - 1):
            a, b = pts[i], pts[i + 1]
            if a == b:
                continue
            key = rows[i].get("category") if category else None
            segments.setdefault(key, []).append((a, b, width(rows[i]["size"]), group))
    out = []
    # The first category (Minard's advance) underneath, later ones on top:
    # the retreat is the part of the story the reader follows home.
    for value in classes:
        colour = colours.get(value, FLOW[0])
        segs = segments.get(value) or []
        if not segs:
            continue
        out.append(f'<g stroke="{colour}" stroke-linecap="round" data-class={quoteattr(str(value))}>')
        for (ax, ay), (bx, by), w, group in segs:
            out.append(f'<line x1="{_num(ax)}" y1="{_num(ay)}" x2="{_num(bx)}" '
                       f'y2="{_num(by)}" stroke-width="{_num(w)}"/>')
            # The band is ink no label may sit on: town names and the notes
            # alike go beside it, on a leader when they cannot.
            labels.ink((ax, ay), (bx, by), w / 2 + 1.0)
        out.append("</g>")

    # The three numbers the story is told with, written beside the flow.
    for note in layer.get("notes") or []:
        x, y = _xy(frame, note["lon"], note["lat"])
        colour = colours.get(note.get("category"), TEXT)
        deferred.append(("note", (note["text"], x, y, NOTE_INK.get(colour, colour))))

    legend = [_band_block(layer, width, vmax)]
    if category:
        legend.append(_swatch_block(category, colours, set(classes), "band"))
    return "".join(out), legend


def _band_block(layer, width, vmax):
    size = layer.get("size") or {}
    heading = f"Band width = {size.get('label') or 'value'}"
    refs = _nice_refs(vmax)
    rows = [(width(v), _count(v)) for v in refs]
    h = 18 + sum(max(w, 10) + 6 for w, _ in rows)
    w = max(_text_w(heading, 11.5), 60 + max(_text_w(t, 10) for _, t in rows))

    def draw(x, y):
        out = [_heading(heading, x, y + 11)]
        cy = y + 18
        for bw, text in rows:
            mid = cy + max(bw, 10) / 2
            out.append(f'<rect x="{_num(x)}" y="{_num(mid - bw / 2)}" width="48" '
                       f'height="{_num(bw)}" fill="#a39d90"/>')
            out.append(f'<text x="{_num(x + 56)}" y="{_num(mid + 3.5)}" font-size="10" '
                       f'fill="{TEXT}">{escape(text)}</text>')
            cy += max(bw, 10) + 6
        return "".join(out)
    return w, h, draw


def _area_labels(layer, frame, labels):
    """District names as quiet letter-spaced capitals at each district's interior
    point. They are the base of the settlement hierarchy, so they are placed
    first, may sit under the dots (a dense map has no free ground) and are
    blocked so that the names which follow keep clear of them."""
    out = []
    for f in sorted(layer["features"], key=lambda f: f["id"]):
        x, y = _xy(frame, f["lon"], f["lat"])
        if not frame.visible(x, y) or not f.get("name"):
            continue
        words = str(f["name"]).upper().split()
        lines = ([" ".join(words[:-1]), words[-1]] if len(words) > 1 else words)
        size, spacing = 11.0, 1.4
        widths = [_text_w(t, size) + spacing * len(t) for t in lines]
        top = y - (len(lines) - 1) * size * 0.6
        for i, text in enumerate(lines):
            labels.out.append(
                f'<text x="{_num(x)}" y="{_num(top + i * size * 1.2 + size * 0.35)}" '
                f'font-size="{size:g}" text-anchor="middle" font-weight="600" '
                f'letter-spacing="{spacing}" fill="#8a8272" {HALO} '
                f'data-district={quoteattr(str(f["name"]))}>{escape(text)}</text>')
        w = max(widths)
        labels.block(x - w / 2, top - size * 0.6, x + w / 2,
                     top + (len(lines) - 1) * size * 1.2 + size * 0.5)
    return "", []


def _dataset_places(layer, frame, labels):
    out = ['<g fill="#25292c">']
    for f in sorted(layer["features"], key=lambda f: f["id"]):
        x, y = _xy(frame, f["lon"], f["lat"])
        if not frame.visible(x, y):
            continue
        out.append(f'<circle cx="{_num(x)}" cy="{_num(y)}" r="2.2" '
                   f'data-fid={quoteattr(str(f["id"]))}/>')
        labels.block(x - 2.2, y - 2.2, x + 2.2, y + 2.2)
    out.append("</g>")
    for f in layer["features"]:
        x, y = _xy(frame, f["lon"], f["lat"])
        if frame.visible(x, y) and f.get("name"):
            if not labels.place(f["name"], x, y, italic=True, bold=bool(f.get("major"))):
                labels.callout(f["name"], x, y, 2.2, bold=bool(f.get("major")), italic=True)
    return "".join(out), []


# ---------------------------------------------------------------- labels ---

class Labels:
    """Greedy, deterministic label placement against everything placed so far."""

    OFFSETS = ((1, 0), (-1, 0), (1, -1), (1, 1), (-1, -1), (-1, 1), (0, -1), (0, 1))

    INK_CELL = 24.0

    def __init__(self, layout, frame):
        self.left = layout["pad"] + 2
        self.right = layout["pad"] + layout["map_w"] - 2
        self.top = layout["map_top"] + 2
        self.bottom = layout["map_top"] + frame.map_h - 2
        self.frame = frame
        self.taken: list[tuple] = []
        self.out: list[str] = []
        self.context_count = 0
        # Lines and bands, as segments with a half-width, bucketed on a grid:
        # an LGV network is ~15,000 segments, and every candidate box is
        # tested against the few near it.
        self.ink_cells: dict = {}
        self.leaders: list[tuple] = []
        # Dots of the named places on the map: a label sits nearest its own.
        self.dots: list[tuple] = []

    def block(self, x0, y0, x1, y1):
        self.taken.append((x0, y0, x1, y1))

    def ink(self, a, b, half):
        """A stroked segment from a to b, `half` px either side, that labels avoid."""
        seg = (a[0], a[1], b[0], b[1], half)
        c = self.INK_CELL
        for i in range(int((min(a[0], b[0]) - half) // c), int((max(a[0], b[0]) + half) // c) + 1):
            for j in range(int((min(a[1], b[1]) - half) // c),
                           int((max(a[1], b[1]) + half) // c) + 1):
                self.ink_cells.setdefault((i, j), []).append(seg)

    def _inked(self, box) -> bool:
        c = self.INK_CELL
        seen = set()
        for i in range(int(box[0] // c), int(box[2] // c) + 1):
            for j in range(int(box[1] // c), int(box[3] // c) + 1):
                for seg in self.ink_cells.get((i, j), ()):
                    if id(seg) in seen:
                        continue
                    seen.add(id(seg))
                    if _segment_meets_box(*seg, box):
                        return True
        return False

    def fits(self, box) -> bool:
        """Whether a box is on the map and clear of everything placed so far.

        The public half of the placer, for callers that draw their own text
        (render's feature labels): test with `fits`, then `block` the box.
        """
        return self._free(box)

    def _free(self, box, ink: bool = True) -> bool:
        """Whether a box is on the map and clear of labels, dots, leaders and --
        unless `ink` is False, for a place's own dot, which may sit on its
        line -- every line and band. Labels keep LABEL_GAP px apart, so two
        neighbours' names do not read as one ("Bobr Orscha")."""
        x0, y0, x1, y1 = box
        if x0 < self.left or x1 > self.right or y0 < self.top or y1 > self.bottom:
            return False
        g = LABEL_GAP
        if any(x0 - g < b[2] and x1 + g > b[0] and y0 < b[3] and y1 > b[1]
               for b in self.taken):
            return False
        if any(_segment_meets_box(*seg, box) for seg in self.leaders):
            return False
        return not (ink and self._inked(box))

    def _try(self, text, x, y, gap, size, extra=""):
        w, h = _text_w(text, size), size
        for dx, dy in self.OFFSETS:
            if dx > 0:
                tx, anchor, x0 = x + gap, "start", x + gap
            elif dx < 0:
                tx, anchor, x0 = x - gap, "end", x - gap - w
            else:
                tx, anchor, x0 = x, "middle", x - w / 2
            if dy < 0:
                ty = y - gap - 1
            elif dy > 0:
                ty = y + gap + h - 1
            else:
                ty = y + h * 0.35
            box = (x0, ty - h * 0.85, x0 + w, ty + h * 0.2)
            if self._free(box) and not self._nearer_another(box, x, y):
                self.taken.append(box)
                self.out.append(
                    f'<text x="{_num(tx)}" y="{_num(ty)}" font-size="{size:g}" '
                    f'text-anchor="{anchor}" fill="{TEXT}" {HALO}{extra}>'
                    f'{escape(text)}</text>')
                return True
        return False

    def _nearer_another(self, box, x, y) -> bool:
        """Whether a label box sits closer to another named place's dot than to
        its own: Moscow's name above Yaroslavl's dot reads as Yaroslavl's."""
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        own = math.hypot(cx - x, cy - y)
        return any(math.hypot(cx - dx, cy - dy) < own
                   for dx, dy in self.dots if abs(dx - x) + abs(dy - y) > 1.0)

    def place(self, text, x, y, italic=False, bold=False):
        extra = (' font-style="italic"' if italic else "") + (
            ' font-weight="600"' if bold else "")
        return self._try(text, x, y, 4.0, LABEL_SIZE, extra)

    def callout(self, text, x, y, r, bold=False, italic=False,
                reaches=(8.0, 18.0, 30.0, 46.0)):
        """A label on a short leader from a circle's edge, further out if crowded.

        Always on a leader: a label merely near a dot in a cluster is read as
        the neighbour's (the two Kamchatka quakes). The leader may not cross
        a placed label or another leader, and the label clears every line,
        band and circle, so a displaced label never sits on the ink.
        """
        extra = (' font-weight="600"' if bold else "") + (
            ' font-style="italic"' if italic else "")
        directions = ((1, 0), (-1, 0), (1, -1), (-1, -1), (1, 1), (-1, 1), (0, -1), (0, 1))
        for reach in reaches:
            for dx, dy in directions:
                norm = math.hypot(dx, dy)
                ux, uy = dx / norm, dy / norm
                sx, sy = x + ux * r, y + uy * r
                lx, ly = x + ux * (r + reach), y + uy * (r + reach)
                if self._leader_blocked(sx, sy, lx, ly):
                    continue
                before = len(self.out)
                if self._try_at(text, lx, ly, dx, dy, LABEL_SIZE, extra):
                    self.out.insert(before, f'<line x1="{_num(sx)}" y1="{_num(sy)}" '
                                    f'x2="{_num(lx)}" y2="{_num(ly)}" stroke="{MUTED}" '
                                    f'stroke-width="0.6"/>')
                    self.leaders.append((sx, sy, lx, ly, 0.5))
                    return True
        return False

    def callout_lines(self, lines, x, y, r,
                      reaches=(10.0, 20.0, 32.0, 46.0, 62.0, 80.0, 100.0, 124.0, 150.0),
                      size=LABEL_SIZE - 0.5):
        """A callout of several lines ((text, bold) each) on a leader from a
        marker's edge: the first line leads, the rest follow it, all beside the
        leader's end. Placed like `callout`: nearest free spot, leader not
        crossing a label or another leader."""
        lead = size * 1.18
        w = max(_text_w(t, size) for t, _ in lines)
        h = lead * len(lines)
        directions = ((1, 0), (-1, 0), (1, -1), (-1, -1), (1, 1), (-1, 1), (0, -1), (0, 1),
                      (2, -1), (-2, -1), (2, 1), (-2, 1), (1, -2), (-1, -2), (1, 2), (-1, 2))
        # Clear of every track first; failing that, haloed text may cross a line.
        for ink in (True, False):
            for reach in reaches:
              for dx, dy in directions:
                  norm = math.hypot(dx, dy)
                  ux, uy = dx / norm, dy / norm
                  sx, sy = x + ux * r, y + uy * r
                  lx, ly = x + ux * (r + reach), y + uy * (r + reach)
                  if dx > 0:
                      x0, anchor, tx = lx + 2, "start", lx + 2
                  elif dx < 0:
                      x0, anchor, tx = lx - 2 - w, "end", lx - 2
                  else:
                      x0, anchor, tx = lx - w / 2, "middle", lx
                  top = ly - h / 2 if dx != 0 else (ly - h - 1 if dy < 0 else ly + 1)
                  box = (x0, top, x0 + w, top + h)
                  if self._leader_blocked(sx, sy, lx, ly) or not self._free(box, ink=ink):
                      continue
                  self.taken.append(box)
                  self.leaders.append((sx, sy, lx, ly, 0.5))
                  spans = []
                  for i, (text, bold) in enumerate(lines):
                      weight = ' font-weight="600"' if bold else ""
                      spans.append(f'<text x="{_num(tx)}" y="{_num(top + lead * i + size * 0.88)}" '
                                   f'font-size="{size:g}" text-anchor="{anchor}" fill="{TEXT}" '
                                   f'{HALO}{weight}>{escape(text)}</text>')
                  self.out.append(f'<line x1="{_num(sx)}" y1="{_num(sy)}" x2="{_num(lx)}" '
                                  f'y2="{_num(ly)}" stroke="{MUTED}" stroke-width="0.6"/>'
                                  + "".join(spans))
                  return True
        return False

    def _leader_blocked(self, sx, sy, lx, ly) -> bool:
        if not (self.left <= lx <= self.right and self.top <= ly <= self.bottom):
            return True
        # The circle the leader leaves (and anything containing its start) is
        # not in its way.
        for b in self.taken:
            if b[0] - 0.5 <= sx <= b[2] + 0.5 and b[1] - 0.5 <= sy <= b[3] + 0.5:
                continue
            if _segment_meets_box(sx, sy, lx, ly, 0.0, b) and _text_box_like(b):
                return True
        return any(_segments_cross((sx, sy, lx, ly), seg[:4]) for seg in self.leaders)

    def _try_at(self, text, x, y, dx, dy, size, extra) -> bool:
        """The text with its near edge on (x, y), on the side dx, dy points to."""
        w, h = _text_w(text, size), size
        if dx > 0:
            tx, anchor, x0 = x + 2, "start", x + 2
        elif dx < 0:
            tx, anchor, x0 = x - 2, "end", x - 2 - w
        else:
            tx, anchor, x0 = x, "middle", x - w / 2
        if dy < 0:
            ty = y - 2 if dx == 0 else y + h * 0.35
        elif dy > 0:
            ty = y + h * 0.85 + 2 if dx == 0 else y + h * 0.35
        else:
            ty = y + h * 0.35
        box = (x0, ty - h * 0.85, x0 + w, ty + h * 0.2)
        if not self._free(box):
            return False
        self.taken.append(box)
        self.out.append(f'<text x="{_num(tx)}" y="{_num(ty)}" font-size="{size:g}" '
                        f'text-anchor="{anchor}" fill="{TEXT}" {HALO}{extra}>'
                        f'{escape(text)}</text>')
        return True

    def note(self, text, x, y, colour):
        extra = f' font-weight="700" style="fill:{colour}"'
        for gap in (8.0, 16.0, 26.0, 40.0, 56.0, 72.0):
            if self._try(text, x, y, gap, 11.0, extra):
                return True
        return False

    def _served_px(self, lon: float, lat: float, km: float = None) -> float:
        """How close a line must pass to count as serving a place: SERVED_PX,
        or SERVED_KM of ground where that is more -- a high-speed line stops
        8-12 km short of the city it serves."""
        x0, y0 = _xy(self.frame, lon, lat)
        x1, y1 = _xy(self.frame, lon + 0.1, lat)
        per_km = math.hypot(x1 - x0, y1 - y0) / (0.1 * 111.2 * math.cos(math.radians(lat)))
        return max(SERVED_PX, (SERVED_KM if km is None else km) * per_km)

    def context(self, places, anchors):
        """Natural Earth places: the ones the overlay serves first, then size."""
        candidates = []
        for p in places:
            x, y = _xy(self.frame, p["lon"], p["lat"])
            if not self.frame.visible(x, y):
                continue
            reach = self._served_px(p["lon"], p["lat"])
            served = any(abs(ax - x) <= reach and abs(ay - y) <= reach
                         and math.hypot(ax - x, ay - y) <= reach for ax, ay in anchors)
            if not served and p["pop"] >= NEAR_POPULATION:
                near = self._served_px(p["lon"], p["lat"], NEAR_KM)
                served = any(abs(ax - x) <= near and abs(ay - y) <= near
                             and math.hypot(ax - x, ay - y) <= near for ax, ay in anchors)
            candidates.append((not served, -p["pop"], p["name"], x, y, served))
        candidates.sort()
        biggest = max((-c[1] for c in candidates if c[5]), default=0)
        bold_from = min(BOLD_POPULATION, 0.5 * biggest)
        self.dots = [(x, y) for _, _, _, x, y, _ in candidates][:CONTEXT_LABELS * 3]
        dots, texts = [], []
        bold_left = CONTEXT_BOLD
        for _, neg_pop, name, x, y, served in candidates:
            if self.context_count >= CONTEXT_LABELS:
                break
            box = (x - 2.5, y - 2.5, x + 2.5, y + 2.5)
            if not self._free(box, ink=False):
                continue
            bold = served and bold_left > 0 and -neg_pop >= bold_from
            before = len(self.out)
            self.taken.append(box)
            ok = self.place(name, x, y, bold=bold) if not bold else self._try(
                name, x, y, 5.0, 11.0, ' font-weight="700"')
            if not ok and served:
                # A place on the overlay is worth a leader; one off it is not.
                ok = self.callout(name, x, y, 3.2 if bold else 2.2, bold=bold,
                                  reaches=(8.0, 18.0, 30.0, 46.0, 64.0, 84.0))
            if not ok:
                self.taken.pop()
                continue
            if bold:
                bold_left -= 1
            r = 3.2 if bold else 2.2
            dots.append(f'<circle cx="{_num(x)}" cy="{_num(y)}" r="{r}" fill="#ffffff" '
                        f'stroke="{TEXT}" stroke-width="{1.2 if bold else 0.9}"/>')
            texts.extend(self.out[before:])
            del self.out[before:]
            self.context_count += 1
        self.out.extend(dots + texts)

    def svg(self) -> str:
        return "".join(self.out)


def _segment_meets_box(ax, ay, bx, by, half, box) -> bool:
    """Whether segment a-b, `half` px either side, touches an axis-aligned box.

    Liang-Barsky clipping against the box grown by `half`: square ends rather
    than round, which errs on the side of keeping a label off the ink.
    """
    x0, y0, x1, y1 = box[0] - half, box[1] - half, box[2] + half, box[3] + half
    t0, t1 = 0.0, 1.0
    dx, dy = bx - ax, by - ay
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return t0 <= t1


def _segments_cross(a, b) -> bool:
    def side(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1 = side(b[0], b[1], b[2], b[3], a[0], a[1])
    d2 = side(b[0], b[1], b[2], b[3], a[2], a[3])
    d3 = side(a[0], a[1], a[2], a[3], b[0], b[1])
    d4 = side(a[0], a[1], a[2], a[3], b[2], b[3])
    return d1 * d2 < 0 and d3 * d4 < 0


def _text_box_like(box) -> bool:
    """Leaders may pass over small circles and dots, not over text."""
    return (box[3] - box[1]) <= 16 and (box[2] - box[0]) > (box[3] - box[1])


def _temperature_block(layer: dict, frame, layout: dict):
    """Minard's dated temperature trace, sharing the map's longitude axis."""
    observations = list(layer.get("features") or ())
    width, height = layout["map_w"], 150.0
    values = [float(f["temperature_c"]) for f in observations]
    low = math.floor(min(values) / 10.0) * 10.0
    high = max(0.0, math.ceil(max(values) / 10.0) * 10.0)
    plot_top, plot_h = 28.0, 68.0

    def py(value):
        return plot_top + (high - value) / max(high - low, 1.0) * plot_h

    def px(lon):
        # The retreat runs close to the frame's central latitude; projecting
        # that parallel makes each observation line up with its longitude.
        return frame.xy(float(lon), frame.lat0)[0]

    def date_label(raw: str) -> str:
        match = re.fullmatch(r"([A-Za-z]{3})(\d{1,2})", raw or "")
        return f"{int(match.group(2))} {match.group(1)}" if match else raw

    def draw(_x, y):
        out = [f'<g class="temperature-panel">',
               f'<text x="{_num(layout["pad"])}" y="{_num(y + 11)}" '
               f'font-size="11.5" font-weight="600" fill="{TEXT}">'
               'Temperature on the retreat · Réaumur readings converted to °C</text>']
        tick = math.ceil(low / 10.0) * 10.0
        while tick <= high + 1e-9:
            yy = y + py(tick)
            out.append(f'<line x1="{_num(layout["pad"])}" y1="{_num(yy)}" '
                       f'x2="{_num(layout["pad"] + layout["map_w"])}" y2="{_num(yy)}" '
                       'stroke="#d8dde0" stroke-width="0.5"/>')
            out.append(f'<text x="{_num(layout["pad"] + 2)}" y="{_num(yy - 2)}" '
                       f'font-size="8.5" fill="{MUTED}">{tick:g}°C</text>')
            tick += 10.0
        points = [(px(f["lon"]), y + py(float(f["temperature_c"])), f)
                  for f in observations]
        tiers, last_x = {}, [-float("inf"), -float("inf")]
        for x, _, feature in sorted(points, key=lambda p: p[0]):
            if not feature.get("date"):
                continue
            tier = 0 if x - last_x[0] >= 62 else 1
            tiers[feature["id"]] = tier
            last_x[tier] = x
        d = "M" + "L".join(f"{_num(x)},{_num(yy)}" for x, yy, _ in points)
        out.append(f'<path d="{d}" fill="none" stroke="#3f5968" stroke-width="1.3"/>')
        for x, yy, feature in points:
            out.append(f'<circle cx="{_num(x)}" cy="{_num(yy)}" r="2.2" fill="#3f5968"/>')
            date = date_label(str(feature.get("date") or ""))
            if date:
                label_y = y + 108 + 22 * tiers.get(feature["id"], 0)
                out.append(f'<text x="{_num(x)}" y="{_num(label_y)}" font-size="8.5" '
                           f'text-anchor="middle" fill="{MUTED}">{escape(date)}</text>')
                out.append(f'<text x="{_num(x)}" y="{_num(label_y + 11)}" font-size="8.5" '
                           f'text-anchor="middle" fill="{TEXT}">'
                           f'{float(feature["temperature_c"]):g}°C</text>')
        out.append("</g>")
        return "".join(out)
    return width, height, draw


def _physical_legend_blocks(layers: list) -> list:
    """Legend samples for physical context whose meaning is not self-evident."""
    if not any(layer.get("class") == "plate-boundaries" for layer in layers or ()):
        return []
    heading = "Plate boundaries (Bird 2003)"
    w, h = _text_w(heading, 10) + 32, 22

    def draw(x, y):
        return (f'<line x1="{_num(x)}" y1="{_num(y + 10)}" '
                f'x2="{_num(x + 22)}" y2="{_num(y + 10)}" stroke="{colors.PLATE}" '
                'stroke-width="0.7" stroke-dasharray="4 2.5"/>'
                f'<text x="{_num(x + 29)}" y="{_num(y + 13.5)}" font-size="10" '
                f'fill="{TEXT}">{heading}</text>')
    return [(w, h, draw)]


# ---------------------------------------------------------------- legend ---

def _legend(art: Art, blocks: list, layout: dict) -> None:
    """Blocks in a row under the map, wrapping when the row is full."""
    if not blocks:
        return
    gap = 36.0
    rows, line, used = [], [], 0.0
    for w, h, draw in blocks:
        if line and used + w > layout["map_w"]:
            rows.append(line)
            line, used = [], 0.0
        line.append((used, w, h, draw))
        used += w + gap
    rows.append(line)
    heights = [max(h for _, _, h, _ in row) for row in rows]

    def draw_all(x, y):
        out = [f'<g font-family="{layout["font"]}">']
        top = y
        for row, height in zip(rows, heights):
            for offset, _, _, draw in row:
                out.append(draw(x + offset, top))
            top += height + 12
        out.append("</g>")
        return "".join(out)

    art.legend_h = sum(heights) + 12 * (len(rows) - 1)
    art.legend = draw_all
