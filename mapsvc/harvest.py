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

__all__ = ["HarvestResult", "HarvestError", "harvest", "load_source",
           "select_region", "validate_window"]

FETCH_TIMEOUT = 60
USER_AGENT = "mapsvc/0.1 (+https://github.com/nvkelso/natural-earth-vector)"

# -99 is Natural Earth's explicit no-data marker. 0 is a no-data marker only for
# counts -- a country with zero people or zero GDP is a placeholder row, but a
# rank or an index of 0 could be real.
NO_DATA = {-99, "-99", "", None}

# Bump when the shape of a cached HarvestResult changes -- new provenance keys,
# a different join, altered drop reasons. Without it a code change silently
# keeps serving rows written in the old format.
CACHE_VERSION = 19

# Physical context is derived from the overlay and its window, never requested
# in the manifest. These files are fetched here and cached with the other input
# data; render only receives clipped, JSON-serialisable geometry.
NATURAL_EARTH_GEOJSON = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{}"
)
RIVERS_50M = "ne_50m_rivers_lake_centerlines.geojson"
LAKES_50M = "ne_50m_lakes.geojson"
# At a close regional scale Natural Earth's supplementary 10m European rivers
# supply tributaries omitted from 50m (including the Neman and Berezina on the
# Minard extent). Selection is still scale- and extent-derived, not map-named.
RIVERS_10M = "ne_10m_rivers_lake_centerlines.geojson"
RIVERS_EUROPE_10M = "ne_10m_rivers_europe.geojson"
PLATES_URL = (
    "https://raw.githubusercontent.com/fraxen/tectonicplates/master/"
    "GeoJSON/PB2002_boundaries.json"
)
REGIONAL_PHYSICAL_MAX_SPAN = 40.0
DETAILED_RIVERS_MAX_SPAN = 20.0


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
    _state_derived_basemap_rule(manifest, result)
    if manifest.overlays:
        _add_overlays(manifest, result)
    return result


def _state_derived_basemap_rule(manifest: Manifest, result: HarvestResult) -> None:
    """Put the enforced wide-admin_0 source choice in the existing footnote."""
    if (manifest.level == "admin_0" and manifest.bbox is not None
            and manifest.bbox[2] - manifest.bbox[0] > 90
            and manifest.basemap_source == "natural_earth"):
        note = "Natural Earth required for admin_0 windows wider than 90°"
        vintage = str(result.provenance.get("vintage") or "")
        if note not in vintage:
            result.provenance["vintage"] = f"{vintage}; {note}".lstrip("; ")


def _framed_on_overlays(manifest: Manifest) -> HarvestResult:
    """No window given: fetch the overlays first and frame the map on them.

    The agent cannot see a dataset's extent before it is fetched; asked to
    guess a window for Minard's army it once started it in Paris. The data
    knows where it is.
    """
    import dataclasses

    from mapsvc import overlay_render

    world = overlays.Window(bbox=None, within=None, extent=WORLD_EXTENT)
    layers = _load_overlay_layers(manifest.overlays, world)
    box = overlay_render.overlay_extent(layers)
    box = WORLD_EXTENT if box is None else project.frame_box(box)
    result = _base(dataclasses.replace(manifest, bbox=box))
    result.overlays = layers
    result.provenance["overlay_bbox"] = list(box)
    _add_physical_context(manifest, result, box)
    brings_places = any(layer["kind"] == "places" or layer.get("own_labels")
                        for layer in layers)
    if box[2] - box[0] <= PLACES_MAX_SPAN and not brings_places:
        result.places = context_places(box, None)
    return result


def frame_box(box: tuple) -> tuple:
    """An overlay's extent padded into a window: 8% plus 0.3° each side, and
    tall enough (a third of its ground width) that a long thin route is not
    framed as a ribbon."""
    return project.frame_box(box)


