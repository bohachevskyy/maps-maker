"""Statistics: values keyed by a join key, and nothing else.

A provider knows nothing about polygons. It answers "what is this variable, for
these keys" and hands back a mapping. Cartography supplies the keys; `harvest`
performs the join.

Keys are ISO3 at country level and ISO 3166-2 below it -- the codes both Natural
Earth and Overture already carry.

Registering a source
--------------------
1. Write `mapsvc/statistics/<name>.py` declaring four module-level names:

       SOURCE      = "world_bank"
       LICENSE     = "CC BY 4.0"
       ATTRIBUTION = "World Bank (CC BY 4.0)"
       VARIABLES   = {"admin_0": {"<id>": {"level", "unit", "year_col"}}}

   and one function, `load(variable_id, level, region) -> Values`.

2. Add the module to `_MODULES` below.

That is the whole procedure. `SOURCES`, `variables_for`, `sources_for` and
`level_of` all derive from the providers, so nothing branches on a source name,
and the agent's JSON schema -- which is generated from these lookups -- picks up
the new indicators with no prompt edit.

`level` in a variable's metadata is its measurement level, and it is load-bearing:
"count" treats 0 as no-data (a country with zero people is a placeholder row),
"ratio" does not (0% internet use is a real observation).
"""

import functools
from dataclasses import dataclass, field

# The registered sources, in the order they are offered.
_MODULES = ("natural_earth", "owid")


@dataclass
class Values:
    values: dict = field(default_factory=dict)      # key -> raw value
    provenance: dict = field(default_factory=dict)  # source, variable, unit, year


class StatisticsError(ValueError):
    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


@functools.lru_cache(maxsize=1)
def _providers() -> dict:
    from importlib import import_module

    loaded = {}
    for name in _MODULES:
        module = import_module(f"mapsvc.statistics.{name}")
        loaded[module.SOURCE] = module
    return loaded


@functools.lru_cache(maxsize=1)
def sources() -> tuple:
    return tuple(_providers())


def provider(name: str):
    providers = _providers()
    if name not in providers:
        raise StatisticsError(
            f"unknown variable source {name!r}; expected one of "
            f"{', '.join(sorted(providers))}", "variable.source"
        )
    return providers[name]


def load(source: str, variable_id: str, level: str, region: str) -> Values:
    return provider(source).load(variable_id, level, region)


def variables_for(level: str, source: str) -> dict:
    """What `source` publishes at `level`."""
    return provider(source).VARIABLES.get(level, {})


def catalogue() -> dict:
    """Every source's catalogue: {source: {level: {id: meta}}}."""
    return {name: module.VARIABLES for name, module in _providers().items()}


def sources_for(variable_id: str) -> list[str]:
    """Which sources publish this variable id."""
    return [
        name for name, module in _providers().items()
        if any(variable_id in ids for ids in module.VARIABLES.values())
    ]


def level_of(variable_id: str) -> str | None:
    """The admin level a variable belongs to, or None if no source has it."""
    for module in _providers().values():
        for level, ids in module.VARIABLES.items():
            if variable_id in ids:
                return level
    return None


@functools.lru_cache(maxsize=1)
def all_variables() -> dict:
    """Every variable across every source and level.

    The agent needs one flat enum; the validator rejects a variable used with
    the wrong source or at the wrong level.
    """
    merged: dict = {}
    for module in _providers().values():
        for ids in module.VARIABLES.values():
            merged.update(ids)
    return merged
