"""The manifest: what to map, how to classify it, how to colour it.

Validation lives here and runs before any I/O, so a bad request never reaches
the network or the cache.
"""

from dataclasses import dataclass

from mapsvc import colors, registry


@dataclass(frozen=True)
class Manifest:
    region: str
    level: str
    variable_source: str
    variable_id: str
    normalize: str | None
    method: str
    k: int
    ramp: str
    projection: str
    missing: str

    def data_key(self) -> dict:
        """The data-relevant fields only.

        `ramp` and `classify` are render-time decisions and must not invalidate
        a fetch, so they are deliberately absent.
        """
        return {
            "region": self.region,
            "level": self.level,
            "variable": {"source": self.variable_source, "id": self.variable_id},
            "normalize": self.normalize,
        }


class ManifestError(ValueError):
    """A manifest that cannot be honoured, and the field to blame."""

    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


REQUIRED = ("region", "level", "variable", "classify", "ramp")
OPTIONAL = {"normalize": None, "projection": "auto", "missing": "hatch"}
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

    variable = raw["variable"]
    if not isinstance(variable, dict):
        raise ManifestError('variable must be an object of the form '
                            '{"source": ..., "id": ...}', "variable")
    for key in ("source", "id"):
        if key not in variable:
            raise ManifestError(f"variable is missing {key!r}", f"variable.{key}")
    if variable["source"] not in registry.SOURCES:
        raise ManifestError(
            f"unknown variable source {variable['source']!r}; "
            f"expected {', '.join(registry.SOURCES)}", "variable.source"
        )
    variable_id = variable["id"]
    if variable_id not in registry.VARIABLES:
        raise ManifestError(
            f"unknown variable {variable_id!r}; expected one of "
            f"{', '.join(sorted(registry.VARIABLES))}", "variable.id"
        )

    normalize = raw.get("normalize", OPTIONAL["normalize"])
    if normalize is not None and normalize not in registry.VARIABLES:
        raise ManifestError(
            f"unknown normalize column {normalize!r}; expected null or one of "
            f"{', '.join(sorted(registry.VARIABLES))}", "normalize"
        )
    # Deliberately no rule relating `normalize` to the variable's level. An
    # un-normalised choropleth of a count is misleading, but that is the
    # caller's call to make, not the validator's.

    classify = raw["classify"]
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
    k = classify["k"]
    if isinstance(k, bool) or not isinstance(k, int):
        raise ManifestError(f"classify.k must be an integer, got {k!r}", "classify.k")
    if not registry.K_MIN <= k <= registry.K_MAX:
        raise ManifestError(
            f"classify.k must be between {registry.K_MIN} and {registry.K_MAX}, got {k}",
            "classify.k",
        )

    ramp = raw["ramp"]
    if ramp not in colors.RAMPS:
        raise ManifestError(
            f"unknown ramp {ramp!r}; expected one of {', '.join(sorted(colors.RAMPS))}",
            "ramp",
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

    # Correctness rule: shading unordered categories light-to-dark asserts an
    # ordering that does not exist -- that one subregion is "more" than another.
    level_of_variable = registry.VARIABLES[variable_id]["level"]
    if level_of_variable == "nominal" and colors.kind(ramp) != "qualitative":
        qualitative = sorted(n for n, kind in colors.RAMPS.items() if kind == "qualitative")
        raise ManifestError(
            f"{variable_id} is nominal, so a {colors.kind(ramp)} ramp like {ramp!r} "
            f"would imply an ordering between categories; use a qualitative ramp "
            f"({', '.join(qualitative)})",
            "ramp",
        )

    return Manifest(
        region=region, level=level,
        variable_source=variable["source"], variable_id=variable_id,
        normalize=normalize, method=classify["method"], k=k,
        ramp=ramp, projection=projection, missing=missing,
    )