def _base(manifest: Manifest) -> HarvestResult:
    key = cache_key(manifest)
    cached = cache_dir() / "harvest" / f"{key}.json"
    if cached.exists():
        stored = json.loads(cached.read_text())
        return HarvestResult(stored["rows"], stored["provenance"], stored["dropped"],
                             context=stored.get("context", []),
                             names=stored.get("names", []),
                             hierarchy=stored.get("hierarchy", []))

    result = _build(manifest)
    _add_context(manifest, result)
    _add_names(manifest, result)
    result.provenance["place"] = _place(manifest, result)
    cached.parent.mkdir(parents=True, exist_ok=True)
    payload = {"rows": result.rows, "provenance": result.provenance,
               "dropped": result.dropped, "context": result.context,
               "names": result.names, "hierarchy": result.hierarchy}
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
    result.overlays.extend(_load_overlay_layers(manifest.overlays, window))
    brings_places = any(layer["kind"] == "places" or layer.get("own_labels")
                        for layer in result.overlays)
    if manifest.bbox is None and geometries:
        # Labels for what will be framed: the units near the overlay, not
        # every unit `within` brings (Réunion is not on a map of LGVs).
        from mapsvc import overlay_render

        extent = project.extent_of(overlay_render.focus(geometries, result.overlays))
    small = (extent[2] - extent[0]) <= PLACES_MAX_SPAN
    if small and not brings_places:
        result.places = context_places(extent, manifest.within)
    elif not brings_places and (routes := [layer for layer in result.overlays
                                           if layer.get("style") == "routes"]):
        # A route across a continent still has its stops: the places it passes.
        from mapsvc import overlay_render

        result.places = overlay_render.places_along(
            context_places(extent, manifest.within, keep=None), routes)[:PLACES_KEEP]
    _add_physical_context(manifest, result, extent)


def _load_overlay_layers(specs, window) -> list:
    """Requested layers plus deterministic catalogue companions, once each."""
    requested = list(specs)
    explicit = set(requested)
    expanded = []
    seen = set()
    for source, overlay_id in requested:
        pair = (source, overlay_id)
        if pair not in seen:
            expanded.append(pair)
            seen.add(pair)
        for companion in overlays.companions(source, overlay_id):
            pair = (source, companion)
            if pair not in seen and pair not in explicit:
                expanded.append(pair)
                seen.add(pair)
    layers = []
    for source, overlay_id in expanded:
        try:
            layers.append(overlays.load(source, overlay_id, window))
        except overlays.OverlayError as exc:
            raise HarvestError(str(exc), exc.field) from exc
    return layers


def _add_physical_context(manifest: Manifest, result: HarvestResult,
                          extent: tuple) -> None:
    """Add derived hydrography and tectonics appropriate to overlay semantics.

    Regional overlays under 40 degrees receive Natural Earth rivers and lakes.
    Any provider or layer declaring a seismic theme receives PB2002 plate
    boundaries.
    A failed optional context fetch never prevents the requested map itself.
    """
    kinds = []
    if extent[2] - extent[0] < REGIONAL_PHYSICAL_MAX_SPAN:
        kinds.append("hydrography")
    # A source declares its theme, or a catalogue layer does (volcanoes).
    if (any(overlays.capabilities(source).theme == "seismic"
            for source, _ in manifest.overlays)
            or any(layer.get("theme") == "seismic" for layer in result.overlays)):
        kinds.append("tectonic_plates")
    for kind in kinds:
        try:
            result.physical.extend(load_physical_context(kind, extent))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result.provenance.setdefault("physical_context_errors", []).append(
                f"{kind}: {exc}"
            )


def load_physical_context(kind: str, extent: tuple) -> list:
    """Fetch and clip one derived physical-context family.

    Kept as a public seam so offline tests can replace the loader. Returned
    features are sorted and contain no source URLs; provenance names the
    dataset and publisher for the renderer's footnote.
    """
    from mapsvc import fetch

    if kind == "tectonic_plates":
        data = json.loads(fetch.cached(PLATES_URL, "physical", "PB2002_boundaries.json"))
        return [{
            "kind": "lines", "class": "plate-boundaries",
            "features": _line_features(data, extent, "plate"),
            "provenance": {
                "name": "PB2002 tectonic plate boundaries",
                "publisher": "Peter Bird (2003), GeoJSON by Hugo Ahlenius",
                "license": "ODC-By 1.0",
            },
        }]
    if kind != "hydrography":
        raise ValueError(f"unknown physical context {kind!r}")

    rivers = _physical_geojson(RIVERS_50M)
    river_features = _line_features(rivers, extent, "river")
    if extent[2] - extent[0] < DETAILED_RIVERS_MAX_SPAN:
        # Natural Earth explicitly publishes these as supplementary rivers for
        # close-scale European maps. min_zoom <= 6 prevents a dense stream mesh.
        known = {_feature_name(f).casefold() for f in rivers.get("features", [])
                 if _feature_name(f)}
        for filename in (RIVERS_10M, RIVERS_EUROPE_10M):
            detailed = _physical_geojson(filename)
            selected = {"features": [f for f in detailed.get("features", [])
                                      if float((f.get("properties") or {}).get(
                                          "min_zoom") or 99) <= 6.0
                                      and _feature_name(f).casefold() not in known]}
            extra = _line_features(selected, extent, "river-detail")
            river_features.extend(extra)
            known.update(f["name"].casefold() for f in extra if f.get("name"))
    river_features.sort(key=lambda f: (f.get("name") or "", f["id"]))
    lakes = _physical_geojson(LAKES_50M)
    return [
        {
            "kind": "polygons", "class": "lakes",
            "features": _polygon_features(lakes, extent, "lake"),
            "provenance": {
                "name": "Natural Earth ne_50m_lakes",
                "publisher": "Natural Earth",
                "license": "public domain",
            },
        },
        {
            "kind": "lines", "class": "rivers",
            "features": river_features,
            "provenance": {
                "name": ("Natural Earth ne_50m_rivers_lake_centerlines"
                         + (" with 10m supplementary rivers"
                            if extent[2] - extent[0] < DETAILED_RIVERS_MAX_SPAN else "")),
                "publisher": "Natural Earth",
                "license": "public domain",
            },
        },
    ]


