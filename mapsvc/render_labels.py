"""Feature labels, callouts, side tables, and geographic context names."""

from __future__ import annotations

import math
import re
from xml.sax.saxutils import escape, quoteattr

from mapsvc import classify, colors, overlay_render, project

from mapsvc.render_frame import (
    FONT,
    Frame,
    MAP_TOP,
    MAP_W,
    MARKER_R,
    MIN_VISIBLE_AREA,
    PAD,
    _d,
    _ground_area,
    _num,
    _physical_line,
    _piece,
    _project,
    _short_name,
    _text_w,
    _visible_area,
)

from mapsvc.render_encoding import (
    Encoding,
    _affixes,
    _box,
    _margin_sides,
)

from mapsvc.render_footer import (
    _REASONS,
)

LABEL_MAX_UNITS = 60

LABEL_EXTREMES = 2

LABEL_PER_SIDE = 3

LABEL_PER_SIDE_STEP = 150

LABEL_MIN_AREA = 40.0

LABEL_POPULOUS_EXTRA = 3

LABEL_APART = 45.0

NAME_SIZE, VALUE_SIZE = 9.5, 8.5

SMALL = 0.84

LABEL_INK = "#1f2a30"

LABEL_HALO = ('stroke="#ffffff" stroke-width="3" stroke-linejoin="round" '
              'stroke-opacity="0.9" paint-order="stroke"')

LEADER = "#5d6870"

CALLOUT_REACH = (8.0, 14.0, 22.0, 32.0, 44.0, 58.0, 74.0, 92.0, 112.0, 136.0,
                 164.0, 196.0, 232.0, 272.0, 316.0)

CALLOUT_DIRECTIONS = ((1, 0), (-1, 0), (1, -0.5), (1, 0.5), (-1, -0.5), (-1, 0.5),
                      (1, -1), (1, 1), (-1, -1), (-1, 1), (0.5, -1), (0.5, 1),
                      (-0.5, -1), (-0.5, 1))

CALLOUT_MAX_COVER = 0.08

CALLOUT_NAMES_COVER = 0.12

STACK_NEAR = 64.0

LEADER_MAX = 70.0

LEADER_SHORT = 60.0

LEADER_LIMIT = 70.0

LEADER_FAR = 70.0

LEADER_LAND_SLACK = 3.0

STACK_GAP = 1.5

STACK_STEP_X = 6.0

STACK_STEP_Y = 5.0

STACK_SHIFT_MAX = 260.0

_KIND_WORDS = ("Oblast", "County", "Province", "Region", "Governorate", "Prefecture",
               "Voivodeship", "Department", "District", "Raion", "Municipality",
               "Parish", "Borough", "Census Area", "City and Borough")

_KIND_PREFIXES = ("Autonomous Republic of ",)

_KIND_SUFFIXES = (", Kreisfreie Stadt", ", Stadtkreis", ", Landkreis", ", Kreis",
                  ", Landeshauptstadt", ", Universitätsstadt", ", Stadt", ", Hansestadt",
                  ", Wissenschaftsstadt", ", Große Kreisstadt", ", Regionalverband")

_COUNTRY_SHORT = {"United Arab Emirates": "UAE", "United States of America": "United States",
                  "Prince Edward Island": "P.E.I.", "District of Columbia": "D.C."}

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
    if encoding.value_scale != 1.0:
        # An index (EU27 = 100): the legend's unit, with the source's own in
        # brackets for the reader who wants it.
        return f"{v * encoding.value_scale:.0f} ({_short_count(v)})"
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
                 inset=None, r=0.0, primary=False, centre=False):
        self.centre = centre        # the leader starts at the unit, not the circle's edge
        self.primary = primary      # worth a leader across a neighbour: the one highest
        # or lowest, or any unit of a map that names only a chosen few
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

    def crossed(self, seg, ring, stop: float = LEADER_LAND_SLACK) -> float:
        """Length (px) of a leader over mapped land that is not its own unit's.

        `ring` is the unit's outline: leaving it and meeting land again is
        crossing another unit.
        """
        sx, sy, ex, ey = seg
        length = math.hypot(ex - sx, ey - sy)
        n = max(2, int(length / 2.0))
        over = 0.0
        for k in range(n + 1):
            x = sx + (ex - sx) * k / n
            y = sy + (ey - sy) * k / n
            if any(a <= x <= b for a, b in self.intervals(y)) and not (
                    ring and project.point_in_ring(x, y, ring)):
                over += length / n
                if over > stop:
                    break
        return over

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
    if frame.table_box:
        placer.block(*frame.table_box)
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

