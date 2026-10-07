"""The manifest: what to map, and over what window.

A manifest says *what to show*, never how to draw it. Ramp, classification,
projection and missing-value treatment are all derived from the data and the
window -- which is why the one hard cartographic rule, that a nominal variable
must not be shaded light-to-dark, cannot be violated here. There is no field in
which to violate it.

Validation runs before any I/O, so a bad manifest never reaches the network.
"""

import datetime
from dataclasses import dataclass

from mapsvc import cartography, overlays as overlay_sources, registry, statistics

# The largest window that still means anything: the whole planet.
WORLD = (-180.0, -90.0, 180.0, 90.0)


@dataclass(frozen=True)
class Manifest:
    # min_lon, min_lat, max_lon, max_lat, or None when `within` scopes the map.
    bbox: tuple | None
    level: str
    # None for a base map: boundaries with nothing painted on them.
    variable_source: str | None
    variable_id: str | None
    # Country codes and/or ISO 3166-2 codes. A bbox is spatial and returns
    # whatever overlaps it; this is the political filter a box cannot be. A list
    # names a region with no code of its own: the Balkans is however many
    # countries you say it is.
    within: tuple | None = None
    basemap_source: str = registry.DEFAULT_BASEMAP
    basemap_detail: str = "simplified"
    # (source, id) of a divisor: diesel *per person*, income *per household*.
    per: tuple | None = None
    # (from_year, to_year): percent change of the variable between two years.
    change: tuple | None = None
    # ((source, id), ...): feature layers drawn above the basemap. What to
    # fetch, never how to draw it -- points, lines or a flow is the data's
    # own geometry, and size and colour follow from what the source declares.
    overlays: tuple = ()

    def data_key(self) -> dict:
        """The data-relevant fields only.

        Everything else about a manifest is now derived, so this is very nearly
        the whole thing -- the exception being that rendering choices cannot
        invalidate a fetch because there are none to make.
        """
        key = {
            "bbox": None if self.bbox is None else [round(v, 6) for v in self.bbox],
            "within": None if self.within is None else list(self.within),
            "level": self.level,
            "basemap": {"source": self.basemap_source, "detail": self.basemap_detail},
            "variable": (None if self.variable_id is None
                         else self._variable_key()),
        }
        # Only present when used, so a manifest without overlays hashes as it
        # always did.
        if self.overlays:
            key["overlays"] = [{"source": s, "id": i} for s, i in self.overlays]
        return key

    def _variable_key(self) -> dict:
        key = {"source": self.variable_source, "id": self.variable_id}
        # Only present when used, so a plain manifest hashes as it always did.
        if self.per is not None:
            key["per"] = {"source": self.per[0], "id": self.per[1]}
        if self.change is not None:
            key["change"] = {"from": self.change[0], "to": self.change[1]}
        return key


class ManifestError(ValueError):
    """A manifest that cannot be honoured, and the field to blame."""

    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


REQUIRED = ("level",)
# One of bbox or within must be present; either can frame a map on its own.
OPTIONAL = {"variable": None, "basemap": None, "bbox": None, "within": None,
            "overlays": None}
KNOWN_KEYS = set(REQUIRED) | set(OPTIONAL)
# What to show, never how: a divisor and a change between years are both
# statements about the quantity. Anything else is rejected, as at top level.
VARIABLE_KEYS = {"source", "id", "per", "change"}


