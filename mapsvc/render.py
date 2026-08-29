"""SVG generation.

This module must not import anything that performs I/O -- no network, no
filesystem, no cache. It takes a validated manifest and an already-harvested
result and returns a string. `tests/test_no_io.py` enforces that by inspection.

Output is deterministic: rows are drawn in sorted id order, coordinates are
formatted to a fixed precision, and nothing carries a timestamp. The same
manifest twice must produce byte-identical bytes.
"""

import textwrap
from xml.sax.saxutils import escape, quoteattr

from mapsvc import classify, colors, project, registry, statistics
from mapsvc.manifest import Manifest
from mapsvc.models import HarvestResult

CANVAS_W = 960
MAP_H = 620
FOOTER_H = 116
CANVAS_H = MAP_H + FOOTER_H
MAP_MARGIN = 18
COORD_DP = 2
# Characters that fit across the footer at 11.5px in the chosen font.
FOOTER_CHARS = 148

SWATCH_W, SWATCH_H, SWATCH_GAP = 20, 13, 4
LEGEND_W, LEGEND_INSET = 212, 20
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"


class RenderError(ValueError):
    """Raised when the manifest cannot be honoured for this data."""

    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


def render(manifest: Manifest, result: HarvestResult) -> str:
    rows = sorted(result.rows, key=lambda r: r["id"])
    dropped = sorted(result.dropped, key=lambda r: r["id"])
    show_missing = manifest.missing != "exclude"

    extent_geoms = [r["geometry"] for r in rows]
    if show_missing:
        extent_geoms += [d["geometry"] for d in dropped if d.get("geometry")]
    if not extent_geoms:
        raise RenderError("no features to draw for this region", "region")

    extent = project.extent_of(extent_geoms)
    name = manifest.projection
    if name == "auto":
        name = project.choose(extent)
    projector = project.make(name, extent)
    bounds = project.viewport_bounds(extent_geoms, projector, extent)
    transform = project.fit(bounds, CANVAS_W, MAP_H, MAP_MARGIN)
    lon0 = (extent[0] + extent[2]) / 2.0

    scheme, swatches = (None, None) if manifest.variable_id is None else _scheme(manifest, rows)

    # Declared explicitly so a saved .svg is self-describing about its encoding
    # (the footnote carries en dashes and a division sign).
    parts: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>']
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS_W}" '
        f'height="{CANVAS_H}" viewBox="0 0 {CANVAS_W} {CANVAS_H}" '
        f'role="img" aria-label={quoteattr(_title(result.provenance))}>'
    )
    parts.append(_defs())
    parts.append(
        f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="{colors.OCEAN}"/>'
    )

    drawn: list[tuple[float, float]] = []
    parts.append('<g clip-path="url(#map-clip)">')
    for row in rows:
        # A base map paints one neutral fill: there is no variable to encode.
        fill = (colors.BASEMAP_FILL if scheme is None
                else swatches[scheme.bin_of(row["value"])])
        parts.append(_path(row, fill, projector, transform, lon0, drawn))
    if show_missing:
        fill = f"url(#{colors.HATCH_ID})" if manifest.missing == "hatch" else colors.MISSING_GREY
        for item in dropped:
            if item.get("geometry"):
                parts.append(_path(item, fill, projector, transform, lon0, drawn))
    parts.append("</g>")

    if scheme is not None:
        parts.append(_legend(manifest, scheme, swatches, dropped, show_missing,
                             result.provenance, drawn))
    footer, footer_lines = _footer(manifest, name, scheme, result)
    parts.append(footer)
    parts.append("</svg>")
    svg = "\n".join(parts) + "\n"
    # The canvas is declared before the footer is laid out, so if attribution
    # pushed it past the reserved band, restate the height rather than clip it.
    needed = MAP_H + 26 + footer_lines * 17 + 12
    if needed > CANVAS_H:
        svg = svg.replace(f'height="{CANVAS_H}" viewBox="0 0 {CANVAS_W} {CANVAS_H}"',
                          f'height="{needed}" viewBox="0 0 {CANVAS_W} {needed}"', 1)
    return svg


