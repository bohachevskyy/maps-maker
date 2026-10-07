"""Cartography: boundaries, and nothing else.

A provider answers one question -- what are the polygons for this level in this
region -- and knows nothing about what will be drawn on them. Statistics arrive
separately and are joined by `key`.

Levels are canonical here (admin_0 .. admin_3) and each provider translates
them into whatever vocabulary it uses.
"""

from dataclasses import dataclass, field


@dataclass
class Boundaries:
    """Polygons for one level of one region.

    units       [{"id", "keys", "name", "geometry"}, ...]
                `id` identifies the polygon within this map; `keys` maps each
                join-key namespace the unit carries to its value, e.g.
                {"ISO3166_2": "US-CA", "FIPS": "06"}. Statistics are matched on
                whichever namespace their source declares.
    provenance  {source, license, attribution, release, level, detail}
                The licence is carried, not assumed: Overture is ODbL and the
                footnote has to say so.
    """

    units: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    # Join-key namespaces carried by every unit.  A unit stores the concrete
    # values in ``keys`` (for example {"ISO3166_2": "US-TX"}).  Keeping this
    # declaration separate from the display id makes incompatible joins
    # impossible to mistake for missing data.
    keys: tuple = field(default_factory=tuple)


class CartographyError(ValueError):
    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


# Registered boundary providers. Each module declares LEVELS and KEYS
# ({level: (key namespace, ...)}) and implements load() and count().
def _modules() -> tuple:
    from mapsvc import registry

    return registry.BASEMAPS


def provider(name: str):
    if name not in _modules():
        raise CartographyError(
            f"unknown basemap source {name!r}; expected one of "
            f"{', '.join(sorted(_modules()))}", "basemap.source"
        )
    from importlib import import_module

    return import_module(f"mapsvc.cartography.{name}")


# A geographic window: min_lon, min_lat, max_lon, max_lat.
BBox = tuple[float, float, float, float]


def load(source: str, level: str, bbox: BBox | None, detail: str = "simplified",
         within: str | None = None) -> Boundaries:
    """Every unit at `level` whose own bounding box intersects `bbox`.

    Spatially scoped, not politically: a box drawn around Ukraine also returns
    Poland's voivodeships and Romania's counties, because they are there. That
    is what a map of an area looks like.
    """
    return provider(source).load(level, bbox, detail, within)


def count(source: str, level: str, bbox: BBox | None,
          within: str | None = None) -> int:
    """How many units a load would return, without fetching their geometry."""
    return provider(source).count(level, bbox, within)


def levels(source: str) -> tuple:
    return provider(source).LEVELS


def keys(source: str, level: str) -> tuple:
    """Named join-key namespaces a boundary provider carries at ``level``."""
    return tuple(provider(source).KEYS.get(level, ()))