def validate(raw: dict) -> Manifest:
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object", "")

    for key in sorted(set(raw) - KNOWN_KEYS):
        raise ManifestError(
            f"unknown field {key!r}; expected one of {', '.join(sorted(KNOWN_KEYS))}. "
            "Ramp, classification, projection and missing-value handling are "
            "derived, not specified.", key,
        )
    for key in REQUIRED:
        if key not in raw:
            raise ManifestError(f"missing required field {key!r}", key)

    within = _within(raw.get("within"))
    bbox = _bbox(raw["bbox"]) if raw.get("bbox") is not None else None
    if bbox is None and not within:
        raise ManifestError(
            "give a bbox, a within, or both: a bbox frames a window, within "
            "scopes it to a country or a sub-national unit", "bbox",
        )

    level = raw["level"]
    if level not in registry.LEVELS:
        raise ManifestError(
            f"level must be one of {', '.join(registry.LEVELS)}, got {level!r}", "level"
        )

    basemap = raw.get("basemap") or {}
    if not isinstance(basemap, dict):
        raise ManifestError('basemap must be an object of the form '
                            '{"source": ..., "detail": ...}', "basemap")
    basemap_source = basemap.get("source", registry.DEFAULT_BASEMAP)
    if basemap_source not in registry.BASEMAPS:
        raise ManifestError(
            f"unknown basemap source {basemap_source!r}; expected one of "
            f"{', '.join(registry.BASEMAPS)}", "basemap.source"
        )
    basemap_detail = basemap.get("detail", "simplified")
    if basemap_detail not in registry.DETAILS:
        raise ManifestError(
            f"basemap.detail must be one of {', '.join(registry.DETAILS)}, "
            f"got {basemap_detail!r}", "basemap.detail"
        )
    supported = cartography.levels(basemap_source)
    if level not in supported:
        raise ManifestError(
            f"basemap {basemap_source!r} has no {level!r}; it provides "
            f"{', '.join(supported)}", "level"
        )

    variable = raw.get("variable")
    variable_source = variable_id = None
    per = change = None
    if variable is not None:
        if not isinstance(variable, dict):
            raise ManifestError('variable must be null, or an object of the form '
                                '{"source": ..., "id": ...}', "variable")
        for key in sorted(set(variable) - VARIABLE_KEYS):
            raise ManifestError(
                f"unknown field variable.{key}; expected one of "
                f"{', '.join(sorted(VARIABLE_KEYS))}", f"variable.{key}")
        variable_source, variable_id = _reference(variable, "variable", level)
        _check_join(variable_source, basemap_source, level, "variable.source")

        if variable.get("per") is not None:
            per_raw = variable["per"]
            if not isinstance(per_raw, dict):
                raise ManifestError('variable.per must be an object of the form '
                                    '{"source": ..., "id": ...}', "variable.per")
            for key in sorted(set(per_raw) - {"source", "id"}):
                raise ManifestError(
                    f"unknown field variable.per.{key}; expected source and id",
                    f"variable.per.{key}")
            per = _reference(per_raw, "variable.per", level)
            if per == (variable_source, variable_id):
                raise ManifestError("a variable divided by itself is 1 everywhere",
                                    "variable.per")
            _check_join(per[0], basemap_source, level, "variable.per.source")

        if variable.get("change") is not None:
            change = _change(variable["change"], variable_source, per)

    return Manifest(
        bbox=bbox, within=within, level=level,
        variable_source=variable_source, variable_id=variable_id,
        basemap_source=basemap_source, basemap_detail=basemap_detail,
        per=per, change=change, overlays=_overlays(raw.get("overlays")),
    )