def _physical_geojson(filename: str) -> dict:
    from mapsvc import fetch

    url = NATURAL_EARTH_GEOJSON.format(filename)
    return json.loads(fetch.cached(url, "physical", filename))


def _feature_name(feature: dict) -> str:
    props = feature.get("properties") or {}
    return str(props.get("name_en") or props.get("name") or props.get("label") or "")


def _line_features(data: dict, extent: tuple, prefix: str) -> list:
    found = []
    for index, feature in enumerate(data.get("features", [])):
        geometry = feature.get("geometry") or {}
        lines = ([geometry.get("coordinates") or []]
                 if geometry.get("type") == "LineString"
                 else geometry.get("coordinates") or []
                 if geometry.get("type") == "MultiLineString" else [])
        name = _feature_name(feature)
        props = feature.get("properties") or {}
        # Natural Earth's own importance: the zoom a feature first shows at.
        # Render names the major rivers first by it.
        rank = props.get("min_zoom", props.get("scalerank"))
        for part, coords in enumerate(lines):
            clean = [[round(float(p[0]), 5), round(float(p[1]), 5)]
                     for p in coords if len(p) >= 2]
            if len(clean) >= 2 and _points_intersect(clean, extent):
                item = {"id": f"{prefix}-{index}-{part}", "name": name, "coords": clean}
                if rank is not None:
                    item["rank"] = round(float(rank), 2)
                found.append(item)
    found.sort(key=lambda f: (f["name"], f["id"]))
    return found


def _polygon_features(data: dict, extent: tuple, prefix: str) -> list:
    found = []
    for index, feature in enumerate(data.get("features", [])):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        points = list(_geometry_points(geometry.get("coordinates") or []))
        if points and _points_intersect(points, extent):
            found.append({"id": f"{prefix}-{index}", "name": _feature_name(feature),
                          "geometry": geometry})
    found.sort(key=lambda f: (f["name"], f["id"]))
    return found


def _geometry_points(value):
    if (isinstance(value, (list, tuple)) and len(value) >= 2
            and all(isinstance(v, (int, float)) for v in value[:2])):
        yield value
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            yield from _geometry_points(child)


def _points_intersect(points, extent: tuple) -> bool:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return not (max(xs) < extent[0] or min(xs) > extent[2]
                or max(ys) < extent[1] or min(ys) > extent[3])


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


def context_places(extent: tuple, within: tuple | None,
                   keep: int | None = PLACES_KEEP) -> list:
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
    return found[:keep] if keep else found


# ----------------------------------------------------------------- context ---

# How far beyond the mapped units the context land reaches, as a share of the
# units' larger ground span. The page is at most ~3:1 either way, so 0.8 on
# each side covers whatever slack the fit leaves; the clipped edges stay off
# the page.
CONTEXT_REACH = 0.8
CONTEXT_LEVEL = "admin_0"


def context_countries() -> list:
    """Natural Earth countries for the context layer. A seam tests replace."""
    return load_source(CONTEXT_LEVEL)["features"]


