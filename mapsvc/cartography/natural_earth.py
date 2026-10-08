"""Natural Earth boundaries, filtered by bounding box.

The same files the statistics provider reads, but this module returns only
geometry and identifiers. Anything thematic is the statistics layer's business.

Natural Earth draws the de facto Western Sahara boundary: most of the territory
west of the berm is part of its Morocco feature.  Mapsvc instead applies the
internationally recognised border at 27°40'N.  This is deliberately implemented
as small GeoJSON clipping primitives rather than introducing a geometry library.
"""

import math

from mapsvc import registry
from mapsvc.cartography import Boundaries, CartographyError

LEVELS = ("admin_0", "admin_1")
KEYS = {"admin_0": ("ISO3",), "admin_1": ("ISO3166_2",)}

FULL_CIRCLE = 360.0
WESTERN_SAHARA_BORDER = 27 + 40 / 60
WESTERN_SAHARA_NOTE = "Western Sahara shown as a non-self-governing territory (UN)"
EARTH_RADIUS_KM = 6371.0088


def _clip_ring(ring: list, latitude: float, *, below: bool) -> list:
    """Clip a closed GeoJSON ring against one side of a latitude line."""
    if len(ring) < 4:
        return []
    points = ring[:-1] if ring[0] == ring[-1] else ring
    inside = ((lambda point: point[1] <= latitude) if below
              else (lambda point: point[1] >= latitude))

    clipped = []
    previous = points[-1]
    previous_inside = inside(previous)
    for current in points:
        current_inside = inside(current)
        if current_inside != previous_inside:
            fraction = (latitude - previous[1]) / (current[1] - previous[1])
            clipped.append([
                previous[0] + fraction * (current[0] - previous[0]),
                latitude,
            ])
        if current_inside:
            clipped.append(list(current))
        previous, previous_inside = current, current_inside

    deduplicated = []
    for point in clipped:
        if not deduplicated or point != deduplicated[-1]:
            deduplicated.append(point)
    if len(deduplicated) < 3:
        return []
    if deduplicated[0] != deduplicated[-1]:
        deduplicated.append(deduplicated[0])
    return deduplicated if len(deduplicated) >= 4 else []


def _polygons(geometry: dict) -> list:
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    return []


def _clip_geometry(geometry: dict, latitude: float, *, below: bool) -> dict:
    """Return the clipped geometry, always represented as a MultiPolygon."""
    polygons = []
    for polygon in _polygons(geometry):
        if not polygon:
            continue
        outer = _clip_ring(polygon[0], latitude, below=below)
        if not outer:
            continue
        holes = [clipped for ring in polygon[1:]
                 if (clipped := _clip_ring(ring, latitude, below=below))]
        polygons.append([outer, *holes])
    return {"type": "MultiPolygon", "coordinates": polygons}


def _western_sahara(features: list) -> list:
    """Move Morocco south of 27°40'N into Western Sahara's geometry."""
    morocco = next((f for f in features
                    if f.get("properties", {}).get("ADM0_A3") == "MAR"), None)
    sahara = next((f for f in features
                   if (f.get("properties", {}).get("ISO_A3") == "ESH"
                       or f.get("properties", {}).get("ADM0_A3") == "SAH")), None)
    if morocco is None or sahara is None:
        return features

    north = _clip_geometry(morocco["geometry"], WESTERN_SAHARA_BORDER, below=False)
    south = _clip_geometry(morocco["geometry"], WESTERN_SAHARA_BORDER, below=True)
    corrected = []
    for feature in features:
        if feature is morocco:
            corrected.append({**feature, "geometry": north})
        elif feature is sahara:
            corrected.append({
                **feature,
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [*_polygons(sahara["geometry"]),
                                    *south["coordinates"]],
                },
            })
        else:
            corrected.append(feature)
    return corrected


def _spherical_area_km2(geometry: dict) -> float:
    """Spherical GeoJSON area, used to verify that the split loses no land."""
    def ring_area(ring):
        if len(ring) < 4:
            return 0.0
        total = 0.0
        for first, second in zip(ring, ring[1:]):
            lon1, lat1 = map(math.radians, first[:2])
            lon2, lat2 = map(math.radians, second[:2])
            delta = lon2 - lon1
            if delta > math.pi:
                delta -= 2 * math.pi
            elif delta < -math.pi:
                delta += 2 * math.pi
            total += delta * (2 + math.sin(lat1) + math.sin(lat2))
        return total * EARTH_RADIUS_KM ** 2 / 2

    area = 0.0
    for polygon in _polygons(geometry):
        if polygon:
            area += abs(ring_area(polygon[0]))
            area -= sum(abs(ring_area(hole)) for hole in polygon[1:])
    return area


def _longitude_arc(longitudes: list[float]) -> tuple[float, float]:
    """Return the smallest circular arc covering all longitudes.

    The arc is ``(start, width)`` in degrees, with ``start`` normalized to
    [0, 360).  Its complement is the largest gap between consecutive vertices
    on the longitude circle.  This makes a feature around +/-180 a small arc
    instead of a nearly-global planar bounding box.
    """
    points = sorted(set(lon % FULL_CIRCLE for lon in longitudes))
    if len(points) == 1:
        return points[0], 0.0

    gaps = [points[index + 1] - point
            for index, point in enumerate(points[:-1])]
    gaps.append(points[0] + FULL_CIRCLE - points[-1])
    gap_index = max(range(len(gaps)), key=gaps.__getitem__)
    start = points[(gap_index + 1) % len(points)]
    return start, FULL_CIRCLE - gaps[gap_index]


