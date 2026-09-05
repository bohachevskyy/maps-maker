"""Natural Earth boundaries, filtered by bounding box.

The same files the statistics provider reads, but this module returns only
geometry and identifiers. Anything thematic is the statistics layer's business.
"""

from mapsvc import registry
from mapsvc.cartography import Boundaries, CartographyError

LEVELS = ("admin_0", "admin_1")

# A ring spanning more than this has wrapped the antimeridian, and its bounding
# box says nothing about where it is. Russia and Alaska would otherwise appear
# on every map on earth.
MAX_BBOX_WIDTH = 180.0


def _feature_bbox(geometry) -> tuple | None:
    rings = (geometry["coordinates"] if geometry["type"] == "Polygon"
             else [r for part in geometry["coordinates"] for r in part])
    xs = [c[0] for ring in rings for c in ring]
    ys = [c[1] for ring in rings for c in ring]
    if not xs:
        return None
    box = (min(xs), min(ys), max(xs), max(ys))
    return None if box[2] - box[0] >= MAX_BBOX_WIDTH else box


def _intersecting(features: list, bbox) -> list:
    min_lon, min_lat, max_lon, max_lat = bbox
    kept = []
    for feature in features:
        box = _feature_bbox(feature["geometry"])
        if box is None:
            continue
        if (box[0] <= max_lon and box[2] >= min_lon
                and box[1] <= max_lat and box[3] >= min_lat):
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
        units.append({
            "id": props.get(id_property),
            # ISO 3166-2 below country level, ADM0_A3 at country level.
            "key": (props.get("iso_3166_2") if level == "admin_1"
                    else props.get(id_property)),
            "name": props.get(name_property),
            "geometry": feature["geometry"],
            "properties": props,
        })
    units.sort(key=lambda u: str(u["id"]))

    return Boundaries(units=units, provenance={
        "source": registry.SOURCE_NAME,
        "license": "public domain",
        "attribution": "Natural Earth",
        "release": registry.SOURCE_VINTAGE,
        "level": level,
        "detail": detail,
        "scale": f"1:{registry.scale_for(level)}",
    })