def _add_context(manifest: Manifest, result: HarvestResult) -> None:
    """Neighbouring land for a `within` map, muted, so it does not float.

    A bbox map already carries everything in its window; a `within` map
    carries only the units asked for, and Russia, Poland and Belarus vanish
    from around Ukraine. Natural Earth countries near the units are clipped to
    a generous window around them and handed to render as plain geometry.
    Countries whose label point lies in a drawn unit are the mapped land
    itself and are left out, so their coarser coastline cannot peek out.
    """
    if not manifest.within:
        return
    drawn = [r["geometry"] for r in result.rows] + [d["geometry"] for d in result.dropped
                                                     if d.get("geometry")]
    if not drawn:
        return
    extent = project.extent_of(drawn)
    if extent == project.WORLD_EXTENT:
        return
    window = _context_window(extent)
    try:
        features = context_countries()
    except OSError as exc:      # no network and no cached file: map without it
        result.provenance["context_error"] = str(exc)
        return

    outer = [(_box(ring), ring) for g in drawn for ring in project.outer_rings(g)
             if len(ring) >= 4]
    context = []
    for feature in features:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry")
        if not geometry or geometry["type"] not in ("Polygon", "MultiPolygon"):
            continue
        label = (props.get("LABEL_X"), props.get("LABEL_Y"))
        if None not in label and any(
                b[0] <= label[0] <= b[2] and b[1] <= label[1] <= b[3]
                and project.point_in_ring(label[0], label[1], ring) for b, ring in outer):
            continue
        rings = []
        for ring in project.outer_rings(geometry):
            box = _box(ring)
            if box[2] < window[0] or box[0] > window[2] or box[3] < window[1] \
                    or box[1] > window[3]:
                continue
            clipped = project.clip_to_extent(ring, window)
            if len(clipped) >= 3:
                clipped = [(round(x, 5), round(y, 5)) for x, y in clipped]
                rings.append([list(p) for p in clipped + [clipped[0]]])
        if rings:
            # The name and Natural Earth's own label point travel with the
            # land, so render can name the neighbours without a lookup.
            item = {"id": registry.iso3_of(props) or props.get("ADM0_A3"),
                    "name": str(props.get("NAME") or props.get("ADMIN") or ""),
                    "geometry": {"type": "MultiPolygon",
                                 "coordinates": [[r] for r in rings]}}
            if None not in label:
                item["label"] = [round(float(label[0]), 4), round(float(label[1]), 4)]
            context.append(item)
    context.sort(key=lambda c: str(c["id"]))
    result.context = list(result.context) + context
    if context:
        result.provenance["context_source"] = (
            f"{registry.SOURCE_NAME} {registry.SOURCE_VINTAGE} 1:"
            f"{registry.scale_for(CONTEXT_LEVEL)} (public domain)")


# ------------------------------------------------------------------- names ---

# Sea and ocean names for orientation: Natural Earth's marine areas, public
# domain, 306 named polygons at 1:10m (1.7 MB). Harvest keeps those Natural
# Earth would label at a country's zoom or wider (min_label <= 6, which takes
# in the Sea of Azov); render picks by its frame's span which of them to name.
MARINE_POLYS = "ne_10m_geography_marine_polys.geojson"
MARINE_CREDIT = "Natural Earth ne_10m_geography_marine_polys (public domain)"
MARINE_MAX_MIN_LABEL = 6.0


def marine_areas() -> list:
    """Natural Earth marine polygons. A seam tests replace."""
    return _physical_geojson(MARINE_POLYS).get("features", [])


