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
from xml.sax.saxutils import escape, quoteattr

from mapsvc import project

# Ordered categories (depth classes, speed classes): strong to faint.
ORDERED = ("#d0312d", "#f0962c", "#3f78b5", "#9a9a9a", "#6a3d9a")
# Unordered categories. A two-class flow is Minard's: tan out, black back.
NOMINAL = ("#1b9e77", "#d95f02", "#7570b3", "#e7298a", "#66a61e", "#e6ab02")
FLOW = ("#d6b27c", "#25292c", "#7d9fbf", "#b3546b")
# Polygon borders under overlays: the base map is orientation, not message.
MUTED_BORDER = "#ffffff"
LINE_W = 1.9
POINT_R = 2.6                 # a point with no size attribute
MAG_R0 = 2.0                  # radius of the smallest magnitude drawn
SQRT_R_MAX = 0.035            # largest sqrt-scaled circle, share of sqrt(area)
FLOW_W_MAX = 0.075            # widest band, share of map height
FLOW_W_MIN = 1.2
SIMPLIFY_PX = 0.7
TEXT = "#1f2a30"
MUTED = "#5d6870"
LABEL_SIZE = 10.0
HALO = ('paint-order="stroke" stroke="#ffffff" stroke-width="3" '
        'stroke-linejoin="round" stroke-opacity="0.9"')
CONTEXT_LABELS = 14
CONTEXT_BOLD = 7
SERVED_PX = 9.0


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

def draw(layers: list, places: list, frame, layout: dict) -> Art:
    """Draw every layer, then the labels, then lay out the legend band."""
    art = Art()
    labels = _Labels(layout, frame)
    blocks: list = []
    order = {"flow": 0, "lines": 1, "points": 2, "places": 3}
    indexed = sorted(enumerate(layers), key=lambda t: (order.get(t[1]["kind"], 9), t[0]))
    anchors: list[tuple] = []         # pixel points the overlays pass through

    for index, layer in indexed:
        kind = layer["kind"]
        stats = "".join(f" data-stat-{k}={quoteattr(_stat(v))}"
                        for k, v in sorted((layer.get("stats") or {}).items()))
        head = (f'<g data-overlay={quoteattr(layer.get("source", ""))} '
                f'data-layer="{index}"{stats}>')
        if kind == "points":
            body, legend = _points(layer, frame, labels, anchors, layout)
        elif kind == "lines":
            body, legend = _lines(layer, frame, anchors)
        elif kind == "flow":
            body, legend = _flow(layer, frame, labels, anchors, layers)
        elif kind == "places":
            body, legend = _dataset_places(layer, frame, labels)
        else:
            continue
        art.svg.append(head + body + "</g>")
        blocks.extend(legend)

    if places:
        labels.context(places, anchors)

    art.labels.append(labels.svg())
    _legend(art, blocks, layout)

    main = [layer for layer in layers if layer["kind"] != "places"] or layers
    if main:
        art.title = main[0].get("title") or ""
        art.subtitle = " · ".join(s for s in (main[0].get("summary"),) if s)
    for layer in layers:
        art.footer.append(_footer_line(layer))
    if places and labels.context_count:
        art.footer.append("Place labels: Natural Earth populated places 1:10m "
                          "(public domain), largest places near the overlay first.")
    return art


def _stat(value) -> str:
    if isinstance(value, float):
        return format(value, ".6g")
    return str(value)


def _footer_line(layer: dict) -> str:
    p = layer.get("provenance") or {}
    credit = p.get("attribution") or p.get("name") or layer.get("source")
    licence = p.get("license") or "licence not stated by source"
    parts = [f"Overlay: {layer.get('title') or layer.get('label')}. Source: "
             f"{credit} ({licence})"]
    if p.get("vintage"):
        parts.append(p["vintage"])
    if p.get("url"):
        parts.append(p["url"])
    line = "; ".join(parts) + "."
    if layer.get("encoding"):
        line += f" {layer['encoding']}."
    drawn = len(layer.get("features") or [])
    noun = {"flow": "rows", "lines": "lines", "places": "places"}.get(layer["kind"],
                                                                     "features")
    excluded = layer.get("excluded") or []
    if excluded:
        detail = ", ".join(f"{e['count']:,} {e['reason']}" for e in excluded)
        line += f" {drawn:,} {noun} drawn; excluded: {detail}."
    else:
        line += f" {drawn:,} {noun} drawn; none excluded."
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
    for f in layer["features"]:
        x, y = _xy(frame, f["lon"], f["lat"])
        if not frame.visible(x, y):
            continue
        r = radius(f["size"]) if f.get("size") is not None else POINT_R
        if r <= 0:
            continue
        placed.append((r, f["id"], x, y, f))
    # Smallest first: the large ones are translucent, the small ones stay
    # visible through them rather than vanishing underneath.
    placed.sort(key=lambda t: (t[0], t[1]))
    out = ['<g fill-opacity="0.78" stroke="#ffffff" stroke-width="0.6">']
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
                       'fill="#9aa3a8" fill-opacity="0.8" stroke="#ffffff" stroke-width="0.6"/>')
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


