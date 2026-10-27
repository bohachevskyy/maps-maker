"""Map titles, subtitles, and provenance footnotes."""

from __future__ import annotations

import re
import textwrap
from xml.sax.saxutils import escape

from mapsvc import registry, usfips
from mapsvc.models import Manifest

from mapsvc.render_frame import (
    CANVAS_W,
    FONT,
    Frame,
    MAP_W,
    PAD,
    _num,
    _text_w,
    _wrap_words,
)

from mapsvc.render_encoding import (
    Encoding,
    _count_label,
    _human,
    _is_share,
    _label,
)

FOOTER_SIZE, FOOTER_LEAD = 10.5, 15

FOOTER_CHARS = 160

_SCOPE = {"admin_0": "country", "admin_1": "first-level subdivision",
          "admin_2": "second-level subdivision", "admin_3": "third-level subdivision",
          "nuts_1": "NUTS 1 region", "nuts_2": "NUTS 2 region", "nuts_3": "NUTS 3 region"}

_REASONS = {"no_data": "no value reported by the source",
            "no_join": "no matching row in the statistics source"}

NAMED_DROPS = 4

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

_PLACE_ALIASES = {
    "United States of America": "United States",
    "Russian Federation": "Russia",
    "Republic of Korea": "South Korea",
    "Dem. Rep. Korea": "North Korea",
}

def _overlay_title(title: str, place: str | None) -> str:
    """An overlay's own title with the place it is about: "The 30 largest
    cities by population" on a map of Europe is "Europe's 30 largest cities by
    population"; "Every magnitude 6+ earthquake" keeps its own scope."""
    place = _PLACE_ALIASES.get(place, place)
    if (not place or place == "World" or place.casefold() in title.casefold()
            or re.fullmatch(r"[A-Z]{2,3}(-[A-Z0-9]+)?", place)):   # a code is no name
        return title
    ranked = re.match(r"^The (\d+) (largest|biggest|smallest|most [\w-]+) (.+)$", title)
    if ranked:
        return f"{place}'s {ranked.group(1)} {ranked.group(2)} {ranked.group(3)}"
    return f"{title}, {place}"

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
    place = _PLACE_ALIASES.get(provenance.get("place"), provenance.get("place"))
    label, period = _label(manifest, provenance), _period(provenance)
    unit = _UNIT_NAMES.get((provenance.get("place"), manifest.level), "")
    if place == "World":
        # "GDP per capita by country, 2025": the world is not a place name.
        scope = _SCOPE.get(manifest.level)
        return ", ".join(str(part) for part in
                         (f"{label} by {scope}" if scope else label, period) if part)
    if label == "Population" and place and unit and "(" not in unit and not unit.endswith("y"):
        return ", ".join(str(part) for part in (f"Population of {place}'s {unit}s", period)
                         if part)
    parts = [label, place, period]
    return ", ".join(str(part) for part in parts if part)

_UNIT_NAMES = {
    ("United States of America", "admin_1"): "state",
    ("United States", "admin_1"): "state",
    ("Canada", "admin_1"): "province or territory",
    ("Ukraine", "admin_1"): "oblast",
    ("Germany", "admin_1"): "state (Land)",
}

def _aggregate_note(rows: list, provenance: dict, encoding: Encoding) -> str:
    """The source's own total for the area the map covers: "Canada +5.2%",
    "Texas statewide 4.1%". Only where the source published one for exactly
    the units drawn (its key is a prefix of every id) and the quantity is a
    ratio, which a total can be compared with; nothing is computed here."""
    aggregates = provenance.get("aggregates") or {}
    if not aggregates or encoding.mode != "choropleth" or not rows:
        return ""
    ids = [str(row["id"]) for row in rows]
    prefixes = [p for p in aggregates if all(i.startswith(p) for i in ids)]
    if not prefixes:
        return ""
    found = aggregates[max(prefixes, key=len)]
    value = found.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if provenance.get("change"):
        text = f"{'+' if value > 0 else '−' if value < 0 else ''}{abs(value):.1f}%"
    elif _is_share(str(provenance.get("unit") or "")):
        text = f"{value:.1f}%"
    else:
        return ""
    return f"{found['label']} {text}"

def _scope_word(manifest: Manifest, provenance: dict) -> str:
    place = str(provenance.get("place") or "")
    if (place, manifest.level) in _UNIT_NAMES:
        return _UNIT_NAMES[(place, manifest.level)]
    if manifest.level == "admin_2" and place in _US_STATES:
        return "county"
    return _SCOPE.get(manifest.level, manifest.level)

