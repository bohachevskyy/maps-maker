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
import re
import os
import pathlib
import urllib.request

from mapsvc import cartography, derive, overlays, project, registry, statistics
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
CACHE_VERSION = 5


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
    # Overlays are fetched and cached by their own sources; the boundaries and
    # values under them do not change with them.
    keyed.pop("overlays", None)
    if manifest.basemap_source == "overture":
        # A new Overture release is new geometry; the old rows must not be
        # served under it.
        from mapsvc.cartography import overture

        keyed["overture_release"] = overture.RELEASE
    canonical = json.dumps(keyed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def harvest(manifest: Manifest) -> HarvestResult:
    if manifest.overlays and manifest.bbox is None and not manifest.within:
        return _framed_on_overlays(manifest)
    result = _base(manifest)
    if manifest.overlays:
        _add_overlays(manifest, result)
    return result


def _framed_on_overlays(manifest: Manifest) -> HarvestResult:
    """No window given: fetch the overlays first and frame the map on them.

    The agent cannot see a dataset's extent before it is fetched; asked to
    guess a window for Minard's army it once started it in Paris. The data
    knows where it is.
    """
    import dataclasses

    from mapsvc import overlay_render

    world = overlays.Window(bbox=None, within=None, extent=WORLD_EXTENT)
    layers = []
    for source, overlay_id in manifest.overlays:
        try:
            layers.append(overlays.load(source, overlay_id, world))
        except overlays.OverlayError as exc:
            raise HarvestError(str(exc), exc.field) from exc
    box = overlay_render.overlay_extent(layers)
    box = WORLD_EXTENT if box is None else frame_box(box)
    result = _base(dataclasses.replace(manifest, bbox=box))
    result.overlays = layers
    result.provenance["overlay_bbox"] = list(box)
    brings_places = any(layer["kind"] == "places" for layer in layers)
    if box[2] - box[0] <= PLACES_MAX_SPAN and not brings_places:
        result.places = context_places(box, None)
    return result


def frame_box(box: tuple) -> tuple:
    """An overlay's extent padded into a window: 8% plus 0.3° each side, and
    tall enough (a third of its ground width) that a long thin route is not
    framed as a ribbon."""
    import math

    west, south, east, north = box
    dx = (east - west) * 0.08 + 0.3
    dy = (north - south) * 0.08 + 0.3
    west, east, south, north = west - dx, east + dx, south - dy, north + dy
    ground = (east - west) * math.cos(math.radians((south + north) / 2))
    if north - south < ground / 3:
        grow = (ground / 3 - (north - south)) / 2
        south, north = south - grow, north + grow
    return (round(max(-180.0, west), 2), round(max(-90.0, south), 2),
            round(min(180.0, east), 2), round(min(90.0, north), 2))


def _base(manifest: Manifest) -> HarvestResult:
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


# ---------------------------------------------------------------- overlays ---

# Context labels are for orientation on a regional map; on a world map the
# overlay carries its own labels and city names would only compete with it.
PLACES_MAX_SPAN = 60.0
PLACES_KEEP = 80
PLACES_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
              "master/geojson/ne_10m_populated_places_simple.geojson")


def _add_overlays(manifest: Manifest, result: HarvestResult) -> None:
    geometries = [r["geometry"] for r in result.rows]
    geometries += [d["geometry"] for d in result.dropped if d.get("geometry")]
    extent = tuple(manifest.bbox or (project.extent_of(geometries) if geometries
                                     else WORLD_EXTENT))
    window = overlays.Window(bbox=manifest.bbox, within=manifest.within, extent=extent,
                             name=_place_name(manifest.within))
    for source, overlay_id in manifest.overlays:
        try:
            result.overlays.append(overlays.load(source, overlay_id, window))
        except overlays.OverlayError as exc:
            raise HarvestError(str(exc), exc.field) from exc
    brings_places = any(layer["kind"] == "places" for layer in result.overlays)
    if manifest.bbox is None and geometries:
        # Labels for what will be framed: the units near the overlay, not
        # every unit `within` brings (Réunion is not on a map of LGVs).
        from mapsvc import overlay_render

        extent = project.extent_of(overlay_render.focus(geometries, result.overlays))
    small = (extent[2] - extent[0]) <= PLACES_MAX_SPAN
    if small and not brings_places:
        result.places = context_places(extent, manifest.within)


WORLD_EXTENT = (-180.0, -90.0, 180.0, 90.0)