def _feature_bbox(geometry) -> tuple | None:
    """Return ``(longitude arc start, arc width, min lat, max lat)``."""
    rings = (geometry["coordinates"] if geometry["type"] == "Polygon"
             else [r for part in geometry["coordinates"] for r in part])
    xs = [c[0] for ring in rings for c in ring]
    ys = [c[1] for ring in rings for c in ring]
    if not xs:
        return None
    start, width = _longitude_arc(xs)
    return start, width, min(ys), max(ys)


def _arc_segments(start: float, width: float) -> list[tuple[float, float]]:
    """Split a circular arc into one or two intervals on [0, 360]."""
    if width >= FULL_CIRCLE:
        return [(0.0, FULL_CIRCLE)]
    end = start + width
    if end < FULL_CIRCLE:
        return [(start, end)]
    # Include longitude zero when an arc ends exactly at 360: the two values
    # name the same meridian and bbox intersection includes touching edges.
    return [(start, FULL_CIRCLE), (0.0, end - FULL_CIRCLE)]


def _arcs_intersect(first: tuple[float, float],
                    second: tuple[float, float]) -> bool:
    return any(a_start <= b_end and a_end >= b_start
               for a_start, a_end in _arc_segments(*first)
               for b_start, b_end in _arc_segments(*second))


def _intersecting(features: list, bbox) -> list:
    min_lon, min_lat, max_lon, max_lat = bbox
    window_arc = (min_lon % FULL_CIRCLE, max_lon - min_lon)
    kept = []
    for feature in features:
        extent = _feature_bbox(feature["geometry"])
        if extent is None:
            continue
        lon_start, lon_width, feature_min_lat, feature_max_lat = extent
        if (_arcs_intersect((lon_start, lon_width), window_arc)
                and feature_min_lat <= max_lat
                and feature_max_lat >= min_lat):
            kept.append(feature)
    return kept


def _belongs(feature, codes, level: str) -> bool:
    props = feature["properties"]
    unit = str(props.get("iso_3166_2") or "").upper()
    country = str(props.get(registry.country_property(level)) or "").upper()
    alpha2 = str(props.get("ISO_A2_EH") or props.get("ISO_A2") or "").upper()
    # ADM0_A3 is not ISO3 for a handful of units (South Sudan is SDS, ISO SSD;
    # Western Sahara is SAH, ISO ESH). The join already uses ISO3, so `within`
    # must accept it too -- otherwise within ["SSD"] silently drops the unit.
    iso3 = str(registry.iso3_of(props) or "").upper() if level == "admin_0" else ""
    return any(code in (unit, country, alpha2, iso3) and code for code in codes)


def _selected(level: str, bbox, within: str | None = None) -> list:
    from mapsvc.harvest import load_source

    if level not in LEVELS:
        raise CartographyError(
            f"natural_earth has no {level!r}; it provides {', '.join(LEVELS)}. "
            "Use the overture basemap for finer levels.", "level"
        )
    # ``load_source`` is the one normalisation gateway for Natural Earth;
    # admin-0 geometry is already corrected there for every consumer.
    features = load_source(level)["features"]
    if within:
        codes = {c.strip().upper() for c in within}
        features = [f for f in features if _belongs(f, codes, level)]
    if bbox is not None:
        features = _intersecting(features, bbox)
    return features


def count(level: str, bbox, within: str | None = None) -> int:
    return len(_selected(level, bbox, within))


def load(level: str, bbox, detail: str = "simplified",
         within: str | None = None) -> Boundaries:
    features = _selected(level, bbox, within)
    if not features:
        raise CartographyError(
            f"no natural_earth features in {', '.join(within)}" if within
            else "no natural_earth features in that area",
            "within" if within else "bbox")

    id_property = registry.id_property(level)
    name_property = registry.name_property(level)
    units = []
    for feature in features:
        props = feature["properties"]
        # ISO 3166-2 below country level, ISO3 at country level.
        key = (props.get("iso_3166_2") if level == "admin_1"
               else registry.iso3_of(props))
        units.append({
            "id": props.get(id_property),
            "key": key,
            "keys": {KEYS[level][0]: key} if key else {},
            "name": props.get(name_property),
            "geometry": feature["geometry"],
            "properties": props,
        })
    # The public join key is the id a reader (and the eval) can check, so it
    # replaces Natural Earth's internal adm1_code (USA-3513 is Wyoming) wherever
    # it is present and unique. Duplicated or placeholder keys keep the old id.
    seen: dict = {}
    for unit in units:
        seen[unit["key"]] = seen.get(unit["key"], 0) + 1
    for unit in units:
        key = unit["key"]
        if key and seen[key] == 1 and "-99" not in str(key):
            unit["id"] = key
    units.sort(key=lambda u: str(u["id"]))

    return Boundaries(units=units, keys=KEYS[level], provenance={
        "source": registry.SOURCE_NAME,
        "license": "public domain",
        "attribution": "Natural Earth",
        "release": registry.SOURCE_VINTAGE,
        "note": (WESTERN_SAHARA_NOTE
                 if level == "admin_0" and any(
                     unit.get("key") in {"MAR", "ESH"} for unit in units)
                 else None),
        "level": level,
        "detail": detail,
        "scale": f"1:{registry.scale_for(level)}",
        "keys": list(KEYS[level]),
    })