def _overlays(value) -> tuple:
    """[{"source": ..., "id": ...}, ...], each id checked by its own grammar.

    The grammars are the point: an Overpass filter is key=value pairs, never
    free Overpass QL, and a web overlay is an https URL, never data values.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ManifestError('overlays must be a list of {"source": ..., "id": ...}',
                            "overlays")
    if len(value) > overlay_sources.MAX_OVERLAYS:
        raise ManifestError(f"at most {overlay_sources.MAX_OVERLAYS} overlays, "
                            f"got {len(value)}", "overlays")
    out = []
    for i, item in enumerate(value):
        field = f"overlays[{i}]"
        if not isinstance(item, dict):
            raise ManifestError(f'{field} must be {{"source": ..., "id": ...}}', field)
        for key in sorted(set(item) - {"source", "id"}):
            raise ManifestError(
                f"unknown field {field}.{key}; an overlay is a source and an id. "
                "How it is drawn is derived from its data.", f"{field}.{key}")
        source = item.get("source")
        if source not in overlay_sources.sources():
            raise ManifestError(
                f"unknown overlay source {source!r}; expected one of "
                f"{', '.join(overlay_sources.sources())}", f"{field}.source")
        try:
            overlay_id = overlay_sources.parse(source, item.get("id"))
        except overlay_sources.OverlayError as exc:
            raise ManifestError(str(exc), f"{field}.id") from None
        if (source, overlay_id) in out:
            raise ManifestError(f"{field} repeats {source} {overlay_id!r}", field)
        out.append((source, overlay_id))
    return tuple(out)


def _reference(variable: dict, field: str, level: str) -> tuple[str, str]:
    """Check one {source, id} pair against the registry, before any I/O."""
    for key in ("source", "id"):
        if key not in variable:
            raise ManifestError(f"{field} is missing {key!r}", f"{field}.{key}")
    source = variable["source"]
    if source not in statistics.sources():
        raise ManifestError(
            f"unknown variable source {source!r}; expected "
            f"{', '.join(statistics.sources())}", f"{field}.source"
        )
    variable_id = variable["id"]

    capabilities = statistics.capabilities(source)
    if level not in capabilities.levels:
        raise ManifestError(
            f"{source} publishes at {', '.join(capabilities.levels)}, "
            f"but level is {level!r}", "level",
        )

    if capabilities.searchable:
        # No local catalogue to check against: the id came from a live
        # search and only the source can say whether it exists. Settled at
        # harvest, which 404s cleanly.
        if not isinstance(variable_id, str) or not variable_id.strip():
            raise ManifestError(
                f"{source} is searchable, so {field}.id must name a "
                "specific indicator", f"{field}.id",
            )
    else:
        available = statistics.variables_for(level, source)
        if variable_id not in available:
            elsewhere = statistics.sources_for(variable_id)
            if elsewhere and source not in elsewhere:
                raise ManifestError(
                    f"{variable_id!r} comes from {' or '.join(elsewhere)}, not "
                    f"{source!r}", f"{field}.source",
                )
            raise ManifestError(
                f"unknown variable {variable_id!r}; at {level} "
                f"{source} offers "
                f"{', '.join(sorted(available)) or 'nothing'}", f"{field}.id"
            )
    return source, variable_id


def _check_join(source: str, basemap: str, level: str, field: str) -> None:
    """Statistics and boundaries must share a key namespace at this level.

    Both sides declare their keys statically, so a mismatch -- county incomes
    keyed by FIPS on Overture counties that carry no code at all -- is refused
    here rather than rendered as a map of nothing but hatching.
    """
    wanted = statistics.key_at(source, level)
    carried = cartography.keys(basemap, level)
    if wanted in carried:
        return
    offering = [b for b in registry.BASEMAPS
                if level in cartography.levels(b) and wanted in cartography.keys(b, level)]
    hint = (f"; use basemap {' or '.join(offering)}" if offering
            else "; no basemap carries that key at this level")
    raise ManifestError(
        f"{source} rows are keyed by {wanted} at {level}, but {basemap} "
        f"{level} boundaries carry {', '.join(carried) or 'no join key'}{hint}",
        "basemap.source" if field == "variable.source" else field,
    )


def _change(value, source: str, per: tuple | None) -> tuple[int, int]:
    """{"from": year, "to": year}: percent change, so both must exist."""
    if not isinstance(value, dict) or set(value) != {"from", "to"}:
        raise ManifestError('variable.change must be {"from": year, "to": year}',
                            "variable.change")
    years = value["from"], value["to"]
    this_year = datetime.date.today().year
    for year in years:
        if isinstance(year, bool) or not isinstance(year, int):
            raise ManifestError(f"variable.change years must be integers, got {year!r}",
                                "variable.change")
        if not 1700 <= year <= this_year:
            raise ManifestError(
                f"variable.change year {year} is outside 1700-{this_year}; "
                "projections are not observations", "variable.change")
    if years[0] >= years[1]:
        raise ManifestError(
            f"variable.change.from ({years[0]}) must be before to ({years[1]})",
            "variable.change")
    for name in {source, *((per[0],) if per else ())}:
        if not statistics.capabilities(name).time_series:
            raise ManifestError(
                f"{name} publishes a single vintage, so it has no change "
                "between years", "variable.change")
    return years


def _within(value) -> tuple | None:
    """One code or several, normalised to a tuple of upper-case strings."""
    if value is None:
        return None
    codes = [value] if isinstance(value, str) else value
    if not isinstance(codes, (list, tuple)) or not codes:
        raise ManifestError(
            "within must be a code, or a list of codes, each either a country "
            "(UKR) or an ISO 3166-2 unit (UA-07)", "within")
    out = []
    for code in codes:
        if not isinstance(code, str) or not code.strip():
            raise ManifestError(f"within contains {code!r}, which is not a code",
                                "within")
        out.append(code.strip().upper())
    return tuple(out)


def _bbox(value) -> tuple:
    """[min_lon, min_lat, max_lon, max_lat], checked hard.

    A silently malformed window is the worst failure available here: it renders
    a perfectly convincing map of the wrong place.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ManifestError(
            "bbox must be [min_lon, min_lat, max_lon, max_lat]", "bbox")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in value)
    except (TypeError, ValueError):
        raise ManifestError("bbox values must be numbers", "bbox") from None

    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        raise ManifestError("bbox longitudes must be between -180 and 180", "bbox")
    if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        raise ManifestError("bbox latitudes must be between -90 and 90", "bbox")
    if min_lon >= max_lon:
        raise ManifestError(
            f"bbox min_lon ({min_lon}) must be west of max_lon ({max_lon}); "
            "a window crossing the antimeridian is not supported", "bbox")
    if min_lat >= max_lat:
        raise ManifestError(
            f"bbox min_lat ({min_lat}) must be south of max_lat ({max_lat})", "bbox")
    return (min_lon, min_lat, max_lon, max_lat)