def _scheme(manifest: Manifest, rows: list) -> tuple[classify.Classification, list[str]]:
    """Classification plus one colour per bin."""
    if not rows:
        raise RenderError("every feature was dropped; nothing to classify", "variable")
    # Dividing by another column yields a continuous ratio whatever the source
    # variable's measurement level was.
    # The measurement level decides graded-vs-categorical bins. It comes from
    # the source, which for a searchable one means a metadata lookup.
    level = ("ratio" if manifest.normalize
             else statistics.describe(manifest.variable_source,
                                      manifest.variable_id).level)
    if level == "nominal" and colors.kind(manifest.ramp) != "qualitative":
        # For a searchable source the measurement level is not known until the
        # metadata has been fetched, which is after validation. Catching it here
        # keeps the rule enforced whichever kind of source supplied the values.
        raise RenderError(
            f"{manifest.variable_id} is nominal, so a {colors.kind(manifest.ramp)} "
            f"ramp like {manifest.ramp!r} would imply an ordering between "
            "categories; use a qualitative ramp", "ramp",
        )
    scheme = classify.build([r["value"] for r in rows], level, manifest.method, manifest.k)
    try:
        return scheme, colors.colors(manifest.ramp, scheme.k)
    except colors.RampError as exc:
        raise RenderError(str(exc), "ramp") from exc


def _num(v: float) -> str:
    s = f"{v:.{COORD_DP}f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0", "-") else s


def _rings(geometry) -> list:
    """Every ring, exterior and interior, so holes survive."""
    if geometry["type"] == "Polygon":
        return list(geometry["coordinates"])
    return [ring for part in geometry["coordinates"] for ring in part]


def _path(item: dict, fill: str, projector, transform, lon0: float,
          drawn: list | None = None) -> str:
    subpaths: list[str] = []
    for ring in _rings(item["geometry"]):
        points: list[str] = []
        previous = None
        for lon, lat in project.unwrap_ring(ring, lon0):
            x, y = transform(*projector(lon, lat))
            point = (_num(x), _num(y))
            if point != previous:
                points.append(f"{point[0]},{point[1]}")
                previous = point
                if drawn is not None:
                    drawn.append((x, y))
        if len(points) < 3:
            continue  # collapsed to a sliver at this scale
        subpaths.append("M" + "L".join(points) + "Z")
    if not subpaths:
        return ""
    return (
        f'<path d="{"".join(subpaths)}" fill="{fill}" fill-rule="evenodd" '
        f'stroke="{colors.BORDER}" stroke-width="0.4" '
        f'data-id={quoteattr(item["id"])}/>'
    )


def _defs() -> str:
    return (
        "<defs>"
        f'<pattern id="{colors.HATCH_ID}" width="6" height="6" '
        'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        f'<rect width="6" height="6" fill="{colors.HATCH_BG}"/>'
        f'<line x1="0" y1="0" x2="0" y2="6" stroke="{colors.HATCH_STROKE}" stroke-width="2"/>'
        "</pattern>"
        f'<clipPath id="map-clip"><rect x="0" y="0" width="{CANVAS_W}" height="{MAP_H}"/></clipPath>'
        "</defs>"
    )


def _legend(manifest, scheme, swatches, dropped, show_missing, provenance, drawn) -> str:
    labels = scheme.labels()
    entries = list(zip(swatches, labels))
    if show_missing and dropped:
        fill = f"url(#{colors.HATCH_ID})" if manifest.missing == "hatch" else colors.MISSING_GREY
        entries.append((fill, "no data"))

    heading = manifest.variable_id
    if manifest.normalize:
        heading += f" / {manifest.normalize}"
    # Without the unit, "0.003465" is unreadable: dividing million USD by people
    # gives million USD per person, not dollars.
    subheading = _unit_label(manifest, provenance)

    box_h = 26 + (13 if subheading else 0) + len(entries) * (SWATCH_H + SWATCH_GAP)
    x, y = _legend_origin(drawn, LEGEND_W, box_h)
    out = [
        f'<g font-family="{FONT}">',
        f'<rect x="{x}" y="{y}" width="{LEGEND_W}" height="{box_h}" fill="#ffffff" '
        'fill-opacity="0.94" stroke="#bfbfbf" stroke-width="0.7" rx="3"/>',
        f'<text x="{x + 10}" y="{y + 17}" font-size="11.5" font-weight="600" '
        f'fill="#1a1a1a">{escape(heading)}</text>',
    ]
    if subheading:
        out.append(
            f'<text x="{x + 10}" y="{y + 29}" font-size="10" '
            f'fill="#737373">{escape(subheading)}</text>'
        )
    top = y + 25 + (13 if subheading else 0)
    for i, (fill, label) in enumerate(entries):
        sy = top + i * (SWATCH_H + SWATCH_GAP)
        out.append(
            f'<rect x="{x + 10}" y="{sy}" width="{SWATCH_W}" height="{SWATCH_H}" '
            f'fill="{fill}" stroke="{colors.BORDER}" stroke-width="0.4"/>'
        )
        out.append(
            f'<text x="{x + 10 + SWATCH_W + 8}" y="{sy + SWATCH_H - 2.5}" '
            f'font-size="11" fill="#333333">{escape(label)}</text>'
        )
    out.append("</g>")
    return "".join(out)