def _feature_labels(rows: list, insets: list, missing: list, frame: Frame,
                    encoding: Encoding, land: list, placer, every: int,
                    change: bool = False, fresh=None,
                    enclaves: frozenset = frozenset()) -> tuple[str, list]:
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
    # Under circles the units are plain land: a name may sit on it. Only a
    # colour-filled unit must not be covered by another's callout.
    space = _Space(land if encoding.mode != "symbols" else [], frame)
    extremes_only = every > LABEL_MAX_UNITS

    def attempt(into, codes: bool):
        out: list[str] = []
        unlabelled: list[str] = []
        leaders: list = []
        if encoding.mode == "symbols":
            pending = _circle_labels(encoding, names, into, out, codes, enclaves)
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
        if len(plain[1]) <= len(mixed[1]):
            (out, unlabelled), chosen = plain, plain_placer
        else:
            (out, unlabelled), chosen = mixed, mixed_placer
        placer.__dict__.update(chosen.__dict__)
    else:
        out, unlabelled = attempt(placer, True)
    if not out:
        return "", unlabelled
    dropped = " ".join(sorted(unlabelled))
    return (f'<g font-family="{FONT}" class="feature-labels" '
            f'data-labels-dropped="{len(unlabelled)}" data-unlabelled={quoteattr(dropped)}>'
            + "".join(out) + "</g>"), unlabelled

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
    story = _populous_dense(units, valued, encoding, per_side + LABEL_POPULOUS_EXTRA)
    diverging = scheme is not None and (
        scheme.zero_at is not None or scheme.center_at is not None)
    if scheme is not None and not diverging and len(valued) >= 3:
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
            apart = primary or diverging or all(
                math.hypot(at[gid][0] - at[o][0], at[gid][1] - at[o][1]) >= LABEL_APART
                for o in picked if o in at and gid in at)
            seen = area.get(gid, 0.0) >= MIN_VISIBLE_AREA
            if (big and apart) or (primary and (seen or not change)):
                picked.append(gid)
        return picked

    if story:
        # A density map is told by its cities: the one emptiest and the one
        # densest unit, then the most populous of the dense ones.
        keep = {valued[0][1], valued[-1][1]} | story
    else:
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

_PER_AREA = re.compile(r"\bper\s+(square\s+kilomet|sq\.?\s*km|km|square\s+mile|sq\.?\s*mi)",
                      re.I)

def _geometry_km2(geometry: dict) -> float:
    polygons = (geometry.get("coordinates") or []
                if geometry.get("type") == "MultiPolygon"
                else [geometry.get("coordinates") or []])
    return sum(_ground_area([tuple(p[:2]) for p in polygon[0]])
               for polygon in polygons if polygon and len(polygon[0]) >= 3)

