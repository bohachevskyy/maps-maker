"""The join: polygons from cartography, values from statistics.

This module owns the seam between the two. It does not know how boundaries are
fetched or where numbers come from -- it asks each side for what it needs and
matches them on a key. Adding a statistics source touches `statistics/` only;
adding a boundary source touches `cartography/` only.

Synchronous, and only lightly cached for now: the Natural Earth files are cached
to disk, Overture is queried live on every request.
"""

import hashlib
import json
import os
import pathlib
import urllib.request

from mapsvc import cartography, registry, statistics
from mapsvc.manifest import Manifest
from mapsvc.models import HarvestResult

__all__ = ["HarvestResult", "HarvestError", "harvest", "load_source", "select_region"]

FETCH_TIMEOUT = 60
USER_AGENT = "mapsvc/0.1 (+https://github.com/nvkelso/natural-earth-vector)"

# -99 is Natural Earth's explicit no-data marker. 0 is a no-data marker only for
# counts -- a country with zero people or zero GDP is a placeholder row, but a
# rank or an index of 0 could be real.
NO_DATA = {-99, "-99", "", None}

# Bump when the shape of a cached HarvestResult changes -- new provenance keys,
# a different join, altered drop reasons. Without it a code change silently
# keeps serving rows written in the old format.
CACHE_VERSION = 2


class HarvestError(ValueError):
    """Data could not be assembled for this manifest."""

    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


# --------------------------------------------------------------- raw files ---

def cache_dir() -> pathlib.Path:
    root = os.environ.get("MAPSVC_CACHE")
    if root:
        return pathlib.Path(root)
    return pathlib.Path(__file__).resolve().parent.parent / "cache"


def source_path(level: str = "admin_0") -> pathlib.Path:
    return cache_dir() / (
        f"ne_{registry.scale_for(level)}_{registry.dataset_for(level)}.geojson"
    )