def _place_name(within: tuple | None) -> str | None:
    """'France' for within ["FRA"]; None for anything that is not one country."""
    if not within or len(within) != 1 or "-" in within[0]:
        return None
    code = within[0].upper()
    for feature in load_source("admin_0")["features"]:
        props = feature["properties"]
        if code in {str(props.get(k, "")).upper() for k in ("ADM0_A3", "ISO_A3", "ISO_A2_EH")}:
            return props.get("NAME")
    return None


def context_places(extent: tuple, within: tuple | None) -> list:
    """Natural Earth populated places inside the window, largest first.

    Public domain, 7,342 places at 1:10m. A `within` of country codes keeps
    only that country's places: a map of France's railways labels French
    cities, not Geneva and Turin.
    """
    from mapsvc import fetch

    data = json.loads(fetch.cached(PLACES_URL, "ne_10m_populated_places_simple.geojson"))
    countries = None
    if within:
        countries = {code for code in within if "-" not in code and len(code) == 3}
        countries = countries or None
    west, south, east, north = extent
    found = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        lon, lat = (feature.get("geometry") or {}).get("coordinates", (None, None))[:2]
        if lon is None or not (west <= lon <= east and south <= lat <= north):
            continue
        if countries and str(props.get("adm0_a3", "")).upper() not in countries:
            continue
        found.append({
            "name": str(props.get("name") or props.get("nameascii") or ""),
            "lon": round(float(lon), 4), "lat": round(float(lat), 4),
            "pop": int(props.get("pop_max") or 0),
            "capital": bool(props.get("adm0cap")),
        })
    found = [p for p in found if p["name"]]
    found.sort(key=lambda p: (-p["pop"], p["name"]))
    return found[:PLACES_KEEP]


# -------------------------------------------------------------------- join ---

# One coarser step, for the message when a level is too fine for a window.
_COARSER = {"admin_3": "admin_2", "admin_2": "admin_1", "admin_1": "admin_0",
            "nuts_3": "nuts_2", "nuts_2": "nuts_1"}


def _check_granularity(manifest: Manifest) -> None:
    """Refuse a window/level pair that would return an unusable number of units.

    Counting is a few seconds and pulls no geometry; fetching Europe at admin_2
    is 11,497 polygons and hundreds of megabytes. Doing the cheap query first
    turns a very slow success into a fast, explicit failure.
    """
    if not registry.COUNT_BEFORE_FETCH:
        return
    units = cartography.count(manifest.basemap_source, manifest.level,
                              manifest.bbox, manifest.within)
    if units <= registry.MAX_UNITS:
        return
    coarser = _COARSER.get(manifest.level)
    suggestion = (f" Try {coarser}, or a smaller bbox." if coarser
                  else " Use a smaller bbox.")
    raise HarvestError(
        f"{units:,} {manifest.level} units intersect that bbox, over the "
        f"{registry.MAX_UNITS:,} a single map can carry.{suggestion}",
        "level",
    )


