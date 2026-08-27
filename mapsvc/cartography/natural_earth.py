"""Natural Earth boundaries.

The same files the statistics provider reads, but this module returns only
geometry and identifiers. Anything thematic is the statistics layer's business.
"""

from mapsvc import registry
from mapsvc.cartography import Boundaries, CartographyError

LEVELS = ("admin_0", "admin_1")


def load(level: str, region: str, detail: str = "simplified") -> Boundaries:
    from mapsvc.harvest import load_source, select_region

    if level not in LEVELS:
        raise CartographyError(
            f"natural_earth has no {level!r}; it provides {', '.join(LEVELS)}. "
            "Use the overture basemap for finer levels.", "level"
        )

    features = select_region(load_source(level)["features"], region, level)
    if not features:
        raise CartographyError(
            f"no features matched region {region!r}", "region"
        )

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
