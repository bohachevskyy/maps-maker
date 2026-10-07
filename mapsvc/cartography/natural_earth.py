"""Natural Earth boundaries, filtered by bounding box.

The same files the statistics provider reads, but this module returns only
geometry and identifiers. Anything thematic is the statistics layer's business.
"""

from mapsvc import registry
from mapsvc.cartography import Boundaries, CartographyError

LEVELS = ("admin_0", "admin_1")
KEYS = {"admin_0": ("ISO3",), "admin_1": ("ISO3166_2",)}

FULL_CIRCLE = 360.0


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
    return any(code == unit or code == country or code == alpha2 for code in codes)


def _selected(level: str, bbox, within: str | None = None) -> list:
    from mapsvc.harvest import load_source

    if level not in LEVELS:
        raise CartographyError(
            f"natural_earth has no {level!r}; it provides {', '.join(LEVELS)}. "
            "Use the overture basemap for finer levels.", "level"
        )
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
    units.sort(key=lambda u: str(u["id"]))

    return Boundaries(units=units, keys=KEYS[level], provenance={
        "source": registry.SOURCE_NAME,
        "license": "public domain",
        "attribution": "Natural Earth",
        "release": registry.SOURCE_VINTAGE,
        "level": level,
        "detail": detail,
        "scale": f"1:{registry.scale_for(level)}",
        "keys": list(KEYS[level]),
    })
