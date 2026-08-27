"""Fetching, filtering and cleaning the data. This is the module that does I/O.

Synchronous by design: one file, fetched once, cached to disk.
"""

import hashlib
import json
import os
import pathlib
import urllib.request

from mapsvc import registry
from mapsvc.manifest import Manifest
from mapsvc.models import HarvestResult

__all__ = ["HarvestResult", "HarvestError", "harvest"]

FETCH_TIMEOUT = 60
USER_AGENT = "mapsvc/0.1 (+https://github.com/nvkelso/natural-earth-vector)"

# -99 is Natural Earth's explicit no-data marker. 0 is a no-data marker only for
# counts -- a country with zero people or zero GDP is a placeholder row, but a
# rank or an index of 0 could be real.
NO_DATA = {-99, "-99", "", None}


class HarvestError(ValueError):
    """Data could not be assembled for this manifest."""

    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


def cache_dir() -> pathlib.Path:
    root = os.environ.get("MAPSVC_CACHE")
    if root:
        return pathlib.Path(root)
    return pathlib.Path(__file__).resolve().parent.parent / "cache"


def cache_key(manifest: Manifest) -> str:
    """Hash of the data-relevant manifest fields, plus the scale.

    `ramp` and `classify` are render-time choices. Letting them into the key
    would re-fetch a file that has not changed just because the colours did.

    SCALE is not a manifest field but it decides which geometry the rows carry,
    so it has to be here: without it, switching to 50m silently reuses harvested
    110m geometry and the map does not change.
    """
    keyed = {**manifest.data_key(), "scale": registry.scale_for(manifest.level)}
    canonical = json.dumps(keyed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def source_path(level: str = "admin_0") -> pathlib.Path:
    return cache_dir() / (
        f"ne_{registry.scale_for(level)}_{registry.dataset_for(level)}.geojson"
    )


def load_source(level: str = "admin_0") -> dict:
    """The raw GeoJSON for a level, fetched once and reused thereafter."""
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


def harvest(manifest: Manifest) -> HarvestResult:
    key = cache_key(manifest)
    cached = cache_dir() / "harvest" / f"{key}.json"
    if cached.exists():
        stored = json.loads(cached.read_text())
        return HarvestResult(stored["rows"], stored["provenance"], stored["dropped"])

    result = _build(manifest)
    cached.parent.mkdir(parents=True, exist_ok=True)
    payload = {"rows": result.rows, "provenance": result.provenance, "dropped": result.dropped}
    temporary = cached.with_suffix(".part")
    temporary.write_text(json.dumps(payload, sort_keys=True))
    temporary.replace(cached)
    return result


def _build(manifest: Manifest) -> HarvestResult:
    level = manifest.level
    features = _select_region(load_source(level)["features"], manifest.region, level)
    if not features:
        expected = ("'world', a CONTINENT name, or an ADM0_A3 code"
                    if registry.continent_property(level)
                    else "'world' or an ADM0_A3 country code (admin-1 features "
                         "carry no continent)")
        raise HarvestError(
            f"no features matched region {manifest.region!r}; expected {expected}",
            "region",
        )

    variables = registry.variables_for(level)
    meta = variables[manifest.variable_id]
    normalize_meta = variables[manifest.normalize] if manifest.normalize else None
    id_property = registry.id_property(level)

    rows: list[dict] = []
    dropped: list[dict] = []
    years: set = set()

    for feature in features:
        props = feature["properties"]
        gid = props.get(id_property)
        entry = {"id": gid, "geometry": feature["geometry"]}

        value = props.get(manifest.variable_id)
        reason = _no_data_reason(value, meta["level"])
        if reason:
            dropped.append({**entry, "reason": reason})
            continue

        if manifest.normalize:
            divisor = props.get(manifest.normalize)
            reason = _no_data_reason(divisor, normalize_meta["level"])
            if reason:
                dropped.append({**entry, "reason": f"{reason}_normalize"})
                continue
            try:
                value = float(value) / float(divisor)
            except (TypeError, ValueError):
                # e.g. normalising a nominal column, which has no numeric meaning.
                dropped.append({**entry, "reason": "not_numeric"})
                continue
            except ZeroDivisionError:
                dropped.append({**entry, "reason": "divide_by_zero"})
                continue
        elif meta["level"] == "count" or isinstance(value, (int, float)):
            value = float(value)

        rows.append({**entry, "value": value})
        if meta["year_col"] and isinstance(props.get(meta["year_col"]), int):
            years.add(props[meta["year_col"]])

    if not rows:
        raise HarvestError(*_nothing_usable(manifest, dropped))

    rows.sort(key=lambda r: r["id"])
    dropped.sort(key=lambda r: (r["id"], r["reason"]))

    provenance = {
        "source": registry.SOURCE_NAME,
        "vintage": registry.SOURCE_VINTAGE,
        "scale": registry.scale_for(level),
        "region": manifest.region,
        "variable": manifest.variable_id,
        "unit": meta["unit"],
        "year": _year_label(years),
        "normalize": manifest.normalize,
        "normalize_unit": normalize_meta["unit"] if normalize_meta else None,
    }
    return HarvestResult(rows=rows, provenance=provenance, dropped=dropped)


# Drop reasons that point at the divisor rather than the variable itself.
_NORMALIZE_REASONS = {"no_data_normalize", "not_numeric", "divide_by_zero"}


def _nothing_usable(manifest: Manifest, dropped: list) -> tuple[str, str]:
    """Explain an empty result by blaming the column actually at fault."""
    reasons = {item["reason"] for item in dropped}
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


def _select_region(features: list, region: str, level: str = "admin_0") -> list:
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
    # non-sovereign entities, so this is a code match, not an ISO3 lookup. At
    # admin-1 it is the only filter there is.
    country = registry.country_property(level)
    return [f for f in features
            if str(f["properties"].get(country, "")).lower() == wanted]


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


def _year_label(years: set) -> str | None:
    """The vintage, stated automatically so the caller never has to remember to."""
    if not years:
        return None
    if len(years) == 1:
        return str(next(iter(years)))
    return f"{min(years)}–{max(years)}"
