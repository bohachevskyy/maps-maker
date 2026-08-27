"""Statistics: values keyed by a join key, and nothing else.

A statistics provider knows nothing about polygons. It answers "what is this
variable, for these keys" and hands back a mapping. Cartography supplies the
keys; `harvest` performs the join.

Keys are ISO3 at country level and ISO 3166-2 below it -- the codes both
Natural Earth and Overture already carry.
"""

from dataclasses import dataclass, field


@dataclass
class Values:
    values: dict = field(default_factory=dict)      # key -> raw value
    provenance: dict = field(default_factory=dict)  # source, variable, unit, year


class StatisticsError(ValueError):
    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


def provider(name: str):
    from mapsvc.statistics import natural_earth

    providers = {"natural_earth": natural_earth}
    if name not in providers:
        raise StatisticsError(
            f"unknown variable source {name!r}; expected one of "
            f"{', '.join(sorted(providers))}", "variable.source"
        )
    return providers[name]


def load(source: str, variable_id: str, level: str, region: str) -> Values:
    return provider(source).load(variable_id, level, region)
