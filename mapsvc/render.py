"""Pure SVG rendering orchestration.

The implementation is split by concern across ``render_frame``,
``render_encoding``, ``render_labels``, and ``render_footer``. This facade
retains the established ``mapsvc.render`` API.
"""

from __future__ import annotations

import dataclasses
import math
import re
from xml.sax.saxutils import escape, quoteattr

from mapsvc import colors, overlay_render, project, registry
from mapsvc.models import HarvestResult, Manifest

from mapsvc.render_frame import (
    CANVAS_W,
    FONT,
    Frame,
    MAP_H_MAX,
    MAP_TOP,
    MAP_W,
    MARKER_R,
    MIN_VISIBLE_AREA,
    PAD,
    RenderError,
    SYMBOL_SCALE,
    WORLD,
    _anchor,
    _context,
    _d,
    _defs,
    _draw_insets,
    density_inset_labels,
    _enclave_ids,
    _enclaves,
    _hierarchy,
    _least_covered,
    _marker,
    _outer_boxes,
    _path,
    _physical_context,
    _project,
    _raw_value,
    _split_insets,
    _symbols,
    _union,
    _unit_stroke_width,
    _visible_area,
    _water,
)

from mapsvc.render_encoding import (
    Encoding,
    _encoding,
    _legend,
    _outside_keys,
)

from mapsvc.render_labels import (
    LABEL_MAX_UNITS,
    _context_labels,
    _feature_labels,
    _missing_label,
    _placer,
    _ranked_table,
)

from mapsvc.render_footer import (
    _SAHARA,
    _aggregate_note,
    _boundary_credit,
    _footer,
    _overlay_title,
    _physical_credits,
    _subtitle,
    _subtitle_lines,
    _title,
    _without_idle_notes,
)

# Compatibility aliases for helpers exercised directly by the renderer's
# focused tests and historically available from mapsvc.render.
from mapsvc.render_frame import (
    CANVAS_W,
    MAP_H_MAX,
    MAP_TOP,
    MAP_W,
    PAD,
    SEPARATE_OVERLAP,
    _separate,
    _text_w,
)
from mapsvc.render_encoding import (
    _outside_keys,
    _reference_values,
    _side_caption,
    _source_decimals,
)
from mapsvc.render_labels import (
    LABEL_EXTREMES,
    LABEL_MAX_UNITS,
    LABEL_PER_SIDE,
    LABEL_PER_SIDE_STEP,
    LEADER_LIMIT,
    MISSING_TEXT,
    SEA_SIZE,
    _Space,
    _code,
    _label_names,
    _populous_dense,
    _segments_cross,
    _value_text,
)

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
    # Parent outlines follow the generalised coast; where a true shoreline is
    # drawn they would run through the sea beside it.
    if not any(layer.get("class") == "sea" for layer in result.physical):
        body.append(_hierarchy(result.hierarchy, frame, land))
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
    table_svg = ""
    placer = art.placer if art is not None else None
    labels_svg = ""
    if encoding.mode != "basemap":
        encoding.missing_label = _missing_label(result, drawn_missing, insets)
        encoding.extra_keys = _outside_keys(result)
        legend, legend_band = _legend(encoding, frame, land,
                                      [u for u in units if u],
                                      bool(drawn_missing) or inset_missing)
        if not has_overlays:
            placer = _placer(frame, encoding)
            labels_svg, unlabelled = _feature_labels(
                rows, insets, drawn_missing, frame, encoding, land, placer,
                len(every_row), bool(p.get("change")), lambda: _placer(frame, encoding),
                _enclave_ids(rows))
            table = _ranked_table(every_row, rows + [r for i in insets for r in i.rows],
                                  encoding) if unlabelled else None
            if table is not None:
                # Units the callouts could not reach are not left nameless:
                # the highest and lowest are ranked in a table, beside the
                # map if there is room, else under it.
                w, h, draw = table
                spot, hidden, covered = _least_covered(
                    frame, land, [u for u in units if u], w, h,
                    avoid=[frame.legend_box] + [(i.box[0], i.box[1], i.box[0] + i.box[2], i.box[1] + i.box[3])
                              for i in frame.insets])
                if not frame.is_world and hidden == 0 and covered <= 0.02:
                    frame.table_box = (spot[0], spot[1], spot[0] + w, spot[1] + h)
                    table_svg = draw(*spot, True)
                    placer = _placer(frame, encoding)
                    labels_svg, unlabelled = _feature_labels(
                        rows, insets, drawn_missing, frame, encoding, land, placer,
                        len(every_row), bool(p.get("change")),
                        lambda: _placer(frame, encoding), _enclave_ids(rows))
                else:
                    table_svg = draw(PAD, frame.bottom + legend_band + 12, False)
                    legend_band += h - 20 + 12 + 6
        body.append(legend)
        body.append(table_svg)
    if placer is None:
        placer = _placer(frame, encoding)
    context_svg, context_credits = _context_labels(result, rows, frame, encoding, manifest,
                                                   placer, land)
    body.append(context_svg)
    body.append(labels_svg)
    if art is not None and art.legend_h:
        body.append(art.legend(PAD, frame.bottom + legend_band + 16))
        legend_band += art.legend_h + 16

    for i, chunk in enumerate(body):
        if "<!--density-labels:" in chunk:
            body[i] = re.sub(
                r"<!--density-labels:(\d+)-->",
                lambda m: density_inset_labels(frame.density_insets[int(m.group(1))], encoding),
                chunk)

    title = _title(manifest, p)
    subtitle = _subtitle(manifest, p, encoding, _aggregate_note(every_row, p, encoding))
    if art is not None and manifest.variable_id is None and art.title:
        title, subtitle = _overlay_title(art.title, p.get("place")), art.subtitle
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
                               list(art.footer if art is not None else ()) + context_credits,
                               tiny_missing=sum(
                                   1 for d in drawn_missing
                                   if _visible_area(_project(d["geometry"], frame))
                                   < MIN_VISIBLE_AREA))
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