_FOOTNOTE_ONLY = re.compile(r"\bprovisional\b|\bestimated\b|^excludes?\b", re.I)

def _subtitle(manifest: Manifest, provenance: dict, encoding: Encoding,
              aggregate: str = "") -> str:
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
    pieces = [head] + ([unit] if unit else []) + ([aggregate] if aggregate else [])
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
            pieces.append("reds = decline; blues = growth")
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
            extra: tuple | list = (), tiny_missing: int = 0):
    """The mandatory footnote. Every rendered map states its own provenance."""
    p = result.provenance

    scale = p.get("scale") or registry.scale_for(manifest.level)
    vintage = _without_idle_notes(p.get("vintage") or registry.SOURCE_VINTAGE, result,
                                 manifest.variable_id is not None)
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
    excluded = _exclusions(result, tiny_missing)

    overlay_only = manifest.variable_id is None and bool(result.overlays)
    lines = [f"Boundaries: {source}."]
    if overlay_only:
        # Where the map is drawn is one paragraph: boundaries, projection and
        # the physical layers under the data.
        lines[0] = " ".join([lines[0], f"Projection: {_projection_label(frame)}.",
                             f"{excluded}.", *_physical_credits(result.physical)])
    else:
        lines.extend([
            f"Variable: {variable}.",
            f"Projection: {_projection_label(frame)}. Classification: {method}. {excluded}."
            + (f" {_plain_land_note(p)}" if _plain_land_note(p) else ""),
        ])
    if not overlay_only:
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

_SAHARA = "Western Sahara shown as a non-self-governing territory (UN)"

def _without_idle_notes(vintage: str, result, variable: bool = True) -> str:
    """The boundary note about Western Sahara says something only on a map
    that colours Morocco or Western Sahara with a variable; on a map of
    quakes, of Europe, or with them as grey context, it is noise."""
    if _SAHARA not in vintage:
        return vintage
    ids = {str(item.get("id") or "").upper()
           for item in list(result.rows) + list(result.dropped)}
    if variable and ids & {"ESH", "SAH", "MAR"}:
        return vintage
    return "; ".join(part for part in vintage.split("; ") if part != _SAHARA)

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
        words = [n.split(" ") for n in names]
        common = 0
        while len(names) > 1 and all(len(w) > common + 1 for w in words) and all(
                w[common] == words[0][common] for w in words):
            common += 1
        if common:
            names = [names[0]] + [" ".join(w[common:]) for w in words[1:]]
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

def _exclusions(result, tiny: int = 0) -> str:
    """Account for every omitted unit from harvest's canonical ledger."""
    excluded = [item for item in result.ledger
                if item.get("status") != "drawn_with_value"]
    if not excluded:
        return "0 features excluded"
    meanings = {**_REASONS, **(result.provenance.get("drop_reasons") or {})}
    status_labels = {
        "drawn_hatched": "drawn hatched",
        "outside_frame": "outside the frame",
        "data_without_geometry": "data rows without basemap geometry",
        "geometry_without_data": "basemap geometries without source data, drawn hatched",
        "filtered_out": "filtered out",
    }
    grouped: dict[tuple[str, str], list] = {}
    for item in excluded:
        grouped.setdefault((item.get("status", "filtered_out"),
                            item.get("reason", "unknown")), []).append(item)
    parts = []
    for (status, reason), items in sorted(grouped.items()):
        explanation = meanings.get(reason, reason.replace("_", " "))
        part = f"{len(items)} {status_labels.get(status, status.replace('_', ' '))}: {explanation}"
        names = sorted({str(i.get("name") or i["id"]) for i in items
                        if not i.get("anonymous")})
        if 0 < len(items) <= NAMED_DROPS and len(names) == len(items):
            part += f" — {', '.join(names)}"
        parts.append(part)
    text = f"{len(excluded)} of {len(result.ledger)} features excluded: " + "; ".join(parts)
    if tiny:
        hatched = sum(item.get("status") in ("drawn_hatched", "geometry_without_data")
                      for item in excluded)
        text += (f" ({tiny} of {hatched} hatched features too small to show at this scale)"
                 if tiny != hatched else " (all hatched features too small to show at this scale)")
    return text

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
        # Many insets: the scale of each is on its frame (a "×2.7" in the
        # caption, a scale bar), and a sentence repeating them is noise.
        if len(frame.insets) > 3:
            label += "; insets, each in its own equal-area projection, scale on its frame"
        else:
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