def _populous_dense(units, valued, encoding, n: int) -> set:
    """For a density map of many units, the most populous of the dense ones.

    A density is told by its cores: the dark patches a reader looks for are
    the big cities (Hamburg, Koeln, Muenchen), not the n-th densest district.
    People per unit is density times ground area, so the harvest rows are
    enough; the dense ones are the top 15% by density. Empty for anything that
    is not a per-area quantity.
    """
    if (not encoding.unit or not _PER_AREA.search(encoding.unit)
            or encoding.mode != "choropleth" or len(valued) < LABEL_MAX_UNITS):
        return set()
    by_id = {str(row["id"]): row for row, _, _ in units}
    cutoff = valued[int(len(valued) * 0.85)][0]
    people = sorted(((value * _geometry_km2(by_id[gid]["geometry"]), gid)
                     for value, gid in valued if value >= cutoff and gid in by_id),
                    reverse=True)
    return {gid for _, gid in people[:n]}

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
                               rank(row, piece), inset=inset,
                               primary=(extremes_only and not frame.is_world)
                               or order.get(gid) in (0, len(valued) - 1)))
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
                # A unit too small for its hatch (Sevastopol) is drawn plain:
                # its outline must carry it, in the ink of the labels.
                tiny = _visible_area(parts) < MIN_VISIBLE_AREA
                out.append(f'<path d="{d}" fill="none" '
                           f'stroke="{colors.TEXT if tiny else colors.TEXT_MUTED}" '
                           f'stroke-width="{1.1 if tiny else 0.7}" class="missing-outline" '
                           f'data-outline-of={quoteattr(gid)}/>')
            t = _Target(gid, x, y, ring, holes, names.get(gid, _short_name(item)),
                        MISSING_TEXT, None, (3, 0.0, -project.planar_area(ring), gid),
                        inset=owner)
            if not _inside_label(t.blocks(), x, y, ring, holes, placer, insets, out, gid):
                pending.append(t)
    return pending

def _circle_labels(encoding, names, placer, out, codes=True, enclaves=frozenset()) -> list:
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
        if gid in enclaves:
            # Its circle hides the small unit beneath: a label beside the
            # circle would read as the neighbour's. A leader from the unit
            # itself, through the circle's edge, says whose it is.
            spots = []
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
                                   (0 if n == 0 else 1, n, 0.0, gid), r=r,
                                   centre=gid in enclaves))
    return pending

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
    if not codes:
        # Names only, so no code to fall back on: the units still unplaced
        # share columns in the nearest free space, however far, in the order
        # of the units on the ground -- not each its own crossing leader.
        far = [t for t in rest if t.inset is None and t.rank[0] != 3]
        rest = ([t for t in rest if t not in far]
                + _stack_phase(far, lambda t: LEADER_FAR, placer, space, frame, out, leaders))

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
            if spot is None:
                continue
            # The nearer side first; the other, if that column is blocked.
            for side in (spot[0], -spot[0]):
                if not _stack([t], side, placer, space, frame, out, leaders, limit,
                              use_code=use_code):
                    placed = True
                    break
            if placed:
                break
        # The last resort, for the highest and lowest only: a callout over a
        # little mapped land, then over as much as it takes. A middling unit
        # goes unlabelled rather than have its name sit on a neighbour.
        # Without codes to fall back on (one name style), a middling unit may
        # also be called out over a little land.
        covers = ((CALLOUT_MAX_COVER,) if t.rank[0] == 0
                  else (CALLOUT_MAX_COVER, CALLOUT_NAMES_COVER) if not codes else ())
        for cover in covers:
            for line in t.lines():
                if placed:
                    break
                placed = _radial(t, line, cover, placer, space, out, leaders,
                                 LEADER_LIMIT if codes or t.rank[0] == 0 else LEADER_FAR)
        if not placed and t.primary and t.r <= 0:
            # The single highest and lowest unit are worth a leader across a
            # neighbour or two, still within the cap and crossing no label.
            for use_code in ((False, True) if t.code else (False,)):
                line = _Line(t.code if use_code else t.name, t.value)
                spot = _nearest_spot(t, line, placer, space, cross=True)
                if spot is not None and not _stack([t], spot[0], placer, space, frame, out,
                                                   leaders, LEADER_LIMIT, use_code=use_code,
                                                   cross=True):
                    placed = True
                    break
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

