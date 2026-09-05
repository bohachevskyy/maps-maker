"""The manifest: what to map, and over what window.

A manifest says *what to show*, never how to draw it. Ramp, classification,
projection and missing-value treatment are all derived from the data and the
window -- which is why the one hard cartographic rule, that a nominal variable
must not be shaded light-to-dark, cannot be violated here. There is no field in
which to violate it.

Validation runs before any I/O, so a bad manifest never reaches the network.
"""

from dataclasses import dataclass

from mapsvc import cartography, registry, statistics

# The largest window that still means anything: the whole planet.
WORLD = (-180.0, -90.0, 180.0, 90.0)


@dataclass(frozen=True)
class Manifest:
    # min_lon, min_lat, max_lon, max_lat
    bbox: tuple
    level: str
    # None for a base map: boundaries with nothing painted on them.
    variable_source: str | None
    variable_id: str | None
    basemap_source: str = registry.DEFAULT_BASEMAP
    basemap_detail: str = "simplified"

    def data_key(self) -> dict:
        """The data-relevant fields only.

        Everything else about a manifest is now derived, so this is very nearly
        the whole thing -- the exception being that rendering choices cannot
        invalidate a fetch because there are none to make.
        """
        return {
            "bbox": [round(v, 6) for v in self.bbox],
            "level": self.level,
            "basemap": {"source": self.basemap_source, "detail": self.basemap_detail},
            "variable": (None if self.variable_id is None
                         else {"source": self.variable_source, "id": self.variable_id}),
        }


class ManifestError(ValueError):
    """A manifest that cannot be honoured, and the field to blame."""

    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


REQUIRED = ("bbox", "level")
OPTIONAL = {"variable": None, "basemap": None}
KNOWN_KEYS = set(REQUIRED) | set(OPTIONAL)


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

    bbox = _bbox(raw["bbox"])

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
    if variable is not None:
        if not isinstance(variable, dict):
            raise ManifestError('variable must be null, or an object of the form '
                                '{"source": ..., "id": ...}', "variable")
        for key in ("source", "id"):
            if key not in variable:
                raise ManifestError(f"variable is missing {key!r}", f"variable.{key}")
        variable_source = variable["source"]
        if variable_source not in statistics.sources():
            raise ManifestError(
                f"unknown variable source {variable_source!r}; expected "
                f"{', '.join(statistics.sources())}", "variable.source"
            )
        variable_id = variable["id"]

        capabilities = statistics.capabilities(variable_source)
        if level not in capabilities.levels:
            raise ManifestError(
                f"{variable_source} publishes at {', '.join(capabilities.levels)}, "
                f"but level is {level!r}", "level",
            )

        if capabilities.searchable:
            # No local catalogue to check against: the id came from a live
            # search and only the source can say whether it exists. Settled at
            # harvest, which 404s cleanly.
            if not str(variable_id).strip():
                raise ManifestError(
                    f"{variable_source} is searchable, so variable.id must name a "
                    "specific indicator", "variable.id",
                )
        else:
            available = statistics.variables_for(level, variable_source)
            if variable_id not in available:
                elsewhere = statistics.sources_for(variable_id)
                if elsewhere and variable_source not in elsewhere:
                    raise ManifestError(
                        f"{variable_id!r} comes from {' or '.join(elsewhere)}, not "
                        f"{variable_source!r}", "variable.source",
                    )
                raise ManifestError(
                    f"unknown variable {variable_id!r}; at {level} "
                    f"{variable_source} offers "
                    f"{', '.join(sorted(available)) or 'nothing'}", "variable.id"
                )

    return Manifest(
        bbox=bbox, level=level,
        variable_source=variable_source, variable_id=variable_id,
        basemap_source=basemap_source, basemap_detail=basemap_detail,
    )


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
