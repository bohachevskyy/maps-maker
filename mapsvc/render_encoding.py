"""Derived value encodings and SVG legends."""

from __future__ import annotations

import math
import re
from xml.sax.saxutils import escape

from mapsvc import classify, colors, registry
from mapsvc.models import Manifest

from mapsvc.render_frame import (
    FONT,
    Frame,
    MAP_W,
    PAD,
    RenderError,
    SYMBOL_MIN_R,
    _least_covered,
    _num,
    _text_w,
)

SWATCH_W, SWATCH_H, SWATCH_GAP = 20, 13, 4

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
        # (fill, label) keys after the hatch: neutral land drawn but not mapped.
        self.extra_keys: list = []
        # (left, right) words under a diverging strip: who leads on each side.
        self.end_captions: tuple | None = None
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
        direction = provenance.get("diverging") or {}
        if center_at is not None and direction.get("negative") and direction.get("positive"):
            enc.end_captions = (_side_caption(direction, "negative"),
                                _side_caption(direction, "positive"))
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

def _side_caption(direction: dict, side: str) -> str:
    """'Harris (D) ahead' / 'Democratic lead': one side of a diverging scale."""
    lead = str(direction[side])
    name = direction.get(f"{side}_candidate")
    if not name:
        return lead
    party = lead.split(" ")[0]
    return f"{name} ({party[0]}) ahead" if party else f"{name} ahead"

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
    return entries + list(encoding.extra_keys)

def _outside_keys(result) -> list[tuple[str, str]]:
    """Legend keys for the neutral land that is drawn but is not data.

    Neighbouring countries on a `within` map are "outside the set"; the
    land with no permanent population (Antarctica) is named as such. Both
    are one swatch when a map draws both.
    """
    if not result.context:
        return []
    p = result.provenance
    plain = [str(n) for n in p.get("plain_land") or []]
    neighbours = bool(p.get("context_source"))
    if neighbours and plain:
        label = "Outside the set, or uninhabited"
    elif neighbours:
        label = "Outside the set"
    elif plain and plain[0] == "Antarctica":
        label = "Uninhabited (Antarctica)"
    elif plain:
        label = "Uninhabited land"
    else:
        return []
    return [(colors.CONTEXT_LAND, label)]

def _plain_keys(encoding: Encoding, has_missing: bool) -> list[tuple[str, str]]:
    """The keys that follow the scale: the hatch, then neutral land."""
    keys = [(f"url(#{colors.HATCH_ID})", encoding.missing_label)] if has_missing else []
    return keys + list(encoding.extra_keys)

def _key_swatch(fill: str, x: float, y: float, w: float = SWATCH_W) -> str:
    if fill.startswith("url("):
        return _hatch_swatch(x, y, w)
    return (f'<rect x="{_num(x)}" y="{_num(y)}" width="{_num(w)}" height="{SWATCH_H}" '
            f'fill="{fill}" stroke="{colors.BORDER}" stroke-width="0.4" class="legend-key"/>')

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
        keys = _plain_keys(encoding, has_missing)
        for _, label in keys:
            w = max(w, 20 + SWATCH_W + 8 + _text_w(label, 10.5))
        h = 12 + head_h + 8 + 2 * big + 22 * len(keys) + 10

        def draw(x, y):
            out = [f'<g font-family="{FONT}">', _box(x, y, w, h)]
            out += _heading(encoding, x + 10, y + 20)
            base = y + 12 + head_h + 8 + 2 * big
            out += _nested_circles(encoding, refs, labels, x + 10 + big, base)
            for i, (fill, label) in enumerate(keys):
                out.append(_key_swatch(fill, x + 10, base + 9 + 22 * i))
                out.append(f'<text x="{_num(x + 10 + SWATCH_W + 8)}" '
                           f'y="{_num(base + 19.5 + 22 * i)}" '
                           f'font-size="10.5" fill="#333333">{escape(label)}</text>')
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
            mx = x + 2 * big + 10 + max(_text_w(s, 10.5) for s in labels) + 36
            for fill, label in _plain_keys(encoding, has_missing):
                out.append(_key_swatch(fill, mx, base - SWATCH_H))
                out.append(f'<text x="{_num(mx + SWATCH_W + 8)}" y="{_num(base - 2.5)}" '
                           f'font-size="10.5" fill="#333333">{escape(label)}</text>')
                mx += SWATCH_W + 8 + _text_w(label, 10.5) + 24
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
    # The keys after the strip (no data, uninhabited ...) must stay on the
    # page: a long label, or many classes, narrows the strip rather than
    # running its last key off the edge.
    keys_w = sum(38 + _text_w(label, 10.5) + 22
                 for _, label in _plain_keys(encoding, has_missing))
    cell = min(84.0, (MAP_W - 160) / k, max(34.0, (MAP_W - 28 - keys_w + 22) / k))
    if scheme.scheme == "quantile":
        edges = [classify.format_value(b) for b in scheme.breaks]
    else:
        affixes = _affixes(encoding.unit)
        edges = [scheme.edge_label(i, *affixes) for i in range(k + 1)]
        if scheme.open_ends:
            # The outer edges are the data's extremes, not class limits: the
            # end classes are open ("< $2k"), so their ends go unwritten.
            edges[0] = edges[-1] = ""
    captions = encoding.end_captions
    h = head_h + 8 + SWATCH_H + 16 + (14 if captions else 0)

    def draw(x, y):
        out = [f'<g font-family="{FONT}">']
        out += _heading(encoding, x, y + 12)
        sy = y + head_h + 8
        if captions:
            cy = sy + SWATCH_H + 28
            out.append(f'<text x="{_num(x)}" y="{_num(cy)}" font-size="10.5" '
                       f'font-weight="600" fill="#333333">\u2190 {escape(captions[0])}</text>')
            out.append(f'<text x="{_num(x + k * cell)}" y="{_num(cy)}" font-size="10.5" '
                       f'font-weight="600" text-anchor="end" fill="#333333">'
                       f'{escape(captions[1])} \u2192</text>')
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
        mx = x + k * cell + 28
        for fill, label in _plain_keys(encoding, has_missing):
            out.append(_key_swatch(fill, mx, sy, 30))
            out.append(f'<text x="{_num(mx + 38)}" y="{_num(sy + SWATCH_H - 2.5)}" '
                       f'font-size="10.5" fill="#333333">{escape(label)}</text>')
            mx += 38 + _text_w(label, 10.5) + 22
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

def _label(manifest: Manifest, provenance: dict) -> str:
    """The quantity's human name. Never an id: a Eurostat id is a query string."""
    label = provenance.get("variable")
    if not label:
        label = str(manifest.variable_id or "").split("?")[0].replace("_", " ")
    # An agency's own wording ("Present population" is Ukrstat's term for the
    # people living there now) is not what a reader calls it.
    label = re.sub(r"^present population\b", "population", str(label), flags=re.I)
    return _human(label)
