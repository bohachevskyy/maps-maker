"""The manifest: what to map, how to classify it, how to colour it.

Validation lives here and runs before any I/O, so a bad request never reaches
the network or the cache.
"""

from dataclasses import dataclass

from mapsvc import cartography, colors, registry


@dataclass(frozen=True)
class Manifest:
    region: str
    level: str
    # variable_id is None for a base map: cartography with nothing painted on it.
    variable_source: str | None
    variable_id: str | None
    normalize: str | None
    method: str
    k: int
    ramp: str
    projection: str
    missing: str
    basemap_source: str = "overture"
    basemap_detail: str = "simplified"

    def data_key(self) -> dict:
        """The data-relevant fields only.

        `ramp` and `classify` are render-time decisions and must not invalidate
        a fetch, so they are deliberately absent.
        """
        return {
            "region": self.region,
            "level": self.level,
            "basemap": {"source": self.basemap_source, "detail": self.basemap_detail},
            "variable": (None if self.variable_id is None
                         else {"source": self.variable_source, "id": self.variable_id}),
            "normalize": self.normalize,
        }


class ManifestError(ValueError):
    """A manifest that cannot be honoured, and the field to blame."""

    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


REQUIRED = ("region", "level")
OPTIONAL = {"normalize": None, "projection": "auto", "missing": "hatch",
            "variable": None, "classify": None, "ramp": None, "basemap": None}
KNOWN_KEYS = set(REQUIRED) | set(OPTIONAL)


def validate(raw: dict) -> Manifest:
    """Check a manifest and return it typed.

    Runs before any I/O: a bad manifest must never reach the network or the
    cache. Every failure names the field that caused it.
    """
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object", "")

    for key in sorted(set(raw) - KNOWN_KEYS):
        raise ManifestError(
            f"unknown field {key!r}; expected one of {', '.join(sorted(KNOWN_KEYS))}", key
        )
    for key in REQUIRED:
        if key not in raw:
            raise ManifestError(f"missing required field {key!r}", key)

    region = raw["region"]
    if not isinstance(region, str) or not region.strip():
        raise ManifestError("region must be a non-empty string", "region")

    # A filter, not geography: "world", a CONTINENT name, or an ADM0_A3 code.
    # Which of those it is cannot be known without the data, so it is resolved
    # in harvest rather than here.

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
    # A provider that cannot serve this level must say so before any I/O.
    supported = cartography.levels(basemap_source)
    if level not in supported:
        raise ManifestError(
            f"basemap {basemap_source!r} has no {level!r}; it provides "
            f"{', '.join(supported)}", "level"
        )

    variable = raw.get("variable")
    variable_source = variable_id = None
    normalize = raw.get("normalize", OPTIONAL["normalize"])

    if variable is None:
        # A base map. Classification and colour have nothing to act on.
        if normalize is not None:
            raise ManifestError(
                "normalize needs a variable to divide; set variable or drop "
                "normalize", "normalize"
            )
        method, k, ramp = "quantile", registry.K_MIN, None
    else:
        if not isinstance(variable, dict):
            raise ManifestError('variable must be null, or an object of the form '
                                '{"source": ..., "id": ...}', "variable")
        for key in ("source", "id"):
            if key not in variable:
                raise ManifestError(f"variable is missing {key!r}", f"variable.{key}")
        if variable["source"] not in registry.SOURCES:
            raise ManifestError(
                f"unknown variable source {variable['source']!r}; "
                f"expected {', '.join(registry.SOURCES)}", "variable.source"
            )
        variable_source = variable["source"]
        variable_id = variable["id"]

        available = registry.variables_for(level)
        if variable_id not in available:
            belongs_to = registry.level_of(variable_id)
            if belongs_to:
                raise ManifestError(
                    f"{variable_id!r} is an {belongs_to} variable, but level is "
                    f"{level!r}; at {level} the choices are "
                    f"{', '.join(sorted(available)) or 'none'}", "variable.id",
                )
            raise ManifestError(
                f"unknown variable {variable_id!r}; at {level} expected one of "
                f"{', '.join(sorted(available)) or 'none'}", "variable.id"
            )

        if normalize is not None and normalize not in available:
            raise ManifestError(
                f"unknown normalize column {normalize!r}; at {level} expected null "
                f"or one of {', '.join(sorted(available))}", "normalize"
            )
        # Deliberately no rule relating `normalize` to the variable's level. An
        # un-normalised choropleth of a count is misleading, but that is the
        # caller's call to make, not the validator's.

        classify = raw.get("classify")
        if classify is None:
            raise ManifestError("classify is required when a variable is set",
                                "classify")
        if not isinstance(classify, dict):
            raise ManifestError('classify must be an object of the form '
                                '{"method": ..., "k": ...}', "classify")
        for key in ("method", "k"):
            if key not in classify:
                raise ManifestError(f"classify is missing {key!r}", f"classify.{key}")
        if classify["method"] not in registry.METHODS:
            raise ManifestError(
                f"classify.method must be one of {', '.join(registry.METHODS)}, "
                f"got {classify['method']!r}", "classify.method"
            )
        method = classify["method"]
        k = classify["k"]
        if isinstance(k, bool) or not isinstance(k, int):
            raise ManifestError(f"classify.k must be an integer, got {k!r}", "classify.k")
        if not registry.K_MIN <= k <= registry.K_MAX:
            raise ManifestError(
                f"classify.k must be between {registry.K_MIN} and {registry.K_MAX}, "
                f"got {k}", "classify.k",
            )

        ramp = raw.get("ramp")
        if ramp is None:
            raise ManifestError("ramp is required when a variable is set", "ramp")
        if ramp not in colors.RAMPS:
            raise ManifestError(
                f"unknown ramp {ramp!r}; expected one of "
                f"{', '.join(sorted(colors.RAMPS))}", "ramp",
            )

        # Correctness rule: shading unordered categories light-to-dark asserts an
        # ordering that does not exist -- that one subregion is "more" than another.
        if available[variable_id]["level"] == "nominal" and colors.kind(ramp) != "qualitative":
            qualitative = sorted(n for n, kind in colors.RAMPS.items()
                                 if kind == "qualitative")
            raise ManifestError(
                f"{variable_id} is nominal, so a {colors.kind(ramp)} ramp like "
                f"{ramp!r} would imply an ordering between categories; use a "
                f"qualitative ramp ({', '.join(qualitative)})", "ramp",
            )

    projection = raw.get("projection", OPTIONAL["projection"])
    if projection not in registry.PROJECTIONS:
        raise ManifestError(
            f"projection must be one of {', '.join(registry.PROJECTIONS)}, "
            f"got {projection!r}", "projection"
        )

    missing = raw.get("missing", OPTIONAL["missing"])
    if missing not in registry.MISSING_MODES:
        raise ManifestError(
            f"missing must be one of {', '.join(registry.MISSING_MODES)}, "
            f"got {missing!r}", "missing"
        )

    return Manifest(
        region=region, level=level,
        variable_source=variable_source, variable_id=variable_id,
        normalize=normalize, method=method, k=k,
        ramp=ramp, projection=projection, missing=missing,
        basemap_source=basemap_source, basemap_detail=basemap_detail,
    )