def _legend_origin(drawn, box_w: float, box_h: float) -> tuple[float, float]:
    """Put the legend in whichever corner covers the least geometry.

    A fixed corner buries Portugal on a map of Europe. Corners are tried in a
    fixed order so ties resolve the same way every time and output stays
    byte-identical between runs.
    """
    inset = LEGEND_INSET
    corners = [
        (inset, MAP_H - box_h - inset),                  # bottom left
        (CANVAS_W - box_w - inset, MAP_H - box_h - inset),
        (inset, inset),                                  # top left
        (CANVAS_W - box_w - inset, inset),
    ]
    best, fewest = corners[0], None
    for x, y in corners:
        covered = sum(1 for px, py in drawn
                      if x <= px <= x + box_w and y <= py <= y + box_h)
        if fewest is None or covered < fewest:
            best, fewest = (x, y), covered
        if fewest == 0:
            break
    return best


def _unit_label(manifest: Manifest, provenance: dict) -> str:
    """Units of the classified value, after any normalisation."""
    unit = provenance.get("unit")
    if not manifest.normalize:
        return unit or ""
    divisor = provenance.get("normalize_unit") or manifest.normalize
    # "people" reads better singular in a per-unit phrase.
    divisor = {"people": "person"}.get(divisor, divisor)
    return f"{unit} per {divisor}" if unit else f"per {divisor}"


def _title(provenance: dict) -> str:
    variable = provenance.get("variable", "value")
    if provenance.get("normalize"):
        variable += f" per {provenance['normalize']}"
    return f"{provenance.get('region', 'map')} — {variable}"


def _footer(manifest, projection_name, scheme, result) -> str:
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

    if scheme is None:
        method = f"boundaries only, no variable ({len(result.rows)} units)"
    elif scheme.kind == "categorical":
        method = f"one class per category ({scheme.k} categories)"
    else:
        method = f"{manifest.method}, k={scheme.k}"
        if scheme.k != manifest.k:
            plural = "" if scheme.k == 1 else "s"
            method += f" (requested {manifest.k}; only {scheme.k} distinct value{plural})"

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
    if manifest.normalize:
        normalize = manifest.normalize
        if p.get("normalize_unit"):
            normalize += f" ({p['normalize_unit']})"
        variable += f" ÷ {normalize}"

    if dropped:
        reasons: dict[str, int] = {}
        for item in dropped:
            reasons[item.get("reason", "unknown")] = reasons.get(item.get("reason", "unknown"), 0) + 1
        detail = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
        shown = "shown as no data" if manifest.missing != "exclude" else "not drawn"
        excluded = f"{len(dropped)} of {len(dropped) + len(result.rows)} features excluded ({detail}), {shown}"
    else:
        excluded = "0 features excluded"

    lines = [
        f"Source: {source}. Projection: {_projection_label(projection_name, manifest)}. "
        f"Classification: {method}.",
        f"Variable: {variable}.",
        excluded + ".",
    ]

    # Attribution can make the first line longer than the canvas. There is no
    # text measurement in a bare SVG, so wrap on a character budget tuned to the
    # font size; overflowing silently off the right edge is worse than a guess.
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(textwrap.wrap(line, width=FOOTER_CHARS) or [""])

    out = [f'<g font-family="{FONT}">',
           f'<line x1="20" y1="{MAP_H + 1}" x2="{CANVAS_W - 20}" y2="{MAP_H + 1}" '
           'stroke="#d9d9d9" stroke-width="1"/>']
    for i, line in enumerate(wrapped):
        out.append(
            f'<text x="20" y="{MAP_H + 26 + i * 17}" font-size="11.5" '
            f'fill="#4d4d4d">{escape(line)}</text>'
        )
    out.append("</g>")
    return "".join(out), len(wrapped)


_PROJECTION_LABELS = {
    "albers": "Albers equal-area conic",
    "mercator": "Mercator",
    "mollweide": "Mollweide equal-area",
}


def _projection_label(name: str, manifest: Manifest) -> str:
    label = _PROJECTION_LABELS.get(name, name)
    return f"{label} (auto)" if manifest.projection == "auto" else label
