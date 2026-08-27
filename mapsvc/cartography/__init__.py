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

    units       [{"id", "key", "name", "geometry"}, ...]
                `id` identifies the polygon within this map; `key` is the join
                key statistics are matched on -- ISO 3166-2 below country level,
                ISO3 at country level.
    provenance  {source, license, attribution, release, level, detail}
                The licence is carried, not assumed: Overture is ODbL and the
                footnote has to say so.
    """

    units: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)


class CartographyError(ValueError):
    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


def provider(name: str):
    from mapsvc.cartography import natural_earth, overture

    providers = {"natural_earth": natural_earth, "overture": overture}
    if name not in providers:
        raise CartographyError(
            f"unknown basemap source {name!r}; expected one of "
            f"{', '.join(sorted(providers))}", "basemap.source"
        )
    return providers[name]


def load(source: str, level: str, region: str, detail: str = "simplified") -> Boundaries:
    return provider(source).load(level, region, detail)


def levels(source: str) -> tuple:
    return provider(source).LEVELS