def load_source(level: str = "admin_0") -> dict:
    """The raw Natural Earth GeoJSON for a level, fetched once and reused."""
    path = source_path(level)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(registry.data_url(level),
                                         headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            payload = response.read()
        # Write via a temporary name so an interrupted fetch cannot leave a
        # truncated file that looks like a valid cache entry.
        temporary = path.with_suffix(".part")
        temporary.write_bytes(payload)
        temporary.replace(path)
    return json.loads(path.read_text())


def select_region(features: list, region: str, level: str = "admin_0") -> list:
    """`region` is a filter, not geography: 'world', a continent, or a country code."""
    wanted = region.strip().lower()
    if wanted == "world":
        return list(features)

    continent = registry.continent_property(level)
    if continent:
        by_continent = [f for f in features
                        if str(f["properties"].get(continent, "")).lower() == wanted]
        if by_continent:
            return by_continent

    # ADM0_A3 is mostly ISO3 but carries custom codes for disputed and
    # non-sovereign entities, so this is a code match, not an ISO3 lookup.
    country = registry.country_property(level)
    return [f for f in features
            if str(f["properties"].get(country, "")).lower() == wanted]


# ------------------------------------------------------------------- cache ---

def cache_key(manifest: Manifest) -> str:
    """Hash of the data-relevant manifest fields, plus the geometry scale.

    `ramp` and `classify` are render-time choices; letting them into the key
    would re-fetch a file that has not changed just because the colours did.
    SCALE is not a manifest field but decides which geometry the rows carry, so
    without it a change of scale would silently reuse the old polygons.
    """
    keyed = {**manifest.data_key(), "scale": registry.scale_for(manifest.level),
             "cache_version": CACHE_VERSION}
    if manifest.basemap_source == "overture":
        # A new Overture release is new geometry; the old rows must not be
        # served under it.
        from mapsvc.cartography import overture

        keyed["overture_release"] = overture.RELEASE
    canonical = json.dumps(keyed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def harvest(manifest: Manifest) -> HarvestResult:
    key = cache_key(manifest)
    cached = cache_dir() / "harvest" / f"{key}.json"
    if cached.exists():
        stored = json.loads(cached.read_text())
        return HarvestResult(stored["rows"], stored["provenance"], stored["dropped"])

    result = _build(manifest)
    cached.parent.mkdir(parents=True, exist_ok=True)
    payload = {"rows": result.rows, "provenance": result.provenance,
               "dropped": result.dropped}
    temporary = cached.with_suffix(".part")
    temporary.write_text(json.dumps(payload, sort_keys=True))
    temporary.replace(cached)
    return result


# -------------------------------------------------------------------- join ---

def _build(manifest: Manifest) -> HarvestResult:
    boundaries = cartography.load(manifest.basemap_source, manifest.level,
                                  manifest.region, manifest.basemap_detail)
    provenance = {
        "region": manifest.region,
        "level": manifest.level,
        **{f"basemap_{k}": v for k, v in boundaries.provenance.items()},
        "source": boundaries.provenance.get("source"),
        "scale": boundaries.provenance.get("scale"),
        "license": boundaries.provenance.get("license"),
        "attribution": boundaries.provenance.get("attribution"),
        "vintage": boundaries.provenance.get("release"),
        "variable": None, "unit": None, "year": None,
        "normalize": None, "normalize_unit": None,
    }

    if manifest.variable_id is None:
        # A base map: cartography with nothing painted on it.
        rows = [{"id": u["id"], "geometry": u["geometry"], "value": None,
                 "name": u.get("name")} for u in boundaries.units]
        return HarvestResult(rows=rows, provenance=provenance, dropped=[])

    values = statistics.load(manifest.variable_source, manifest.variable_id,
                             manifest.level, manifest.region)
    divisor = None
    if manifest.normalize:
        divisor = statistics.load(manifest.variable_source, manifest.normalize,
                                  manifest.level, manifest.region)

    provenance.update({
        "variable": values.provenance.get("variable"),
        "unit": values.provenance.get("unit"),
        "year": values.provenance.get("year"),
        "statistics_source": values.provenance.get("source"),
        # Carried separately from the basemap's: boundaries and numbers can be
        # licensed differently, and both have to be credited.
        "statistics_license": values.provenance.get("license"),
        "statistics_attribution": values.provenance.get("attribution"),
        "normalize": manifest.normalize,
        "normalize_unit": divisor.provenance.get("unit") if divisor else None,
    })

    value_level = values.provenance.get("level", "count")
    rows: list[dict] = []
    dropped: list[dict] = []

    for unit in boundaries.units:
        entry = {"id": unit["id"], "geometry": unit["geometry"]}
        key = str(unit.get("key")).upper() if unit.get("key") else None

        if key is None or key not in values.values:
            # The polygon exists but this source has no row for it. Common when
            # boundaries and statistics come from different providers.
            dropped.append({**entry, "reason": "no_join"})
            continue

        value = values.values[key]
        reason = _no_data_reason(value, value_level)
        if reason:
            dropped.append({**entry, "reason": reason})
            continue

        if divisor:
            other = divisor.values.get(key)
            reason = _no_data_reason(other, divisor.provenance.get("level", "count"))
            if reason:
                dropped.append({**entry, "reason": f"{reason}_normalize"})
                continue
            try:
                value = float(value) / float(other)
            except (TypeError, ValueError):
                dropped.append({**entry, "reason": "not_numeric"})
                continue
            except ZeroDivisionError:
                dropped.append({**entry, "reason": "divide_by_zero"})
                continue
        elif value_level == "count" or isinstance(value, (int, float)):
            value = float(value)

        rows.append({**entry, "value": value})

    if not rows:
        raise HarvestError(*_nothing_usable(manifest, dropped))

    rows.sort(key=lambda r: str(r["id"]))
    dropped.sort(key=lambda r: (str(r["id"]), r["reason"]))
    return HarvestResult(rows=rows, provenance=provenance, dropped=dropped)


# Drop reasons that point at the divisor rather than the variable itself.
_NORMALIZE_REASONS = {"no_data_normalize", "not_numeric", "divide_by_zero"}


def _nothing_usable(manifest: Manifest, dropped: list) -> tuple[str, str]:
    """Explain an empty result by blaming the column actually at fault."""
    reasons = {item["reason"] for item in dropped}
    if reasons == {"no_join"}:
        return (
            f"none of the {len(dropped)} {manifest.level} boundaries could be "
            f"matched to a {manifest.variable_source} row for "
            f"{manifest.variable_id}; the basemap and the statistics do not "
            "share a join key at this level",
            "variable.id",
        )
    if reasons and reasons <= _NORMALIZE_REASONS:
        return (
            f"every feature in region {manifest.region!r} was dropped dividing "
            f"{manifest.variable_id} by {manifest.normalize!r}; "
            f"{manifest.normalize} is not usable as a divisor here",
            "normalize",
        )
    return (
        f"every feature in region {manifest.region!r} lacks a usable "
        f"{manifest.variable_id} value",
        "variable.id",
    )


def _no_data_reason(value, level: str) -> str | None:
    if value in NO_DATA:
        return "no_data"
    if level == "count" and isinstance(value, (int, float)) and value == 0:
        # A zero count is a placeholder row, not a measurement. Left in, it is
        # shaded as the lowest real value -- the Vatican coloured as the poorest
        # country rather than marked as having no data -- and it silently
        # becomes a divide-by-zero when used as a `normalize` column.
        return "no_data"
    return None