def _add_names(manifest: Manifest, result: HarvestResult) -> None:
    """Named seas around the map, clipped to its neighbourhood.

    Derived, never requested: a regional map gets the seas Natural Earth
    would label at its scale; a world map gets none (its oceans are obvious
    and its labels are the data). A failed fetch leaves the map unnamed,
    never unmade.
    """
    drawn = [r["geometry"] for r in result.rows] + [d["geometry"] for d in result.dropped
                                                     if d.get("geometry")]
    if manifest.bbox is not None:
        extent = tuple(manifest.bbox)
    elif drawn:
        extent = project.extent_of(drawn)
    else:
        return
    if extent == project.WORLD_EXTENT or extent[2] - extent[0] >= 180.0:
        return
    window = _context_window(extent) if manifest.bbox is None else extent
    try:
        features = marine_areas()
    except (OSError, ValueError) as exc:
        result.provenance["names_error"] = str(exc)
        return
    names = []
    for feature in features:
        props = feature.get("properties") or {}
        name = str(props.get("name_en") or props.get("name") or "").strip()
        geometry = feature.get("geometry") or {}
        if not name or geometry.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        if float(props.get("min_label") or 99) > MARINE_MAX_MIN_LABEL:
            continue
        rings = []
        for ring in project.outer_rings(geometry):
            box = _box(ring)
            if box[2] < window[0] or box[0] > window[2] or box[3] < window[1] \
                    or box[1] > window[3]:
                continue
            clipped = project.clip_to_extent(ring, window)
            if len(clipped) >= 3:
                clipped = [(round(x, 4), round(y, 4)) for x, y in clipped]
                rings.append([list(p) for p in clipped + [clipped[0]]])
        if rings:
            names.append({"kind": "sea", "name": name,
                          "class": str(props.get("featurecla") or "sea").lower(),
                          "rank": float(props.get("min_label") or 0),
                          "geometry": {"type": "MultiPolygon",
                                       "coordinates": [[r] for r in rings]}})
    names.sort(key=lambda n: (n["rank"], n["name"]))
    result.names = list(result.names) + names
    if names:
        result.provenance["names_source"] = MARINE_CREDIT


_UNION = {"EU", "EU27", "EU27_2020"}


def _place(manifest: Manifest, result: HarvestResult) -> str | None:
    """Where the map is, in words, for the title: 'Ukraine', 'California', 'Europe'.

    From the political filter the manifest names, resolved against Natural
    Earth's country names (the context file, already on disk) and the static
    US state table. A long list of countries is named by the continent most of
    them share, or not at all; a bbox is named only when it is the world.
    """
    from mapsvc import usfips

    if not manifest.within:
        return "World" if manifest.bbox is not None and tuple(manifest.bbox) == \
            project.WORLD_EXTENT else None
    try:
        countries = context_countries()
    except OSError:
        countries = []
    by_code: dict = {}
    for feature in countries:
        props = feature.get("properties") or {}
        for key in ("ADM0_A3", "ISO_A3", "ISO_A2_EH", "ISO_A2"):
            code = str(props.get(key) or "").upper()
            if code and code != "-99":
                by_code.setdefault(code, props)
    unit_names = {str(u["id"]).upper(): u.get("name") for u in result.rows + result.dropped}

    names, continents = [], []
    for code in manifest.within:
        if code in _UNION:
            names.append("European Union")
        elif code.startswith("US-") and code[3:] in usfips.NAMES:
            names.append(usfips.NAMES[code[3:]])
        elif code in by_code:
            names.append(by_code[code].get("NAME") or code)
            continents.append(by_code[code].get("CONTINENT"))
        elif unit_names.get(code):
            names.append(unit_names[code])
        else:
            names.append(code)
    if len(names) == 1:
        return names[0]
    if len(names) <= 3:
        return ", ".join(names[:-1]) + " and " + names[-1]
    if continents and len(continents) == len(names):
        top = max(set(continents), key=lambda c: (continents.count(c), str(c)))
        if top and continents.count(top) >= 0.8 * len(continents):
            return top
    return None


def _context_window(extent) -> tuple:
    import math
    min_lon, min_lat, max_lon, max_lat = extent
    lat0 = (min_lat + max_lat) / 2.0
    squash = max(0.2, math.cos(math.radians(lat0)))
    ground = max((max_lon - min_lon) * squash, max_lat - min_lat, 1.0)
    reach_lat = CONTEXT_REACH * ground
    reach_lon = reach_lat / squash
    window = (max(-180.0, min_lon - reach_lon), max(-90.0, min_lat - reach_lat),
              min(180.0, max_lon + reach_lon), min(90.0, max_lat + reach_lat))
    return window


def _box(ring) -> tuple:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


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