def _build(manifest: Manifest) -> HarvestResult:
    _check_granularity(manifest)
    boundaries = cartography.load(manifest.basemap_source, manifest.level,
                                  manifest.bbox, manifest.basemap_detail,
                                  manifest.within)
    provenance = {
        "bbox": None if manifest.bbox is None else list(manifest.bbox),
        "within": manifest.within,
        "level": manifest.level,
        **{f"basemap_{k}": v for k, v in boundaries.provenance.items()},
        "source": boundaries.provenance.get("source"),
        "scale": boundaries.provenance.get("scale"),
        "license": boundaries.provenance.get("license"),
        "attribution": boundaries.provenance.get("attribution"),
        "vintage": boundaries.provenance.get("release"),
        "variable": None, "unit": None, "year": None,
    }

    if manifest.variable_id is None:
        # A base map: cartography with nothing painted on it.
        rows = [{"id": u["id"], "geometry": u["geometry"], "value": None,
                 "name": u.get("name")} for u in boundaries.units]
        return HarvestResult(rows=rows, provenance=provenance, dropped=[])

    statistics_key = statistics.key_at(manifest.variable_source, manifest.level)
    boundary_keys = tuple(boundaries.keys or boundaries.provenance.get("keys", ()))
    if statistics_key not in boundary_keys:
        # validate() refuses this before any I/O; this guards providers whose
        # result disagrees with what their module declared.
        available = ", ".join(boundary_keys) or "no standard join keys"
        raise HarvestError(
            f"{manifest.basemap_source} {manifest.level} boundaries carry "
            f"{available}, but {manifest.variable_source} requires "
            f"{statistics_key}; choose a basemap and statistics source that "
            "share a join key",
            "basemap.source",
        )

    values = derive.values(manifest)

    provenance.update({
        "variable": values.provenance.get("variable"),
        "unit": values.provenance.get("unit"),
        "year": values.provenance.get("year"),
        "statistics_source": values.provenance.get("source"),
        "statistics_vintage": values.provenance.get("vintage"),
        # Carried separately from the basemap's: boundaries and numbers can be
        # licensed differently, and both have to be credited.
        "statistics_license": values.provenance.get("license"),
        "statistics_attribution": values.provenance.get("attribution"),
        "statistics_note": values.provenance.get("note"),
        "statistics_level": values.provenance.get("level"),
        "join_key": statistics_key,
        "per": values.provenance.get("per"),
        "change": values.provenance.get("change"),
    })

    # The measurement level of what is drawn, which render reads instead of
    # consulting a catalogue (that would be I/O). A `per` or `change` result is
    # a ratio whatever its numerator was: derive says so in its provenance.
    value_level = values.provenance.get("level")
    if value_level is None:
        value_level = statistics.describe(manifest.variable_source,
                                          manifest.variable_id).level
    provenance["measurement"] = value_level
    joined_years: set = set()
    joined_divisor_years: set = set()
    rows: list[dict] = []
    dropped: list[dict] = []

    for unit in boundaries.units:
        entry = {"id": unit["id"], "geometry": unit["geometry"]}
        raw_key = (unit.get("keys") or {}).get(statistics_key)
        # Compatibility for third-party providers written against the original
        # single-key protocol, but only after their result explicitly declared
        # that the legacy key is in the required namespace.
        if raw_key is None and statistics_key in boundary_keys:
            raw_key = unit.get("key")
        key = str(raw_key).upper() if raw_key else None

        if key is not None and key in values.missing:
            # The source covers this unit and says why it has no value.
            dropped.append({**entry, "reason": values.missing[key]})
            continue
        if key is None or key not in values.values:
            # The polygon exists but this source has no row for it. Common when
            # boundaries and statistics come from different providers.
            dropped.append({**entry, "reason": "no_join"})
            continue

        value = values.values[key]
        reason = _no_data_reason(
            value, "ratio" if values.provenance.get("zero_is_data") else value_level)
        if reason:
            dropped.append({**entry, "reason": reason})
            continue

        if value_level == "count" or isinstance(value, (int, float)):
            value = float(value)

        rows.append({**entry, "value": value})
        if key in values.years:
            joined_years.add(values.years[key])
        if key in values.divisor_years:
            joined_divisor_years.add(values.divisor_years[key])

    if not rows:
        raise HarvestError(*_nothing_usable(manifest, dropped))

    # A source reports the years of everything it loaded; the footnote must
    # state the years of what is drawn. Eurostat's NUTS 2 file spans 2021-2024
    # only because of candidate countries an EU map never shows.
    if joined_years and re.fullmatch(r"\d{4}(–\d{4})?", str(provenance.get("year") or "")):
        provenance["year"] = derive.year_label(joined_years)
    per = provenance.get("per")
    if per and values.divisor_years is not None and (joined_divisor_years
                                                     or values.divisor_years):
        # The divisor's year over the drawn units only; None if none is dated.
        per = provenance["per"] = {**per, "year": derive.year_label(joined_divisor_years)}
        if per["year"] and per["year"] != provenance.get("year"):
            # Divided by a figure from another year: the footnote must say so.
            provenance["year"] = f"{provenance.get('year')}; {per['phrase']} {per['year']}"

    rows.sort(key=lambda r: str(r["id"]))
    dropped.sort(key=lambda r: (str(r["id"]), r["reason"]))
    return HarvestResult(rows=rows, provenance=provenance, dropped=dropped)


def _nothing_usable(manifest: Manifest, dropped: list) -> tuple[str, str]:
    """Explain an empty result by blaming the column actually at fault."""
    reasons = {item["reason"] for item in dropped}
    if reasons and reasons <= {"no_join", "not_estimated"} and "no_join" in reasons:
        return (
            f"none of the {len(dropped)} {manifest.level} boundaries could be "
            f"matched to a {manifest.variable_source} row for "
            f"{manifest.variable_id}; the basemap and the statistics do not "
            "share a join key at this level",
            "variable.id",
        )
    return (
        f"no unit in that bbox has a usable {manifest.variable_id} value",
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