def _nearest_spot(t, line, placer, space, cross=False):
    """(side, distance) of the nearest box in free space for this label."""
    for reach in CALLOUT_REACH:
        for dx, dy in CALLOUT_DIRECTIONS:
            if dx == 0:
                continue
            norm = math.hypot(dx, dy)
            ex, ey = t.x + dx / norm * (t.r + reach), t.y + dy / norm * (t.r + reach)
            side = 1 if dx > 0 else -1
            box = line.box(ex + side * 2.0, ey - line.h / 2, side)
            if placer.fits(box) and space.free(box) and (
                    cross or t.r > 0
                    or space.crossed((t.x, t.y, ex, ey), t.ring) <= LEADER_LAND_SLACK):
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
    if t.r <= 0 or t.centre:
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
           use_code=False, dry=False, start=None, cross=False) -> list:
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
            if not cross and any(t.r <= 0 and space.crossed(seg, t.ring) > LEADER_LAND_SLACK
                                 for seg, t in zip(segs, members)):
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
                      out, leaders, max_leader, use_code, start=start, cross=cross)
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
    dot = ("" if t.r > 0 and not t.centre else
           f'<circle cx="{_num(sx)}" cy="{_num(sy)}" r="1.3" fill="{LEADER}" '
           'stroke="#ffffff" stroke-width="0.5" class="leader-end"/>')
    return (f'<line x1="{_num(sx)}" y1="{_num(sy)}" x2="{_num(ex)}" y2="{_num(ey)}" '
            f'stroke="{LEADER}" stroke-width="0.6" class="leader"/>' + dot)

def _radial(t, line, max_cover, placer, space, out, leaders,
            limit: float = None, cross: bool = False) -> bool:
    """A single callout, nearest first, over at most `max_cover` mapped land."""
    limit = LEADER_LIMIT if limit is None else limit
    for reach in (r for r in CALLOUT_REACH if r <= limit):
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
            if not cross and t.r <= 0 and space.crossed(seg, t.ring) > LEADER_LAND_SLACK:
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

CONTEXT_INK = "#7b8288"

SEA_INK = "#5b7d93"

RIVER_INK = "#4d7ea6"

CONTEXT_HALO = ('stroke="#ffffff" stroke-width="2.4" stroke-linejoin="round" '
                'stroke-opacity="0.7" paint-order="stroke"')

SEA_SIZE, COUNTRY_SIZE, RIVER_SIZE = 10.0, 8.5, 8.5

SEA_MIN_LABEL = ((100.0, 2.0), (45.0, 4.0), (25.0, 5.0), (0.0, 6.0))

SEA_LABELS, COUNTRY_LABELS, RIVER_LABELS = 5, 10, 5

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
    reach = min(max((box[3] for box, _ in land), default=frame.bottom) + 60.0,
                frame.core_bottom)
    _country_labels(result, rows, frame, encoding, manifest, placer, mapped, out, reach)
    aliases: dict = {}
    for layer in result.overlays:
        aliases.update(layer.get("river_names") or {})
    _river_labels(result.physical, frame, placer, out, aliases)
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
            if box[3] > frame.core_bottom or not (
                    placer.fits(box) and water.free(box, pad=4.0)):
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

def _country_labels(result, rows, frame, encoding, manifest, placer, mapped, out,
                    lowest: float = float("inf")) -> int:
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
                # Data labels win: a context name keeps a clear 4 px around it
                # from every one, so it is never read as part of a label.
                if not _fits_inside(box, thin, []) or not placer.fits(
                        (box[0] - 3.0, box[1] - 4.0, box[2] + 3.0, box[3] + 4.0)):
                    continue
                # On the map itself: a neighbour reaches under the insets, and
                # the page below the mapped land is blank.
                if box[3] > lowest or not all(frame.visible(x, y) for x, y in (
                        (box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3]))):
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