def validate_window(manifest: Manifest):
    """Ensure a political selection is not silently clipped by its bbox.

    Returns the already-loaded boxed boundaries so harvest need not issue that
    request again. The unboxed selection defines all member units.
    """
    if manifest.bbox is None or not manifest.within:
        return None
    complete = cartography.load(manifest.basemap_source, manifest.level, None,
                                manifest.basemap_detail, manifest.within)
    # Providers all use this same antimeridian-aware geometry intersection
    # rule. Applying it to the complete selection avoids a second remote query
    # and, crucially, does not turn an unrelated provider failure into a false
    # "all units clipped" diagnosis.
    from mapsvc.cartography.natural_earth import _intersecting

    features = [{"geometry": unit["geometry"], "properties": {"id": unit["id"]}}
                for unit in complete.units]
    present = {str(feature["properties"]["id"])
               for feature in _intersecting(features, manifest.bbox)}
    missing = [unit for unit in complete.units if str(unit["id"]) not in present]
    if missing:
        names = sorted(str(unit.get("name") or unit["id"]) for unit in missing)
        shown = ", ".join(names[:12])
        if len(names) > 12:
            shown += f", and {len(names) - 12} more"
        raise HarvestError(
            f"bbox cuts {len(names)} units selected by within off the political "
            f"map: {shown}; drop the bbox or widen it to include every unit",
            "bbox",
        )
    return complete


def _build(manifest: Manifest) -> HarvestResult:
    _check_granularity(manifest)
    boundaries = validate_window(manifest)
    if boundaries is None:
        boundaries = cartography.load(manifest.basemap_source, manifest.level,
                                      manifest.bbox, manifest.basemap_detail,
                                      manifest.within)
    _require_named_units(manifest, boundaries)
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
    boundary_note = boundaries.provenance.get("note")
    if boundary_note:
        provenance["vintage"] = (
            f"{provenance['vintage']}; {boundary_note}"
            if provenance.get("vintage") else boundary_note
        )

    if manifest.variable_id is None:
        # A base map: cartography with nothing painted on it.
        rows = [{"id": u["id"], "geometry": u["geometry"], "value": None,
                 "name": u.get("name")} for u in boundaries.units]
        hierarchy = _hierarchy(manifest, provenance)
        return HarvestResult(rows=rows, provenance=provenance, dropped=[],
                             hierarchy=hierarchy)

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
        "diverging": values.provenance.get("diverging"),
        "index_reference": values.provenance.get("index_reference"),
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
    drawn_years: list = []          # (name, year) of each drawn unit
    keys_of: dict = {}
    rows: list[dict] = []
    dropped: list[dict] = []

    for unit in boundaries.units:
        entry = {"id": unit["id"], "geometry": unit["geometry"]}
        if unit.get("name"):
            # For the footnote and labels: "Crimea", not an Overture UUID.
            entry["name"] = unit["name"]
        raw_key = (unit.get("keys") or {}).get(statistics_key)
        # Compatibility for third-party providers written against the original
        # single-key protocol, but only after their result explicitly declared
        # that the legacy key is in the required namespace.
        if raw_key is None and statistics_key in boundary_keys:
            raw_key = unit.get("key")
        key = str(raw_key).upper() if raw_key else None
        keys_of[unit["id"]] = key

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
            drawn_years.append((entry.get("name") or str(unit["id"]), values.years[key]))
        if key in values.divisor_years:
            joined_divisor_years.add(values.divisor_years[key])

    if not rows:
        raise HarvestError(*_nothing_usable(manifest, dropped))

    plain = _plain_land(dropped, keys_of, statistics_key, manifest.within)
    if plain:
        dropped = [d for d in dropped if d not in plain]
        provenance["plain_land"] = sorted(str(d.get("name") or d["id"]) for d in plain)
    _state_what_is_drawn(provenance, values, dropped, joined_years,
                         joined_divisor_years, drawn_years)
    rows.sort(key=lambda r: str(r["id"]))
    dropped.sort(key=lambda r: (str(r["id"]), r["reason"]))
    context = [{"id": str(d["id"]), "geometry": d["geometry"]}
               for d in sorted(plain, key=lambda d: str(d["id"]))]
    hierarchy = _hierarchy(manifest, provenance)
    return HarvestResult(rows=rows, provenance=provenance, dropped=dropped,
                         context=context, hierarchy=hierarchy)


def _hierarchy(manifest: Manifest, provenance: dict) -> list:
    """A coarser boundary layer implied by a detailed administrative map.

    NUTS 2 and 3 polygons do not make their NUTS 1 parents legible merely by
    sharing many hairline edges. Fetching the published parent geometry once
    is both smaller and more faithful than trying to union hundreds of child
    rings without a topology library.
    """
    if manifest.basemap_source != "gisco" or manifest.level not in ("nuts_2", "nuts_3"):
        return []
    parents = cartography.load("gisco", "nuts_1", manifest.bbox, "full",
                               manifest.within)
    provenance["hierarchy_level"] = "nuts_1"
    provenance["hierarchy_scale"] = parents.provenance.get("scale")
    return [{"id": unit["id"], "name": unit.get("name"),
             "geometry": unit["geometry"]} for unit in parents.units]