def _swatch_block(category, colours, present, mark):
    classes = [c for c in category["classes"] if c["value"] in present]
    heading = category.get("label") or ""
    rows = [(colours.get(c["value"], ORDERED[0]), c["label"]) for c in classes]
    w = max([_text_w(heading, 11.5)] + [30 + _text_w(t, 10) for _, t in rows])
    h = 18 + 16 * len(rows)

    def draw(x, y):
        out = [_heading(heading, x, y + 11)] if heading else []
        for i, (colour, text) in enumerate(rows):
            cy = y + 18 + 16 * i + 6
            if mark == "dot":
                out.append(f'<circle cx="{_num(x + 6)}" cy="{_num(cy)}" r="5" '
                           f'fill="{colour}" fill-opacity="0.85"/>')
            elif mark == "line":
                out.append(f'<line x1="{_num(x)}" y1="{_num(cy)}" x2="{_num(x + 20)}" '
                           f'y2="{_num(cy)}" stroke="{colour}" stroke-width="{LINE_W + 0.6}" '
                           'stroke-linecap="round"/>')
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


def _lines(layer, frame, anchors):
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


# ------------------------------------------------------------------- flow ---

def _flow(layer, frame, labels, anchors, layers):
    category = layer.get("category")
    colours = _palette(category, "flow")
    classes = [c["value"] for c in category["classes"]] if category else [None]
    sizes = [f["size"] for f in layer["features"] if f.get("size") is not None]
    vmax = max(sizes) if sizes else 1.0
    w_max = FLOW_W_MAX * frame.map_h

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
        out.append("</g>")

    # The three numbers the story is told with, written on the map.
    notes = layer.get("notes") or []
    for note in notes:
        x, y = _xy(frame, note["lon"], note["lat"])
        labels.note(note["text"], x, y, colours.get(note.get("category"), TEXT))

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
            labels.place(f["name"], x, y, italic=True, bold=bool(f.get("major")))
    return "".join(out), []


# ---------------------------------------------------------------- labels ---

class _Labels:
    """Greedy, deterministic label placement against everything placed so far."""

    OFFSETS = ((1, 0), (-1, 0), (1, -1), (1, 1), (-1, -1), (-1, 1), (0, -1), (0, 1))

    def __init__(self, layout, frame):
        self.left = layout["pad"] + 2
        self.right = layout["pad"] + layout["map_w"] - 2
        self.top = layout["map_top"] + 2
        self.bottom = layout["map_top"] + frame.map_h - 2
        self.frame = frame
        self.taken: list[tuple] = []
        self.out: list[str] = []
        self.context_count = 0

    def block(self, x0, y0, x1, y1):
        self.taken.append((x0, y0, x1, y1))

    def _free(self, box) -> bool:
        x0, y0, x1, y1 = box
        if x0 < self.left or x1 > self.right or y0 < self.top or y1 > self.bottom:
            return False
        return not any(x0 < b[2] and x1 > b[0] and y0 < b[3] and y1 > b[1]
                       for b in self.taken)

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
            if self._free(box):
                self.taken.append(box)
                self.out.append(
                    f'<text x="{_num(tx)}" y="{_num(ty)}" font-size="{size:g}" '
                    f'text-anchor="{anchor}" fill="{TEXT}" {HALO}{extra}>'
                    f'{escape(text)}</text>')
                return True
        return False

    def place(self, text, x, y, italic=False, bold=False):
        extra = (' font-style="italic"' if italic else "") + (
            ' font-weight="600"' if bold else "")
        return self._try(text, x, y, 4.0, LABEL_SIZE, extra)

    def callout(self, text, x, y, r, bold=False):
        """A label next to a circle, or further out on a leader line."""
        extra = ' font-weight="600"' if bold else ""
        if self._try(text, x, y, r + 3, LABEL_SIZE, extra):
            return True
        for reach in (28, 48):
            for dx, dy in ((1, -1), (-1, -1), (1, 1), (-1, 1)):
                lx, ly = x + dx * reach, y + dy * reach * 0.7
                before = len(self.out)
                if self._try(text, lx, ly, 2.0, LABEL_SIZE, extra):
                    self.out.insert(before, f'<line x1="{_num(x + dx * r * 0.7)}" '
                                    f'y1="{_num(y + dy * r * 0.7)}" x2="{_num(lx)}" '
                                    f'y2="{_num(ly)}" stroke="{MUTED}" stroke-width="0.6"/>')
                    return True
        return False

    def note(self, text, x, y, colour):
        extra = f' font-weight="700" fill-opacity="1" style="fill:{colour}"'
        self._try(text, x, y, 10.0, 11.0, extra)

    def context(self, places, anchors):
        """Natural Earth places: the ones the overlay serves first, then size."""
        candidates = []
        for p in places:
            x, y = _xy(self.frame, p["lon"], p["lat"])
            if not self.frame.visible(x, y):
                continue
            served = any(abs(ax - x) <= SERVED_PX and abs(ay - y) <= SERVED_PX
                         and math.hypot(ax - x, ay - y) <= SERVED_PX for ax, ay in anchors)
            candidates.append((not served, -p["pop"], p["name"], x, y, served))
        candidates.sort()
        dots, texts = [], []
        bold_left = CONTEXT_BOLD
        for _, _, name, x, y, served in candidates:
            if self.context_count >= CONTEXT_LABELS:
                break
            box = (x - 2.5, y - 2.5, x + 2.5, y + 2.5)
            if not self._free(box):
                continue
            bold = served and bold_left > 0
            before = len(self.out)
            self.taken.append(box)
            ok = self.place(name, x, y, bold=bold) if not bold else self._try(
                name, x, y, 5.0, 11.0, ' font-weight="700"')
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