def _river_labels(layers, frame, placer, out, aliases=None) -> int:
    """Major rivers, named along a straight reach of each: those the overlay
    crosses first, then by Natural Earth's rank, then the longest on the page."""
    runs: dict = {}
    ranks: dict = {}
    for layer in layers or []:
        if layer.get("class") != "rivers" or layer.get("kind") != "lines":
            continue
        for feature in layer.get("features", []):
            name = str(feature.get("name") or "").strip()
            name = (aliases or {}).get(name, name)
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

    named = set((aliases or {}).values())
    ranked = sorted(((name not in named, not served(rs), math.floor(ranks.get(name, 9.0)),
                      -sum(length(r) for r in rs), name) for name, rs in runs.items()))
    placed = 0
    for plain, unserved, rank, neg_total, name in ranked:
        total = -neg_total
        if placed >= RIVER_LABELS and plain:
            break
        # Major: Natural Earth shows it by zoom 5, or the overlay crosses a
        # long reach of it. The Mūša is neither.
        if total < RIVER_MIN_PX or (plain and rank > RIVER_MAJOR
                                    and (unserved or total < 2 * RIVER_MIN_PX)):
            continue
        w = _text_w(name, RIVER_SIZE) * 1.02
        h = RIVER_SIZE
        spot = None
        for run in sorted(runs[name], key=lambda r: (-length(r), r[0])):
            spot = _river_spot(run, w, h, frame, placer, 88.0 if not plain else 65.0)
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

def _river_spot(run, w, h, frame, placer, steepest: float = 65.0):
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
        if abs(angle) > steepest:
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

TABLE_ROWS = 5

TABLE_PITCH = 14.0

def _ranked_table(every_row: list, rows: list, encoding: Encoding):
    """A "Highest / Lowest" table of the extremes: (w, h, draw(x, y, boxed)).

    For a map whose callouts could not name every unit: the reader still
    gets the extremes, with their values, in a column of their own. None
    unless the variable is a number on a choropleth with enough units.
    """
    if encoding.mode != "choropleth":
        return None
    valued = sorted(((v, str(r["id"])) for r in rows
                     if (v := _number(r["value"])) is not None))
    n = min(TABLE_ROWS, len(valued) // 2)
    if n < 3:
        return None
    by_id = {str(r["id"]): r for r in rows}
    names = _label_names(rows)
    columns = []
    top, bottom = ("Highest", "Lowest")
    if encoding.end_captions:
        # A margin or a change reads from one end to the other: name the ends.
        bottom, top = encoding.end_captions[0], encoding.end_captions[1]
    for title, picked in ((top, valued[::-1][:n]), (bottom, valued[:n])):
        items = [(names.get(gid, _short_name(by_id[gid])), _value_text(by_id[gid]["value"], encoding))
                 for _, gid in picked]
        left = max(_text_w(name, 10.5) for name, _ in items)
        right = max(_text_w(value, 10.5) for _, value in items)
        columns.append((title, items, max(96.0, left + 14.0 + right,
                                          _text_w(title, 11) * 1.08)))
    gap = 26.0
    w = sum(c[2] for c in columns) + gap
    h = 16.0 + n * TABLE_PITCH + 4.0

    def draw(x, y, boxed):
        out = [f'<g font-family="{FONT}" class="ranked-table">']
        ox, oy = (x + 10, y + 10) if boxed else (x, y)
        if boxed:
            out.append(_box(x, y, w + 20, h + 20))
        cx = ox
        for title, items, cw in columns:
            out.append(f'<text x="{_num(cx)}" y="{_num(oy + 11)}" font-size="11" '
                       f'font-weight="600" fill="{colors.TEXT}">{title}</text>')
            for i, (name, value) in enumerate(items):
                ty = oy + 16 + (i + 1) * TABLE_PITCH - 3
                out.append(f'<text x="{_num(cx)}" y="{_num(ty)}" font-size="10.5" '
                           f'fill="#333333">{escape(name)}</text>')
                out.append(f'<text x="{_num(cx + cw)}" y="{_num(ty)}" font-size="10.5" '
                           f'text-anchor="end" fill="#333333">{escape(value)}</text>')
            cx += cw + gap
        out.append("</g>")
        return "".join(out)
    return w + 20, h + 20, draw