# Territories with no permanent population, which no statistics source covers.
# Hatching Antarctica as "no data" on every world map says a value is missing
# where none could exist; it is drawn as plain land and not counted as excluded.
UNINHABITED = {"ATA", "ATF", "HMD", "BVT", "SGS"}


def _require_named_units(manifest: Manifest, boundaries) -> None:
    """An explicitly named country may not disappear before the value join.

    Descendant selectors (CAN at admin_1, US-CA at admin_2, EU at NUTS) do not
    name units at the requested level and are checked by the ordinary invariant
    that every returned boundary enters either rows or dropped. At admin_0 each
    selector is the unit itself, so absence is unambiguous and must be loud.
    """
    if manifest.level != "admin_0" or not manifest.within:
        return
    aliases = set()
    for unit in boundaries.units:
        aliases.add(str(unit.get("id") or "").upper())
        aliases.update(str(value).upper() for value in (unit.get("keys") or {}).values()
                       if value)
        if unit.get("key"):
            aliases.add(str(unit["key"]).upper())
    special = {"EU", "EU27", "EU27_2020", "WORLD"}
    missing = sorted(code for code in manifest.within
                     if code.upper() not in aliases and code.upper() not in special)
    if missing:
        raise HarvestError(
            "the boundary source did not return explicitly selected unit(s): "
            + ", ".join(missing), "within")


def _plain_land(dropped: list, keys_of: dict, statistics_key: str,
                within: tuple | None = None) -> list:
    if statistics_key != "ISO3":
        return []
    explicit = {str(code).upper() for code in (within or ())}
    return [d for d in dropped if d["reason"] in ("no_join", "no_data")
            and keys_of.get(d["id"]) in UNINHABITED
            and keys_of.get(d["id"]) not in explicit]


# What a drop reason means, for the footnote. A source can add its own (Ukrstat:
# "not_estimated") or override these through provenance["reasons"].
DROP_REASONS = {
    "no_data": "no value reported by the source",
    "no_join": "no matching row in the statistics source",
    "no_divisor": "no divisor value to divide by",
    "not_estimated": "not estimated by the source",
}


def _state_what_is_drawn(provenance: dict, values, dropped: list,
                         joined_years: set, joined_divisor_years: set,
                         drawn_years: list = ()) -> None:
    """Restate the provenance over the units actually drawn, for render to read.

    A source reports the years of everything it loaded; the title and footnote
    must state the years of what is drawn. Eurostat's NUTS 2 file spans
    2021-2024 only because of candidate countries an EU map never shows, and
    Natural Earth's GDP_YEAR spans 2009-2025 over the world but not over a
    16-country map of Europe.
    """
    if joined_years and re.fullmatch(r"\d{4}(–\d{4})?", str(provenance.get("year") or "")):
        provenance["year"] = derive.year_label(joined_years)
    # More than one distinct year means each unit shows its latest observation
    # and they disagree: the title says "latest data" rather than implying one
    # vintage.
    provenance["years_drawn"] = sorted(joined_years)
    if len(joined_years) > 1 and drawn_years:
        # The year most units share, and who differs: "2025 (Ukraine 2022;
        # Albania, Iceland 2024)" rather than a "latest data (2022-2025)" range.
        counts: dict = {}
        for _, year in drawn_years:
            counts[year] = counts.get(year, 0) + 1
        mode = max(counts, key=lambda y: (counts[y], y))
        others: dict = {}
        for name, year in drawn_years:
            if year != mode:
                others.setdefault(str(year), []).append(name)
        provenance["year_mode"] = mode
        provenance["year_exceptions"] = {y: sorted(n) for y, n in sorted(others.items())}

    per = provenance.get("per")
    if per and values.divisor_years is not None and (joined_divisor_years
                                                     or values.divisor_years):
        # The divisor's year over the drawn units only; None if none is dated.
        # The footnote states it beside the divisor, and the title keeps the
        # numerator's year.
        provenance["per"] = {**per, "year": derive.year_label(joined_divisor_years)}

    meanings = {**DROP_REASONS, **(values.provenance.get("reasons") or {})}
    present = sorted({item["reason"] for item in dropped})
    provenance["drop_reasons"] = {reason: meanings.get(reason, reason.replace("_", " "))
                                  for reason in present}


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
